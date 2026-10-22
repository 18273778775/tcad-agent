# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Supervise owned CAD workers so one drawing cannot hang the JSONL service."""

import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from .errors import CadError
from .output import DEFAULT_OUTPUT_BYTES, MAX_OUTPUT_BYTES, bounded_failure, encoded_size


def _limits(request):
    args = request.get("args", {}) if isinstance(request, dict) else {}
    if not isinstance(args, dict):
        args = {}
    seconds = args.get("timeout_seconds", 1800 if request.get("command") == "split" else
                       600 if request.get("command") == "setup-dwg" else 60)
    memory = args.get("max_memory_mb", 1024)
    budget = args.get("max_output_bytes", DEFAULT_OUTPUT_BYTES)
    for value, low, high, name in [(seconds, 0.1, 1800, "timeout_seconds"),
                                    (memory, 64, 16384, "max_memory_mb"),
                                    (budget, 4096, MAX_OUTPUT_BYTES, "max_output_bytes")]:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise CadError("INVALID_ARGUMENT", f"{name} must be a finite number.")
        if not low <= value <= high or (name != "timeout_seconds" and int(value) != value):
            raise CadError("INVALID_ARGUMENT", f"{name} must be between {low} and {high}.")
    return float(seconds), int(memory), int(budget)


def _kill_tree(process):
    import psutil
    try:
        root = psutil.Process(process.pid)
        children = root.children(recursive=True)
    except psutil.Error:
        children = []
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for child in reversed(children):
        try:
            child.kill()
        except psutil.Error:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def _cleanup_atomic_temporaries(request, operation):
    args = request.get("args", {}) if isinstance(request, dict) else {}
    if not isinstance(args, dict):
        return
    for key in ("output", "result_file"):
        value = args.get(key)
        if not isinstance(value, str):
            continue
        parent = Path(value).expanduser().resolve().parent
        for candidate in parent.glob(f".tcad-{operation}-*"):
            if candidate.is_file() and not candidate.is_symlink():
                try:
                    candidate.unlink()
                except OSError:
                    pass
    # A split stages a complete, new directory. Only this worker's UUID-prefixed
    # sibling staging directories can be removed after it exits or is killed.
    value = args.get("output_dir")
    if isinstance(value, str):
        import shutil
        import stat

        parent = Path(value).expanduser().resolve().parent
        for candidate in parent.glob(f".tcad-{operation}-sheet-*"):
            try:
                attributes = candidate.lstat()
                reparse = getattr(attributes, "st_file_attributes", 0) & getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if (candidate.is_symlink() or reparse or not candidate.is_dir()
                        or candidate.resolve().parent != parent):
                    continue
                shutil.rmtree(candidate)
            except OSError:
                pass


def run_request(request, *, _command=None):
    try:
        return _run_request(request, _command=_command)
    except OSError as exc:
        return bounded_failure(CadError("IO_ERROR", "Cannot start or supervise CAD worker.",
                                       {"reason": str(exc)[:512]}))


def _run_request(request, *, _command=None):
    """CLI boundary only; `_command` is a private injection point for resource tests."""
    import psutil

    request_id = request.get("id") if isinstance(request, dict) else None
    if not isinstance(request_id, (str, int, float)) or isinstance(request_id, bool):
        request_id = None
    if isinstance(request_id, str) and len(request_id) > 256:
        request_id = None
    if isinstance(request_id, (int, float)) and abs(request_id) > 2 ** 53 - 1:
        request_id = None
    try:
        if not isinstance(request, dict):
            raise CadError("INVALID_REQUEST", "Request must be an object.")
        seconds, memory_mb, budget = _limits(request)
    except CadError as exc:
        return bounded_failure(exc, request_id)
    operation = uuid.uuid4().hex
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["TCAD_OPERATION_ID"] = operation
    started, peak = time.monotonic(), 0
    failure_code = None
    with tempfile.TemporaryDirectory(prefix="tcad-worker-") as directory:
        directory = Path(directory)
        environment["TMP"] = environment["TEMP"] = environment["TMPDIR"] = str(directory)
        input_path, output_path, error_path = [directory / name for name in
                                               ("request.json", "response.json", "diagnostics.txt")]
        with input_path.open("w", encoding="utf-8") as stream:
            json.dump(request, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with input_path.open("rb") as source, output_path.open("wb") as output, error_path.open("wb") as diagnostics:
            process = subprocess.Popen(_command or [sys.executable, "-m", "agentcad.worker"],
                                       stdin=source, stdout=output, stderr=diagnostics,
                                       env=environment, start_new_session=os.name != "nt",
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                       if os.name == "nt" else 0)
            try:
                root = psutil.Process(process.pid)
            except psutil.Error:
                root = None
            try:
                while process.poll() is None:
                    try:
                        processes = [root, *root.children(recursive=True)] if root else []
                        rss = 0
                        for item in processes:
                            try:
                                rss += item.memory_info().rss
                            except psutil.Error:
                                pass
                        peak = max(peak, rss)
                    except psutil.Error:
                        pass
                    if peak > memory_mb * 1024 * 1024:
                        failure_code = "MEMORY_LIMIT"
                    elif time.monotonic() - started > seconds:
                        failure_code = "OPERATION_TIMEOUT"
                    elif output_path.stat().st_size > budget:
                        failure_code = "OUTPUT_LIMIT"
                    elif error_path.stat().st_size > 1024 * 1024:
                        failure_code = "DIAGNOSTIC_LIMIT"
                    if failure_code:
                        _kill_tree(process)
                        break
                    time.sleep(0.025)
            except KeyboardInterrupt:
                failure_code = "INTERRUPTED"
                _kill_tree(process)
        if failure_code is None and error_path.stat().st_size > 1024 * 1024:
            failure_code = "DIAGNOSTIC_LIMIT"
        _cleanup_atomic_temporaries(request, operation)
        usage = {"elapsed_seconds": round(time.monotonic() - started, 4),
                 "peak_rss_bytes": peak, "timeout_seconds": seconds,
                 "max_memory_mb": memory_mb}
        if failure_code:
            return bounded_failure(CadError(failure_code, "Operation interrupted." if failure_code == "INTERRUPTED" else "Operation exceeded its resource budget.",
                                   {**usage, "retry": "Inspect output revision before retrying; "
                                    "an atomic commit may have completed before cancellation."}),
                                   request_id, budget)
        size = output_path.stat().st_size
        if size > budget:
            return bounded_failure(CadError("OUTPUT_LIMIT", "Worker response exceeds its byte budget.",
                                           {"bytes": size, "max_bytes": budget}), request_id, budget)
        try:
            response = json.loads(output_path.read_text(encoding="utf-8"))
            if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                raise ValueError("Missing response envelope")
        except (ValueError, UnicodeError) as exc:
            with error_path.open("rb") as stream:
                stream.seek(max(0, error_path.stat().st_size - 2048))
                diagnostic = stream.read(2048).decode("utf-8", errors="replace")
            return bounded_failure(CadError("WORKER_FAILED", "CAD worker did not return valid JSON.",
                                           {"returncode": process.returncode,
                                            "reason": str(exc)[:256], "diagnostic": diagnostic}),
                                   request_id, budget)
        with_usage = {**response, "resources": usage}
        return with_usage if encoded_size(with_usage, budget) + 1 <= budget else response
