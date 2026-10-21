# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""A cancelled batch must publish confirmed outcomes before reporting the stop."""

import copy
import hashlib
import json
from pathlib import Path

from agentcad import batch


def test_keyboard_interrupt_preserves_confirmed_results_in_complete_automatic_report(tmp_path,
                                                                                     monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "private-cache"))
    requests = [{"id": name, "command": "capabilities", "args": {}}
                for name in ("confirmed", "interrupted", "must-not-run")]
    original = copy.deepcopy(requests)
    calls = []

    def run(request):
        calls.append(request["id"])
        if request["id"] == "interrupted":
            raise KeyboardInterrupt
        return {"schema_version": "1.0", "id": request["id"], "ok": True,
                "result": {"confirmed_outcome": "retained exact response", "value": 42}}

    monkeypatch.setattr(batch, "run_request", run)
    args = {"max_memory_mb": 128, "timeout_seconds": 5, "max_output_bytes": 16384}
    response = batch.execute_batch(requests, args)
    assert calls == ["confirmed", "interrupted"]
    assert requests == original
    assert not response["ok"]
    assert response["executed"] == 1 and response["not_executed"] == 2
    assert response["failed_count"] == 0
    assert response["stop_reason"] == "BATCH_INTERRUPTED"
    assert response["interrupted_unconfirmed_index"] == 1
    assert response["results"][0]["id"] == "confirmed"
    artifact = response["artifact"]
    assert artifact["automatic_cache"] and artifact["complete_executed_responses"]
    path = Path(artifact["path"])
    assert path.is_relative_to(tmp_path / "private-cache")
    raw = path.read_bytes()
    assert raw.endswith(b"\n") and len(raw) == artifact["bytes"]
    assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
    saved = json.loads(raw)
    assert saved["results"] == response["results"]
    assert saved["executed"] == 1 and saved["not_executed"] == 2
    assert saved["stop_reason"] == "BATCH_INTERRUPTED"
    assert saved["interrupted_unconfirmed_index"] == 1
    assert saved["results"][0]["result"] == {
        "confirmed_outcome": "retained exact response", "value": 42}
    assert not list(path.parent.glob(".tcad-*"))
    # Cancellation is scoped to the batch, so a later batch remains usable.
    subsequent = batch.execute_batch([requests[0]], args)
    assert subsequent["ok"] and subsequent["executed"] == 1
    assert subsequent["artifact"]["path"] != artifact["path"]
