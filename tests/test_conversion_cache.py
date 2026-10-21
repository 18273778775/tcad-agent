# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""DWG cache integrity, cross-process cursors, and bounded real subprocess IO."""
import json
import subprocess
import sys
import time
from unittest.mock import patch

import ezdxf
import pytest

from agentcad import converters
from agentcad.engine import query_entities
from agentcad.errors import CadError
from agentcad.io import file_sha256


@pytest.fixture
def dwg(tmp_path, monkeypatch):
    source = tmp_path / "room.dwg"
    source.write_bytes(b"AC1032" + b"controlled mock source")
    monkeypatch.setenv("TCAD_DWG2DXF", sys.executable)
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    calls = []

    def convert(command, **kwargs):
        calls.append(command)
        doc = ezdxf.new("R2013", units=4)
        for index in range(40):
            doc.modelspace().add_line((index, 0), (index, 1))
        doc.saveas(command[command.index("-o") + 1])
        return subprocess.CompletedProcess(command, 0, b"converted", b"original converter warning")

    monkeypatch.setattr(converters, "_run_converter", convert)
    return source, calls


def test_stable_cached_path_stat_diagnostics_and_integrity(dwg):
    source, calls = dwg
    info = {}
    with converters.readable_dxf(source, conversion_info=info) as path:
        original_path, original_stat = path, path.stat()
        digest = file_sha256(path)
    assert len(calls) == 1
    assert not info["cache"]["hit"]
    assert info["source_sha256"] == file_sha256(source)
    with patch("agentcad.converters.read_document", side_effect=AssertionError("Cache reparse")):
        reused_info = {}
        with converters.readable_dxf(source, conversion_info=reused_info) as reused:
            assert reused == original_path
            assert reused.stat().st_mtime_ns == original_stat.st_mtime_ns
            assert reused.stat().st_ctime_ns == original_stat.st_ctime_ns
    assert len(calls) == 1
    assert reused_info["cache"]["hit"]
    assert reused_info["normalized_dxf_sha256"] == digest
    assert reused_info["diagnostics"]["stderr"] == "original converter warning"
    assert reused_info["input"] == str(source.resolve())
    record = json.loads(original_path.with_suffix(".json").read_text())
    assert record["complete"] is True
    assert record["dxf_sha256"] == digest


def test_dwg_cursor_survives_a_fresh_process_without_conversion_or_parse(dwg):
    source, calls = dwg
    with converters.readable_dxf(source) as converted:
        first = query_entities(converted, detail="summary", limit=3)
        original_path = str(converted)
    code = """
import json,sys
from unittest.mock import patch
from agentcad.converters import readable_dxf
from agentcad.engine import query_entities
with patch('agentcad.converters._run_converter',side_effect=AssertionError('reconverted DWG')), \\
     patch('agentcad.converters.read_document',side_effect=AssertionError('reparsed conversion')), \\
     patch('agentcad.engine.read_document',side_effect=AssertionError('reparsed query')):
    with readable_dxf(sys.argv[1]) as path:
        result=query_entities(path,detail='summary',limit=3,cursor=sys.argv[2])
        print(json.dumps({'path':str(path),'result':result}))
"""
    result = subprocess.run([sys.executable, "-c", code, str(source), first["next_cursor"]],
                            capture_output=True, text=True, check=False, timeout=20)
    assert result.returncode == 0, result.stderr
    second = json.loads(result.stdout)
    assert second["path"] == original_path
    assert len(calls) == 1
    assert second["result"]["cache"]["hit"]
    assert not ({e["handle"] for e in first["entities"]}
                & {e["handle"] for e in second["result"]["entities"]})


def test_changed_dwg_uses_new_cache_and_rejects_old_cursor(dwg):
    source, calls = dwg
    with converters.readable_dxf(source) as original:
        first = query_entities(original, detail="summary", limit=3)
        original_path = original
    source.write_bytes(source.read_bytes() + b"changed")
    with converters.readable_dxf(source) as changed:
        assert changed != original_path
        with pytest.raises(CadError) as error:
            query_entities(changed, detail="summary", cursor=first["next_cursor"])
    assert error.value.code == "STALE_CURSOR"
    assert len(calls) == 2


@pytest.mark.parametrize("damaged", ["dxf", "manifest", "incomplete", "wrong-shape"])
def test_damaged_cache_is_reconverted(dwg, damaged):
    source, calls = dwg
    with converters.readable_dxf(source) as path:
        original_path = path
    manifest = original_path.with_suffix(".json")
    if damaged == "dxf":
        original_path.write_bytes(b"damaged DXF")
    elif damaged == "manifest":
        manifest.write_text("corrupt JSON")
    elif damaged == "incomplete":
        data = json.loads(manifest.read_text())
        data["complete"] = False
        manifest.write_text(json.dumps(data))
    else:
        manifest.write_text("[]")
    with converters.readable_dxf(source) as restored:
        assert restored == original_path
        assert len(ezdxf.readfile(restored).modelspace()) == 40
    assert len(calls) == 2


def test_converter_identity_invalidates_cached_conversion(dwg, tmp_path, monkeypatch):
    source, calls = dwg
    executable = tmp_path / "mock-converter.exe"
    executable.write_bytes(b"version one")
    monkeypatch.setattr(converters, "_backend_executable", lambda backend: str(executable))
    with converters.readable_dxf(source) as first:
        first_path = first
    executable.write_bytes(b"version two")
    with converters.readable_dxf(source) as second:
        assert second != first_path
    assert len(calls) == 2


def test_dwg_cache_lru_caps_entries(dwg, monkeypatch):
    source, calls = dwg
    monkeypatch.setattr(converters, "_DWG_CACHE_MAX_FILES", 2)
    for index in range(4):
        source.write_bytes(b"AC1032" + str(index).encode())
        with converters.readable_dxf(source):
            pass
    root = converters._dwg_cache_root()
    assert len(list(root.glob("dwg-*.dxf"))) == 2
    assert len(list(root.glob("dwg-*.json"))) == 2
    assert len(calls) == 4


def test_invalid_explicit_converter_does_not_use_installed_fallback(monkeypatch):
    monkeypatch.setenv("TCAD_DWG2DXF", "missing-explicit-converter")
    monkeypatch.setattr(converters.shutil, "which", lambda path: None)
    monkeypatch.setattr("agentcad.install.find_installed_dwg2dxf", lambda: pytest.fail("Unexpected fallback"))
    assert not converters.converter_status()["libredwg"]["available"]
    with pytest.raises(CadError) as error:
        converters._backend_executable("auto")
    assert error.value.code == "CONVERTER_NOT_FOUND"


def test_explicitly_installed_converter_is_discovered_without_path(monkeypatch):
    monkeypatch.delenv("TCAD_DWG2DXF", raising=False)
    monkeypatch.setattr(converters.shutil, "which", lambda path: None)
    monkeypatch.setattr("agentcad.install.find_installed_dwg2dxf", lambda: sys.executable)
    status = converters.converter_status()["libredwg"]
    assert status["executable"] == sys.executable
    assert status["detection_source"] == "setup-dwg"


def test_oversized_dwg_is_rejected_before_conversion(tmp_path, monkeypatch):
    source = tmp_path / "huge.dwg"
    with source.open("wb") as stream:
        stream.write(b"AC1032")
        stream.truncate(2 * 1024 * 1024)
    monkeypatch.setenv("TCAD_MAX_INPUT_MB", "1")
    monkeypatch.setattr(converters, "_backend_executable", lambda backend: pytest.fail("Must reject before detection"))
    with pytest.raises(CadError) as error:
        with converters.readable_dxf(source):
            pass
    assert error.value.code == "INPUT_TOO_LARGE"


def test_actual_converter_runner_retains_only_bounded_tails():
    command = [sys.executable, "-c", "import os;os.write(1,b'A'*1048576+b'end-out');os.write(2,b'B'*1048576+b'end-err')"]
    result = converters._run_converter(command, timeout=15)
    assert result.returncode == 0
    assert len(result.stdout) == len(result.stderr) == 4096
    assert result.stdout.endswith(b"end-out")
    assert result.stderr.endswith(b"end-err")
    assert len(converters._diagnostic("中" * 10000).encode()) <= 4096


def test_actual_converter_runner_stops_log_flood(monkeypatch):
    monkeypatch.setattr(converters, "_CONVERTER_LOG_LIMIT", 32768)
    command = [sys.executable, "-c", "import os,time;os.write(1,b'x'*1048576);time.sleep(30)"]
    started = time.monotonic()
    with pytest.raises(CadError) as error:
        converters._run_converter(command, timeout=10)
    assert error.value.code == "CONVERTER_LOG_LIMIT"
    assert len(error.value.details["stdout"].encode()) <= 4096
    assert time.monotonic() - started < 5


def test_actual_timeout_kills_converter_descendants(tmp_path):
    marker, ready = tmp_path / "descendant-survived", tmp_path / "descendant-started"
    child = ("from pathlib import Path;import time;"
             f"Path({str(ready)!r}).write_text('ready');time.sleep(2);"
             f"Path({str(marker)!r}).write_text('survived')")
    parent = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired):
        converters._run_converter([sys.executable, "-c", parent], timeout=1)
    assert ready.exists(), "Test must actually launch a descendant"
    time.sleep(2.1)
    assert not marker.exists(), "A converter descendant survived the timeout"


def test_actual_converter_runner_stops_oversized_output(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_MAX_INPUT_MB", "1")
    output = tmp_path / "too-large.dxf"
    command = [sys.executable, "-c",
               "import sys,time;open(sys.argv[2],'wb').truncate(2*1024*1024);time.sleep(20)",
               "-o", str(output)]
    started = time.monotonic()
    with pytest.raises(CadError) as error:
        converters._run_converter(command, timeout=10)
    assert error.value.code == "CONVERSION_OUTPUT_TOO_LARGE"
    assert time.monotonic() - started < 5
