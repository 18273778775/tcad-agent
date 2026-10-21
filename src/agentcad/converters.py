# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Optional external DWG conversion with explicit, verified file boundaries.

LibreDWG is invoked for DWG input or an explicit conversion request. Detection does not
launch any program. No bundled/proprietary CAD application is used implicitly.
"""

import math
import hashlib
import json
import signal
import threading
import time
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

from .errors import CadError
from .io import atomic_output, check_input_size, destination_lock, file_sha256, read_document, revision_guard

_DIAGNOSTIC_LIMIT = 4096
_CONVERTER_LOG_LIMIT = 8 * 1024 * 1024
_DWG_CACHE_MAX_FILES = 16
_DWG_CACHE_MAX_BYTES = 512 * 1024 * 1024
_DWG_CACHE_VERSION = 1


def _detect(executable, environment_variable, *, enabled):
    configured = os.environ.get(environment_variable, "").strip()
    resolved = shutil.which(configured or executable)
    detection_source = "environment" if configured else "PATH"
    if not configured and not resolved and executable == "dwg2dxf":
        from .install import find_installed_dwg2dxf
        resolved = find_installed_dwg2dxf()
        if resolved:
            detection_source = "setup-dwg"
    return {
        "available": resolved is not None,
        "detection_source": detection_source,
        "executable": resolved,
        "environment_variable": environment_variable,
        "configured_path": configured or None,
        "adapter_enabled": enabled,
    }


def converter_status():
    """Report executable detection; never execute the detected programs."""
    return {
        "libredwg": {
            **_detect("dwg2dxf", "TCAD_DWG2DXF", enabled=True),
            "conversions": ["dwg->dxf"],
        },
        "oda": {
            **_detect("ODAFileConverter", "TCAD_ODA_CONVERTER", enabled=False),
            "conversions": [],
            "note": "Detection only; the ODA conversion adapter is not enabled.",
        },
    }


def _source_path(input_path):
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise CadError("FILE_NOT_FOUND", "Drawing does not exist.", {"path": str(source)})
    check_input_size(source)
    extension = source.suffix.lower()
    if extension not in {".dwg", ".dxf"}:
        raise CadError("UNSUPPORTED_FORMAT", "Drawing input must be .dwg or .dxf.")
    # Consumers parse DXF once. Avoid loading a large drawing solely to pass its path on.
    if extension == ".dwg":
        try:
            with source.open("rb") as stream:
                signature = stream.read(6)
        except OSError as exc:
            raise CadError(
                "INPUT_READ_ERROR", "Cannot read DWG input.", {"reason": str(exc)}
            ) from exc
        # This is only a preliminary signature check. The external converter is
        # responsible for decoding DWG; a matching signature does not prove validity.
        if re.fullmatch(rb"AC[0-9]{4}", signature) is None:
            raise CadError("INVALID_DWG", "Input does not have a supported DWG signature.")
    return source


def _diagnostic(value):
    if not isinstance(value, bytes):
        value = (value or "").encode("utf-8", errors="replace")
    # Truncate by bytes, not characters; multibyte diagnostics retain the budget.
    return value[-_DIAGNOSTIC_LIMIT:].decode("utf-8", errors="ignore")


def _backend_executable(backend):
    if backend not in {"auto", "libredwg", "dwg2dxf", "oda"}:
        raise CadError(
            "INVALID_BACKEND",
            "Unknown conversion backend.",
            {"backend": backend, "supported": ["auto", "libredwg"]},
        )
    status = converter_status()
    if backend == "oda":
        raise CadError(
            "UNSUPPORTED_CONVERSION",
            "The ODA adapter is detection-only in this release.",
            {"backend": "oda", "detected": status["oda"]["available"]},
        )
    executable = status["libredwg"]["executable"]
    if executable is None:
        raise CadError(
            "CONVERTER_NOT_FOUND",
            "Install LibreDWG and add dwg2dxf to PATH, or set TCAD_DWG2DXF to its executable.",
            {
                "backend": "libredwg",
                "environment_variable": "TCAD_DWG2DXF",
                "configured_path": status["libredwg"]["configured_path"],
            },
        )
    return executable



class _WindowsJob:
    """Kill converter descendants when the Job Object is terminated or closed."""
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IoCounters), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.kernel, self.handle = kernel, handle
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle)))):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def terminate(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _kill_tree(process, job=None):
    if job is not None:
        job.terminate()
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        # Job creation can be denied by an enclosing system job. taskkill still
        # addresses the converter's active descendant tree, without buffering logs.
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=False, shell=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _run_converter(command, *, timeout):
    """Drain both pipes into 4 KiB tails; stop after 8 MiB of emitted diagnostics."""
    options = {"shell": False, "stdin": subprocess.DEVNULL,
               "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        options["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    job = None
    if os.name == "nt":
        try:
            job = _WindowsJob(process)
        except OSError:
            pass
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    lock = threading.Lock()
    exceeded = threading.Event()
    total = 0

    def drain(stream, name):
        nonlocal total
        try:
            while block := stream.read(65536):
                with lock:
                    total += len(block)
                    tails[name].extend(block)
                    if len(tails[name]) > _DIAGNOSTIC_LIMIT:
                        del tails[name][:-_DIAGNOSTIC_LIMIT]
                    if total > _CONVERTER_LOG_LIMIT:
                        exceeded.set()
        except OSError:
            pass
        finally:
            stream.close()

    threads = [threading.Thread(target=drain, args=(getattr(process, name), name), daemon=True)
               for name in ("stdout", "stderr")]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    output_path = Path(command[command.index("-o") + 1]) if "-o" in command else None
    try:
        while True:
            if output_path is not None and output_path.is_file():
                try:
                    check_input_size(output_path)
                except CadError as exc:
                    _kill_tree(process, job)
                    if exc.code == "INPUT_TOO_LARGE":
                        raise CadError("CONVERSION_OUTPUT_TOO_LARGE", "Converter exceeded its DXF file budget.",
                                       exc.details) from exc
                    raise
            if exceeded.is_set():
                _kill_tree(process, job)
                raise CadError("CONVERTER_LOG_LIMIT", "Converter exceeded its diagnostic output budget.",
                               {"max_log_bytes": _CONVERTER_LOG_LIMIT,
                                **{name: _diagnostic(bytes(value)) for name, value in tails.items()}})
            if process.poll() is not None and not any(thread.is_alive() for thread in threads):
                break
            if time.monotonic() >= deadline:
                _kill_tree(process, job)
                raise subprocess.TimeoutExpired(command, timeout,
                                                output=bytes(tails["stdout"]), stderr=bytes(tails["stderr"]))
            if process.poll() is not None:
                # A converter should not leave background children holding its pipes.
                if job is not None:
                    job.close()
                elif os.name != "nt":
                    _kill_tree(process)
            exceeded.wait(0.01)
        return subprocess.CompletedProcess(command, process.returncode,
                                           bytes(tails["stdout"]), bytes(tails["stderr"]))
    finally:
        if process.poll() is None:
            _kill_tree(process, job)
        if job is not None:
            job.close()
        for thread in threads:
            thread.join(timeout=1)


def convert_file(input_path, output_path, backend="auto", overwrite=False, timeout=60):
    """Convert DWG to DXF with LibreDWG, validating DXF before atomic publication."""
    source = _source_path(input_path)
    target = Path(output_path).expanduser().resolve()
    if source.suffix.lower() != ".dwg" or target.suffix.lower() != ".dxf":
        raise CadError(
            "UNSUPPORTED_CONVERSION",
            "This release supports DWG to DXF only.",
            {
                "input_format": source.suffix.lower(),
                "output_format": target.suffix.lower(),
                "supported": ["dwg->dxf"],
            },
        )
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (float, int))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise CadError("INVALID_TIMEOUT", "Conversion timeout must be a finite positive number.")
    executable = _backend_executable(backend)
    with atomic_output(target, overwrite=overwrite) as temporary:
        # LibreDWG's documented -o specifies a single destination. -y permits
        # overwriting only our reserved temporary file, never the user's target.
        command = [executable, "-y", "-o", str(temporary), str(source)]
        try:
            result = _run_converter(command, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise CadError(
                "CONVERSION_TIMEOUT",
                "DWG conversion exceeded its time limit.",
                {"timeout_seconds": timeout, "stderr": _diagnostic(exc.stderr)},
            ) from exc
        except OSError as exc:
            raise CadError(
                "CONVERTER_EXECUTION_FAILED",
                "Cannot execute the configured DWG converter.",
                {"executable": executable, "reason": str(exc)},
            ) from exc
        if result.returncode != 0:
            raise CadError(
                "CONVERSION_FAILED",
                "LibreDWG reported a conversion error.",
                {
                    "returncode": result.returncode,
                    "stderr": _diagnostic(result.stderr),
                    "stdout": _diagnostic(result.stdout),
                },
            )
        try:
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise CadError("EMPTY_OUTPUT", "Converter produced no DXF data.")
            document = read_document(temporary)
        except (CadError, OSError) as exc:
            raise CadError(
                "CONVERSION_INVALID_OUTPUT",
                "Converter output is not a readable DXF drawing.",
                {"reason": str(exc), "stderr": _diagnostic(result.stderr)},
            ) from exc
    response = {
        "input": str(source),
        "output": str(target),
        "backend": "libredwg",
        "format": "dxf",
        "dxf_version": document.dxfversion,
        "warnings": [
            "DWG conversion may lose unsupported entities or properties; verify critical geometry."
        ],
    }
    diagnostics = {
        key: text
        for key, value in (("stdout", result.stdout), ("stderr", result.stderr))
        if (text := _diagnostic(value))
    }
    if diagnostics:
        response["diagnostics"] = diagnostics
    return response


def _converter_identity(executable):
    path = Path(executable).expanduser().resolve()
    try:
        return {"path": str(path), "sha256": file_sha256(path)}
    except OSError as exc:
        raise CadError("CONVERTER_EXECUTION_FAILED", "Cannot read the selected converter identity.",
                       {"executable": str(path)}) from exc


def _dwg_cache_root():
    configured = os.environ.get("TCAD_CACHE_DIR")
    root = (Path(configured).expanduser() if configured else
            Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "tcad-agent" / "cache") / "dwg"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root.resolve()


def _cached_conversion(converted, manifest, source_sha, identity):
    try:
        if (not converted.is_file() or not manifest.is_file() or converted.is_symlink()
                or manifest.is_symlink() or manifest.stat().st_size > 16384):
            return None
        check_input_size(converted)
        record = json.loads(manifest.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("version") != _DWG_CACHE_VERSION or record.get("complete") is not True
                or record.get("source_sha256") != source_sha or record.get("converter") != identity
                or file_sha256(converted) != record.get("dxf_sha256")
                or not isinstance(record.get("info"), dict)):
            return None
        return record
    except (OSError, ValueError, TypeError):
        return None


def _prune_dwg_cache(root, keep=None):
    files = list(root.glob("dwg-*.dxf"))
    files.sort(key=lambda p: p.with_suffix(".json").stat().st_mtime_ns
               if p.with_suffix(".json").is_file() else 0, reverse=True)
    if keep in files:
        files.remove(keep)
        files.insert(0, keep)
    total = retained = 0
    for path in files:
        manifest = path.with_suffix(".json")
        size = path.stat().st_size + (manifest.stat().st_size if manifest.is_file() else 0)
        if path == keep or (retained < _DWG_CACHE_MAX_FILES and total + size <= _DWG_CACHE_MAX_BYTES):
            retained += 1
            total += size
            continue
        try:
            # Respect another process actively consuming a conversion.
            with destination_lock(path, timeout=0):
                path.unlink(missing_ok=True)
                manifest.unlink(missing_ok=True)
        except (OSError, CadError):
            pass


@contextmanager
def readable_dxf(path, backend="auto", timeout=60, conversion_info=None):
    """Yield native DXF or a stable, hash-verified persistent DWG conversion.

    Cache reuse performs sequential SHA-256 IO for the source and converted file,
    but does not parse DXF or launch a converter. Only the completion manifest is
    touched for LRU, preserving the DXF stat identity used by SQLite query cursors.
    Cached DXFs can be removed from TCAD_CACHE_DIR/dwg when no command uses them.
    """
    source = _source_path(path)
    if source.suffix.lower() == ".dxf":
        yield source
        return
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise CadError("INVALID_TIMEOUT", "Conversion timeout must be a finite positive number.")
    identity = _converter_identity(_backend_executable(backend))
    source_sha = file_sha256(source)
    payload = json.dumps({"version": _DWG_CACHE_VERSION, "source_sha256": source_sha,
                          "converter": identity}, sort_keys=True, separators=(",", ":")).encode()
    root = _dwg_cache_root()
    key = hashlib.sha256(payload).hexdigest()
    converted, manifest = root / f"dwg-{key}.dxf", root / f"dwg-{key}.json"
    _prune_dwg_cache(root, keep=converted)
    with destination_lock(converted):
        if converted.is_symlink() or manifest.is_symlink():
            raise CadError("UNSAFE_PATH", "DWG cache entries must be ordinary files.")
        record = _cached_conversion(converted, manifest, source_sha, identity)
        hit = record is not None
        if record is None:
            with revision_guard(source, converted):
                info = convert_file(source, converted, backend=backend, timeout=timeout,
                                    overwrite=converted.exists())
                if file_sha256(source) != source_sha:
                    raise CadError("SOURCE_CHANGED", "DWG changed while its conversion was being prepared.")
                record = {"version": _DWG_CACHE_VERSION, "complete": True,
                          "source_sha256": source_sha, "converter": identity,
                          "dxf_sha256": file_sha256(converted),
                          "info": {key: value for key, value in info.items() if key not in {"input", "output"}}}
                with atomic_output(manifest, overwrite=manifest.exists()) as temporary:
                    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        if conversion_info is not None:
            conversion_info.update(record["info"])
            conversion_info.update({"input": str(source), "source_sha256": source_sha,
                                    "normalized_dxf_sha256": record["dxf_sha256"],
                                    "cache": {"hit": hit, "persistent": True, "validation": "sha256"}})
        os.utime(manifest, None)
        try:
            yield converted
        finally:
            _prune_dwg_cache(root, keep=converted)
