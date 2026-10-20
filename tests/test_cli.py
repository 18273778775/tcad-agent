# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

import json
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

from agentcad.errors import CadError
from agentcad.protocol import execute_request, loads_json

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args, input=None, cwd=None):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "agentcad", *args],
        input=input,
        capture_output=True,
        encoding="utf-8",
        env=environment,
        cwd=cwd or ROOT,
        timeout=60,
    )


def test_cli_real_workflow_with_unicode_and_spaces(tmp_path):
    plan = tmp_path / "图纸 plan.json"
    plan.write_text(
        (ROOT / "examples" / "plate.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    output = tmp_path / "测试 plate.dxf"
    created = run_cli("apply", "--plan", str(plan), "--output", str(output))
    assert created.returncode == 0, created.stderr + created.stdout
    assert json.loads(created.stdout)["ok"]
    inspected = json.loads(run_cli("info", str(output)).stdout)
    assert inspected["result"]["entity_types"]["CIRCLE"] == 4
    queried = json.loads(run_cli("query", str(output), "--type", "CIRCLE", "--limit", "2").stdout)
    assert queried["result"]["total"] == 4
    assert len(queried["result"]["entities"]) == 2
    handle = queried["result"]["entities"][0]["handle"]
    measured = json.loads(run_cli("measure", str(output), "--handle", handle).stdout)
    assert measured["result"]["measurements"][0]["area"] == pytest.approx(78.5398163397)
    preview = tmp_path / "preview.svg"
    rendered = run_cli("render", str(output), "--output", str(preview))
    assert rendered.returncode == 0, rendered.stderr + rendered.stdout
    assert ElementTree.parse(preview).getroot().tag == "{http://www.w3.org/2000/svg}svg"


def test_jsonl_recovers_errors_and_preserves_ids():
    requests = (
        "\n".join(
            [
                "{broken",
                '{"id":42,"command":"capabilities","args":{}}',
                '{"id":"bad","command":"query","args":{"input":"absent.dxf","limit":false}}',
                '{"id":"after","command":"schema","args":{"target":"request"}}',
            ]
        )
        + "\n"
    )
    result = run_cli("serve", input=requests)
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(responses) == 4
    assert responses[0]["error"]["code"] == "INVALID_JSON"
    assert responses[1]["id"] == 42 and responses[1]["ok"]
    assert responses[2]["id"] == "bad" and not responses[2]["ok"]
    assert responses[3]["id"] == "after" and responses[3]["ok"]


def test_jsonl_invalid_utf8_and_surrogate_do_not_terminate_service():
    valid = b'{"id":"after","command":"capabilities","args":{}}\n'
    malformed = b"\xff\n" + b'{"id":"\\ud800","command":"capabilities","args":{}}\n'
    result = subprocess.run(
        [sys.executable, "-m", "agentcad", "serve"],
        input=malformed + valid,
        capture_output=True,
        cwd=ROOT,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
    assert len(responses) == 3
    assert responses[0]["error"]["code"] == "INVALID_JSON"
    assert responses[1]["error"]["code"] == "INVALID_JSON"
    assert responses[2]["id"] == "after" and responses[2]["ok"]


@pytest.mark.parametrize(
    "raw", ['{"x":NaN}', '{"x":1e999}', '{"x":1,"x":2}', "[" * 70 + "0" + "]" * 70]
)
def test_strict_json_rejects_ambiguous_or_nonfinite(raw):
    with pytest.raises(CadError) as error:
        loads_json(raw)
    assert error.value.code == "INVALID_JSON"


def test_invalid_cli_returns_json_without_traceback():
    result = run_cli("query", "absent.dxf", "--limit", "oops")
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "INVALID_ARGUMENT"
    assert "Traceback" not in result.stderr


def test_batch_failure_is_not_a_batch_transaction(tmp_path):
    output = tmp_path / "created.dxf"
    requests = [
        {"id": "missing", "command": "info", "args": {"input": "missing.dxf"}},
        {"id": "create", "command": "new", "args": {"output": str(output)}},
    ]
    result = run_cli("batch", input=json.dumps(requests))
    assert result.returncode == 1
    response = json.loads(result.stdout)
    assert not response["ok"]
    assert not response["results"][0]["ok"] and response["results"][1]["ok"]
    assert output.is_file()


def test_unknown_arguments_do_not_reach_file_operations(tmp_path):
    output = tmp_path / "should-not-exist.dxf"
    result = execute_request({"command": "new", "args": {"output": str(output), "shell": "bad"}})
    assert result["error"]["code"] == "INVALID_REQUEST"
    assert not output.exists()


def test_no_clobber_and_dry_run(tmp_path):
    output = tmp_path / "empty.dxf"
    assert run_cli("new", "--output", str(output), "--dry-run").returncode == 0
    assert not output.exists()
    assert run_cli("new", "--output", str(output)).returncode == 0
    original = output.read_bytes()
    rejected = run_cli("new", "--output", str(output))
    assert rejected.returncode == 1
    assert json.loads(rejected.stdout)["error"]["code"] == "OUTPUT_EXISTS"
    assert output.read_bytes() == original
