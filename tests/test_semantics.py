# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

import json

import ezdxf
import pytest
from ezdxf.enums import TextEntityAlignment

from agentcad.errors import CadError
from agentcad.semantics import associate_entities, axes_entities


def rect(space, x, y, w=2, h=2, layer="TARGET"):
    return space.add_lwpolyline(
        [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True, dxfattribs={"layer": layer}
    )


def text(space, value, point, layer="LABEL"):
    return space.add_text(value, dxfattribs={"insert": point, "height": 0.2, "layer": layer})


def save(document, tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    path = tmp_path / "source.dxf"
    document.saveas(path)
    return path


def test_inside_uses_concave_polygon_not_bbox(tmp_path, monkeypatch):
    doc = ezdxf.new()
    space = doc.modelspace()
    space.add_lwpolyline(
        [(0, 0), (3, 0), (3, 1), (1, 1), (1, 3), (0, 3)], close=True, dxfattribs={"layer": "TARGET"}
    )
    text(space, "inside", (0.5, 0.5))
    text(space, "outside", (2, 2))
    path = save(doc, tmp_path, monkeypatch)
    result = associate_entities(path, label_layers="LABEL", target_layers="TARGET")
    assert result["summary"]["pair_count"] == 1
    assert result["pairs"][0]["label"]["text"] == "inside"
    assert result["unmatched"][0]["label"]["text"] == "outside"


def test_nearest_tie_is_ambiguous_and_threshold_applies(tmp_path, monkeypatch):
    doc = ezdxf.new()
    space = doc.modelspace()
    rect(space, 0, 0)
    rect(space, 8, 0)
    text(space, "tie", (5, 1))
    text(space, "far", (50, 1))
    path = save(doc, tmp_path, monkeypatch)
    result = associate_entities(
        path, label_layers=["LABEL"], target_layers=["TARGET"], method="nearest", max_distance=4
    )
    assert result["summary"]["ambiguous_count"] == 1
    assert result["ambiguous"][0]["reason"] == "multiple_equal_candidates"
    assert result["summary"]["unmatched_count"] == 1
    assert not result["pairs"]


def test_inside_multiple_targets_and_one_to_one_conflicts(tmp_path, monkeypatch):
    doc = ezdxf.new()
    space = doc.modelspace()
    rect(space, 0, 0)
    text(space, "A", (0.5, 0.5))
    text(space, "B", (1, 1))
    path = save(doc, tmp_path, monkeypatch)
    result = associate_entities(
        path, label_layers=["LABEL"], target_layers=["TARGET"], one_to_one=True
    )
    assert not result["pairs"]
    assert result["summary"]["ambiguous_count"] == 2
    assert {v["reason"] for v in result["ambiguous"]} == {"one_to_one_conflict"}
    rect(space, 0, 0)
    doc.saveas(path)
    result = associate_entities(path, label_layers=["LABEL"], target_layers=["TARGET"])
    assert not result["pairs"]
    assert result["summary"]["ambiguous_count"] == 2


def test_leader_annotation_handle_beats_nearby_unrelated_leader(tmp_path, monkeypatch):
    doc = ezdxf.new()
    space = doc.modelspace()
    first = rect(space, 0, 0)
    rect(space, 10, 0)
    label = text(space, "COL", (5, 5))
    explicit = space.add_leader([(1, 1), (100, 100)])
    explicit.dxf.annotation_handle = label.dxf.handle
    space.add_leader([(11, 1), (5, 5)])
    path = save(doc, tmp_path, monkeypatch)
    result = associate_entities(
        path, label_layers="LABEL", target_layers="TARGET", method="leader", max_distance=2
    )
    assert result["pairs"][0]["target"]["handle"] == first.dxf.handle
    assert result["pairs"][0]["leader"]["link_kind"] == "annotation_handle"


def test_hidden_layer_and_correct_center_alignment_anchor(tmp_path, monkeypatch):
    doc = ezdxf.new()
    space = doc.modelspace()
    doc.layers.new("HIDDEN").off()
    rect(space, 0, 0)
    label = text(space, "center", (100, 100))
    label.set_placement((1, 1), align=TextEntityAlignment.MIDDLE_CENTER)
    text(space, "hidden", (1, 1), "HIDDEN")
    path = save(doc, tmp_path, monkeypatch)
    result = associate_entities(path, target_layers="TARGET")
    assert result["summary"]["label_count"] == 1
    assert result["pairs"][0]["label"]["anchor"] == [1, 1]
    result = associate_entities(path, target_layers="TARGET", include_hidden=True)
    assert result["summary"]["label_count"] == 2


def test_block_instance_provenance_and_document_roots_reuse(tmp_path, monkeypatch):
    doc = ezdxf.new()
    block = doc.blocks.new("ROOM")
    rect(block, 0, 0)
    text(block, "room", (1, 1))
    first = doc.modelspace().add_blockref("ROOM", (10, 20), dxfattribs={"rotation": 90})
    doc.modelspace().add_blockref("ROOM", (50, 50))
    path = save(doc, tmp_path, monkeypatch)
    result = associate_entities(
        path, label_layers="LABEL", target_layers="TARGET", document=doc, roots=[first]
    )
    assert result["summary"]["pair_count"] == 1
    assert result["pairs"][0]["label"]["instance_path"] == [first.dxf.handle]


def test_bounded_inline_and_full_artifact(tmp_path, monkeypatch):
    doc = ezdxf.new()
    space = doc.modelspace()
    rect(space, 0, 0)
    for i in range(120):
        text(space, str(i), (1, 1))
    path = save(doc, tmp_path, monkeypatch)
    inline = associate_entities(path, label_layers="LABEL", target_layers="TARGET")
    assert len(inline["pairs"]) == 100
    assert inline["evidence"]["truncated"]
    target = tmp_path / "assoc.json"
    unmatched = tmp_path / "unmatched.json"
    receipt = associate_entities(
        path,
        label_layers="LABEL",
        target_layers="TARGET",
        output=target,
        unmatched_output=unmatched,
    )
    assert receipt["summary"]["pair_count"] == 120
    assert len(json.loads(target.read_text(encoding="utf-8"))["pairs"]) == 120
    assert unmatched.is_file()
    with pytest.raises(CadError) as error:
        associate_entities(path, output=path, overwrite=True)
    assert error.value.code == "OUTPUT_CONFLICT"


def grid_drawing():
    doc = ezdxf.new()
    space = doc.modelspace()
    for x, name in [(0, "A"), (10, "B")]:
        space.add_line((x, 0), (x, 10), dxfattribs={"layer": "AXIS"})
        space.add_circle((x, -4), 0.8, dxfattribs={"layer": "BUBBLE"})
        text(space, name, (x - 0.3, -4), "AX_LABEL")
    for y, name in [(0, "1"), (10, "2")]:
        space.add_line((0, y), (10, y), dxfattribs={"layer": "AXIS"})
        space.add_circle((-4, y), 0.8, dxfattribs={"layer": "BUBBLE"})
        text(space, name, (-4.3, y), "AX_LABEL")
    return doc


def test_axes_grid_point_query_bbox_and_source_revision(tmp_path, monkeypatch):
    path = save(grid_drawing(), tmp_path, monkeypatch)
    target = tmp_path / "axes.json"
    receipt = axes_entities(
        path,
        line_layers="AXIS",
        label_layers="AX_LABEL",
        max_distance=5,
        tolerance=0.01,
        output=target,
    )
    assert receipt["summary"]["labeled_axis_count"] == 4
    assert receipt["summary"]["intersection_count"] == 4
    result = axes_entities(path, axes=target, point=[0.1, 0.1], bbox=[-1, -1, 1, 1], tolerance=0.2)
    assert result["nearest_intersections"][0]["labels"] == ["A", "1"]
    assert result["nearest"]["vertical"][0]["on_axis"]
    assert len(result["axes_in_region"]["vertical"]) == 1
    with path.open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(CadError) as error:
        axes_entities(path, axes=target, point=[0, 0])
    assert error.value.code == "STALE"


def test_axes_disjoint_line_segments_do_not_create_phantom_intersection(tmp_path, monkeypatch):
    doc = ezdxf.new()
    space = doc.modelspace()
    space.add_line((0, 0), (0, 2), dxfattribs={"layer": "AXIS"})
    space.add_line((0, 8), (0, 10), dxfattribs={"layer": "AXIS"})
    space.add_line((-2, 5), (2, 5), dxfattribs={"layer": "AXIS"})
    path = save(doc, tmp_path, monkeypatch)
    target = tmp_path / "axes.json"
    result = axes_entities(path, line_layers="AXIS", tolerance=0.01, output=target)
    assert result["summary"]["intersection_count"] == 0
    result = axes_entities(path, axes=target, point=[0, 5], tolerance=0.01)
    assert not result["nearest"]["vertical"][0]["on_axis"]
    assert result["nearest_intersections"] == []


def test_axes_missing_conflicting_duplicate_numbers_and_hidden(tmp_path, monkeypatch):
    doc = grid_drawing()
    space = doc.modelspace()
    text(space, "C", (0, -4), "AX_LABEL")
    space.add_line((20, 0), (20, 10), dxfattribs={"layer": "AXIS"})
    space.add_line((30, 0), (30, 10), dxfattribs={"layer": "AXIS", "invisible": 1})
    path = save(doc, tmp_path, monkeypatch)
    result = axes_entities(
        path, line_layers="AXIS", label_layers="AX_LABEL", tolerance=0.01, max_distance=5
    )
    assert result["summary"]["vertical_count"] == 3
    codes = {w["code"] for w in result["warnings"] if isinstance(w, dict)}
    assert {"AXIS_UNLABELED", "AXIS_LABEL_CONFLICT"} <= codes
    assert result["axes"]["vertical"][0]["label"] is None


@pytest.mark.parametrize(
    "malformed",
    [
        [],
        {"vertical": [], "horizontal": [{"id": "Y", "position": "bad", "range": [0, 1]}]},
        {"vertical": [{"id": "X", "position": 0, "range": [1, 0]}], "horizontal": []},
    ],
)
def test_saved_axes_schema_is_validated(tmp_path, monkeypatch, malformed):
    path = save(grid_drawing(), tmp_path, monkeypatch)
    from agentcad.io import file_sha256

    target = tmp_path / "bad.json"
    target.write_text(
        json.dumps({"source": {"sha256": file_sha256(path)}, "layout": "Model", "axes": malformed}),
        encoding="utf-8",
    )
    with pytest.raises(CadError) as error:
        axes_entities(path, axes=target, point=[0, 0])
    assert error.value.code == "INVALID_AXES"


def test_saved_axes_cannot_overwrite_itself(tmp_path, monkeypatch):
    path = save(grid_drawing(), tmp_path, monkeypatch)
    target = tmp_path / "axes.json"
    axes_entities(path, line_layers="AXIS", label_layers="AX_LABEL", output=target)
    with pytest.raises(CadError) as error:
        axes_entities(path, axes=target, point=[0, 0], output=target, overwrite=True)
    assert error.value.code == "OUTPUT_CONFLICT"
