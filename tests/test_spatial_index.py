# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0
"""WCS block selection, annotation identities and persistent R-tree reuse."""
import json
import sqlite3
import time
from unittest.mock import patch

import ezdxf
import pytest

from agentcad.errors import CadError
from agentcad.query import query_entities
from agentcad.spatial import entity_bounds, index_drawing, iter_world_entities, select_handles


@pytest.fixture
def spatial_case(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    doc = ezdxf.new("R2013")
    for layer in ("COLUMN", "AXIS", "HIDDEN"):
        doc.layers.add(layer)
    doc.layers.get("HIDDEN").off()
    block = doc.blocks.new("OFFSET", base_point=(100, 100))
    line = block.add_line((100, 100), (110, 100))
    label = block.add_text("KZ1", dxfattribs={"insert": (102, 103), "height": 1})
    first = doc.modelspace().add_blockref("OFFSET", (1000, 2000),
                                         dxfattribs={"rotation": 90, "xscale": 2, "layer": "COLUMN"})
    first.add_attrib("NUMBER", "C01", insert=(1001, 2010), dxfattribs={"height": 1})
    second = doc.modelspace().add_blockref("OFFSET", (3000, 4000), dxfattribs={"layer": "HIDDEN"})
    native = doc.modelspace().add_line((1000, 2005), (1001, 2005), dxfattribs={"layer": "AXIS"})
    source = tmp_path / "drawing.dxf"
    doc.saveas(source)
    return source, doc, first, second, line, label, native


def test_rtree_includes_insert_world_geometry_and_attached_attributes(spatial_case):
    source, _, first, second, *_ = spatial_case
    receipt = index_drawing(source)
    assert receipt["entity_count"] == 3
    assert receipt["block_annotation_count"] == 3
    assert receipt["source_sha256"]
    # The definition is far from WCS, and its insertion point is outside this
    # box: selection must use transformed geometry, not either anchor.
    selected = select_handles(source, bbox=[999.9, 2018, 1000.1, 2020.1])
    assert selected["handles"] == [first.dxf.handle]
    assert second.dxf.handle not in selected["handles"]
    page = query_entities(source, bbox=[999.9, 2018, 1000.1, 2020.1], fields=["handle", "bounds"])
    assert [row["handle"] for row in page["entities"]] == selected["handles"]
    assert page["entities"][0]["bounds"]["max"][1] >= 2020
    attribute = select_handles(source, bbox=[1000.9, 2009.9, 1001.1, 2010.1], types=["INSERT"])
    assert attribute["handles"] == [first.dxf.handle]
    cache = next((source.parent / "cache").glob("*.sqlite3"))
    with sqlite3.connect(cache) as db:
        assert db.execute("SELECT count(*) FROM spatial_bounds").fetchone()[0] == 3
        plan = db.execute("EXPLAIN QUERY PLAN SELECT id FROM spatial_bounds WHERE minx<=? AND maxx>=?", (1001, 999)).fetchall()
    assert any("VIRTUAL TABLE INDEX" in row[-1] for row in plan)


def test_warm_region_and_new_base_projection_do_not_parse_source(spatial_case):
    source, *_ = spatial_case
    index_drawing(source)
    with patch("agentcad.engine.read_document", side_effect=AssertionError("reparsed source")):
        result = select_handles(source, bbox=[900, 1900, 1100, 2100])
        page = query_entities(source, bbox=[900, 1900, 1100, 2100],
                              fields=["handle", "type", "layer", "world_bbox", "visible"])
    assert result["cache"]["hit"]
    assert result["entity_count"] == page["total"] == 2


def test_block_text_world_positions_unique_keys_and_visibility(spatial_case):
    source, _, first, second, _, label, _ = spatial_case
    assert query_entities(source, text="KZ1", fields=["handle"])["total"] == 0
    page = query_entities(source, text="KZ1", text_scope="block",
                          fields=["handle", "insert", "layer", "visible"])
    rows = page["entities"]
    assert len(rows) == 2
    assert rows[0]["handle"] == rows[1]["handle"] == label.dxf.handle
    assert rows[0]["entity_key"] != rows[1]["entity_key"]
    assert rows[0]["instance_path"] == [first.dxf.handle]
    assert rows[1]["instance_path"] == [second.dxf.handle]
    assert rows[0]["insert"] == pytest.approx([997, 2004, 0])
    assert rows[0]["layer"] == "COLUMN" and rows[0]["visible"]
    assert rows[1]["layer"] == "HIDDEN" and not rows[1]["visible"]
    assert query_entities(source, text="C01", text_scope="all", fields=["text", "insert"])["entities"][0]["insert"] == [1001, 2010, 0]


def test_leaf_layer_filters_ocs_and_nonuniform_circle_are_explicit(spatial_case):
    _, doc, first, *_ = spatial_case
    circle = doc.blocks.get("OFFSET").add_circle((105, 105), 2, dxfattribs={"layer": "AXIS"})
    diagnostics = {}
    rows = list(iter_world_entities(doc, roots=[first], layers=["AXIS"], diagnostics=diagnostics))
    assert len(rows) == 1
    assert rows[0]["handle"] == circle.dxf.handle
    assert rows[0]["entity"].dxftype() == "ELLIPSE"
    assert rows[0]["layer"] == "AXIS"
    assert diagnostics["complete"]
    assert circle.dxftype() == "CIRCLE" and circle.dxf.center == (105, 105, 0)


def test_nested_minsert_keys_and_transform_composition(tmp_path):
    doc = ezdxf.new("R2013")
    inner = doc.blocks.new("INNER")
    label = inner.add_text("AX", dxfattribs={"insert": (1, 2), "height": 1})
    outer = doc.blocks.new("OUTER")
    child = outer.add_blockref("INNER", (10, 20))
    root = doc.modelspace().add_blockref("OUTER", (100, 200), dxfattribs={"rotation": 90})
    root.dxf.column_count = 2
    root.dxf.column_spacing = 30
    rows = list(iter_world_entities(doc, types=["TEXT"]))
    assert len(rows) == 2
    assert rows[0]["instance_path"] == [root.dxf.handle + "[0]", child.dxf.handle]
    assert rows[1]["instance_path"] == [root.dxf.handle + "[1]", child.dxf.handle]
    assert rows[0]["handle"] == rows[1]["handle"] == label.dxf.handle
    assert rows[0]["world_insert"] == pytest.approx([78, 211, 0])
    assert rows[1]["world_insert"] == pytest.approx([78, 241, 0])
    assert len({row["entity_key"] for row in rows}) == 2


def test_minsert_attached_attributes_keep_source_handle(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    doc = ezdxf.new("R2013")
    doc.blocks.new("ATTR")
    insert = doc.modelspace().add_blockref("ATTR", (10, 20))
    attribute = insert.add_attrib("ID", "A1", insert=(12, 23), dxfattribs={"height": 1})
    insert.dxf.column_count = 2
    insert.dxf.column_spacing = 30
    source = tmp_path / "attrs.dxf"
    doc.saveas(source)
    page = query_entities(source, text_scope="block", fields=["handle", "insert", "entity_key"])
    rows = page["entities"]
    assert [row["handle"] for row in rows] == [attribute.dxf.handle, attribute.dxf.handle]
    assert rows[0]["insert"] == [12, 23, 0]
    assert rows[1]["insert"] == [42, 23, 0]
    assert rows[0]["entity_key"] != rows[1]["entity_key"]


def test_cycle_limit_stale_revision_and_cache_expiration(spatial_case, monkeypatch):
    source, doc, first, *_ = spatial_case
    report = index_drawing(source)
    doc.modelspace().add_point((1, 1))
    doc.saveas(source)
    with pytest.raises(CadError) as stale:
        select_handles(source, expected_source_revision=report["source_revision"])
    assert stale.value.code == "STALE_SOURCE"
    with pytest.raises(CadError) as limit:
        list(iter_world_entities(doc, max_entities=1))
    assert limit.value.code == "SPATIAL_TRAVERSAL_LIMIT"
    cycle = doc.blocks.new("CYCLE")
    cycle.add_blockref("CYCLE", (0, 0))
    root = doc.modelspace().add_blockref("CYCLE", (0, 0))
    diagnostic = {}
    assert list(iter_world_entities(doc, roots=[root], diagnostics=diagnostic)) == []
    assert diagnostic["omissions"][0]["reason"] == "cyclic_block"
    from agentcad import query_cache
    cache = source.parent / "cache"
    expired = cache / "query-old.sqlite3"
    expired.write_text("old", encoding="utf-8")
    import os
    os.utime(expired, (time.time() - 100, time.time() - 100))
    monkeypatch.setattr(query_cache, "MAX_CACHE_AGE_SECONDS", 10)
    query_cache._prune(cache, cache / "absent.sqlite3")
    assert not expired.exists()


def test_multi_layer_filters_match_native_selection(spatial_case):
    source, *_ = spatial_case
    selected = select_handles(source, layers=["COLUMN", "AXIS"], exclude_layers=["AXIS"])
    page = query_entities(source, layers=["COLUMN", "AXIS"], exclude_layers=["AXIS"], fields=["handle"])
    assert selected["handles"] == [row["handle"] for row in page["entities"]]
    assert len(selected["handles"]) == 1


def test_central_bounds_are_conservative_for_rotated_blocks(spatial_case):
    _, _, first, *_ = spatial_case
    bounded, missing, statistics = entity_bounds([first])
    assert not missing and statistics["definition_cache_entries"] == 1
    assert json.dumps(bounded[0][1], allow_nan=False)


def test_measure_export_query_share_native_region_selection(spatial_case, tmp_path):
    from agentcad.engine import export_entities, measure_entities
    source, _, first, *_ = spatial_case
    bbox = [900, 1900, 1100, 2100]
    query = query_entities(source, bbox=bbox, fields=["handle"])
    measure = measure_entities(source, bbox=bbox)
    output = tmp_path / "selection.jsonl"
    exported = export_entities(source, output, bbox=bbox, fields=["handle", "bounds"])
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert {row["handle"] for row in rows} == {row["handle"] for row in query["entities"]}
    assert {row["handle"] for row in rows} == {row["handle"] for row in measure["measurements"]}
    assert exported["selection"]["source_revision"] == measure["selection"]["source_revision"]
    assert next(row for row in rows if row["handle"] == first.dxf.handle)["bounds"] is not None
    assert not measure["totals"]["area_complete"]  # INSERT remains unsupported native geometry.


def test_invalid_area_and_extended_boundary_measurements(tmp_path):
    from agentcad.engine import measure_entities
    doc = ezdxf.new("R2013")
    space = doc.modelspace()
    bowtie = space.add_lwpolyline([(0, 0), (2, 2), (0, 2), (2, 0)], close=True)
    ellipse = space.add_ellipse((10, 0), major_axis=(3, 0), ratio=.5)
    hatch = space.add_hatch()
    hatch.paths.add_polyline_path([(0, 0), (10, 0), (10, 10), (0, 10)], is_closed=True)
    hatch.paths.add_polyline_path([(2, 2), (4, 2), (4, 4), (2, 4)], is_closed=True)
    source = tmp_path / "geometry.dxf"
    doc.saveas(source)
    result = measure_entities(source, [bowtie.dxf.handle, ellipse.dxf.handle, hatch.dxf.handle])
    rows = result["measurements"]
    assert rows[0]["area"] is None and rows[0]["area_kind"] == "invalid_boundary"
    assert rows[1]["area"] == pytest.approx(4.5 * __import__("math").pi)
    assert rows[2]["area"] == 96
    assert rows[2]["length"] == 48
    assert not result["totals"]["area_complete"]
    assert result["invalid_area_handles"] == [bowtie.dxf.handle]


def test_info_exposes_layer_blocks_and_unit_evidence(spatial_case):
    from agentcad.engine import inspect_drawing
    source, doc, *_ = spatial_case
    doc.units = 0
    label = doc.modelspace().add_text("所有尺寸单位毫米，标高单位米", dxfattribs={"height": 1})
    doc.saveas(source)
    info = inspect_drawing(source, include_bounds=False)
    assert info["units"]["name"] == "unitless"
    assert info["unit_evidence"]["annotations"][0]["handle"] == label.dxf.handle
    assert info["blocks"]["native_reference_count"] == 2
    layer = next(row for row in info["layers"] if row["name"] == "COLUMN")
    assert "linetype" in layer and "lineweight" in layer and "plot" in layer


def test_measure_exact_repeated_endpoint_without_closed_flag(tmp_path):
    from agentcad.engine import measure_entities
    doc = ezdxf.new("R2013")
    exact = doc.modelspace().add_lwpolyline([(0, 0), (3, 0), (3, 4), (0, 4), (0, 0)])
    gap = doc.modelspace().add_lwpolyline([(10, 0), (13, 0), (13, 4), (10, 4), (10, 1e-12)])
    source = tmp_path / "closed-by-endpoint.dxf"
    doc.saveas(source)
    result = measure_entities(source, [exact.dxf.handle, gap.dxf.handle])["measurements"]
    assert result[0]["closed"] and result[0]["area"] == 12 and result[0]["length"] == 14
    assert any("no gap tolerance" in warning for warning in result[0]["warnings"])
    assert not result[1]["closed"] and result[1]["area"] is None
