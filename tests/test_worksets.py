# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

import concurrent.futures
import json
from pathlib import Path

import ezdxf
import pytest

from agentcad.errors import CadError
from agentcad.io import file_sha256
from agentcad import worksets


def source_drawing(tmp_path):
    document = ezdxf.new("R2013")
    document.units = 4
    document.layers.new("FOUNDATION")
    document.layers.new("BEAM")
    model = document.modelspace()
    roots = []
    for x in (0, 100, 200):
        roots.append(model.add_line((x, 0), (x + 10, 0), dxfattribs={"layer": "FOUNDATION"}))
        roots.append(model.add_circle((x + 5, 5), 2, dxfattribs={"layer": "BEAM"}))
    path = tmp_path / "source.dxf"
    document.saveas(path)
    return path, roots


def record(result):
    return json.loads(Path(result["manifest"]).read_text("utf-8"))


def test_disjoint_boxes_are_union_not_envelope_and_handles_are_mapped(tmp_path):
    path, roots = source_drawing(tmp_path)
    before = file_sha256(path)
    result = worksets.create_workset(path, tmp_path / "scope", bboxes=[[-1, -1, 12, 9], [199, -1, 212, 9]])
    manifest = record(result)
    selected = list(ezdxf.readfile(result["drawing"]).modelspace())
    assert len(selected) == 4
    assert {item["source_handle"] for item in manifest["handle_map"]} == {
        roots[index].dxf.handle for index in (0, 1, 4, 5)}
    assert manifest["units"] == {"name": "mm", "code": 4}
    assert manifest["verification"]["ok"]
    assert all(entity.dxf.start.x != 100 for entity in selected if entity.dxftype() == "LINE")
    assert file_sha256(path) == before
    assert "source_handle" not in json.dumps(result)


def test_native_filters_intersect_the_spatial_union_and_apply_before_bounds(tmp_path, monkeypatch):
    path, roots = source_drawing(tmp_path)
    original = worksets.entity_bounds
    examined = []

    def bounded(entities):
        examined.extend(entity.dxf.handle for entity in entities)
        return original(entities)
    monkeypatch.setattr(worksets, "entity_bounds", bounded)
    result = worksets.create_workset(path, tmp_path / "scope", bboxes=[[-1, -1, 212, 9]],
        handles=[roots[0].dxf.handle, roots[1].dxf.handle, roots[4].dxf.handle],
        layers=["FOUNDATION"], types=["line"])
    assert examined == [roots[0].dxf.handle, roots[4].dxf.handle]
    assert result["summary"]["selected_entity_count"] == 2


def sheet_manifest(tmp_path, path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"layout": "Model", "source": {"sha256": file_sha256(path)}, "sheets": [
        {"id": "sheet-001", "name": "1#基础", "title": "1#基础平面图", "building": "1#", "kind": "plan", "bbox": [-1, -1, 12, 9]},
        {"id": "sheet-002", "name": "2#基础", "title": "2#基础平面图", "building": "2#", "kind": "plan", "bbox": [99, -1, 112, 9]},
        {"id": "sheet-003", "name": "1#详图", "title": "1#基础详图", "building": "1#", "kind": "detail", "bbox": [199, -1, 212, 9]},
    ]}), "utf-8")
    return manifest


def test_sheet_metadata_groups_related_disjoint_sheets_without_semantic_guess(tmp_path):
    path, _ = source_drawing(tmp_path)
    manifest = sheet_manifest(tmp_path, path)
    result = worksets.create_workset(path, tmp_path / "scope", manifest=manifest, building="1#", title_regex="基础")
    assert result["summary"]["selected_entity_count"] == 4
    assert [item["id"] for item in record(result)["selection"]["sheets"]] == ["sheet-001", "sheet-003"]
    one = worksets.create_workset(path, tmp_path / "one", manifest=manifest, building="1#", kind="plan")
    assert one["summary"]["selected_entity_count"] == 2
    all_sheets = worksets.create_workset(path, tmp_path / "all", manifest=manifest)
    assert all_sheets["summary"]["sheet_count"] == 3
    combined = worksets.create_workset(path, tmp_path / "combined", manifest=manifest,
        sheets=["sheet-001", "sheet-001"], bboxes=[[199, -1, 212, 9]])
    assert combined["summary"]["selected_entity_count"] == 4


def test_manifest_mismatch_ambiguous_missing_and_conjunctive_metadata(tmp_path):
    path, _ = source_drawing(tmp_path)
    manifest = sheet_manifest(tmp_path, path)
    with pytest.raises(CadError, match="No sheet") as error:
        worksets.create_workset(path, tmp_path / "none", manifest=manifest, building="1#", title_regex="梁")
    assert error.value.code == "SHEET_NOT_FOUND"
    with pytest.raises(CadError) as error:
        worksets.create_workset(path, tmp_path / "none", manifest=manifest, sheets=["missing"])
    assert error.value.code == "SHEET_NOT_FOUND"
    data = json.loads(manifest.read_text("utf-8"))
    data["sheets"][1]["title"] = data["sheets"][0]["title"]
    manifest.write_text(json.dumps(data), "utf-8")
    with pytest.raises(CadError) as error:
        worksets.create_workset(path, tmp_path / "none", manifest=manifest, sheets=["1#基础平面图"])
    assert error.value.code == "AMBIGUOUS_SHEET"
    data["source"]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(data), "utf-8")
    with pytest.raises(CadError) as error:
        worksets.create_workset(path, tmp_path / "none", manifest=manifest, sheets=["sheet-001"])
    assert error.value.code == "STALE_MANIFEST"


def test_crossing_entities_stay_whole_and_unknown_bounds_are_explicit(tmp_path, monkeypatch):
    path, roots = source_drawing(tmp_path)
    original = worksets.entity_bounds

    def unknown_one(entities):
        bounded, missing, statistics = original(entities)
        return [item for item in bounded if item[0].dxf.handle != roots[1].dxf.handle], [roots[1].dxf.handle], statistics
    monkeypatch.setattr(worksets, "entity_bounds", unknown_one)
    result = worksets.create_workset(path, tmp_path / "scope", bboxes=[[3, -1, 6, 9]])
    value = record(result)
    assert result["summary"]["selected_entity_count"] == 1
    assert value["selection"]["unknown_bounds_handles"] == [roots[1].dxf.handle]
    assert value["selection"]["crossing_handles"] == [roots[0].dxf.handle]
    assert result["summary"]["spatial_selection_complete"] is False
    line = list(ezdxf.readfile(result["drawing"]).modelspace())[0]
    assert line.dxf.start.x == 0 and line.dxf.end.x == 10
    full = worksets.create_workset(path, tmp_path / "full")
    assert full["summary"]["selected_entity_count"] == 6


def test_blocks_resource_graph_attributes_and_units_preserved(tmp_path):
    document = ezdxf.new("R2013")
    document.units = 0
    document.layers.new("ROOT", dxfattribs={"color": 2})
    document.layers.new("CHILD", dxfattribs={"color": 3})
    block = document.blocks.new("CAP", base_point=(10, 20))
    block.add_circle((12, 22), 1, dxfattribs={"layer": "CHILD"})
    insert = document.modelspace().add_blockref("CAP", (100, 200), dxfattribs={"layer": "ROOT"})
    insert.add_attrib("TAG", "CT2", (100, 200))
    path = tmp_path / "source.dxf"
    document.saveas(path)
    result = worksets.create_workset(path, tmp_path / "scope", layers=["ROOT"])
    value = record(result)
    reopened = ezdxf.readfile(result["drawing"])
    assert reopened.units == 0
    assert reopened.layers.get("CHILD").color == 3
    assert len(reopened.blocks.get("CAP")) == 1
    assert list(reopened.modelspace())[0].attribs[0].dxf.text == "CT2"
    assert len(value["handle_map"]) == 3
    assert any("definitions retain" in message for message in result["warnings"])


def test_prepare_once_reuse_never_parses_original_and_subset_analysis_reads_only_subset(tmp_path, monkeypatch):
    path, _ = source_drawing(tmp_path)
    original = ezdxf.readfile
    parsed = []

    def reader(filename):
        parsed.append(Path(filename).resolve())
        return original(filename)
    monkeypatch.setattr(ezdxf, "readfile", reader)
    result = worksets.create_workset(path, tmp_path / "scope", bboxes=[[-1, -1, 12, 9]])
    assert parsed.count(path.resolve()) == 1
    assert len(parsed) == 2  # One source parse plus serialized subset readback.
    for _ in range(4):
        drawing, context = worksets.resolve_workset(result["workset"])
        assert drawing == Path(result["drawing"])
        assert context["source_document_parse_count"] == 0
        worksets.read_document(drawing)  # Each subsequent analysis still parses its small subset.
    assert parsed.count(path.resolve()) == 1
    assert parsed.count(Path(result["drawing"])) == 4
    assert len(parsed) == 6  # Old workflow for prepare + four analyses would parse source five times.


def test_source_and_subset_tampering_are_stale_without_parsing(tmp_path, monkeypatch):
    path, _ = source_drawing(tmp_path)
    result = worksets.create_workset(path, tmp_path / "scope")
    monkeypatch.setattr(worksets, "read_document", lambda _: pytest.fail("Reuse must not parse CAD"))
    drawing, _ = worksets.resolve_workset(tmp_path / "scope", input_path=path)
    data = drawing.read_bytes()
    drawing.write_bytes(data + b"\n")
    with pytest.raises(CadError) as error:
        worksets.resolve_workset(result["workset"])
    assert error.value.code == "STALE_WORKSET"
    drawing.write_bytes(data)
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(CadError) as error:
        worksets.resolve_workset(result["workset"])
    assert error.value.code == "STALE_WORKSET"


def test_alternate_input_must_match_source_bytes_and_missing_source_is_stale(tmp_path):
    path, _ = source_drawing(tmp_path)
    result = worksets.create_workset(path, tmp_path / "scope")
    other = tmp_path / "other.dxf"
    other.write_text("unrelated", "utf-8")
    with pytest.raises(CadError) as error:
        worksets.resolve_workset(result["workset"], input_path=other)
    assert error.value.code == "STALE_WORKSET"
    path.unlink()
    with pytest.raises(CadError) as error:
        worksets.resolve_workset(result["workset"])
    assert error.value.code == "STALE_WORKSET"


@pytest.mark.parametrize("path_key", ["input", "path"])
def test_verified_original_identity_from_normalized_input_is_checked_without_conversion(tmp_path, monkeypatch, path_key):
    path, _ = source_drawing(tmp_path)
    original = tmp_path / "original.dwg"
    original.write_bytes(b"AC1027controlled-original")
    info = {path_key: str(original), "sha256": file_sha256(original), "normalized_dxf_sha256": file_sha256(path)}
    result = worksets.create_workset(path, tmp_path / "scope", source_info=info)
    assert record(result)["source"]["input"] == str(original)
    monkeypatch.setattr(worksets, "readable_dxf", lambda _: pytest.fail("Reuse must not convert DWG"))
    worksets.resolve_workset(result["workset"], input_path=original)
    original.write_bytes(b"AC1027changed-original")
    with pytest.raises(CadError) as error:
        worksets.resolve_workset(result["workset"])
    assert error.value.code == "STALE_WORKSET"


def test_budget_failure_and_verification_failure_publish_nothing(tmp_path, monkeypatch):
    path, _ = source_drawing(tmp_path)
    for arguments, code in [({"max_entities": 1}, "WORKSET_TOO_LARGE"),
                            ({"max_bytes": 1024}, "WORKSET_OUTPUT_TOO_LARGE")]:
        with pytest.raises(CadError) as error:
            worksets.create_workset(path, tmp_path / "scope", **arguments)
        assert error.value.code == code
        assert not (tmp_path / "scope").exists()
        assert not list(tmp_path.glob(".tcad-*-workset-*"))
    from agentcad import sheets
    monkeypatch.setattr(sheets, "_verify_dxf_inventory", lambda *args: {"ok": False, "failures": ["test"]})
    with pytest.raises(CadError) as error:
        worksets.create_workset(path, tmp_path / "scope")
    assert error.value.code == "WORKSET_VERIFICATION_FAILED"
    assert not (tmp_path / "scope").exists()


def test_source_change_during_prepare_is_not_published(tmp_path, monkeypatch):
    path, _ = source_drawing(tmp_path)
    original = worksets.read_document

    def mutate(filename):
        doc = original(filename)
        if Path(filename).resolve() == path.resolve():
            path.write_bytes(path.read_bytes() + b"\n")
        return doc
    monkeypatch.setattr(worksets, "read_document", mutate)
    with pytest.raises(CadError) as error:
        worksets.create_workset(path, tmp_path / "scope")
    assert error.value.code == "REVISION_CONFLICT"
    assert not (tmp_path / "scope").exists()


def test_concurrent_preparation_one_publisher_and_explicit_safe_overwrite(tmp_path, monkeypatch):
    path, _ = source_drawing(tmp_path)
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))

    def prepare():
        try:
            return worksets.create_workset(path, tmp_path / "scope")
        except CadError as exc:
            return exc.code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: prepare(), range(2)))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert "OUTPUT_EXISTS" in results
    result = worksets.create_workset(path, tmp_path / "scope", layers=["FOUNDATION"], overwrite=True)
    assert result["summary"]["selected_entity_count"] == 3
    worksets.resolve_workset(result["workset"])
    (tmp_path / "scope" / "keep.txt").write_text("user data", "utf-8")
    with pytest.raises(CadError) as error:
        worksets.create_workset(path, tmp_path / "scope", overwrite=True)
    assert error.value.code == "UNSAFE_PATH"
    assert (tmp_path / "scope" / "keep.txt").read_text("utf-8") == "user data"


def test_path_traversal_and_output_containing_source_are_rejected(tmp_path):
    path, _ = source_drawing(tmp_path)
    with pytest.raises(CadError) as error:
        worksets.create_workset(path, tmp_path)
    assert error.value.code == "UNSAFE_PATH"
    result = worksets.create_workset(path, tmp_path / "scope")
    value = record(result)
    value["artifact"]["dxf"] = "../source.dxf"
    Path(result["workset"]).write_text(json.dumps(value), "utf-8")
    with pytest.raises(CadError) as error:
        worksets.resolve_workset(result["workset"])
    assert error.value.code == "INVALID_WORKSET"


@pytest.mark.parametrize("arguments", [
    {"max_entities": True}, {"max_entities": 0}, {"max_bytes": 0}, {"overwrite": "yes"},
    {"bboxes": []}, {"bboxes": [[0, 0, float("nan"), 2]]}, {"sheets": []},
    {"types": "LINE"}, {"layers": [""]}, {"title_regex": "["}, {"title_regex": 42},
    {"kind": "foundation"}, {"kind": []}, {"handles": ["NOT-A-HANDLE"]},
])
def test_invalid_requests_are_structured(tmp_path, arguments):
    path, _ = source_drawing(tmp_path)
    with pytest.raises(CadError):
        worksets.create_workset(path, tmp_path / "scope", **arguments)
