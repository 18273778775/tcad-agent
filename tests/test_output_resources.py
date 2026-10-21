# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Agent output and worker budgets: preserve cursors, files, and service recovery."""

import hashlib
import json
import sys
import time

import pytest

from agentcad.errors import CadError
from agentcad.output import ENCODER, finalize_response
from agentcad.resources import run_request


def envelope(result, request_id="预算边界"):
    return {"schema_version": "1.0", "id": request_id, "ok": True, "result": result}


def wire(response):
    return (ENCODER.encode(response) + "\n").encode("utf-8")


def test_summary_bounds_utf8_bytes_and_explicitly_reports_text_and_list_omissions():
    original = envelope({"description": "测量结果🌏" * 10000,
                         "items": [{"handle": str(i)} for i in range(10000)]})
    bounded = finalize_response(original, {"max_output_bytes": 4096})
    assert len(wire(bounded)) <= 4096
    assert bounded["id"] == original["id"] and bounded["ok"]
    assert bounded["output"]["truncated"]
    omissions = {item["kind"]: item for item in bounded["output"]["omissions"]}
    assert omissions["string_characters"]["total"] == len(original["result"]["description"])
    assert omissions["array_items"]["total"] == 10000
    assert len(bounded["result"]["items"]) < 10000


def test_schema_exact_byte_boundary_includes_the_transport_newline():
    response = envelope({"padding": "图"})
    response["result"]["padding"] += "x" * (4096 - len(ENCODER.encode(response).encode("utf-8")))
    assert len(wire(response)) == 4097
    try:
        actual = finalize_response(response, {"max_output_bytes": 4096}, "schema")
    except CadError as error:
        assert error.code == "RESPONSE_TOO_LARGE"
    else:
        assert len(wire(actual)) <= 4096


def test_query_entities_and_cursor_survive_generic_summary_compaction():
    entities = [{"handle": format(i, "X"), "type": "LINE"} for i in range(90)]
    response = envelope({"entities": entities, "total": 900, "returned": 90,
                         "has_more": True, "next_cursor": "cursor-after-90",
                         "source_revision": "a" * 64})
    bounded = finalize_response(response, {"max_output_bytes": 16384}, "query")
    assert bounded["result"]["entities"] == entities
    assert bounded["result"]["returned"] == 90
    assert bounded["result"]["next_cursor"] == "cursor-after-90"
    assert len(wire(bounded)) <= 16384


def test_query_vertex_page_cannot_be_clipped_without_advancing_its_actual_offset():
    points = [[i, i + 1, 0, 0, 0] for i in range(128)]
    entity = {"handle": "A", "type": "LWPOLYLINE", "points": points,
              "geometry_page": {"offset": 0, "returned": 128, "total": 300,
                                "has_more": True, "next_offset": 128}}
    response = envelope({"entities": [entity], "returned": 1, "total": 1,
                         "has_more": False, "next_cursor": None})
    bounded = finalize_response(response, {"max_output_bytes": 32768}, "query")
    actual = bounded["result"]["entities"][0]
    page = actual["geometry_page"]
    assert page["returned"] == len(actual["points"])
    assert page["next_offset"] == page["offset"] + len(actual["points"])
    assert actual["points"] == points


def test_query_too_large_fails_instead_of_dropping_entities_before_its_cursor():
    entities = [{"handle": str(i), "output": "unclippable-" * 100} for i in range(100)]
    response = envelope({"entities": entities, "returned": 100, "total": 200,
                         "has_more": True, "next_cursor": "after-100"})
    with pytest.raises(CadError) as error:
        finalize_response(response, {"max_output_bytes": 4096}, "query")
    assert error.value.code == "RESPONSE_TOO_LARGE"


def test_result_file_has_complete_json_exact_bytes_and_sha256(tmp_path):
    path = tmp_path / "complete.json"
    result = {"description": "未裁剪" * 10000, "items": list(range(2000)),
              "provenance": {"handles": ["A", "B"]}}
    bounded = finalize_response(envelope(result), {"result_file": str(path),
                                                  "max_output_bytes": 4096})
    assert json.loads(path.read_text(encoding="utf-8")) == result
    raw = path.read_bytes()
    artifact = bounded["result"]["artifact"]
    assert artifact["complete_selected_result"]
    assert artifact["sha256"] == hashlib.sha256(raw).hexdigest()
    assert artifact["bytes"] == len(raw)
    assert raw.endswith(b"\n") and len(wire(bounded)) <= 4096
    assert not list(tmp_path.glob(".tcad-*"))


def test_result_file_overwrite_requires_its_own_explicit_flag(tmp_path):
    path = tmp_path / "result.json"
    path.write_bytes(b"preserve existing bytes")
    with pytest.raises(CadError) as error:
        finalize_response(envelope({"new": True}), {"result_file": str(path), "overwrite": True})
    assert error.value.code == "OUTPUT_EXISTS"
    assert path.read_bytes() == b"preserve existing bytes"
    finalized = finalize_response(envelope({"new": True}), {"result_file": str(path),
                                    "overwrite_result_file": True})
    assert finalized["result"]["artifact"]["complete_selected_result"]
    assert json.loads(path.read_text()) == {"new": True}


@pytest.mark.parametrize("already_exists", [False, True])
def test_result_file_size_failure_leaves_no_partial_artifact(tmp_path, monkeypatch, already_exists):
    monkeypatch.setenv("TCAD_MAX_RESULT_MB", "1")
    path = tmp_path / "result.json"
    if already_exists:
        path.write_bytes(b"original")
    with pytest.raises(CadError) as error:
        finalize_response(envelope({"text": "图" * 400000}),
                          {"result_file": str(path), "overwrite_result_file": True})
    assert error.value.code == "RESULT_FILE_TOO_LARGE"
    assert path.read_bytes() == b"original" if already_exists else not path.exists()
    assert not list(tmp_path.glob(".tcad-*"))


def test_fields_projection_and_no_provenance_apply_before_agent_response():
    result = {"totals": {"length": 42, "area": 100}, "units": {"name": "mm"},
              "provenance": {"handles": ["private-A"]},
              "nested": {"source": "private.dxf", "value": 7}, "source": "private.dxf"}
    projected = finalize_response(envelope(result), {"fields": ["totals.length", "units"]}, "measure")
    assert projected["result"] == {"totals": {"length": 42}, "units": {"name": "mm"}}
    removed = finalize_response(envelope(result), {"no_provenance": True}, "measure")
    assert "provenance" not in removed["result"] and "source" not in removed["result"]
    assert removed["result"]["nested"] == {"value": 7}
    assert any(item["kind"] == "provenance_removed"
               for item in removed["output"]["omissions"])


def child_request(code, *, timeout=2, memory=128, budget=4096):
    return run_request({"id": "owned-worker", "command": "capabilities",
                        "args": {"timeout_seconds": timeout, "max_memory_mb": memory,
                                 "max_output_bytes": budget}},
                       _command=[sys.executable, "-c", code])


def assert_worker_recovers():
    response = child_request("print('{\"schema_version\":\"1.0\",\"id\":\"owned-worker\",\"ok\":true,\"result\":{\"alive\":true}}')")
    assert response["ok"] and response["result"]["alive"]
    assert len(wire(response)) <= 4096


def test_supervisor_cancels_timed_out_child_and_subsequent_request_runs():
    started = time.monotonic()
    response = child_request("import time; time.sleep(3)", timeout=0.1)
    assert response["error"]["code"] == "OPERATION_TIMEOUT"
    assert time.monotonic() - started < 2.5
    assert len(wire(response)) <= 4096
    assert_worker_recovers()


def test_supervisor_cancels_memory_overrun_child_and_subsequent_request_runs():
    response = child_request("import time; payload = bytearray(100 * 1024 * 1024); time.sleep(2)",
                             timeout=2, memory=64)
    assert response["error"]["code"] == "MEMORY_LIMIT"
    assert response["error"]["details"]["peak_rss_bytes"] > 64 * 1024 * 1024
    assert_worker_recovers()


def test_supervisor_bounds_stdout_even_when_child_exits_before_next_poll():
    response = child_request("import sys; sys.stdout.write('x' * (2 * 1024 * 1024)); sys.stdout.flush()")
    assert response["error"]["code"] == "OUTPUT_LIMIT"
    assert len(wire(response)) <= 4096
    assert_worker_recovers()


def test_supervisor_enforces_requested_stdout_budget_before_timeout():
    started = time.monotonic()
    response = child_request("import sys,time; sys.stdout.write('x' * 16384); sys.stdout.flush(); time.sleep(3)",
                             timeout=1, budget=4096)
    assert response["error"]["code"] == "OUTPUT_LIMIT"
    assert time.monotonic() - started < 0.9
    assert_worker_recovers()


def test_fast_exit_diagnostic_overrun_is_checked_after_last_poll(monkeypatch):
    import agentcad.resources as resources

    real_sleep = time.sleep
    # Exercise the real process race: it can finish between two supervisor polls.
    monkeypatch.setattr(resources.time, "sleep", lambda _: real_sleep(0.4))
    response = child_request("import sys,time; time.sleep(0.2); sys.stderr.write('x' * (2 * 1024 * 1024)); "
                             "sys.stderr.flush(); print('{\"ok\":true,\"result\":{}}')")
    assert not response["ok"] and response["error"]["code"] == "DIAGNOSTIC_LIMIT"
    assert len(wire(response)) <= 4096


def test_cancellation_cleans_only_its_own_atomic_temporary_files(tmp_path):
    unrelated = tmp_path / ".tcad-other-operation-partial.json"
    unrelated.write_bytes(b"another request owns this")
    output = tmp_path / "result.json"
    code = (
        "import json,os,pathlib,sys,time; request=json.load(sys.stdin); "
        "parent=pathlib.Path(request['args']['result_file']).parent; "
        "temporary=parent/('.tcad-'+os.environ['TCAD_OPERATION_ID']+'-partial.json'); "
        "temporary.write_text('partial'); time.sleep(3)"
    )
    response = run_request({"id": "owned-temp", "command": "capabilities",
                            "args": {"result_file": str(output), "timeout_seconds": 0.5}},
                           _command=[sys.executable, "-c", code])
    assert response["error"]["code"] == "OPERATION_TIMEOUT"
    assert list(tmp_path.glob(".tcad-*")) == [unrelated]
    assert unrelated.read_bytes() == b"another request owns this"
    assert not output.exists()


def test_malformed_child_output_is_bounded_and_does_not_break_next_request():
    response = child_request("import sys; print('not JSON'); sys.stderr.write('诊断' * 2000)")
    assert response["error"]["code"] == "WORKER_FAILED"
    assert len(wire(response)) <= 4096
    assert_worker_recovers()
