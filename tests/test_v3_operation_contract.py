# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0
"""Public command integration and original-DWG preservation boundaries."""
import json

import ezdxf
import pytest

from agentcad.io import file_sha256
from agentcad.protocol import execute_request


@pytest.fixture
def operations(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    doc = ezdxf.new("R2013", units=4)
    for name in ("COLU", "COLU_LABEL", "AXIS", "AX_LABEL"):
        doc.layers.add(name)
    space = doc.modelspace()
    column = space.add_lwpolyline([(10, 10), (20, 10), (20, 20), (10, 20)], close=True,
                                  dxfattribs={"layer": "COLU"})
    space.add_lwpolyline([(1000, 1000), (1010, 1000), (1010, 1010), (1000, 1010)], close=True,
                         dxfattribs={"layer": "COLU"})
    space.add_text("KZ1", dxfattribs={"insert": (15, 15), "height": 1, "layer": "COLU_LABEL"})
    space.add_line((0, 0), (0, 100), dxfattribs={"layer": "AXIS"})
    space.add_line((0, 0), (100, 0), dxfattribs={"layer": "AXIS"})
    space.add_text("A", dxfattribs={"insert": (0, -4), "height": 1, "layer": "AX_LABEL"})
    space.add_text("1", dxfattribs={"insert": (-4, 0), "height": 1, "layer": "AX_LABEL"})
    source = tmp_path / "source.dxf"
    doc.saveas(source)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"source": {"sha256": file_sha256(source)}, "sheets": [
        {"id": "sheet-001", "title": "Columns", "bbox": [-10, -10, 110, 110]}]}), encoding="utf-8")
    return source, manifest, column.dxf.handle


def _request(command, args):
    response = execute_request({"command": command, "args": args})
    assert response["ok"], response
    return response["result"]


def test_index_measure_export_area_associate_axes_sheet_contract(operations, tmp_path):
    source, manifest, handle = operations
    before = file_sha256(source)
    index = _request("index", {"input": str(source)})
    assert index["entity_count"] == 7 and index["block_annotation_count"] == 0
    selected = {"input": str(source), "sheet": "sheet-001", "manifest": str(manifest)}
    query = _request("query", {**selected, "layers": ["COLU"], "fields": ["handle"]})
    assert query["entities"] == [{"handle": handle}]
    measure = _request("measure", {**selected, "layers": ["COLU"], "types": ["LWPOLYLINE"]})
    assert measure["totals"]["area"] == 100 and measure["totals"]["area_complete"]
    exported = _request("export", {**selected, "layers": ["COLU"], "fields": ["handle", "bounds"],
                                    "output": str(tmp_path / "selected.jsonl")})
    assert exported["entity_count"] == 1
    assert json.loads((tmp_path / "selected.jsonl").read_text(encoding="utf-8"))["handle"] == handle
    area = _request("area", {**selected, "layers": ["COLU"], "by": "sheet"})
    assert area["union_area"] == 100 and area["area_complete"]
    assert area["groups"][0]["key"] == "sheet-001"
    associate = _request("associate", {**selected, "label_layers": ["COLU_LABEL"], "target_layers": ["COLU"]})
    assert associate["summary"]["pair_count"] == 1
    axes = _request("axes", {**selected, "line_layers": ["AXIS"], "label_layers": ["AX_LABEL"],
                              "max_distance": 5, "tolerance": .01})
    assert axes["summary"]["labeled_axis_count"] == 2
    assert axes["summary"]["intersection_count"] == 1
    assert file_sha256(source) == before


@pytest.mark.parametrize("command", ["area", "associate", "axes"])
def test_semantic_artifacts_cannot_overwrite_original_dwg_after_conversion(operations, tmp_path, command):
    from agentcad.area import area_entities
    from agentcad.semantics import associate_entities, axes_entities
    from agentcad.errors import CadError
    source, _, _ = operations
    original = tmp_path / "private-source.dwg"
    original.write_bytes(b"Original CAD source bytes remain unchanged.")
    before = original.read_bytes()
    identity = {"path": str(original), "sha256": file_sha256(original),
                "normalized_dxf_sha256": file_sha256(source)}
    function = {"area": area_entities, "associate": associate_entities, "axes": axes_entities}[command]
    with pytest.raises(CadError) as error:
        function(source, output=original, overwrite=True, source_info=identity)
    assert error.value.code == "OUTPUT_CONFLICT"
    assert original.read_bytes() == before


@pytest.mark.parametrize("command", ["query", "measure", "export", "area", "associate", "axes"])
def test_sheet_and_bbox_refine_the_same_selection(operations, tmp_path, command):
    source, manifest, _ = operations
    args = {"input": str(source), "sheet": "sheet-001", "manifest": str(manifest), "bbox": [0, 0, 50, 50]}
    if command == "export":
        args["output"] = str(tmp_path / "refined.jsonl")
    response = execute_request({"command": command, "args": args})
    assert response["ok"], response
    if command in {"area", "measure"}:
        area = response["result"].get("union_area", response["result"].get("totals", {}).get("area"))
        assert area == 100


@pytest.mark.parametrize("command", ["query", "measure", "render", "export", "area", "associate", "axes"])
def test_disjoint_bbox_cannot_escape_selected_sheet(operations, tmp_path, command):
    source, manifest, _ = operations
    args = {"input": str(source), "sheet": "sheet-001", "manifest": str(manifest),
            "bbox": [500, 500, 1100, 1100]}
    output = tmp_path / ("refused.svg" if command == "render" else "refused.jsonl")
    if command in {"render", "export"}:
        args["output"] = str(output)
    response = execute_request({"command": command, "args": args})
    assert not response["ok"] and response["error"]["code"] == "EMPTY_SELECTION", response
    assert not output.exists()


def test_explicit_stale_source_revision_is_structured(operations):
    source, _, _ = operations
    response = execute_request({"command": "measure", "args": {
        "input": str(source), "layers": ["COLU"], "expected_source_revision": "stat-v1:obsolete"}})
    assert not response["ok"] and response["error"]["code"] == "STALE_SOURCE"
