# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Source identity and independent CAD extraction are separate model guarantees."""

from copy import deepcopy
import importlib.util
from pathlib import Path

import ezdxf
import pytest

from agentcad.errors import CadError
from agentcad.room import extract_room
from agentcad.room_model import (
    build_room_model,
    verify_room_model,
    verify_room_model_against_source,
)


ROOT = Path(__file__).resolve().parents[1]
_factory_spec = importlib.util.spec_from_file_location(
    "room_factory_integrity", ROOT / "scripts" / "build_room_fixture.py"
)
factory = importlib.util.module_from_spec(_factory_spec)
_factory_spec.loader.exec_module(factory)


@pytest.fixture
def evidence(tmp_path):
    source = tmp_path / "current.dxf"
    factory.create_room_drawing(source)
    mapping = factory.room_mapping()
    spec = extract_room(source, mapping)["spec"]
    model = tmp_path / "current.glb"
    build_room_model(spec, model)
    return source, mapping, spec, model


def test_model_only_result_does_not_claim_current_source_verified(evidence):
    source, mapping, spec, model = evidence
    ordinary = verify_room_model(spec, model)
    assert ordinary["model_verified"] and not ordinary["source_verified"]
    assert ordinary["verification_scope"] == "model_against_spec"
    strict = verify_room_model_against_source(spec, model, source, mapping)
    assert strict["model_verified"] and strict["source_verified"] and strict["passed"]
    assert strict["source"]["sha256"] == spec["source"]["sha256"]
    assert all(item["passed"] for item in strict["source_checks"])


def test_modified_current_source_rejected_even_when_old_model_still_matches(evidence):
    source, mapping, spec, model = evidence
    document = ezdxf.readfile(source)
    document.modelspace().add_line((100000, 100000), (100001, 100001))
    document.saveas(source)
    assert verify_room_model(spec, model)["model_verified"]
    with pytest.raises(CadError) as error:
        verify_room_model_against_source(spec, model, source, mapping)
    assert error.value.code == "ROOM_SOURCE_MISMATCH"
    assert not error.value.details["source_verified"]


@pytest.mark.parametrize("part", ["dimensions", "construction", "openings", "origin"])
def test_hash_alone_cannot_validate_forged_spec(evidence, tmp_path, part):
    source, mapping, spec, _ = evidence
    forged = deepcopy(spec)
    if part == "origin":
        forged[part][0] += 10
    elif part == "openings":
        forged[part][0]["width"] += 10
    else:
        key = "length" if part == "dimensions" else "wall_thickness"
        forged[part][key] += 10
    model = tmp_path / f"forged-{part}.obj"
    build_room_model(forged, model)
    assert verify_room_model(forged, model)["model_verified"]
    with pytest.raises(CadError) as error:
        verify_room_model_against_source(forged, model, source, mapping)
    assert error.value.code == "ROOM_SOURCE_MISMATCH"
    assert any(not check["passed"] for check in error.value.details["source_checks"])


def test_strict_verification_requires_original_hash_and_original_mapping(evidence):
    source, mapping, spec, model = evidence
    no_hash = deepcopy(spec)
    del no_hash["source"]["sha256"]
    with pytest.raises(CadError, match="hash differs"):
        verify_room_model_against_source(no_hash, model, source, mapping)
    other_mapping = deepcopy(mapping)
    other_mapping["name"] = "Ambiguous alternative association"
    with pytest.raises(CadError, match="mapping differs"):
        verify_room_model_against_source(spec, model, source, other_mapping)


def test_identical_source_can_move_without_breaking_identity(evidence, tmp_path):
    source, mapping, spec, model = evidence
    moved = tmp_path / "moved.dxf"
    moved.write_bytes(source.read_bytes())
    assert verify_room_model_against_source(spec, model, moved, mapping)["source_verified"]


def test_strict_failure_reports_which_layer_failed(evidence, tmp_path):
    source, mapping, spec, _ = evidence
    wrong_spec = deepcopy(spec)
    wrong_spec["dimensions"]["height"] += 100
    wrong_model = tmp_path / "different.obj"
    build_room_model(wrong_spec, wrong_model)
    with pytest.raises(CadError) as error:
        verify_room_model_against_source(spec, wrong_model, source, mapping)
    assert error.value.code == "MODEL_DIMENSION_MISMATCH"
    assert error.value.details["source_verified"]
    assert not error.value.details["model_verified"]


def test_source_mutation_during_extraction_rejected(evidence, monkeypatch):
    import agentcad.room_model as module

    source, mapping, spec, model = evidence
    real = module.extract_room

    def mutate(*args, **kwargs):
        extracted = real(*args, **kwargs)
        with source.open("ab") as stream:
            stream.write(b"\n")
        return extracted

    monkeypatch.setattr(module, "extract_room", mutate)
    with pytest.raises(CadError, match="changed during strict"):
        verify_room_model_against_source(spec, model, source, mapping)


def test_source_verification_reconverts_dwg_before_extracting(evidence, monkeypatch, tmp_path):
    import agentcad.converters as converters
    from contextlib import contextmanager

    source, mapping, spec, model = evidence
    dwg = tmp_path / "controlled.dwg"
    dwg.write_bytes(b"A simulated converter input; not claimed as a real DWG acceptance fixture")
    from agentcad.room import source_identity

    spec["source"] = source_identity(dwg)
    seen = []

    @contextmanager
    def converted(path):
        seen.append(path)
        yield source

    monkeypatch.setattr(converters, "readable_dxf", converted)
    assert verify_room_model_against_source(spec, model, dwg, mapping)["source_verified"]
    assert seen == [dwg]


def test_local_mapped_edit_preserves_unrelated_blocks_attributes_and_xdata(tmp_path):
    """Translate explicit mapped evidence; do not claim dependent drawing resize."""
    from agentcad.engine import apply_plan
    from ezdxf.lldxf.tagwriter import TagCollector

    source, revised = tmp_path / "mapped.dxf", tmp_path / "mapped-moved.dxf"
    document = ezdxf.new("R2013", setup=True, units=ezdxf.units.MM)
    space = document.modelspace()
    mapping = factory.room_mapping()
    del mapping["dimension_labels"], mapping["units_label"]
    mapped = []
    # Independent minimal mapped room: no undeclared dependent plan graphics.
    for layer, start, end in [
        ("ROOM_HEIGHT", (9500, 0), (9500, 3000)),
        ("WALL_THICKNESS", (-200, 2000), (0, 2000)),
        ("FLOOR_THICKNESS", (9400, -150), (9400, 0)),
        ("CEILING_THICKNESS", (9400, 3000), (9400, 3150)),
        ("DOOR_PLAN", (1000, 0), (1900, 0)),
        ("WINDOW_PLAN", (2200, 4500), (3800, 4500)),
    ]:
        document.layers.new(layer)
        mapped.append(space.add_line(start, end, dxfattribs={"layer": layer}).dxf.handle)
    for layer, points in [
        ("ROOM_CLEAR", [(0, 0), (6000, 0), (6000, 4500), (0, 4500)]),
        ("DOOR_ELEV", [(11000, 0), (11900, 0), (11900, 2100), (11000, 2100)]),
        ("WINDOW_ELEV", [(22200, 900), (23800, 900), (23800, 2100), (22200, 2100)]),
    ]:
        document.layers.new(layer)
        mapped.append(space.add_lwpolyline(points, close=True,
                                          dxfattribs={"layer": layer}).dxf.handle)
    document.appids.new("PRESERVE_ME")
    block = document.blocks.new("UNRELATED_ASSET")
    block.add_line((0, 0), (123, 456))
    block.add_attdef("TAG", (10, 10), text="Original attribute")
    reference = space.add_blockref("UNRELATED_ASSET", (40000, 50000))
    reference.add_auto_attribs({"TAG": "Keep this exact value"})
    reference.set_xdata("PRESERVE_ME", [(1000, "non-room application payload"), (1070, 77)])
    space.add_spline(fit_points=[(40000, 0), (41000, 2000), (42000, 0)])
    space.add_linear_dim(base=(40000, 5000), p1=(40000, 0), p2=(42000, 0)).render()
    document.saveas(source)

    def untouched_tags(doc):
        result = {}
        for block in doc.blocks:
            for entity in block:
                if entity.dxf.handle in mapped:
                    continue
                for record in [entity, *getattr(entity, "attribs", [])]:
                    result[record.dxf.handle] = list(TagCollector.dxftags(record))
        return result

    before_document = ezdxf.readfile(source)
    original = source.read_bytes()
    before = extract_room(source, mapping)["spec"]
    plan = {"version": 1, "operations": [
        {"op": "transform", "handles": mapped, "translate": [1250, 750, 0]},
    ]}
    apply_plan(plan, revised, input_path=source)
    after_document = ezdxf.readfile(revised)
    assert untouched_tags(after_document) == untouched_tags(before_document)
    assert source.read_bytes() == original
    after = extract_room(revised, mapping)["spec"]
    assert after["origin"] == [1250, 750, 0]
    for key in ("dimensions", "construction", "openings"):
        assert after[key] == before[key]
    assert set(mapped) <= {entity.dxf.handle for entity in after_document.modelspace()}
    assert len(after_document.modelspace()) == len(before_document.modelspace())
