# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Semantic failures must not silently produce a plausible room."""

from copy import deepcopy
import importlib.util
from pathlib import Path

import ezdxf
import pytest

from agentcad.errors import CadError
from agentcad.room import extract_room

ROOT = Path(__file__).resolve().parents[1]
_loader_spec = importlib.util.spec_from_file_location(
    "room_factory_for_extraction_tests", ROOT / "scripts" / "build_room_fixture.py"
)
factory = importlib.util.module_from_spec(_loader_spec)
_loader_spec.loader.exec_module(factory)


@pytest.fixture
def drawing(tmp_path):
    path = tmp_path / "room.dxf"
    factory.create_room_drawing(path)
    return path


def rewrite(path, callback):
    document = ezdxf.readfile(path)
    callback(document.modelspace())
    document.saveas(path)


def test_extracts_native_coordinates_and_elevation_not_sheet_extent(drawing):
    result = extract_room(drawing, factory.room_mapping())
    room = result["spec"]
    assert room["dimensions"] == {"length": 6000, "width": 4500, "height": 3000}
    assert room["construction"] == {
        "wall_thickness": 200,
        "floor_thickness": 150,
        "ceiling_thickness": 150,
    }
    assert room["evidence"]["footprint"]["measurement"]["area"] == 27000000
    assert room["source"]["sha256"]
    assert room["openings"][1]["sill"] == 900
    assert room["openings"][1]["height"] == 1200


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("missing-height", "ROOM_EVIDENCE_MISSING"),
        ("ambiguous-height", "ROOM_EVIDENCE_AMBIGUOUS"),
        ("wrong-height-text", "ROOM_DIMENSION_MISMATCH"),
        ("bow-tie", "UNSUPPORTED_ROOM_GEOMETRY"),
        ("bulge", "UNSUPPORTED_ROOM_GEOMETRY"),
        ("flipped-ocs", "UNSUPPORTED_ROOM_GEOMETRY"),
        ("wrong-opening-width", "ROOM_DIMENSION_MISMATCH"),
    ],
)
def test_rejects_missing_ambiguous_or_conflicting_evidence(drawing, mutation, code, tmp_path):
    def mutate(space):
        if mutation == "missing-height":
            for entity in list(space):
                if entity.dxf.layer == "ROOM_HEIGHT":
                    space.delete_entity(entity)
        elif mutation == "ambiguous-height":
            space.add_line((9500, 0), (9500, 2800), dxfattribs={"layer": "ROOM_HEIGHT"})
        elif mutation == "wrong-height-text":
            next(e for e in space if e.dxf.layer == "DIM_HEIGHT").dxf.text = "9999 mm"
        elif mutation == "bow-tie":
            next(e for e in space if e.dxf.layer == "ROOM_CLEAR").set_points(
                [(0, 0), (6000, 4500), (6000, 0), (0, 4500)]
            )
        elif mutation == "bulge":
            next(e for e in space if e.dxf.layer == "ROOM_CLEAR").set_points(
                [(0, 0, 1), (6000, 0, 0), (6000, 4500, 0), (0, 4500, 0)], format="xyb"
            )
        elif mutation == "flipped-ocs":
            next(e for e in space if e.dxf.layer == "ROOM_CLEAR").dxf.extrusion = (0, 0, -1)
        elif mutation == "wrong-opening-width":
            next(e for e in space if e.dxf.layer == "DOOR_PLAN").dxf.end = (2000, 0, 0)

    rewrite(drawing, mutate)
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(CadError) as error:
        extract_room(drawing, factory.room_mapping(), output=output)
    assert error.value.code == code
    assert not output.exists()


def test_units_must_be_corroborated_when_header_unitless(drawing):
    document = ezdxf.readfile(drawing)
    document.units = 0
    document.saveas(drawing)
    assert extract_room(drawing, factory.room_mapping())["spec"]["units"] == "mm"
    mapping = deepcopy(factory.room_mapping())
    del mapping["units_label"]
    with pytest.raises(CadError) as error:
        extract_room(drawing, mapping)
    assert error.value.code == "ROOM_UNITS_MISSING"


def test_native_units_conflict_is_not_rescaled_implicitly(drawing):
    document = ezdxf.readfile(drawing)
    document.units = 6  # m contradicts the mm declaration and mapping.
    document.saveas(drawing)
    with pytest.raises(CadError) as error:
        extract_room(drawing, factory.room_mapping())
    assert error.value.code == "ROOM_UNITS_MISMATCH"


def test_query_returns_exact_3d_face_vertices(tmp_path):
    from agentcad.engine import query_entities

    source = tmp_path / "face.dxf"
    drawing = ezdxf.new()
    drawing.modelspace().add_3dface([(0, 0, 0), (6000, 0, 0), (6000, 0, 3000)])
    drawing.saveas(source)
    record = query_entities(source, types=["3DFACE"])["entities"][0]
    assert record["vertices"][:3] == [[0, 0, 0], [6000, 0, 0], [6000, 0, 3000]]
    assert record["coordinate_system"] == "WCS"
