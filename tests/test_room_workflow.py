# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Independent controlled drawing and real CLI revision acceptance tests."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import ezdxf
import pytest


PROJECT = Path(__file__).resolve().parents[1]
_MODULE_SPEC = importlib.util.spec_from_file_location(
    "controlled_room_factory", PROJECT / "scripts" / "build_room_fixture.py"
)
factory = importlib.util.module_from_spec(_MODULE_SPEC)
_MODULE_SPEC.loader.exec_module(factory)


def cli(*arguments):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "agentcad", *map(str, arguments)], cwd=PROJECT,
        env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60,
        check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt" else 0,
    )
    response = json.loads(completed.stdout)
    assert completed.returncode == 0 and response["ok"], response
    return response["result"]


def test_factory_is_complete_and_has_independent_expected_geometry(tmp_path):
    drawing = tmp_path / "controlled complete room.dxf"
    result = factory.create_room_drawing(drawing)
    doc = ezdxf.readfile(drawing)
    assert doc.dxfversion == "AC1015"
    assert doc.units == ezdxf.units.MM
    assert not doc.audit().errors
    entities = list(doc.modelspace())
    assert result["entity_count"] == len(entities)
    by_layer = {}
    for entity in entities:
        by_layer.setdefault(entity.dxf.layer, []).append(entity)
    for layer in ("ROOM_CLEAR", "ROOM_HEIGHT", "WALL_THICKNESS", "FLOOR_THICKNESS",
                  "CEILING_THICKNESS", "DOOR_PLAN", "WINDOW_PLAN", "DOOR_ELEV", "WINDOW_ELEV"):
        assert len(by_layer[layer]) == 1
    footprint = by_layer["ROOM_CLEAR"][0]
    assert footprint.closed
    assert list(footprint.get_points("xy")) == [(0, 0), (6000, 0), (6000, 4500), (0, 4500)]
    assert tuple(by_layer["ROOM_HEIGHT"][0].dxf.start) == (9500, 0, 0)
    assert tuple(by_layer["ROOM_HEIGHT"][0].dxf.end) == (9500, 3000, 0)
    assert by_layer["DIM_LENGTH"][0].dxf.text == "6000 mm"
    assert by_layer["DIM_WIDTH"][0].dxf.text == "4500 mm"
    assert by_layer["DIM_HEIGHT"][0].dxf.text == "3000 mm"
    assert by_layer["DRAWING_UNITS"][0].dxf.text == "UNITS: mm"
    assert any(e.dxftype() == "ARC" for e in by_layer["DOOR_DETAIL"])
    assert len(by_layer["WINDOW_DETAIL"]) >= 4
    assert len(by_layer["ELEVATION_OUTLINE"]) == 2
    assert len(by_layer["SLAB_OUTLINE"]) == 4
    assert result["oracle"]["clear_area_mm2"] == 27_000_000
    assert result["oracle"]["clear_perimeter_mm"] == 21000
    with pytest.raises(FileExistsError):
        factory.create_room_drawing(drawing)


def test_cli_reads_and_atomically_revises_complete_drawing_from_extracted_dimensions(tmp_path):
    source = tmp_path / "before.dxf"
    factory.create_room_drawing(source)
    original = source.read_bytes()
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps(factory.room_mapping()), encoding="utf-8")
    before = cli("room-extract", source, "--mapping", mapping)["spec"]
    assert before["dimensions"] == {"length": 6000, "width": 4500, "height": 3000}
    entities = cli("query", source, "--limit", "1000")["entities"]
    dimensions = {axis: before["dimensions"][axis] + delta
                  for axis, delta in (("length", 400), ("width", 300), ("height", 200))}
    plan = tmp_path / "transaction.json"
    plan.write_text(json.dumps({"version": 1, "operations": [
        {"op": "delete", "handles": [item["handle"] for item in entities]},
        *({"op": "add", "entity": item} for item in factory.geometry_specs(**dimensions)),
    ]}), encoding="utf-8")
    after_file = tmp_path / "after.dxf"
    dry = cli("apply", "--input", source, "--plan", plan, "--output", after_file, "--dry-run")
    assert dry["written"] is False and not after_file.exists()
    cli("apply", "--input", source, "--plan", plan, "--output", after_file)
    after = cli("room-extract", after_file, "--mapping", mapping)["spec"]
    assert after["dimensions"] == {"length": 6400, "width": 4800, "height": 3200}
    assert after["construction"] == before["construction"]
    assert after["openings"] == before["openings"]
    assert all(item["passed"] for item in after["checks"])
    assert source.read_bytes() == original
    height_evidence = after["evidence"]["height"]
    assert height_evidence["entity"]["type"] == "LINE"
    assert height_evidence["measurement"]["length"] == 3200
    assert after["evidence"]["footprint"]["measurement"]["area"] == 30_720_000


def test_parameterized_fixture_changes_geometric_evidence_and_labels():
    specs = factory.geometry_specs(length=7300, width=5100, height=3400)
    by_layer = {spec["layer"]: spec for spec in specs}
    assert by_layer["ROOM_CLEAR"]["points"] == [[0, 0], [7300, 0], [7300, 5100], [0, 5100]]
    assert by_layer["ROOM_HEIGHT"]["end"] == [9500, 3400]
    assert by_layer["WINDOW_PLAN"]["start"] == [2200, 5100]
    assert by_layer["CEILING_THICKNESS"]["start"] == [9400, 3400]
    assert by_layer["DIM_LENGTH"]["text"] == "7300 mm"
    assert by_layer["DIM_WIDTH"]["text"] == "5100 mm"
    assert by_layer["DIM_HEIGHT"]["text"] == "3400 mm"


@pytest.mark.parametrize("dimensions", [(float("nan"), 4500, 3000), (6000, 0, 3000),
                                        (3000, 4500, 3000), (6000, 4500, 2000)])
def test_fixture_rejects_invalid_or_truncated_dimensions(dimensions):
    with pytest.raises(ValueError):
        factory.geometry_specs(*dimensions)
