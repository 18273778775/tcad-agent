# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Undecodable legacy CAD bytes remain visible without breaking UTF-8 output."""
import hashlib
import json
from unittest.mock import patch

import ezdxf
import pytest

from agentcad.engine import _record, export_entities, query_entities
from agentcad.errors import CadError
from agentcad.io import jsonable
from agentcad.output import encoded_size
from agentcad.protocol import execute_request
from agentcad.query_cache import CACHE_VERSION
from agentcad.transport import loads_json


@pytest.fixture
def legacy_drawing(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    doc = ezdxf.new("R2000", units=4)
    doc.encoding = "gbk"
    doc.header["$DWGCODEPAGE"] = "ANSI_936"
    doc.layers.new("LEGACY_LAYER")
    space = doc.modelspace()
    bad = space.add_text("LEGACY_BAD", dxfattribs={"layer": "LEGACY_LAYER"})
    good = space.add_text("承台 CT2")
    literal = space.add_text(r"\udca2")
    space.add_line((0, 0), (3, 4))
    source = tmp_path / "legacy.dxf"
    doc.saveas(source)
    raw = source.read_bytes()
    # A lone lead byte before newline is invalid in the declared CP936/GBK.
    raw = raw.replace(b"LEGACY_BAD", b"BAD\xa2").replace(b"LEGACY_LAYER", b"LAYER\xa2")
    source.write_bytes(raw)
    return source, bad.dxf.handle, good.dxf.handle, literal.dxf.handle, hashlib.sha256(raw).hexdigest()


def test_actual_legacy_dxf_read_preserves_invalid_byte(legacy_drawing):
    source, handle, *_ = legacy_drawing
    doc = ezdxf.readfile(source)
    assert doc.encoding == "gbk"
    assert doc.entitydb[handle].dxf.text == "BAD" + chr(0xDCA2)
    assert doc.entitydb[handle].dxf.layer == "LAYER" + chr(0xDCA2)


def test_jsonable_escapes_surrogates_with_bounded_field_evidence():
    original = {"normal": "承台 CT2", "literal": r"\udca2", "nested": [{"text": "X" + chr(0xDCA2)}]}
    converted = jsonable(original)
    assert converted["normal"] == "承台 CT2"
    assert converted["literal"] == r"\udca2"
    assert converted["nested"][0]["text"] == r"X\udca2"
    metadata = converted["text_encoding"]
    assert metadata["encoding_complete"] is False
    assert metadata["issue_count"] == 1
    assert metadata["issues_complete"] is True
    assert metadata["issues"][0] == {
        "path": "/nested/0/text", "path_complete": True,
        "original_character_index": 1, "escaped_character_index": 1,
        "codepoint": "U+DCA2", "escape": r"\udca2", "byte_escape": r"\xa2",
    }
    assert original["nested"][0]["text"][-1] == chr(0xDCA2)  # Never mutate input.
    assert json.loads(json.dumps(converted, ensure_ascii=False).encode("utf-8")) == converted
    assert jsonable(r"\udca2") == r"\udca2"
    assert jsonable(chr(0xDCA2)) == r"\udca2"
    many = jsonable({"text": chr(0xDCA2) * 1000})["text_encoding"]
    assert many["issue_count"] == 1000
    assert many["issues_returned"] == len(many["issues"]) == 4
    assert many["issues_complete"] is False


def test_query_indexes_whole_legacy_source_before_ct_filter_without_failure(legacy_drawing):
    source, _, good, _, original_sha = legacy_drawing
    page = query_entities(source, text="CT2", fields=["handle", "text"])
    assert page["entities"] == [{"handle": good, "text": "承台 CT2"}]
    assert page["source_sha256"] == original_sha
    assert CACHE_VERSION >= 4
    with patch("agentcad.engine.read_document", side_effect=AssertionError("reparsed source")):
        assert query_entities(source, text="CT2", fields=["handle", "text"])["cache"]["hit"] is True


def test_query_bad_text_and_layer_are_visible_and_literal_is_distinguished(legacy_drawing):
    source, bad, _, literal, original_sha = legacy_drawing
    row = query_entities(source, handles=[bad], fields=["handle", "text", "layer"])["entities"][0]
    assert row["text"] == r"BAD\udca2"
    assert row["layer"] == r"LAYER\udca2"
    assert {issue["path"] for issue in row["text_encoding"]["issues"]} == {"/text", "/layer"}
    literal_row = query_entities(source, handles=[literal], fields=["handle", "text"])["entities"][0]
    assert literal_row["text"] == r"\udca2"
    assert "text_encoding" not in literal_row
    filtered = query_entities(source, layer=r"LAYER\udca2", fields=["handle"])
    assert filtered["entities"] == [{"handle": bad}]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_sha


def test_native_export_keeps_normal_entities_and_encoding_diagnostics(legacy_drawing, tmp_path):
    source, bad, good, _, original_sha = legacy_drawing
    output = tmp_path / "selected.jsonl"
    result = export_entities(source, output, fields=["handle", "type", "text", "layer", "start", "end"])
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert result["entity_count"] == len(rows) == 4
    by_handle = {row["handle"]: row for row in rows}
    assert by_handle[bad]["text"] == r"BAD\udca2"
    assert by_handle[bad]["text_encoding"]["encoding_complete"] is False
    assert by_handle[good]["text"] == "承台 CT2"
    assert any(row.get("start") == [0, 0, 0] and row.get("end") == [3, 4, 0] for row in rows)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_sha


def test_protocol_legacy_text_remains_within_utf8_budget(legacy_drawing):
    source, bad, *_ = legacy_drawing
    response = execute_request({"id": "legacy", "command": "query", "args": {
        "input": str(source), "handles": [bad], "fields": ["handle", "text", "layer"], "max_output_bytes": 4096,
    }})
    assert response["ok"] is True, response
    assert encoded_size(response) + 1 <= 4096
    assert response["result"]["entities"][0]["text_encoding"]["encoding_complete"] is False
    info = execute_request({"command": "info", "args": {"input": str(source), "include_bounds": False}})
    assert info["ok"] is True, info
    assert info["result"]["text_encoding"]["encoding_complete"] is False
    assert all(ord(character) < 0xD800 or ord(character) > 0xDFFF for character in json.dumps(info, ensure_ascii=False))


def test_many_invalid_bytes_fit_small_query_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    doc = ezdxf.new("R2000")
    doc.encoding = "gbk"
    doc.header["$DWGCODEPAGE"] = "ANSI_936"
    text = doc.modelspace().add_text("REPLACE_ME")
    source = tmp_path / "many-invalid.dxf"
    doc.saveas(source)
    source.write_bytes(source.read_bytes().replace(b"REPLACE_ME", b"\xa2 " * 500))
    response = execute_request({"command": "query", "args": {
        "input": str(source), "handles": [text.dxf.handle], "fields": ["handle", "text"], "max_output_bytes": 4096,
    }})
    assert response["ok"] is True, response
    assert encoded_size(response) + 1 <= 4096
    row = response["result"]["entities"][0]
    assert row["text_encoding"]["issue_count"] == 500
    assert row["text_encoding"]["issues_complete"] is False


def test_cad_output_tolerance_does_not_relax_strict_json_requests():
    with pytest.raises(CadError) as error:
        loads_json(r'{"command":"query","args":{"input":"\udca2"}}')
    assert error.value.code == "INVALID_JSON"
    response = execute_request({"command": "query", "args": {"input": chr(0xDCA2)}})
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_JSON"


def test_attached_legacy_attribute_keeps_parent_completeness_false(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    doc = ezdxf.new("R2000")
    doc.encoding = "gbk"
    doc.header["$DWGCODEPAGE"] = "ANSI_936"
    doc.blocks.new("CAP")
    bad = doc.modelspace().add_blockref("CAP", (0, 0))
    bad.add_attrib("TYPE", "LEGACY_ATTRIBUTE", (0, 0))
    good = doc.modelspace().add_blockref("CAP", (1, 0))
    good.add_attrib("TYPE", "正常中文 CT2", (1, 0))
    source = tmp_path / "legacy-attribute.dxf"
    doc.saveas(source)
    raw = source.read_bytes().replace(b"LEGACY_ATTRIBUTE", b"CT2\xa2")
    source.write_bytes(raw)
    original_sha = hashlib.sha256(raw).hexdigest()
    decoded = ezdxf.readfile(source)
    assert decoded.entitydb[bad.dxf.handle].attribs[0].dxf.text == "CT2" + chr(0xDCA2)
    request = {"command": "query", "args": {
        "input": str(source), "handles": [bad.dxf.handle], "fields": ["handle", "attributes"], "max_output_bytes": 4096,
    }}
    response = execute_request(request)
    assert response["ok"] is True, response
    assert encoded_size(response) + 1 <= 4096
    row = response["result"]["entities"][0]
    assert row["attribute_complete"] is False
    assert row["truncation"]["attributes"]["reason"] == "text_encoding"
    assert row["attributes"][0]["text"] == r"CT2\udca2"
    assert row["attributes"][0]["text_encoding"]["encoding_complete"] is False
    with patch("agentcad.engine.read_document", side_effect=AssertionError("reparsed source")):
        warm = execute_request(request)
    assert warm["ok"] is True
    assert warm["result"]["cache"]["hit"] is True
    assert warm["result"]["entities"][0]["attribute_complete"] is False
    assert CACHE_VERSION >= 6
    normal = query_entities(source, handles=[good.dxf.handle], fields=["attributes"])["entities"][0]
    assert normal["attribute_complete"] is True
    assert normal["attributes"][0]["text"] == "正常中文 CT2"
    assert "truncation" not in normal
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_sha


def test_attached_attribute_reports_both_caps_and_incomplete_encoding():
    doc = ezdxf.new("R2013")
    doc.blocks.new("CAP")
    insert = doc.modelspace().add_blockref("CAP", (0, 0))
    insert.add_attrib("TYPE", chr(0xDCA2) + "x" * 2000, (0, 0))
    row = _record(insert, fields=["attributes"])
    assert row["attribute_complete"] is False
    assert row["truncation"]["attributes"]["reason"] == "annotation_cap_and_text_encoding"
    assert row["attributes"][0]["truncation"]["text"]["complete"] is False
    assert row["attributes"][0]["text_encoding"]["encoding_complete"] is False
