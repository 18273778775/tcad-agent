# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Agents can discover annotation fields and inspection limits before querying."""
from unittest.mock import patch

from agentcad.output import encoded_size
from agentcad.protocol import capabilities, execute_request
from agentcad.query import _QUERY_FIELDS


def test_capabilities_discovers_native_query_export_fields_and_limits():
    with patch("agentcad.converters.converter_status", return_value={}):
        inspection = capabilities()["entity_inspection"]
    assert inspection["query_export_fields"] == sorted(_QUERY_FIELDS)
    assert {"measurement", "actual_measurement", "text_override", "dimlfac", "attributes",
            "block_entities", "matrix44"} <= set(inspection["query_export_fields"])
    assert inspection["attributes"]["maximum_attached_attributes"] == 128
    assert inspection["blocks"]["maximum_children"] == 128
    assert inspection["blocks"]["maximum_child_vertices"] == 128
    assert "row_vector" in inspection["blocks"]["matrix44_convention"]
    assert inspection["text_encoding"]["maximum_diagnostic_issues"] == 4
    assert inspection["export"]["annotation_caps_apply"] is True
    assert "sha256" in inspection["export"]["receipt_fields"]


def test_field_discovery_command_retains_complete_inventory_under_default_budget():
    with patch("agentcad.converters.converter_status", return_value={}):
        response = execute_request({"command": "capabilities", "args": {
            "detail": "full", "fields": ["entity_inspection"],
        }})
    assert response["ok"] is True
    assert response["output"]["truncated"] is False
    assert encoded_size(response) + 1 <= 32768
    assert response["result"]["entity_inspection"]["query_export_fields"] == sorted(_QUERY_FIELDS)
