# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Independent regression cases for bounded CLI integration boundaries."""

import json
import os
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest
from jsonschema import Draft202012Validator

from agentcad.protocol import REQUEST_SCHEMA, execute_request

ROOT = Path(__file__).resolve().parents[1]


def cli(*arguments, requests=None):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, "-m", "agentcad", *arguments],
                          input=requests, capture_output=True, env=environment,
                          cwd=ROOT, timeout=45)


@pytest.mark.parametrize("field", ["timeout_seconds", "max_memory_mb"])
def test_batch_invalid_child_limit_is_json_and_later_requests_continue(field):
    requests = [
        {"id": "bad", "command": "capabilities", "args": {field: "invalid"}},
        {"id": "after", "command": "capabilities", "args": {"fields": ["name"]}},
    ]
    result = cli("batch", requests=json.dumps(requests).encode())
    assert result.returncode == 1, result.stderr.decode("utf-8", errors="replace")
    response = json.loads(result.stdout)
    assert response["ok"] is False
    assert response["executed"] == 2 and response["not_executed"] == 0
    assert response["failed_count"] == 1
    assert response["results"][0]["id"] == "bad"
    assert response["results"][0]["error"]["code"] in {"INVALID_ARGUMENT", "INVALID_REQUEST"}
    assert response["results"][1]["id"] == "after"
    assert response["results"][1]["ok"] is True
    assert b"Traceback" not in result.stderr


def test_batch_does_not_retruncate_an_independently_bounded_query_page(tmp_path):
    source = tmp_path / "fifty-lines.dxf"
    document = ezdxf.new()
    for index in range(50):
        document.modelspace().add_line((index, 0), (index, 1))
    document.saveas(source)
    request = {"id": "page", "command": "query",
               "args": {"input": str(source), "limit": 50}}
    result = cli("batch", requests=json.dumps([request]).encode())
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    response = json.loads(result.stdout)
    outcomes = response["results"]
    if not outcomes:
        # A whole response may live only in the persisted report; never rerun
        # an already executed request just because its inline result was omitted.
        outcomes = json.loads(Path(response["artifact"]["path"]).read_text(encoding="utf-8"))["results"]
    page = outcomes[0]["result"]
    assert page["returned"] == len(page["entities"]) == 50
    assert page["has_more"] is False and page["next_cursor"] is None
    assert len(result.stdout) <= 32768


def test_result_file_cannot_replace_the_cli_edit_plan(tmp_path):
    plan = tmp_path / "输入编辑计划.json"
    original = b'{"version":1,"operations":[]}'
    plan.write_bytes(original)
    drawing = tmp_path / "must-not-create.dxf"
    result = cli("apply", "--plan", str(plan), "--output", str(drawing),
                 "--result-file", str(plan), "--overwrite-result-file")
    assert result.returncode != 0
    assert json.loads(result.stdout)["error"]["code"] == "OUTPUT_CONFLICT"
    assert plan.read_bytes() == original
    assert not drawing.exists()


def test_result_file_cannot_replace_the_drawing_via_json_protocol(tmp_path):
    source = tmp_path / "source.dxf"
    ezdxf.new().saveas(source)
    original = source.read_bytes()
    result = execute_request({"id": "conflict", "command": "info", "args": {
        "input": str(source), "result_file": str(source), "overwrite_result_file": True}})
    assert result["ok"] is False and result["error"]["code"] == "OUTPUT_CONFLICT"
    assert source.read_bytes() == original


@pytest.mark.parametrize("role", ["input", "output"])
def test_batch_result_file_cannot_replace_any_child_drawing(tmp_path, role):
    drawing = tmp_path / "child.dxf"
    if role == "input":
        ezdxf.new().saveas(drawing)
        original = drawing.read_bytes()
        request = {"command": "info", "args": {"input": str(drawing)}}
    else:
        original = None
        request = {"command": "new", "args": {"output": str(drawing)}}
    result = cli("batch", "--result-file", str(drawing), "--overwrite-result-file",
                 requests=json.dumps([request]).encode())
    assert result.returncode != 0
    assert json.loads(result.stdout)["error"]["code"] == "OUTPUT_CONFLICT"
    if original is None:
        assert not drawing.exists()
    else:
        assert drawing.read_bytes() == original


@pytest.mark.parametrize("option,value", [("--max-output-bytes", "1"),
                                           ("--timeout-seconds", "nan"),
                                           ("--timeout-seconds", "0")])
def test_invalid_global_batch_budget_is_rejected_before_any_child_edit(tmp_path, option, value):
    drawing = tmp_path / "must-not-create.dxf"
    request = {"command": "new", "args": {"output": str(drawing)}}
    result = cli("batch", option, value, requests=json.dumps([request]).encode())
    assert result.returncode != 0
    assert json.loads(result.stdout)["error"]["code"] in {"INVALID_ARGUMENT", "INVALID_REQUEST"}
    assert not drawing.exists()


def test_request_schema_constrains_ids_that_cannot_fit_the_minimum_response_budget():
    request = {"id": 10 ** 4200, "command": "capabilities",
               "args": {"max_output_bytes": 4096}}
    assert not Draft202012Validator(REQUEST_SCHEMA).is_valid(request)
    response = execute_request(request)
    assert response["ok"] is False and response["error"]["code"] == "INVALID_REQUEST"
    assert len(json.dumps(response, ensure_ascii=False).encode("utf-8")) + 1 <= 4096


def test_real_jsonl_uses_utf8_and_single_lf_and_recovers_invalid_utf8():
    request = {"id": "机器人🧱", "command": "capabilities", "args": {"fields": ["name"]}}
    raw = b"\xff\n" + json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
    result = cli("serve", requests=raw)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout.endswith(b"\n") and b"\r\n" not in result.stdout
    responses = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
    assert len(responses) == 2
    assert responses[0]["error"]["code"] == "INVALID_JSON"
    assert responses[1]["id"] == "机器人🧱" and responses[1]["ok"] is True


def test_cli_request_schema_is_complete_and_valid_json_schema():
    result = cli("schema", "request", "--max-output-bytes", "1048576")
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    response = json.loads(result.stdout)
    assert response["ok"] is True and response["result"] == REQUEST_SCHEMA
    Draft202012Validator.check_schema(response["result"])


def test_schema_field_projection_cannot_silently_remove_validation_constraints():
    response = execute_request({"command": "schema", "args": {
        "target": "request", "fields": ["title"], "max_output_bytes": 1048576}})
    if response["ok"]:
        assert response["result"] == REQUEST_SCHEMA
    else:
        assert response["error"]["code"] in {"INVALID_ARGUMENT", "INVALID_REQUEST"}
