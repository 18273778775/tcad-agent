# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

import json
import math
import time

import ezdxf
import pytest
from ezdxf.math import Matrix44

from agentcad.area import area_selected, area_entities
from agentcad.errors import CadError
from agentcad.geometry import entity_to_polygon, measurement_extension


def rectangle(space, x, y, w, h, layer="0"):
    return space.add_lwpolyline(
        [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True, dxfattribs={"layer": layer}
    )


@pytest.mark.parametrize(
    "points,expected",
    [
        ([(0, 0), (3, 0), (3, 1), (1, 1), (1, 3), (0, 3)], 5),
        ([(0, 2), (1, 2), (1, 0), (2, 0), (2, 2), (3, 2), (3, 3), (0, 3)], 5),
        ([(0, 0), (4, 0), (4, 4), (2, 1), (0, 4)], 10),
    ],
)
def test_concave_l_t_boundaries(points, expected):
    entity = ezdxf.new().modelspace().add_lwpolyline(points, close=True)
    result = area_selected([entity])
    assert result["union_area"] == expected
    assert result["sum_area"] == expected
    assert result["area_complete"]


def test_shared_edge_disjoint_duplicate_containment_overlap():
    space = ezdxf.new().modelspace()
    entities = [
        rectangle(space, 0, 0, 10, 10),
        rectangle(space, 0, 0, 10, 10, "B"),
        rectangle(space, 2, 2, 2, 2),
        rectangle(space, 5, 0, 10, 10),
        rectangle(space, 15, 0, 10, 10),
        rectangle(space, 50, 0, 2, 2),
    ]
    result = area_selected(entities, by="layer")
    assert result["sum_area"] == 408
    assert result["union_area"] == 254
    assert result["overlap_area"] == 154
    assert result["duplicate_group_count"] == 1
    assert result["duplicates"][0]["cross_layer"]
    assert result["contained_count"] == 1
    assert result["overlap_pair_count"] == 1
    assert {r["key"] for r in result["groups"]} == {"0", "B"}


def test_duplicate_with_collinear_subdivision_is_detected():
    space = ezdxf.new().modelspace()
    a = rectangle(space, 0, 0, 2, 2)
    b = space.add_lwpolyline([(0, 0), (1, 0), (2, 0), (2, 2), (0, 2)], close=True)
    result = area_selected([a, b])
    assert result["union_area"] == 4
    assert result["duplicate_group_count"] == 1
    assert result["contained_count"] == 0


def test_hatch_islands_and_styles():
    space = ezdxf.new().modelspace()
    hatch = space.add_hatch()
    for x, y, w in [(0, 0, 10), (2, 2, 6), (4, 4, 2)]:
        hatch.paths.add_polyline_path(
            [(x, y), (x + w, y), (x + w, y + w), (x, y + w)], is_closed=True
        )
    assert area_selected([hatch])["union_area"] == 68
    assert measurement_extension(hatch)["area"] == 68
    assert measurement_extension(hatch)["length"] == 72
    hatch.dxf.hatch_style = 1
    assert area_selected([hatch])["union_area"] == 64
    hatch.dxf.hatch_style = 2
    assert area_selected([hatch])["union_area"] == 100


def test_hatch_arc_edge_hole():
    hatch = ezdxf.new().modelspace().add_hatch()
    hatch.paths.add_edge_path().add_arc((0, 0), 5, 0, 360)
    hatch.paths.add_edge_path().add_arc((0, 0), 2, 0, 360)
    result = area_selected([hatch], flatten_tolerance=0.001)
    assert result["union_area"] == pytest.approx(math.pi * 21, abs=0.03)
    assert result["approximate"]
    assert result["area_error_bound"] is None


def test_invalid_default_rejected_repair_explicit_and_length_retained():
    entity = ezdxf.new().modelspace().add_lwpolyline([(0, 0), (2, 2), (0, 2), (2, 0)], close=True)
    rejected = area_selected([entity])
    assert rejected["union_area"] == 0
    assert not rejected["area_complete"]
    assert "Self-intersection" in rejected["rejected"][0]["reason"]
    extension = measurement_extension(entity)
    assert extension["area"] is None
    assert extension["area_kind"] == "invalid_boundary"
    assert not extension["area_valid"]
    repaired = area_selected([entity], make_valid=True)
    assert repaired["union_area"] == 2
    assert repaired["repaired_count"] == 1
    assert repaired["repaired"][0]["area_delta"] == 2


def test_open_boundary_excluded():
    entity = ezdxf.new().modelspace().add_lwpolyline([(0, 0), (5, 0), (5, 5)])
    result = area_selected([entity])
    assert result["accepted_count"] == 0
    assert not result["area_complete"]
    assert not result["rejected"][0]["closed"]
    assert measurement_extension(entity)["area"] is None


def test_exact_repeated_endpoint_is_closed_without_native_flag():
    space = ezdxf.new().modelspace()
    entity = space.add_lwpolyline([(0, 0), (3, 0), (3, 2), (0, 2), (0, 0)])
    assert not entity.closed
    result = area_selected([entity])
    assert result["union_area"] == 6
    assert result["entities"][0]["closure_evidence"] == "exact_repeated_endpoint"
    assert measurement_extension(entity)["closed"]
    almost = space.add_lwpolyline([(0, 0), (3, 0), (3, 2), (0, 2), (0, 0.00001)])
    assert area_selected([almost], flatten_tolerance=1)["rejected_count"] == 1


def test_visible_selection_overlap_mode_sheet_identity_and_source_protection(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    space = ezdxf.new().modelspace()
    a, b = rectangle(space, 0, 0, 2, 2), rectangle(space, 0, 0, 2, 2)
    result = area_selected([{"entity": a, "visible": False}, b])
    assert result["hidden_excluded_count"] == 1
    assert result["sum_area"] == 4
    assert area_selected([a, b], mode="overlap")["area"] == 4
    assert area_selected([a], by="sheet", sheet_id="sheet-001")["groups"][0]["key"] == "sheet-001"
    with pytest.raises(CadError):
        area_selected([a], by="sheet")
    protected = tmp_path / "source.dwg"
    protected.write_bytes(b"source")
    from agentcad.io import file_sha256

    with pytest.raises(CadError) as error:
        area_selected(
            [a],
            output=protected,
            overwrite=True,
            source_info={"path": str(protected), "sha256": file_sha256(protected)},
        )
    assert error.value.code == "OUTPUT_CONFLICT"


def test_circle_bulge_and_ellipse_flatten_deterministic():
    space = ezdxf.new().modelspace()
    circle = space.add_circle((0, 0), 10)
    ellipse = space.add_ellipse((50, 0), (10, 0), 0.5)
    bulged = space.add_lwpolyline([(100, 0, 1), (120, 0, 1)], format="xyb", close=True)
    result = area_selected([circle, ellipse, bulged], flatten_tolerance=0.001)
    assert result["union_area"] == pytest.approx(math.pi * 250, abs=0.15)
    assert result == area_selected([circle, ellipse, bulged], flatten_tolerance=0.001)
    assert measurement_extension(ellipse)["area"] == pytest.approx(math.pi * 50)
    assert measurement_extension(ellipse)["area_kind"] == "exact_ellipse_in_entity_plane"
    assert result["area_error_bound_status"] == "not_certified"


def test_closed_spline_area_and_open_spline_length():
    space = ezdxf.new().modelspace()
    spline = space.add_open_spline([(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)], degree=1)
    spline.closed = True
    result = measurement_extension(spline, tolerance=0.001)
    assert result["area"] == 4
    assert result["length"] == 8
    assert result["length_kind"] == "flattened_boundary"
    open_spline = space.add_open_spline([(0, 0, 0), (1, 0, 1), (2, 0, 0)], degree=1)
    result = measurement_extension(open_spline)
    assert result["area"] is None
    assert result["length"] == pytest.approx(math.sqrt(2) * 2)
    assert result["length_complete"]


def test_local_origin_large_wcs_and_explicit_unit_only():
    space = ezdxf.new().modelspace()
    a = rectangle(space, 1e12, -1e12, 1000, 2000)
    b = rectangle(space, 1e12 + 500, -1e12, 1000, 2000)
    result = area_selected([a, b], linear_unit="mm")
    assert result["union_area"] == 3_000_000
    assert result["conversion"]["union_area_m2"] == 3
    assert result["local_origin"] == [1e12, -1e12]
    assert "conversion" not in area_selected([a])


def test_ocs_plane_and_transform_without_silent_projection():
    entity = (
        ezdxf.new()
        .modelspace()
        .add_lwpolyline(
            [(0, 0), (3, 0), (3, 2), (0, 2)], close=True, dxfattribs={"extrusion": (0, 1, 0)}
        )
    )
    assert entity_to_polygon(entity).invalid_reason
    assert entity_to_polygon(entity, plane="entity").geometry.area == 6
    native = ezdxf.new().modelspace().add_circle((0, 0), 1)
    transformed = entity_to_polygon(
        native, transform=Matrix44.scale(2, 3, 1), flatten_tolerance=0.0001
    )
    assert transformed.geometry.area == pytest.approx(math.pi * 6, abs=0.003)


def test_precision_grid_is_explicit_and_delta_reported():
    entity = rectangle(ezdxf.new().modelspace(), 0.1, 0.1, 0.6, 0.6)
    result = area_selected([entity], tolerance=0.5)
    assert result["sum_area"] == pytest.approx(0.36)
    assert result["union_area"] == pytest.approx(0.25)
    assert result["precision_area_delta"] == pytest.approx(-0.11)
    assert result["approximate"]


def test_instance_keys_distinct_and_block_groups():
    space = ezdxf.new().modelspace()
    a, b = rectangle(space, 0, 0, 2, 2), rectangle(space, 10, 0, 2, 2)
    selected = [
        {"entity": a, "handle": "ABC", "entity_key": "I1/ABC", "instance_path": ["I1"]},
        {"entity": b, "handle": "ABC", "entity_key": "I2/ABC", "instance_path": ["I2"]},
    ]
    result = area_selected(selected, by="block")
    assert result["union_area"] == 8
    assert len(result["entities"]) == 2
    assert {g["key"] for g in result["groups"]} == {"I1", "I2"}


def test_full_evidence_artifact_bounded_inline(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    space = ezdxf.new().modelspace()
    entities = [rectangle(space, 0, 0, 1, 1) for _ in range(200)]
    target = tmp_path / "areas.json"
    result = area_selected(entities, output=target, evidence_limit=3)
    assert len(result["entities"]) == 3
    assert len(result["duplicates"][0]["entity_keys"]) == 3
    assert result["duplicates"][0]["entity_keys_count"] == 200
    assert result["evidence"]["truncated"]
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert len(saved["entities"]) == 200
    assert len(saved["duplicates"][0]["entity_keys"]) == 200
    assert saved["union_geometry"]["type"] == "Polygon"
    assert result["artifact"]["sha256"]
    with pytest.raises(CadError, match="exists"):
        area_selected(entities, output=target)


def test_relation_cap_keeps_complete_union():
    space = ezdxf.new().modelspace()
    entities = [rectangle(space, 0, 0, i, i) for i in range(1, 20)]
    result = area_selected(entities, max_relations=2)
    assert result["union_area"] == 19 * 19
    assert not result["relations_complete"]
    assert result["area_complete"]


def test_ten_thousand_contours_artifact_performance(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    space = ezdxf.new().modelspace()
    entities = [rectangle(space, (i % 100) * 2, (i // 100) * 2, 1, 1) for i in range(10000)]
    start = time.monotonic()
    result = area_selected(entities, output=tmp_path / "10000.json", evidence_limit=5)
    elapsed = time.monotonic() - start
    assert result["union_area"] == 10000
    assert result["sum_area"] == 10000
    assert result["relation_candidate_count"] == 0
    assert result["artifact"]["bytes"] > 1_000_000
    assert len(result["entities"]) == 5
    # A generous regression guard, not a universal hardware performance promise.
    assert elapsed < 45, f"10000 disjoint contour union took {elapsed:.2f}s"


def test_world_block_adapter(tmp_path):
    document = ezdxf.new()
    block = document.blocks.new("COL")
    rectangle(block, 0, 0, 3, 2)
    document.modelspace().add_blockref("COL", (10, 20), dxfattribs={"xscale": 2, "yscale": 3})
    source = tmp_path / "block.dxf"
    document.saveas(source)
    result = area_entities(source, entity_types=["LWPOLYLINE"])
    assert result["union_area"] == 36
    assert result["entities"][0]["instance_path"]


@pytest.mark.parametrize(
    "options",
    [{"tolerance": -1}, {"flatten_tolerance": 0}, {"linear_unit": "guess"}, {"evidence_limit": -1}],
)
def test_invalid_options(options):
    entity = rectangle(ezdxf.new().modelspace(), 0, 0, 1, 1)
    with pytest.raises(CadError):
        area_selected([entity], **options)
