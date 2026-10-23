# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""A split page keeps its frame while previewing effective leaf layers."""

import json
import shutil

import ezdxf
import pytest

from agentcad.errors import CadError
from agentcad.io import file_sha256
from agentcad.protocol import execute_request
from agentcad.render import render_drawing
from agentcad.render_jobs import split_preview_frame
from agentcad.sheets import split_drawing


@pytest.fixture
def split_page(tmp_path):
    doc = ezdxf.new("R2013")
    for name in ("FRAMES", "AXIS", "AXIS_LABEL", "COLUMN", "COLUMN_LABEL", "BEAM"):
        doc.layers.add(name)
    model = doc.modelspace()
    model.add_lwpolyline([(0, 0), (100, 0), (100, 80), (0, 80)], close=True,
                        dxfattribs={"layer": "FRAMES"})
    # Whole native crossing geometry must survive split but be clipped in SVG.
    model.add_line((-10000, 25), (10000, 25), dxfattribs={"layer": "AXIS"})
    model.add_text("A", dxfattribs={"layer": "AXIS_LABEL", "height": 3, "insert": (5, 27)})
    mixed = doc.blocks.new("COLUMN_AND_BEAM")
    mixed.add_lwpolyline([(-3, -3), (3, -3), (3, 3), (-3, 3)], close=True)
    mixed.add_line((-20, 0), (20, 0), dxfattribs={"layer": "BEAM"})
    mixed.add_text("KZ1", dxfattribs={"layer": "COLUMN_LABEL", "height": 3, "insert": (4, 0)})
    model.add_blockref("COLUMN_AND_BEAM", (50, 45), dxfattribs={"layer": "COLUMN"})
    source = tmp_path / "source.dxf"
    doc.saveas(source)
    directory = tmp_path / "pages"
    split_drawing(source, directory, frames=[{"bbox": [0, 0, 100, 80], "name": "Column plan"}],
                  preview=False)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    item = manifest["sheets"][0]
    page = directory / item["dxf"]
    return source, page, manifest_path, manifest


LAYERS = ["AXIS", "AXIS_LABEL", "COLUMN", "COLUMN_LABEL"]


def test_split_page_layers_automatically_use_only_own_frame(split_page, tmp_path):
    source, page, manifest_path, manifest = split_page
    before = {p: file_sha256(p) for p in (source, page)}
    # These are parent drawing handles, deliberately invalid in the split DXF.
    manifest["sheets"][0]["source_handles"] = ["FFFFFFFE"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = render_drawing(page, tmp_path / "selected.svg", layers=LAYERS)
    assert result["bbox"] == [0, 0, 100, 80]
    assert result["sheet"]["title"] == "Column plan"
    assert result["sheet_context"]["scope"] == "split_artifact"
    assert result["sheet_context"]["automatic"] is True
    assert result["sheet_context"]["artifact_sha256_verified"] is True
    expected = render_drawing(source, tmp_path / "original-crop.svg", layers=LAYERS, bbox=[0, 0, 100, 80])
    assert result["render_stats"]["geometry_bounds"] == pytest.approx(expected["render_stats"]["geometry_bounds"])
    assert result["render_stats"]["geometry_bounds"][:3] == pytest.approx([0, 25, 100])
    assert set(result["render_stats"]["drawn_layers"]) == set(LAYERS)
    assert result["render_stats"]["excluded_layers"]["BEAM"] == 1
    assert all(file_sha256(p) == digest for p, digest in before.items())


def test_explicit_manifest_selects_current_page_without_parent_handles(split_page, tmp_path):
    _, page, manifest_path, manifest = split_page
    item = manifest["sheets"][0]
    manifest["sheets"][0]["source_handles"] = ["FFFFFFFF"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = render_drawing(page, tmp_path / "explicit.svg", layers=LAYERS,
                            sheet=item["id"], manifest=manifest_path)
    assert result["sheet"]["id"] == item["id"]
    assert result["sheet_context"]["automatic"] is False
    with pytest.raises(CadError) as error:
        render_drawing(page, tmp_path / "other.svg", layers=LAYERS,
                       sheet="another-sheet", manifest=manifest_path)
    assert error.value.code == "SHEET_NOT_FOUND"


def test_stale_split_manifest_refused_and_explicit_bbox_has_priority(split_page, tmp_path):
    _, page, manifest_path, manifest = split_page
    manifest["sheets"][0]["dxf_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "stale.svg"
    with pytest.raises(CadError) as error:
        render_drawing(page, target, layers=LAYERS)
    assert error.value.code == "STALE_MANIFEST" and not target.exists()
    result = render_drawing(page, tmp_path / "bbox.svg", layers=LAYERS, bbox=[40, 20, 70, 55])
    assert result["bbox"] == [40, 20, 70, 55]
    assert result["sheet_context"] == {"scope": "explicit_bbox", "automatic": False}


def test_standalone_copy_reports_fallback_and_supports_bbox(split_page, tmp_path):
    _, page, _, _ = split_page
    moved = tmp_path / "copied.dxf"
    shutil.copyfile(page, moved)
    result = render_drawing(moved, tmp_path / "copied.svg", layers=LAYERS)
    assert result["bbox"] is None
    assert result["sheet_context"]["verified_split_frame"] is False
    assert any("No verified adjacent split manifest" in value for value in result["warnings"])
    assert result["render_stats"]["geometry_bounds"][0] == pytest.approx(-10000)
    cropped = render_drawing(moved, tmp_path / "copied-crop.svg", layers=LAYERS, bbox=[0, 0, 100, 80])
    assert cropped["render_stats"]["geometry_bounds"][0] == pytest.approx(0)


def test_identical_bytes_at_other_filename_cannot_borrow_sheet_frame(split_page):
    _, page, _, _ = split_page
    other = page.with_name("other.dxf")
    shutil.copyfile(page, other)
    assert split_preview_frame(other) is None


@pytest.mark.parametrize("mutation", ["missing_sha", "invalid_bbox", "duplicate_artifact", "unsafe_path"])
def test_split_context_requires_unambiguous_safe_artifact(split_page, mutation):
    _, page, manifest_path, manifest = split_page
    item = manifest["sheets"][0]
    if mutation == "missing_sha":
        item.pop("dxf_sha256")
    elif mutation == "invalid_bbox":
        item["bbox"] = [0, 0, float("nan"), 80]
    elif mutation == "duplicate_artifact":
        manifest["sheets"].append(dict(item, id="sheet-999"))
    else:
        item["dxf"] = "../" + page.name
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CadError) as error:
        split_preview_frame(page)
    assert error.value.code in {"INVALID_MANIFEST", "STALE_MANIFEST"}


def test_protocol_split_page_png_reports_frame_and_layer_evidence(split_page, tmp_path):
    _, page, _, _ = split_page
    response = execute_request({"command": "render", "args": {
        "input": str(page), "output": str(tmp_path / "column.png"), "layers": LAYERS, "width": 640}})
    assert response["ok"], response
    result = response["result"]
    assert result["renderer"] == "resvg_py" and max(result["width"], result["height"]) == 640
    assert result["sheet_context"]["artifact_sha256_verified"] is True
    assert result["sheet"]["title"] == "Column plan"
    assert set(result["render_stats"]["drawn_layers"]) == set(LAYERS)
