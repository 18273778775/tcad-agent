# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Real-process write serialization, revision preconditions, and nonpublication."""
import json
import os
import subprocess
import sys
import time
from unittest.mock import patch

import ezdxf
import pytest

from agentcad import engine
from agentcad.errors import CadError
from agentcad.io import file_sha256, revision_guard


@pytest.fixture
def drawing(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    source = tmp_path / "drawing.dxf"
    doc = ezdxf.new("R2013", units=4)
    space = doc.modelspace()
    handles = [space.add_line((index, 0), (index, 1)).dxf.handle for index in range(2)]
    doc.saveas(source)
    return source, handles


_WORKER = r"""
import json,sys,time
from pathlib import Path
from unittest.mock import patch
from agentcad import engine
from agentcad.errors import CadError
source,handle,color,expected,ready,gate=sys.argv[1:]
Path(ready).write_text('ready')
expiry=time.monotonic()+15
while not Path(gate).exists():
    if time.monotonic()>expiry: raise RuntimeError('start gate timed out')
    time.sleep(0.01)
operation=engine._apply_operation
def delayed(*args,**kwargs):
    value=operation(*args,**kwargs)
    time.sleep(0.25)
    return value
try:
    with patch('agentcad.engine._apply_operation',side_effect=delayed):
        engine.apply_plan({'version':1,'operations':[{'op':'update','handles':[handle],
        'attributes':{'color':int(color)}}]},source,input_path=source,overwrite=True,
        expected_source_sha256=None if expected=='none' else expected)
    result={'ok':True}
except CadError as exc:
    result={'ok':False,'code':exc.code}
print(json.dumps(result))
"""


def _two_writers(tmp_path, source, handles, expected=None):
    gate = tmp_path / "start"
    workers = []
    ready_paths = []
    for index, handle in enumerate(handles):
        ready = tmp_path / f"ready-{index}"
        ready_paths.append(ready)
        command = [sys.executable, "-c", _WORKER, str(source), handle, str(index + 2),
                   expected or "none", str(ready), str(gate)]
        workers.append(subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=os.environ.copy()))
    try:
        expiry = time.monotonic() + 15
        while not all(path.exists() for path in ready_paths):
            if time.monotonic() > expiry:
                raise AssertionError("Workers did not reach the simultaneous start gate")
            time.sleep(0.01)
        gate.write_text("go", encoding="utf-8")
        results = []
        for process in workers:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
        return results
    finally:
        for process in workers:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_concurrent_in_place_edits_keep_both_entity_changes(drawing, tmp_path):
    source, handles = drawing
    results = _two_writers(tmp_path, source, handles)
    assert all(result["ok"] for result in results)
    reopened = ezdxf.readfile(source)
    assert [reopened.entitydb[handle].dxf.color for handle in handles] == [2, 3]
    assert not list(tmp_path.glob(".tcad-*.dxf"))


def test_same_expected_revision_allows_exactly_one_writer(drawing, tmp_path):
    source, handles = drawing
    expected = file_sha256(source)
    results = _two_writers(tmp_path, source, handles, expected)
    assert sum(result["ok"] for result in results) == 1
    assert [result["code"] for result in results if not result["ok"]] == ["REVISION_CONFLICT"]
    colors = [ezdxf.readfile(source).entitydb[handle].dxf.color for handle in handles]
    assert colors in ([2, 256], [256, 3])


@pytest.mark.parametrize("changed", ["source", "output"])
def test_external_change_during_preparation_is_not_overwritten(drawing, tmp_path, changed):
    source, handles = drawing
    output = tmp_path / "output.dxf"
    output.write_bytes(b"previous output")
    original_output = output.read_bytes()
    outside_bytes = b"external writer owns these bytes"
    operation = engine._apply_operation

    def change_outside(*args, **kwargs):
        result = operation(*args, **kwargs)
        (source if changed == "source" else output).write_bytes(outside_bytes)
        return result

    with patch("agentcad.engine._apply_operation", side_effect=change_outside), \
            pytest.raises(CadError) as failure:
        engine.apply_plan({"version": 1, "operations": [{"op": "update", "handles": [handles[0]],
                                                          "attributes": {"color": 2}}]},
                          output, input_path=source, overwrite=True)
    assert failure.value.code == "REVISION_CONFLICT"
    assert output.read_bytes() == (outside_bytes if changed == "output" else original_output)
    if changed == "source":
        assert source.read_bytes() == outside_bytes
    assert not list(tmp_path.glob(".tcad-*.dxf"))


def test_failed_operation_does_not_publish(drawing, tmp_path):
    source, handles = drawing
    output = tmp_path / "output.dxf"
    output.write_bytes(b"previous output")
    original = source.read_bytes()
    with pytest.raises(CadError) as failure:
        engine.apply_plan({"version": 1, "operations": [
            {"op": "update", "handles": [handles[0]], "attributes": {"color": 2}},
            {"op": "delete", "handles": ["FFFFFF"]}]}, output, input_path=source, overwrite=True)
    assert failure.value.code == "ENTITY_NOT_FOUND"
    assert source.read_bytes() == original
    assert output.read_bytes() == b"previous output"


def test_missing_output_precondition_is_compare_and_swap(drawing, tmp_path):
    source, handles = drawing
    plan = {"version": 1, "operations": [{"op": "update", "handles": [handles[0]],
                                           "attributes": {"color": 2}}]}
    output = tmp_path / "new.dxf"
    result = engine.apply_plan(plan, output, input_path=source,
                               expected_source_sha256=file_sha256(source), expected_output_sha256="missing")
    assert result["written"]
    original = output.read_bytes()
    with pytest.raises(CadError) as failure:
        engine.apply_plan(plan, output, input_path=source, overwrite=True,
                          expected_output_sha256="missing")
    assert failure.value.code == "REVISION_CONFLICT"
    assert output.read_bytes() == original


def test_outer_guard_keeps_original_source_identity(drawing, tmp_path):
    source, handles = drawing
    converted = tmp_path / "converted.dxf"
    converted.write_bytes(source.read_bytes())
    output = tmp_path / "result.dxf"
    operation = engine._apply_operation

    def mutate_original(*args, **kwargs):
        result = operation(*args, **kwargs)
        source.write_bytes(b"original source modified externally")
        return result

    with revision_guard(source, output), \
            patch("agentcad.engine._apply_operation", side_effect=mutate_original), \
            pytest.raises(CadError) as failure:
        engine.apply_plan({"version": 1, "operations": [{"op": "update", "handles": [handles[0]],
                                                          "attributes": {"color": 2}}]},
                          output, input_path=converted)
    assert failure.value.code == "REVISION_CONFLICT"
    assert not output.exists()
