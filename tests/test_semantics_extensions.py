# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

import json
import math

import ezdxf
import pytest
from ezdxf.math import Vec2
from ezdxf.render.mleader import ConnectionSide

from agentcad.errors import CadError
from agentcad.io import file_sha256
from agentcad.semantics import associate_entities, axes_entities


def save(doc, tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    source = tmp_path / "source.dxf"
    doc.saveas(source)
    return source


def rect(space, x, y):
    return space.add_lwpolyline([(x, y), (x + 2, y), (x + 2, y + 2), (x, y + 2)],
                               close=True, dxfattribs={"layer": "TARGET"})


def label(space, value, point, layer="LABEL"):
    return space.add_text(value, dxfattribs={"insert": point, "height": 0.2, "layer": layer})


def test_nearest_preserves_competing_candidates_and_configurable_margin(tmp_path, monkeypatch):
    doc = ezdxf.new()
    space = doc.modelspace()
    first = rect(space, 0, 0)
    second = rect(space, 8.1, 0)
    label(space, "A", (5, 1))
    source = save(doc, tmp_path, monkeypatch)
    result = associate_entities(source, label_layers="LABEL", target_layers="TARGET",
                                method="nearest", max_distance=4)
    pair = result["pairs"][0]
    assert pair["target"]["handle"] == first.dxf.handle
    assert pair["confidence"] == "unique_nearest_geometry"
    assert pair["distance_margin"] == pytest.approx(0.1)
    assert {c["target"]["handle"] for c in pair["candidates"]} == {
        first.dxf.handle, second.dxf.handle}
    ambiguous = associate_entities(source, label_layers="LABEL", target_layers="TARGET",
                                  method="nearest", max_distance=4, ambiguity_tolerance=0.2)
    assert not ambiguous["pairs"]
    assert ambiguous["ambiguous"][0]["competing_candidate_count"] == 2
    assert ambiguous["ambiguous"][0]["reason"] == "candidates_within_ambiguity_tolerance"


def test_polyline_leader_opt_in_and_native_tip_tail_evidence(tmp_path, monkeypatch):
    doc = ezdxf.new()
    space = doc.modelspace()
    target = rect(space, 0, 0)
    label(space, "A", (8, 8))
    line = space.add_lwpolyline([(1, 1), (4, 8), (8, 8)], dxfattribs={"layer": "LEADERS"})
    source = save(doc, tmp_path, monkeypatch)
    result = associate_entities(source, label_layers="LABEL", target_layers="TARGET",
                                method="leader", max_distance=0.5)
    assert not result["pairs"]
    result = associate_entities(source, label_layers="LABEL", target_layers="TARGET",
                                method="leader", leader_layers="LEADERS", max_distance=0.5)
    pair = result["pairs"][0]
    assert pair["target"]["handle"] == target.dxf.handle
    assert pair["leader"]["handle"] == line.dxf.handle
    assert pair["leader"]["tip"] == [1, 1]
    assert pair["leader"]["tail"] == [8, 8]
    assert pair["leader"]["direction_evidence"] == "polyline_endpoint_hypothesis"


def test_crossing_polyline_leaders_keep_equal_competitors_ambiguous(tmp_path, monkeypatch):
    doc = ezdxf.new()
    space = doc.modelspace()
    rect(space, 0, 0)
    rect(space, 10, 0)
    label(space, "A", (6, 8))
    for x in (1, 11):
        space.add_lwpolyline([(x, 1), (6, 8)], dxfattribs={"layer": "LEADERS"})
    source = save(doc, tmp_path, monkeypatch)
    result = associate_entities(source, label_layers="LABEL", target_layers="TARGET",
                                method="leader", leader_layers="LEADERS", max_distance=1)
    assert not result["pairs"]
    assert len(result["ambiguous"][0]["candidates"]) == 2
    assert all("tip" in c["leader"] for c in result["ambiguous"][0]["candidates"])


def test_mleader_intrinsic_content_nested_world_transform(tmp_path, monkeypatch):
    doc = ezdxf.new("R2013")
    block = doc.blocks.new("INNER")
    target = rect(block, 0, 0)
    builder = block.add_multileader_mtext(dxfattribs={"layer": "LABEL"})
    builder.set_content("A", char_height=0.5)
    builder.add_leader_line(ConnectionSide.left, [Vec2(1, 1), Vec2(4, 5)])
    builder.build(insert=Vec2(8, 8))
    outer = doc.blocks.new("OUTER")
    nested = outer.add_blockref("INNER", (10, 0), dxfattribs={"rotation": 90})
    root = doc.modelspace().add_blockref("OUTER", (100, 200), dxfattribs={"rotation": 90})
    source = save(doc, tmp_path, monkeypatch)
    before = file_sha256(source)
    result = associate_entities(source, label_layers="LABEL", target_layers="TARGET",
                                method="leader", max_distance=0.1)
    pair = result["pairs"][0]
    assert pair["target"]["handle"] == target.dxf.handle
    assert pair["label"]["text"] == "A"
    assert pair["label"]["instance_path"] == [root.dxf.handle, nested.dxf.handle]
    assert pair["leader"]["link_kind"] == "annotation_handle"
    assert pair["leader"]["tip"] == pytest.approx([99, 209])
    assert pair["label"]["anchor"] == pytest.approx([92, 202])
    assert file_sha256(source) == before


def rotated_grid(doc):
    space = doc.blocks.new("GRID")
    for x, name in ((0, "A"), (10, "B")):
        space.add_lwpolyline([(x, 0), (x, 5), (x, 10)], dxfattribs={"layer": "AXIS"})
        space.add_circle((x, -4), 0.8, dxfattribs={"layer": "BUBBLE"})
        label(space, name, (x, -4), "AX_LABEL")
    for y, name in ((0, "1"), (10, "2")):
        space.add_line((0, y), (10, y), dxfattribs={"layer": "AXIS"})
        space.add_circle((-4, y), 0.8, dxfattribs={"layer": "BUBBLE"})
        label(space, name, (-4, y), "AX_LABEL")
    return doc.modelspace().add_blockref("GRID", (100, 200), dxfattribs={"rotation": 30})


def test_rotated_axes_intersections_saved_point_and_bbox(tmp_path, monkeypatch):
    doc = ezdxf.new()
    root = rotated_grid(doc)
    source = save(doc, tmp_path, monkeypatch)
    artifact = tmp_path / "axes.json"
    receipt = axes_entities(source, line_layers="AXIS", label_layers="AX_LABEL",
                            tolerance=0.01, max_distance=5, output=artifact)
    assert receipt["summary"]["oblique_count"] == 4
    assert receipt["summary"]["labeled_axis_count"] == 4
    assert receipt["summary"]["intersection_count"] == 4
    full = json.loads(artifact.read_text(encoding="utf-8"))
    assert full["axis_geometry_complete"]
    assert all(a["lines"][0]["instance_path"] == [root.dxf.handle]
               for a in full["axes"]["oblique"])
    p = [100 + 10 * math.cos(math.pi / 6), 205]
    result = axes_entities(source, axes=artifact, point=p, tolerance=0.01,
                           bbox=[p[0] - .1, p[1] - .1, p[0] + .1, p[1] + .1])
    assert len(result["axes_in_region"]["oblique"]) == 2
    assert result["nearest_intersections"][0]["point"] == pytest.approx(p)
    assert len(result["nearest"]["oblique"]) == 2
    assert all(v["on_axis"] for v in result["nearest"]["oblique"])
    selected = axes_entities(source, line_layers="AXIS", label_layers="AX_LABEL",
                            tolerance=0.01, bbox=[99, 199, 101, 201])
    assert selected["summary"]["oblique_count"] == 2
    assert selected["summary"]["intersection_count"] == 1


def test_curved_and_bent_axes_report_incomplete_instead_of_silent_skip(tmp_path, monkeypatch):
    doc = ezdxf.new()
    space = doc.modelspace()
    space.add_arc((0, 0), 5, 0, 90, dxfattribs={"layer": "AXIS"})
    space.add_lwpolyline([(0, 0), (1, 3), (5, 4)], dxfattribs={"layer": "AXIS"})
    space.add_line((0, 0), (10, 10), dxfattribs={"layer": "AXIS"})
    source = save(doc, tmp_path, monkeypatch)
    result = axes_entities(source, line_layers="AXIS", tolerance=0.01)
    assert result["summary"]["oblique_count"] == 1
    assert result["summary"]["unsupported_axis_count"] == 2
    assert not result["axis_geometry_complete"]
    assert not result["selection_complete"]
    assert {v["reason"] for v in result["unsupported_axes"]} == {
        "curved_closed_or_unsupported_axis_path", "non_collinear_polyline_axis"}


def test_axis_duplicate_names_and_output_intersection_budget(tmp_path, monkeypatch):
    doc = ezdxf.new()
    space = doc.modelspace()
    for y in (0, 10):
        space.add_line((0, y), (10, y + 10), dxfattribs={"layer": "AXIS"})
        label(space, "A", (-2, y - 2), "AX_LABEL")
    space.add_line((10, -5), (10, 30), dxfattribs={"layer": "AXIS"})
    source = save(doc, tmp_path, monkeypatch)
    result = axes_entities(source, line_layers="AXIS", label_layers="AX_LABEL", tolerance=.01,
                           max_distance=5, max_intersections=1)
    assert result["summary"]["intersection_count"] == 2
    assert len(result["intersections"]) == 1
    assert not result["intersections_complete"]
    duplicates = [w for w in result["warnings"] if isinstance(w, dict)
                  and w.get("code") == "DUPLICATE_AXIS_LABEL"]
    assert duplicates[0]["orientation"] == "oblique"
    with pytest.raises(CadError) as error:
        axes_entities(source, line_layers="AXIS", max_axes=1)
    assert error.value.code == "AXIS_LIMIT"


def test_saved_oblique_axis_geometry_validation_and_missing_direction(tmp_path, monkeypatch):
    doc = ezdxf.new()
    source = save(doc, tmp_path, monkeypatch)
    artifact = tmp_path / "axes.json"
    artifact.write_text(json.dumps({"source": {"sha256": file_sha256(source)}, "layout": "Model",
        "axes": {"vertical": [], "horizontal": [], "oblique": [
            {"id": "O-001", "position": 0, "range": [0, 10]}]}}), encoding="utf-8")
    with pytest.raises(CadError) as error:
        axes_entities(source, axes=artifact, point=[0, 0])
    assert error.value.code == "INVALID_AXES"
