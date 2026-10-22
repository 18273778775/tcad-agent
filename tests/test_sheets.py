# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

import hashlib
import json

import ezdxf
import pytest

from agentcad.errors import CadError
from agentcad.sheets import discover_drawing, discover_sheets, sanitize_name, split_drawing


def rectangle(layout, x=0, y=0, width=1000, height=700, **attributes):
    return layout.add_lwpolyline([(x, y), (x + width, y), (x + width, y + height),
                                 (x, y + height)], close=True, dxfattribs=attributes)


def save(tmp_path, document, name="source.dxf"):
    path = tmp_path / name
    document.saveas(path)
    return path


def test_discovery_titles_layer_multiple_labels_dedup_and_safe_names():
    document = ezdxf.new()
    model = document.modelspace()
    for x in (0, 1200):
        rectangle(model, x)
        rectangle(model, x + 10, 10, 980, 680)
        # Model labels can occur near the sheet center rather than its title block.
        model.add_text("基础平面图", dxfattribs={"layer": "图名", "insert": (x + 400, 350), "height": 20})
        model.add_text("桩位平面图", dxfattribs={"layer": "图名", "insert": (x + 400, 150), "height": 18})
        model.add_text("局部做法图", dxfattribs={"layer": "图名", "insert": (x + 800, 50), "height": 5})
    result = discover_sheets(document)
    assert result["summary"]["sheet_count"] == 2
    assert result["summary"]["nested_candidates_removed"] == 2
    assert result["sheets"][0]["title"] == "基础平面图；桩位平面图"
    assert result["sheets"][1]["name"] == result["sheets"][0]["name"] + "_02"
    assert all(s["title_confidence"] == "heuristic" for s in result["sheets"])


def test_four_lines_with_merged_collinear_segments():
    document = ezdxf.new()
    model = document.modelspace()
    for first, second in [((0, 0), (500, 0)), ((500, 0), (1000, 0)),
                          ((0, 700), (1000, 700)), ((0, 0), (0, 700)),
                          ((1000, 0), (1000, 700))]:
        model.add_line(first, second)
    result = discover_sheets(document)
    assert result["summary"]["sheet_count"] == 1
    assert result["sheets"][0]["bbox"] == [0, 0, 1000, 700]
    assert result["sheets"][0]["detection_method"] == "four_sides"
    assert len(result["sheets"][0]["frame_handles"]) == 5


def test_block_basepoint_rotation_nested_layer_and_attribute_title():
    document = ezdxf.new()
    block = document.blocks.new("FRAME", base_point=(100, 200))
    rectangle(block, 100, 200)
    block.add_attdef("DRAWING_TITLE", insert=(800, 230), height=20)
    outer = document.blocks.new("NESTED")
    outer.add_blockref("FRAME", (0, 0))
    insert = document.modelspace().add_blockref("NESTED", (2000, 3000),
                                               dxfattribs={"rotation": 270, "layer": "FRAMES"})
    result = discover_sheets(document, frame_layer="FRAMES")
    assert result["summary"]["sheet_count"] == 1
    assert result["sheets"][0]["bbox"] == pytest.approx([2000, 2000, 2700, 3000])
    assert result["sheets"][0]["frame_handles"] == [insert.dxf.handle]
    direct = document.modelspace().add_blockref("FRAME", (5000, 3000))
    direct.add_attrib("DRAWING_TITLE", "结构总说明", insert=(5700, 3030))
    result = discover_sheets(document, frames=[direct.dxf.handle])
    assert result["sheets"][0]["title"] == "结构总说明"


def test_explicit_bboxes_bypass_nested_and_name_inference():
    result = discover_sheets(ezdxf.new(), frames=[
        {"bbox": [0, 0, 1000, 700], "name": "../CON:总图", "number": "结-01"},
        {"bbox": [10, 10, 200, 100], "name": "内部详图"},
    ])
    assert len(result["sheets"]) == 2
    assert result["sheets"][0]["title_confidence"] == "explicit"
    assert "/" not in result["sheets"][0]["name"]
    assert sanitize_name("NUL.txt") == "_NUL.txt"
    assert sanitize_name("../../") == "sheet"
    assert sanitize_name("<>:\"/\\|?*\x00") == "sheet"


def test_catalog_names_come_from_project_and_directory_evidence():
    document = ezdxf.new()
    model = document.modelspace()
    rectangle(model)
    model.add_text("天城单元安置房项目-4#住宅楼", dxfattribs={"insert": (50, 600), "height": 20})
    model.add_text("目录", dxfattribs={"insert": (400, 550), "height": 30})
    model.add_text("施工图", dxfattribs={"insert": (700, 40), "height": 10})
    result = discover_sheets(document)
    assert result["sheets"][0]["title"] == "4#住宅楼图纸目录"
    assert result["sheets"][0]["number"] is None
    assert len(result["sheets"][0]["title_evidence"]) == 2


def test_split_preserves_source_blocks_dimensions_hatches_and_wcs(tmp_path):
    document = ezdxf.new("R2013")
    document.units = 4
    model = document.modelspace()
    rectangle(model)
    document.layers.new("STRUCTURE", dxfattribs={"color": 2})
    block = document.blocks.new("OFFSET", base_point=(10000, 10000))
    block.add_circle((10100, 10100), 10, dxfattribs={"layer": "STRUCTURE"})
    inserted = model.add_blockref("OFFSET", (100, 100))
    dimension = model.add_linear_dim(base=(0, 400), p1=(10, 200), p2=(500, 200))
    dimension.render()
    hatch = model.add_hatch()
    hatch.paths.add_polyline_path([(20, 20), (80, 20), (80, 80), (20, 80)], is_closed=True)
    model.add_circle((5000, 5000), 10)
    source = save(tmp_path, document)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = split_drawing(source, tmp_path / "sheets", preview=False)
    manifest = json.loads((tmp_path / "sheets" / "manifest.json").read_text(encoding="utf8"))
    sheet = manifest["sheets"][0]
    exported = ezdxf.readfile(tmp_path / "sheets" / sheet["dxf"])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert result["summary"]["sheet_count"] == 1
    assert result["summary"]["unassigned_entity_count"] == 1
    assert inserted.dxf.handle in sheet["source_handles"]
    assert exported.units == 4
    assert len(exported.modelspace().query("INSERT")) == 1
    assert exported.blocks.get("OFFSET") is not None
    assert exported.layers.get("STRUCTURE").color == 2
    assert len(exported.modelspace().query("HATCH")) == 1
    dim = exported.modelspace().query("DIMENSION")[0]
    assert exported.blocks.get(dim.dxf.geometry) is not None
    assert exported.modelspace().query("INSERT")[0].dxf.insert == (100, 100, 0)
    assert (tmp_path / "sheets" / "index.html").is_file()


def test_crossing_entities_are_retained_whole_and_reported(tmp_path):
    document = ezdxf.new()
    rectangle(document.modelspace())
    crossing = document.modelspace().add_line((-100, 300), (1100, 300))
    source = save(tmp_path, document)
    result = split_drawing(source, tmp_path / "split", preview=False)
    manifest = json.loads((tmp_path / "split/manifest.json").read_text(encoding="utf8"))
    assert manifest["sheets"][0]["crossing_entity_count"] == 1
    assert manifest["sheets"][0]["crossing_handles"] == [crossing.dxf.handle]
    out = ezdxf.readfile(tmp_path / "split" / result["examples"][0]["dxf"])
    assert out.modelspace().query("LINE")[0].dxf.start == (-100, 300, 0)


def test_cached_definition_bounds_are_conservative_after_nested_transforms():
    from agentcad.sheets import _entity_bounds
    from ezdxf.math import Vec3
    import math
    document = ezdxf.new()
    child = document.blocks.new("SHAPE", base_point=(500, 200))
    child.add_circle((550, 250), 30)
    nested = document.blocks.new("WRAPPER")
    inner = nested.add_blockref("SHAPE", (100, 100), dxfattribs={"rotation": 35, "xscale": 2})
    first = document.modelspace().add_blockref("WRAPPER", (3000, 4000),
                                             dxfattribs={"rotation": 65, "yscale": 3})
    second = document.modelspace().add_blockref("WRAPPER", (6000, 4000))
    first.add_attrib("LABEL", "far attribute", insert=(10000, 20000))
    measured, missing, stats = _entity_bounds([first, second])
    assert not missing
    assert stats["definition_cache_entries"] == 2
    assert stats["definition_cache_hits"] >= 1
    for entity, conservative in measured:
        composed = inner.matrix44() @ entity.matrix44()
        for index in range(1000):
            angle = index * math.tau / 1000
            point = composed.transform(Vec3(550 + 30 * math.cos(angle), 250 + 30 * math.sin(angle)))
            assert conservative[0] <= point.x <= conservative[2]
            assert conservative[1] <= point.y <= conservative[3]
    assert measured[0][1][3] >= 20000  # independently positioned attached ATTRIB


def test_output_budget_failure_rolls_back_entire_directory(tmp_path):
    document = ezdxf.new()
    rectangle(document.modelspace())
    source = save(tmp_path, document)
    with pytest.raises(CadError) as error:
        split_drawing(source, tmp_path / "failed", preview=False, max_total_bytes=1024)
    assert error.value.code == "SPLIT_OUTPUT_TOO_LARGE"
    assert not (tmp_path / "failed").exists()
    assert not list(tmp_path.glob(".tcad-*-sheet-*"))


def test_existing_directory_is_never_replaced(tmp_path):
    document = ezdxf.new()
    rectangle(document.modelspace())
    source = save(tmp_path, document)
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    with pytest.raises(CadError) as error:
        split_drawing(source, target, preview=False)
    assert error.value.code == "OUTPUT_EXISTS"
    assert (target / "keep.txt").read_text() == "keep"


def test_discovery_artifact_is_full_but_receipt_is_compact(tmp_path):
    document = ezdxf.new()
    for x in range(0, 5000, 1200):
        rectangle(document.modelspace(), x)
    source = save(tmp_path, document)
    receipt = discover_drawing(source, output=tmp_path / "frames.json")
    full = json.loads((tmp_path / "frames.json").read_text(encoding="utf8"))
    assert len(receipt["examples"]) == 3
    assert len(full["sheets"]) == 5
    assert "sheets" not in receipt


@pytest.mark.parametrize("frames", [[{"bbox": [0, 0, 0, 1]}], [{"bbox": [0, 0, float("nan"), 1]}], []])
def test_invalid_manual_frames_are_errors(frames):
    with pytest.raises(CadError) as error:
        discover_sheets(ezdxf.new(), frames=frames)
    assert error.value.code == "INVALID_ARGUMENT"


def image_source():
    document = ezdxf.new("R2013")
    model = document.modelspace()
    rectangle(model)
    definitions = [document.add_image_def("出图章.png", (200, 100), name="FIRST"),
                   document.add_image_def("注册章.png", (200, 100), name="SECOND")]
    for index, definition in enumerate(definitions):
        image = model.add_image(definition, insert=(100 + 300 * index, 100), size_in_units=(200, 100))
        image.set_boundary_path([(0, 0), (180, 0), (180, 90), (0, 90)])
    # Some converted DWGs retain IMAGE->IMAGEDEF links but omit dictionary
    # names. Loader then sees both definitions under an empty name and merges
    # them. The source objects themselves must remain distinct and alive.
    image_dict = document.rootdict.get_required_dict("ACAD_IMAGE_DICT")
    image_dict.discard("FIRST")
    image_dict.discard("SECOND")
    return document


def assert_image_links(document):
    images = list(document.modelspace().query("IMAGE"))
    assert [image.image_def.dxf.filename for image in images] == ["出图章.png", "注册章.png"]
    assert len({image.dxf.image_def_handle for image in images}) == 2
    for image in images:
        reactor = image.image_def_reactor
        assert reactor is not None
        assert reactor.dxf.image_handle == image.dxf.handle
        assert reactor.dxf.owner == image.dxf.handle
        assert image.dxf.image_def_reactor_handle == reactor.dxf.handle
        assert reactor.dxf.handle in image.image_def.get_reactors()
        assert len(image.boundary_path) == 5


def test_export_retains_distinct_equal_size_unnamed_image_definitions(tmp_path):
    source = save(tmp_path, image_source())
    result = split_drawing(source, tmp_path / "images", preview=False)
    output = ezdxf.readfile(tmp_path / "images" / result["examples"][0]["dxf"])
    assert_image_links(output)


def test_export_retains_builtin_resource_values_arrows_and_presentation_headers(tmp_path):
    document = ezdxf.new("R2013")
    rectangle(document.modelspace())
    document.layers.get("Defpoints").dxf.color = 1
    document.styles.get("Standard").dxf.font = "CustomSource.ttf"
    document.styles.get("Standard").dxf.bigfont = "SourceBigfont.shx"
    document.styles.get("Standard").set_xdata("ACAD", [(1000, "SimSun"), (1071, 34306)])
    standard = document.dimstyles.get("Standard")
    standard.dxf.dimtxt = .18
    standard.dxf.dimasz = .18
    standard.dxf.dimtxsty = "Standard"
    arrow = document.blocks.new("SOURCE_ARROW")
    arrow.add_line((0, 0), (1, 1))
    standard.dxf.dimblk = "SOURCE_ARROW"
    document.dimstyles.new("CURRENT_SOURCE", dxfattribs={"dimtxt": 12, "dimtxsty": "Standard"})
    document.header["$DIMSTYLE"] = "CURRENT_SOURCE"
    document.header["$LTSCALE"] = 1000.0
    document.header["$PSLTSCALE"] = 0
    document.header["$TEXTSIZE"] = 200.0
    document.header["$LUPREC"] = 8
    document.header["$CECOLOR"] = 0
    source = save(tmp_path, document)
    result = split_drawing(source, tmp_path / "styles", preview=False)
    output = ezdxf.readfile(tmp_path / "styles" / result["examples"][0]["dxf"])
    assert output.layers.get("Defpoints").dxf.color == 1
    assert output.styles.get("Standard").dxf.font == "CustomSource.ttf"
    assert output.styles.get("Standard").dxf.bigfont == "SourceBigfont.shx"
    assert [(tag.code, tag.value) for tag in output.styles.get("Standard").get_xdata("ACAD")] == [(1000, "SimSun"), (1071, 34306)]
    assert output.dimstyles.get("Standard").dxf.dimtxt == .18
    assert output.dimstyles.get("Standard").dxf.dimasz == .18
    assert output.dimstyles.get("Standard").dxf.dimtxsty == "Standard"
    assert output.dimstyles.get("Standard").dxf.dimblk == "SOURCE_ARROW"
    assert output.blocks.get("SOURCE_ARROW") is not None
    for key in ("$DIMSTYLE", "$LTSCALE", "$PSLTSCALE", "$TEXTSIZE", "$LUPREC", "$CECOLOR"):
        assert output.header[key] == document.header[key]


def test_existing_output_repair_retains_svg_and_saves_all_unassigned_entities(tmp_path):
    from agentcad.sheets import repair_split_artifacts
    from ezdxf import xref
    document = image_source()
    document.layers.get("Defpoints").dxf.color = 1
    document.dimstyles.get("Standard").dxf.dimtxt = .18
    document.header["$LTSCALE"] = 2.5
    chosen = list(document.modelspace())
    document.blocks.new("EMPTY")
    document.modelspace().add_blockref("EMPTY", (10000, 10000))
    document.modelspace().add_text("invisible outside", dxfattribs={"insert": (10000, 10000), "invisible": 1})
    document.modelspace().add_lwpolyline([(10000, 10000), (10100, 10000)])
    source = save(tmp_path, document)
    before = source.read_bytes()
    output_dir = tmp_path / "old"
    output_dir.mkdir()
    old = ezdxf.new(document.dxfversion)
    loader = xref.Loader(document, old)
    selected = {entity.dxf.handle for entity in chosen}
    loader.load_modelspace(filter_fn=lambda entity: entity.dxf.handle in selected)
    loader.execute()
    old.saveas(output_dir / "sheet.dxf")
    svg_content = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 70"><path d="M0 0L100 70"/></svg>'
    (output_dir / "sheet.svg").write_text(svg_content, encoding="utf-8")
    manifest = {"source": {"path": str(source), "sha256": hashlib.sha256(before).hexdigest()},
                "layout": "Model", "statistics": {}, "summary": {},
                "sheets": [{"id": "sheet-001", "name": "sheet", "dxf": "sheet.dxf", "svg": "sheet.svg",
                            "source_handles": [entity.dxf.handle for entity in chosen]}]}
    (output_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    receipt = repair_split_artifacts(source, output_dir, overwrite=True)
    assert receipt["image_count"] == 2
    repaired = ezdxf.readfile(output_dir / "sheet.dxf")
    assert_image_links(repaired)
    assert repaired.layers.get("Defpoints").dxf.color == 1
    assert repaired.dimstyles.get("Standard").dxf.dimtxt == .18
    assert repaired.header["$LTSCALE"] == 2.5
    assert (output_dir / "sheet.svg").read_text(encoding="utf-8") == svg_content
    assert source.read_bytes() == before
    final = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert final["unassigned_artifact"]["entity_count"] == 3
    assert final["summary"]["preserved_native_entity_count"] == 6
    remainder = ezdxf.readfile(output_dir / "__tcad_unassigned__.dxf")
    assert len(remainder.modelspace()) == 3
    assert len(remainder.modelspace().query("INSERT")) == 1
    assert remainder.modelspace().query("TEXT")[0].dxf.invisible == 1
    assert remainder.modelspace().query("INSERT")[0].block().name == "EMPTY"
    # A repeated repair must use the same source definition identity, not create
    # another pair of image definitions or change source/SVG data.
    repeated = repair_split_artifacts(source, output_dir, overwrite=True)
    assert repeated["image_count"] == 2
    assert_image_links(ezdxf.readfile(output_dir / "sheet.dxf"))


def test_repair_rejects_manifest_path_traversal(tmp_path):
    from agentcad.sheets import repair_split_artifacts
    document = ezdxf.new()
    frame = rectangle(document.modelspace())
    source = save(tmp_path, document)
    output = tmp_path / "bad"
    output.mkdir()
    (output / "manifest.json").write_text(json.dumps({"sheets": [
        {"id": "01", "dxf": "../outside.dxf", "source_handles": [frame.dxf.handle]}]}))
    with pytest.raises(CadError) as error:
        repair_split_artifacts(source, output, overwrite=True)
    assert error.value.code == "UNSAFE_PATH"


def test_configured_naming_hints_capture_metadata_and_selective_override():
    document = ezdxf.new()
    block = document.blocks.new("A1_FRAME")
    rectangle(block)
    root = document.modelspace().add_blockref("A1_FRAME", (0, 0))
    model = document.modelspace()
    model.add_text("误认平面图", dxfattribs={"layer": "OTHER", "insert": (600, 80), "height": 40})
    model.add_text("2#楼 3.000~6.000m 柱平法施工图", dxfattribs={"layer": "TITLE", "insert": (200, 90), "height": 20})
    model.add_text("结施-12", dxfattribs={"layer": "NUMBERS", "insert": (300, 300)})
    rectangle(model, 1200)
    model.add_text("基础平面图", dxfattribs={"layer": "TITLE", "insert": (1800, 70), "height": 20})
    options = dict(frame_block_regex="^A1_", frame_size="1000x700", title_layer="TITLE",
                   title_regex=r"(?P<building>2#楼).*?(?P<elevation_range>3\.000~6\.000m).*图|基础平面图",
                   number_layer="NUMBERS")
    found = discover_sheets(document, **options)
    first = found["sheets"][0]
    assert first["title"] == "2#楼 3.000~6.000m 柱平法施工图"
    assert first["number"] == "结施-12"
    assert first["hint_score"] == 2
    assert first["building"] == "2#楼"
    assert first["elevation_range"] == "3.000~6.000m"
    assert first["parent_handle"] == root.dxf.handle
    altered = discover_sheets(document, names={first["id"]: {"name": "柱图验收"}}, **options)
    assert altered["sheets"][0]["name"] == "柱图验收"
    assert altered["sheets"][0]["title"] == first["title"]
    assert altered["sheets"][1] == found["sheets"][1]
    with pytest.raises(CadError) as caught:
        discover_sheets(document, names={"does-not-exist": "unknown"}, **options)
    assert caught.value.code == "NAME_OVERRIDE_NOT_FOUND"
    captured = discover_sheets(document, title_layer="TITLE",
        title_regex=r"(?P<building>2#楼).*?(?P<elevation_range>3\.000~6\.000m) (?P<title>柱平法施工图)")
    assert captured["sheets"][0]["title"] == "柱平法施工图"
    assert captured["sheets"][0]["building"] == "2#楼"
    assert captured["sheets"][0]["metadata_evidence"]["building"]["text"].startswith("2#楼")


def test_shared_frame_split_requires_actual_disjoint_titled_page_borders():
    document = ezdxf.new()
    model = document.modelspace()
    rectangle(model, width=2000, height=700)
    rectangle(model, width=1000, height=700)
    rectangle(model, x=1000, width=1000, height=700)
    for x, text in [(500, "基础平面图"), (1500, "柱施工图")]:
        model.add_text(text, dxfattribs={"layer": "图名", "insert": (x, 80), "height": 20})
    assert len(discover_sheets(document)["sheets"]) == 1
    split = discover_sheets(document, shared_frame="split")
    assert len(split["sheets"]) == 2
    assert all(s["shared_frame_parent"] for s in split["sheets"])
    ambiguous = ezdxf.new()
    model = ambiguous.modelspace()
    rectangle(model, width=2000, height=700)
    rectangle(model, width=1200, height=700)
    rectangle(model, x=800, width=1200, height=700)
    for x in (400, 1600):
        model.add_text("柱施工图", dxfattribs={"layer": "图名", "insert": (x, 80), "height": 20})
    result = discover_sheets(ambiguous, shared_frame="split")
    assert len(result["sheets"]) == 1
    assert any("SHARED_FRAME_AMBIGUOUS" in w for w in result["warnings"])


def test_real_verification_reopens_all_and_detects_geometry_header_and_svg_damage(tmp_path):
    from agentcad.sheets import verify_split_artifacts
    document = ezdxf.new()
    rectangle(document.modelspace())
    line = document.modelspace().add_line((20, 20), (90, 90))
    document.header["$LTSCALE"] = 1000.0
    source = save(tmp_path, document)
    output = tmp_path / "split"
    split_drawing(source, output, preview=False)
    manifest = json.loads((output / "manifest.json").read_text("utf-8"))
    v = manifest["verification"]
    assert v["ok"] and v["reopened_dxf_count"] == 1
    assert v["source_unchanged"] and v["original_source_unchanged"]
    path = output / manifest["sheets"][0]["dxf"]
    exported = ezdxf.readfile(path)
    exported.modelspace().query("LINE")[0].dxf.end = (91, 91)
    exported.header["$LTSCALE"] = 1
    exported.saveas(path)
    bad = verify_split_artifacts(source, output, manifest=manifest)
    assert not bad["ok"]
    checks = {f["check"] for f in bad["sheets"][0]["dxf"]["failures"]}
    assert {"native_geometry", "presentation_header", "artifact_sha256"} <= checks
    assert line.dxf.handle in manifest["sheets"][0]["source_handles"]
    from agentcad.sheets import _verify_svg
    svg = tmp_path / "preview.svg"
    svg.write_text('<svg viewBox="0 0 100 70"><path d="M0 0L1 1"/></svg>', encoding="utf-8")
    assert _verify_svg(svg)["ok"]
    svg.write_text('<svg viewBox="0 0 100 70"><image href="file:///private.png"/></svg>', encoding="utf-8")
    assert not _verify_svg(svg)["ok"]
    svg.write_text('<svg viewBox="0 0 100 70"/>', encoding="utf-8")
    assert not _verify_svg(svg)["ok"]


def test_split_default_thumbnail_is_real_verified_png_and_svg_upgrade_preserves_cad(tmp_path):
    from agentcad.sheets import create_split_thumbnails, _verify_thumbnail
    document = ezdxf.new()
    document.layers.add("图名")
    model = document.modelspace()
    rectangle(model)
    model.add_text("柱施工图", dxfattribs={"layer": "图名", "insert": (400, 60), "height": 25})
    source = save(tmp_path, document)
    output = tmp_path / "with_png"
    split_drawing(source, output, thumbnail_width=320)
    manifest = json.loads((output / "manifest.json").read_text("utf-8"))
    sheet = manifest["sheets"][0]
    png = sheet["thumbnail"]
    assert png["output"].endswith(".png") and png["width"] == 320
    assert _verify_thumbnail(output / png["output"], png)["ok"]
    assert manifest["verification"]["verified_thumbnail_count"] == 1
    assert manifest["summary"]["thumbnail_count"] == 1
    before = {key: (output / sheet[key]).read_bytes() for key in ("dxf", "svg")}
    upgraded = create_split_thumbnails(output, max_side=640, overwrite=True)
    assert upgraded["thumbnail_count"] == 1
    modified = json.loads((output / "manifest.json").read_text("utf-8"))
    assert modified["sheets"][0]["thumbnail"]["width"] == 640
    assert all((output / sheet[key]).read_bytes() == value for key, value in before.items())
    with pytest.raises(CadError) as caught:
        create_split_thumbnails(output, max_side=640)
    assert caught.value.code == "OUTPUT_EXISTS"


def test_thumbnail_failure_is_structured_and_split_publication_rolls_back(tmp_path, monkeypatch):
    import agentcad.thumbnails
    document = ezdxf.new()
    rectangle(document.modelspace())
    source = save(tmp_path, document)
    def fail(*args, **kwargs):
        raise CadError("THUMBNAIL_RENDER_FAILED", "Deliberate PNG failure.")
    monkeypatch.setattr(agentcad.thumbnails, "create_thumbnail", fail)
    output = tmp_path / "failure"
    with pytest.raises(CadError) as caught:
        split_drawing(source, output)
    assert caught.value.code == "THUMBNAIL_RENDER_FAILED"
    assert caught.value.details["sheet_id"] == "sheet-001"
    assert caught.value.details["stage"] == "png_thumbnail"
    assert not output.exists()
