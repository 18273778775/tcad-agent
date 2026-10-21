# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Serialized mesh measurements, openings, unit boundaries and publication safety."""

import copy
from pathlib import Path

import ezdxf
import numpy as np
import pytest
import trimesh

from agentcad.errors import CadError
from agentcad.room_model import build_room_model, verify_room_model


def room():
    return {
        "schema_version": "room-1.0", "name": "Nondefault", "units": "mm",
        "drawing_unit_to_m": 0.001, "origin": [12000, -4000, 700],
        "dimensions": {"length": 7345, "width": 4210, "height": 2875},
        "construction": {"wall_thickness": 185, "floor_thickness": 125,
                         "ceiling_thickness": 110},
        "openings": [
            {"id": "D1", "kind": "door", "wall": "south", "offset": 815,
             "width": 935, "height": 2075, "sill": 0},
            {"id": "W1", "kind": "window", "wall": "north", "offset": 2325,
             "width": 1615, "height": 1125, "sill": 925},
            {"id": "W2", "kind": "window", "wall": "west", "offset": 1350,
             "width": 820, "height": 950, "sill": 1050},
        ], "source": {}, "evidence": {}, "checks": [],
    }


@pytest.mark.parametrize("suffix", ["glb", "obj", "dxf"])
def test_serialized_nondefault_dimensions_and_opening_voids(tmp_path, suffix):
    spec = room()
    output = tmp_path / f"model.{suffix}"
    built = build_room_model(spec, output)
    actual = verify_room_model(spec, output)
    assert built["written"] and actual["passed"]
    assert actual["components"]["each_component_watertight"]
    assert not actual["components"]["assembly_boolean_union"]
    for key, expected_mm in spec["dimensions"].items():
        assert actual["dimensions"][key]["actual_mm"] == pytest.approx(expected_mm, abs=0.01)
    openings = {entry["id"]: entry for entry in actual["openings"]}
    for expected in spec["openings"]:
        measured = openings[expected["id"]]
        assert measured["void_clear"]
        for key in ("offset", "width", "height", "sill"):
            assert measured["measurements"][key]["actual_mm"] == pytest.approx(expected[key], abs=0.01)
    if suffix == "glb":
        scene = trimesh.load_scene(output, process=False)
        assert scene.bounds[1, 0] == pytest.approx(7.530, abs=0.00001)
        # Standard glTF is right-handed Y-up, independent of our verifier.
        # Architectural floor Z becomes glTF Y; plan north becomes -Z.
        assert scene.bounds[0, 1] == pytest.approx(-0.125, abs=0.00001)
        assert scene.bounds[1, 1] == pytest.approx(2.985, abs=0.00001)
        assert scene.bounds[0, 2] == pytest.approx(-4.395, abs=0.00001)
        assert scene.bounds[1, 2] == pytest.approx(0.185, abs=0.00001)
        assert scene.geometry["tcad_floor"].volume > 0
    if suffix == "dxf":
        doc = ezdxf.readfile(output)
        assert doc.units == 4
        assert {e.dxftype() for e in doc.modelspace()} == {"3DFACE"}
        assert max(e.dxf.vtx0.x for e in doc.modelspace()) == pytest.approx(7530)


def test_scene_node_transform_is_measured_and_dimension_error_reported(tmp_path):
    spec = room()
    output = tmp_path / "model.glb"
    build_room_model(spec, output)
    scene = trimesh.load_scene(output, process=False)
    for node in list(scene.graph.nodes_geometry):
        if node.startswith("tcad_wall_east_"):
            transform, geometry = scene.graph[node]
            transform = transform.copy()
            transform[0, 3] += 0.025
            scene.graph.update(frame_to=node, matrix=transform, geometry=geometry)
    output.write_bytes(scene.export(file_type="glb"))
    with pytest.raises(CadError) as error:
        verify_room_model(spec, output)
    assert error.value.code == "MODEL_DIMENSION_MISMATCH"
    length = error.value.details["dimensions"]["length"]
    assert length["absolute_error_mm"] == pytest.approx(25, abs=0.01)
    assert not length["passed"]


@pytest.mark.parametrize("axis,key,delta", [(2, "width", -0.04), (1, "height", 0.06)])
def test_all_clear_axes_are_independently_measured(tmp_path, axis, key, delta):
    spec, output = room(), tmp_path / "model.glb"
    build_room_model(spec, output)
    scene = trimesh.load_scene(output, process=False)
    for node in list(scene.graph.nodes_geometry):
        if (key == "width" and node.startswith("tcad_wall_north_")) or (
                key == "height" and node == "tcad_ceiling"):
            transform, geometry = scene.graph[node]
            transform = transform.copy()
            transform[axis, 3] += delta
            scene.graph.update(frame_to=node, matrix=transform, geometry=geometry)
    output.write_bytes(scene.export(file_type="glb"))
    with pytest.raises(CadError) as error:
        verify_room_model(spec, output)
    assert error.value.details["dimensions"][key]["absolute_error_mm"] == pytest.approx(
        abs(delta) * 1000, abs=0.01)


def test_panel_blocking_a_door_void_is_detected_without_dimension_change(tmp_path):
    spec, output = room(), tmp_path / "blocked.glb"
    build_room_model(spec, output)
    scene = trimesh.load_scene(output, process=False)
    blocker = trimesh.creation.box(extents=[0.935, 0.185, 2.075])
    blocker.apply_translation([0.815 + 0.935 / 2, -0.185 / 2, 2.075 / 2])
    blocker.vertices = np.column_stack([blocker.vertices[:, 0], blocker.vertices[:, 2],
                                       -blocker.vertices[:, 1]])
    scene.add_geometry(blocker, node_name="tcad_wall_south_999", geom_name="tcad_wall_south_999")
    output.write_bytes(scene.export(file_type="glb"))
    with pytest.raises(CadError) as error:
        verify_room_model(spec, output)
    assert error.value.code == "MODEL_DIMENSION_MISMATCH"
    assert all(value["passed"] for value in error.value.details["dimensions"].values())
    assert not next(check for check in error.value.details["checks"]
                    if check["name"] == "south_opening_count")["passed"]


def test_window_sill_is_measured_from_void_not_component_metadata(tmp_path):
    spec, output = room(), tmp_path / "window.glb"
    build_room_model(spec, output)
    changed = copy.deepcopy(spec)
    changed["openings"][1]["sill"] += 45
    with pytest.raises(CadError) as error:
        verify_room_model(changed, output)
    entry = next(item for item in error.value.details["openings"] if item["id"] == "W1")
    assert entry["measurements"]["sill"]["actual_mm"] == pytest.approx(925, abs=0.01)
    assert entry["measurements"]["sill"]["absolute_error_mm"] == pytest.approx(45, abs=0.01)


def test_source_metres_and_dxf_serialized_units(tmp_path):
    spec = room()
    spec["units"], spec["drawing_unit_to_m"] = "m", 1.0
    spec["origin"] = [value / 1000 for value in spec["origin"]]
    spec["dimensions"] = {key: value / 1000 for key, value in spec["dimensions"].items()}
    spec["construction"] = {key: value / 1000 for key, value in spec["construction"].items()}
    for opening in spec["openings"]:
        for key in ("offset", "width", "height", "sill"):
            opening[key] /= 1000
    output = tmp_path / "metres.dxf"
    build_room_model(spec, output)
    assert ezdxf.readfile(output).units == 6
    result = verify_room_model(spec, output)
    assert result["dimensions"]["length"]["actual_model_units"] == pytest.approx(7.345)
    assert result["dimensions"]["length"]["actual_mm"] == pytest.approx(7345)
    doc = ezdxf.readfile(output)
    doc.units = 4
    doc.saveas(output)
    with pytest.raises(CadError) as error:
        verify_room_model(spec, output)
    assert error.value.code == "MODEL_DIMENSION_MISMATCH"


def test_does_not_overwrite_and_overwrite_is_explicit(tmp_path):
    output = tmp_path / "model.obj"
    build_room_model(room(), output)
    previous = output.read_bytes()
    with pytest.raises(CadError) as error:
        build_room_model(room(), output)
    assert error.value.code == "OUTPUT_EXISTS"
    assert output.read_bytes() == previous
    spec = room()
    spec["dimensions"]["height"] += 100
    assert build_room_model(spec, output, overwrite=True)["verification"]["passed"]


@pytest.mark.parametrize("mutation", [
    lambda spec: spec["dimensions"].pop("height"),
    lambda spec: spec["dimensions"].update(height=-1),
    lambda spec: spec["dimensions"].update(height=True),
    lambda spec: spec.update(units="unknown"),
    lambda spec: spec.update(drawing_unit_to_m=1),
    lambda spec: spec["openings"][0].update(offset=7300),
    lambda spec: spec["openings"].append({**spec["openings"][0], "id": "D2", "offset": 900}),
    lambda spec: spec["openings"].append({**spec["openings"][0], "id": "D2", "offset": 1750}),
    lambda spec: spec["openings"][0].update(offset=0, width=7345, height=2875),
])
def test_invalid_spec_does_not_publish(tmp_path, mutation):
    spec, output = room(), tmp_path / "invalid.glb"
    mutation(spec)
    with pytest.raises(CadError) as error:
        build_room_model(spec, output)
    assert error.value.code == "INVALID_ROOM_SPEC"
    assert not output.exists()


def test_removed_roles_and_skewed_non_box_mesh_are_rejected(tmp_path):
    spec, output = room(), tmp_path / "invalid.glb"
    build_room_model(spec, output)
    scene = trimesh.load_scene(output, process=False)
    scene.geometry["tcad_floor"].vertices[0, 0] += 0.02
    output.write_bytes(scene.export(file_type="glb"))
    with pytest.raises(CadError) as error:
        verify_room_model(spec, output)
    assert error.value.code == "MODEL_DIMENSION_MISMATCH"
    assert "rectangular solid" in error.value.details["checks"][-1]["reason"]
    metadata = scene.metadata.copy()
    scene = trimesh.Scene(trimesh.creation.box(extents=np.ones(3)))
    scene.metadata = metadata
    output.write_bytes(scene.export(file_type="glb"))
    with pytest.raises(CadError) as error:
        verify_room_model(spec, output)
    assert "component role" in error.value.details["checks"][-1]["reason"]


def test_unmarked_legacy_z_up_glb_requires_explicit_regeneration(tmp_path):
    spec, output = room(), tmp_path / "legacy.glb"
    build_room_model(spec, output)
    scene = trimesh.load_scene(output, process=False)
    scene.metadata.clear()
    output.write_bytes(scene.export(file_type="glb"))
    with pytest.raises(CadError) as error:
        verify_room_model(spec, output)
    assert error.value.code == "UNSUPPORTED_MODEL_CONVENTION"
    assert "room-model" in error.value.details["recovery"]


def test_reflected_scene_node_with_unchanged_bounds_is_rejected(tmp_path):
    spec, output = room(), tmp_path / "reflected.glb"
    build_room_model(spec, output)
    scene = trimesh.load_scene(output, process=False)
    _, geometry = scene.graph["tcad_floor"]
    reflected = np.eye(4)
    reflected[0, 0], reflected[0, 3] = -1, 7.345
    scene.graph.update(frame_to="tcad_floor", matrix=reflected, geometry=geometry)
    output.write_bytes(scene.export(file_type="glb"))
    with pytest.raises(CadError) as error:
        verify_room_model(spec, output)
    assert error.value.code == "MODEL_DIMENSION_MISMATCH"
    assert "outward" in error.value.details["checks"][-1]["reason"]


@pytest.mark.parametrize("suffix", ["glb", "obj", "dxf"])
def test_inward_normals_rejected_even_with_identical_dimensions(tmp_path, suffix):
    spec, output = room(), tmp_path / f"inside-out.{suffix}"
    build_room_model(spec, output)
    if suffix == "glb":
        scene = trimesh.load_scene(output, process=False)
        scene.geometry["tcad_floor"].invert()
        output.write_bytes(scene.export(file_type="glb"))
    elif suffix == "obj":
        lines = output.read_text().splitlines()
        output.write_text("\n".join(" ".join(["f", *line.split()[:0:-1]])
                                    if line.startswith("f ") else line for line in lines) + "\n")
    else:
        doc = ezdxf.readfile(output)
        for face in doc.modelspace():
            if face.dxf.layer == "tcad_floor":
                left, right = face.dxf.vtx1, face.dxf.vtx2
                face.dxf.vtx1, face.dxf.vtx2, face.dxf.vtx3 = right, left, left
        doc.saveas(output)
    with pytest.raises(CadError) as error:
        verify_room_model(spec, output)
    assert error.value.code == "MODEL_DIMENSION_MISMATCH"
    assert "outward" in error.value.details["checks"][-1]["reason"]


def test_failed_serialized_verification_does_not_publish(tmp_path, monkeypatch):
    import agentcad.room_model as module

    output = tmp_path / "model.glb"

    def reject(*args, **kwargs):
        raise CadError("MODEL_DIMENSION_MISMATCH", "Injected serialized geometry mismatch.")

    monkeypatch.setattr(module, "verify_room_model", reject)
    with pytest.raises(CadError):
        build_room_model(room(), output)
    assert not output.exists()
    assert not list(Path(tmp_path).glob(".tcad-*"))
