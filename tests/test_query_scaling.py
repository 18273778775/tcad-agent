# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Persistent query pages, projection order, byte budgets, and edit regressions."""
import json
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import ezdxf
import pytest

from agentcad.engine import apply_plan, measure_entities, query_entities
from agentcad.errors import CadError
from agentcad import query_cache


@pytest.fixture
def drawing(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    source = tmp_path / "drawing.dxf"
    doc = ezdxf.new("R2013", units=4)
    doc.layers.add("WALLS")
    for index in range(250):
        doc.modelspace().add_line((index, 0), (index, 2),
                                 dxfattribs={"layer": "WALLS" if index % 2 else "0"})
    doc.saveas(source)
    return source


def test_warm_pages_do_not_parse_or_format_source(drawing):
    first = query_entities(drawing, detail="summary", limit=30, fields=["handle", "type"])
    assert first["total"] == 250
    assert not first["cache"]["hit"]
    seen = [row["handle"] for row in first["entities"]]
    cursor = first["next_cursor"]
    with patch("agentcad.engine.read_document", side_effect=AssertionError("reparsed source")), \
            patch("agentcad.engine._record", side_effect=AssertionError("formatted source")):
        while cursor:
            page = query_entities(drawing, detail="summary", limit=30,
                                  fields=["handle", "type"], cursor=cursor)
            assert page["cache"]["hit"]
            seen.extend(row["handle"] for row in page["entities"])
            cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 250


def test_cross_process_snapshot_and_indexed_filters(drawing):
    first = query_entities(drawing, detail="summary", types=["LINE"], layer="WALLS", limit=2)
    code = """
import json,sys
from unittest.mock import patch
from agentcad.engine import query_entities
with patch('agentcad.engine.read_document',side_effect=AssertionError('reparsed source')):
    result=query_entities(sys.argv[1],detail='summary',types=['LINE'],layer='WALLS',limit=2,cursor=sys.argv[2])
print(json.dumps(result))
"""
    child = subprocess.run([sys.executable, "-c", code, str(drawing), first["next_cursor"]],
                           capture_output=True, text=True, check=True)
    second = json.loads(child.stdout)
    assert second["total"] == 125
    assert not ({e["handle"] for e in first["entities"]} & {e["handle"] for e in second["entities"]})
    cache = next((drawing.parent / "cache").glob("*.sqlite3"))
    with sqlite3.connect(cache) as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT ordinal FROM entities WHERE layout=? AND layer=? AND type=?",
                          ("Model", "WALLS", "LINE")).fetchall()
    assert any("INDEX by_layer_type" in row[-1] for row in plan)


def test_cursor_rejects_changed_source_filter_and_expired_cache(drawing):
    first = query_entities(drawing, detail="summary", limit=2)
    with pytest.raises(CadError, match="different filters"):
        query_entities(drawing, detail="summary", layer="WALLS", cursor=first["next_cursor"])
    cache = next((drawing.parent / "cache").glob("*.sqlite3"))
    cache.unlink()
    with pytest.raises(CadError) as expired:
        query_entities(drawing, detail="summary", cursor=first["next_cursor"])
    assert expired.value.code == "CURSOR_EXPIRED"
    doc = ezdxf.readfile(drawing)
    doc.modelspace().add_circle((0, 0), 2)
    doc.saveas(drawing)
    with pytest.raises(CadError) as stale:
        query_entities(drawing, detail="summary", cursor=first["next_cursor"])
    assert stale.value.code == "STALE_CURSOR"


def test_fields_avoid_bounds_and_vertex_arrays_before_formatting(drawing):
    doc = ezdxf.readfile(drawing)
    poly = doc.modelspace().add_lwpolyline([(i, 0) for i in range(20000)])
    doc.saveas(drawing)
    with patch("agentcad.engine._bounds", side_effect=AssertionError("computed bounds")), \
            patch("ezdxf.entities.LWPolyline.get_points", side_effect=AssertionError("copied all vertices")):
        result = query_entities(drawing, handles=[poly.dxf.handle], fields=["handle", "point_count"])
    assert result["entities"] == [{"handle": poly.dxf.handle, "point_count": 20000}]


def test_byte_cropped_pages_never_skip_entities(drawing):
    seen = []
    cursor = None
    for index in range(100):
        page = query_entities(drawing, detail="summary", limit=100,
                              max_output_bytes=4096, cursor=cursor)
        assert len(json.dumps(page, ensure_ascii=False).encode()) < 4096
        assert page["returned"] == len(page["entities"])
        seen.extend(row["handle"] for row in page["entities"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 250
    assert index > 2


def test_large_single_entity_reports_vertex_and_text_truncation(drawing):
    doc = ezdxf.readfile(drawing)
    poly = doc.modelspace().add_lwpolyline([(i, i * 0.7) for i in range(20000)])
    text = doc.modelspace().add_mtext("x" * 20000)
    doc.saveas(drawing)
    first = query_entities(drawing, handles=[poly.dxf.handle], fields=["handle", "points"],
                           vertex_limit=10000, max_output_bytes=4096)["entities"][0]
    assert first["geometry_page"]["has_more"]
    assert first["geometry_page"]["returned"] == len(first["points"])
    second = query_entities(drawing, handles=[poly.dxf.handle], fields=["handle", "points"],
                            vertex_offset=first["geometry_page"]["next_offset"],
                            vertex_limit=10)["entities"][0]
    assert second["points"][0][0] == first["geometry_page"]["returned"]
    label = query_entities(drawing, handles=[text.dxf.handle], fields=["handle", "text"])["entities"][0]
    assert len(label["text"]) == 1024
    assert label["truncation"]["text"]["total_characters"] == 20000


def test_query_does_not_expand_insert_entities(drawing):
    doc = ezdxf.readfile(drawing)
    block = doc.blocks.new("BIG_BLOCK")
    for index in range(10):
        block.add_line((index, 0), (index, 1))
    doc.modelspace().add_blockref("BIG_BLOCK", (0, 0))
    doc.saveas(drawing)
    with patch("ezdxf.entities.Insert.virtual_entities", side_effect=AssertionError("expanded block")):
        result = query_entities(drawing, detail="full", types=["INSERT"])
    assert result["total"] == 1
    assert result["entities"][0]["name"] == "BIG_BLOCK"


def test_layer_color_preserves_off_state(drawing, tmp_path):
    doc = ezdxf.readfile(drawing)
    doc.layers.get("WALLS").off()
    doc.saveas(drawing)
    output = tmp_path / "updated.dxf"
    apply_plan({"version": 1, "operations": [{"op": "layer", "name": "WALLS", "color": 3}]},
               output, input_path=drawing)
    updated = ezdxf.readfile(output).layers.get("WALLS")
    assert updated.is_off()
    assert updated.color == 3


def test_elevated_polyline_transform_keeps_measurement(drawing, tmp_path):
    doc = ezdxf.readfile(drawing)
    entity = doc.modelspace().add_polyline2d([(0, 0), (3, 0), (3, 4)],
                                           dxfattribs={"elevation": (0, 0, 12)})
    doc.saveas(drawing)
    output = tmp_path / "moved.dxf"
    original = measure_entities(drawing, [entity.dxf.handle])["measurements"][0]
    apply_plan({"version": 1, "operations": [{"op": "transform", "handles": [entity.dxf.handle],
                                              "translate": [10, 20, 5]}]}, output, input_path=drawing)
    moved = measure_entities(output, [entity.dxf.handle])["measurements"][0]
    assert original["length"] == moved["length"] == 7
    assert moved["supported"]


def test_cache_cleanup_caps_count(tmp_path, monkeypatch):
    monkeypatch.setattr(query_cache, "MAX_CACHE_FILES", 2)
    for index in range(5):
        (tmp_path / f"query-{index}.sqlite3").write_bytes(b"x" * 10)
    keep = tmp_path / "query-4.sqlite3"
    query_cache._prune(tmp_path, keep)
    assert len(list(tmp_path.glob("*.sqlite3"))) == 2
