# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

from agentcad.output import finalize_response


def test_entity_export_projection_keeps_file_receipt():
    receipt = {"output": "walls.jsonl", "entity_count": 28, "bytes": 4096,
               "sha256": "a" * 64, "format": "jsonl"}
    response = finalize_response({"id": "export", "ok": True, "result": receipt},
                                 {"fields": ["handle", "start", "end"]}, "export")
    assert response["result"] == receipt
    assert response["output"]["truncated"] is False
