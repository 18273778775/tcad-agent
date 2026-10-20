# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Optional external DWG conversion with explicit, verified file boundaries.

LibreDWG is invoked for DWG input or an explicit conversion request. Detection does not
launch any program. No bundled/proprietary CAD application is used implicitly.
"""

import math
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .errors import CadError
from .io import atomic_output, read_document

_DIAGNOSTIC_LIMIT = 4096


def _detect(executable, environment_variable, *, enabled):
    configured = os.environ.get(environment_variable, "").strip()
    resolved = shutil.which(configured or executable)
    return {
        "available": resolved is not None,
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
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value or "")[-_DIAGNOSTIC_LIMIT:]


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
            result = subprocess.run(
                command,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
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


@contextmanager
def readable_dxf(path, backend="auto", timeout=60, conversion_info=None):
    """Yield a DXF path, cleaning up a temporary DWG conversion on exit."""
    source = _source_path(path)
    if source.suffix.lower() == ".dxf":
        yield source
        return
    with tempfile.TemporaryDirectory(prefix="tcad-dwg-") as directory:
        converted = Path(directory) / "converted.dxf"
        converted_info = convert_file(source, converted, backend=backend, timeout=timeout)
        if conversion_info is not None:
            conversion_info.update(
                {key: value for key, value in converted_info.items() if key != "output"}
            )
        yield converted
