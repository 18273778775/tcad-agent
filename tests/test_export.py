# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Complete selected-data export is separate from compact query stdout."""
import hashlib
import json
from unittest.mock import patch

import ezdxf
import pytest

from agentcad.engine import export_entities
from agentcad.errors import CadError


@pytest.fixture
def drawing(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    source = tmp_path / "drawing.dxf"
    doc = ezdxf.new("R2013", units=4)
    doc.layers.add("DETAIL")
    poly = doc.modelspace().add_lwpolyline([(index, index * 0.5) for index in range(2000)],
                                         dxfattribs={"layer": "DETAIL"})
    text_value = "Complete annotation: " + "height and room; " * 1000
    text = doc.modelspace().add_mtext(text_value, dxfattribs={"layer": "DETAIL"})
    line = doc.modelspace().add_line((0, 0), (1, 1))
    doc.saveas(source)
    return source, poly.dxf.handle, text.dxf.handle, line.dxf.handle, text_value


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_export_keeps_full_vertices_and_text(drawing, tmp_path):
    source, poly_handle, text_handle, _, text_value = drawing
    original = source.read_bytes()
    target = tmp_path / "complete.jsonl"
    result = export_entities(source, target, layer="DETAIL")
    rows = {row["handle"]: row for row in _rows(target)}
    assert len(rows[poly_handle]["points"]) == 2000
    assert rows[poly_handle]["points"][-1][:2] == [1999, 999.5]
    assert rows[text_handle]["text"] == text_value
    assert rows[text_handle]["plain_text"] == text_value
    assert all("truncation" not in row and "geometry_page" not in row for row in rows.values())
    assert result["entity_count"] == 2
    assert result["bytes"] == target.stat().st_size
    assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert "entities" not in result
    assert source.read_bytes() == original


def test_export_applies_type_layer_handle_and_fields_before_geometry(drawing, tmp_path):
    source, poly_handle, _, line_handle, _ = drawing
    target = tmp_path / "handles.ndjson"
    with patch("agentcad.engine._bounds", side_effect=AssertionError("Unrequested bounds")):
        result = export_entities(source, target, types=["lwpolyline"], layer="DETAIL",
                                 handles=[poly_handle, line_handle], fields=["handle", "point_count"])
    assert result["entity_count"] == 1
    assert _rows(target) == [{"handle": poly_handle, "point_count": 2000}]


@pytest.mark.parametrize("budget", ["entities", "bytes"])
def test_export_limit_failure_never_publishes_or_changes_source(drawing, tmp_path, budget):
    source, _, _, _, _ = drawing
    target = tmp_path / "preserve.jsonl"
    target.write_bytes(b"previous export\n")
    original = source.read_bytes()
    limits = {"max_entities": 1} if budget == "entities" else {"max_bytes": 100}
    with pytest.raises(CadError) as error:
        export_entities(source, target, overwrite=True, **limits)
    assert error.value.code == ("EXPORT_ENTITY_LIMIT" if budget == "entities" else "EXPORT_BYTE_LIMIT")
    assert target.read_bytes() == b"previous export\n"
    assert source.read_bytes() == original
    assert not list(tmp_path.glob(".tcad-*"))


def test_export_requires_explicit_overwrite(drawing, tmp_path):
    source, *_ = drawing
    target = tmp_path / "preserve.jsonl"
    target.write_bytes(b"previous export\n")
    with patch("agentcad.engine.read_document", side_effect=AssertionError("Must fail before parsing")), \
            pytest.raises(CadError) as error:
        export_entities(source, target)
    assert error.value.code == "OUTPUT_EXISTS"


def test_export_refuses_to_replace_dxf(drawing):
    source, *_ = drawing
    original = source.read_bytes()
    with pytest.raises(CadError) as error:
        export_entities(source, source, overwrite=True)
    assert error.value.code == "UNSUPPORTED_FORMAT"
    assert source.read_bytes() == original


def test_empty_export_has_explicit_error_without_publishing(drawing, tmp_path):
    source, *_ = drawing
    target = tmp_path / "empty.jsonl"
    with pytest.raises(CadError) as error:
        export_entities(source, target, types=[])
    assert error.value.code == "EMPTY_SELECTION"
    assert not target.exists()
