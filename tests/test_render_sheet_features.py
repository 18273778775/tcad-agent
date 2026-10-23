# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Sheet preview correctness: real clipping, portable CJK paths, and diagnostics."""

from xml.etree import ElementTree as ET

import ezdxf
from ezdxf.fonts import fonts
import pytest

from agentcad.errors import CadError
from agentcad import render


def test_bbox_excludes_distant_geometry_and_clips_crossing_strokes(tmp_path):
    doc = ezdxf.new()
    space = doc.modelspace()
    space.add_line((-1000, 5), (1000, 5))
    space.add_line((5, -1000), (5, 1000))
    space.add_circle((10000, 10000), 50)
    result = render.render_document(doc, tmp_path / "clipped.svg", bbox=[0, 0, 10, 10])
    assert result["entity_count"] == 2
    assert result["render_stats"]["excluded_by_bbox"] == 1
    assert result["render_stats"]["geometry_bounds"] == pytest.approx([0, 0, 10, 10])
    # Check real cropped SVG geometry, not merely the region receipt.
    root = ET.parse(tmp_path / "clipped.svg").getroot()
    from tests.test_render_limits import rectangle_path_points
    points = [point for path in root.findall(".//{*}path")
              for point in rectangle_path_points(path.attrib["d"])]
    _, _, width, height = map(float, root.attrib["viewBox"].split())
    assert all(0 <= x <= width and 0 <= y <= height for x, y in points)
    assert width / height == pytest.approx(1)


def test_assigned_sheet_entities_reuse_document_and_clip_nested_block(tmp_path, monkeypatch):
    from ezdxf.addons.drawing import Frontend
    doc = ezdxf.new()
    block = doc.blocks.new("CROSSING")
    block.add_line((-1000, 5), (1000, 5))
    block.add_circle((5000, 5000), 100)
    insert = doc.modelspace().add_blockref("CROSSING", (0, 0))
    doc.modelspace().add_line((10000, 10000), (20000, 20000))

    def forbidden(*args, **kwargs):
        pytest.fail("Assigned entities must not traverse the complete layout")

    monkeypatch.setattr(Frontend, "draw_layout", forbidden)
    monkeypatch.setattr(render, "read_document", forbidden)
    result = render.render_document(doc, tmp_path / "assigned.svg", entities=[insert],
                                    bbox=[0, 0, 10, 10])
    assert result["entity_count"] == 1
    assert result["render_stats"]["geometry_bounds"] == pytest.approx([0, 5, 10, 5])
    # Distant nested leaves are now rejected before rendering, rather than
    # recorded and discarded after resource/font diagnostics have been emitted.
    assert result["render_stats"]["records_before_clip"] >= result["render_stats"]["records_after_clip"]
    assert len(doc.modelspace()) == 2
    assert len(block) == 2


def test_cjk_paths_use_installed_real_glyphs_without_modifying_styles(tmp_path):
    face = render._cjk_face(fonts, None)
    if face is None:
        pytest.skip("No installed CJK font on this test host")
    doc = ezdxf.new()
    style = doc.styles.new("MISSING_BIGFONT", dxfattribs={"font": "absent-cad.shx",
                                                         "bigfont": "absent-cjk.shx"})
    text = doc.modelspace().add_text("结构施工图", dxfattribs={"height": 10,
                                                             "style": style.dxf.name})
    before = style.dxf.all_existing_dxf_attribs().copy()
    target = tmp_path / "cjk.svg"
    result = render.render_document(doc, target)
    codes = {item["code"] for item in result["diagnostics"]}
    assert "FONT_SUBSTITUTED" in codes
    assert "FONT_FILE_UNAVAILABLE" in codes
    assert not codes.intersection({"MISSING_GLYPHS", "TEXT_RENDER_FAILED"})
    assert result["font"]["fallback_file"] == face.filename
    assert style.dxf.all_existing_dxf_attribs() == before
    assert text.dxf.text == "结构施工图"
    # Distinct, complex glyph outlines rule out repeated missing-glyph boxes.
    engine = render._text_renderer(render._Diagnostics(), face)
    paths = engine.get_text_glyph_paths("结构", face)
    assert len(paths) == 2
    assert all(len(path) > 20 for path in paths)
    assert len(paths[0]) != len(paths[1])
    root = ET.parse(target).getroot()
    assert root.findall(".//{*}path")
    assert not root.findall(".//{*}text")
    assert not root.findall(".//{*}image")
    assert "font-face" not in target.read_text(encoding="utf-8")


def test_missing_cjk_font_is_reported_instead_of_claiming_complete_text(tmp_path, monkeypatch):
    doc = ezdxf.new()
    doc.modelspace().add_text("结构", dxfattribs={"height": 10})
    monkeypatch.setattr(render, "_cjk_face", lambda *args: None)
    result = render.render_document(doc, tmp_path / "missing.svg")
    assert any(item["code"] == "MISSING_GLYPHS" for item in result["diagnostics"])


def test_available_original_cjk_outline_font_is_retained(tmp_path):
    choices = [name for name in ("Deng.ttf", "simsun.ttc", "simfang.ttf")
               if fonts.font_manager.has_font(name)]
    if not choices:
        pytest.skip("No second installed CJK font on this host")
    doc = ezdxf.new()
    doc.styles.new("ORIGINAL_CJK", dxfattribs={"font": choices[0]})
    doc.modelspace().add_text("结构施工图", dxfattribs={"style": "ORIGINAL_CJK", "height": 10})
    result = render.render_document(doc, tmp_path / "original-font.svg")
    codes = {item["code"] for item in result["diagnostics"]}
    assert not codes.intersection({"FONT_SUBSTITUTED", "MISSING_GLYPHS", "TEXT_RENDER_FAILED"})


def test_custom_codes_and_encoding_replacements_are_bounded_and_explicit(tmp_path):
    doc = ezdxf.new()
    for index in range(30):
        doc.modelspace().add_text(f"%%13220-\udca2-{index}",
                                 dxfattribs={"insert": (0, index * 3), "height": 2})
    result = render.render_document(doc, tmp_path / "encoding.svg")
    diagnostics = {item["code"]: item for item in result["diagnostics"]}
    assert diagnostics["CUSTOM_CAD_TEXT_CODES"]["count"] == 30
    assert diagnostics["TEXT_ENCODING_REPLACED"]["count"] >= 30
    assert all(len(item["samples"]) <= 4 for item in result["diagnostics"])
    assert all("\udca2" in text.dxf.text for text in doc.modelspace())
    ET.parse(tmp_path / "encoding.svg")


def test_background_monochrome_and_lineweight_options(tmp_path):
    doc = ezdxf.new()
    doc.modelspace().add_line((0, 0), (10, 10), dxfattribs={"color": 1})
    target = tmp_path / "black.svg"
    result = render.render_document(doc, target, background="black", foreground="monochrome",
                                    lineweight_scale=2.0)
    root = ET.parse(target).getroot()
    assert root.find("{*}rect").attrib["fill"] == "#000000"
    styles = " ".join(node.text or "" for node in root.findall(".//{*}style"))
    assert "#ffffff" in styles
    assert result["lineweight_scale"] == 2


def patterned_document(scale):
    doc = ezdxf.new()
    hatch = doc.modelspace().add_hatch()
    hatch.paths.add_polyline_path([(0, 0), (1000, 0), (1000, 1000), (0, 1000)], is_closed=True)
    hatch.set_pattern_fill("ANSI31", scale=scale)
    return doc, hatch


def test_dense_hatch_is_complete_solid_fill_with_explicit_bounded_diagnostics(tmp_path):
    doc, hatch = patterned_document(0.000001)
    pattern_scale = hatch.dxf.pattern_scale
    result = render.render_document(doc, tmp_path / "dense.svg", bbox=[0, 0, 1000, 1000])
    simplified = [item for item in result["diagnostics"] if item["code"] == "HATCH_SIMPLIFIED"]
    assert simplified and simplified[0]["count"] == 1
    assert result["bytes"] < 20000
    assert result["render_stats"]["geometry_bounds"] == pytest.approx([0, 0, 1000, 1000])
    assert result["render_stats"]["records_after_clip"] <= 2
    assert hatch.dxf.pattern_scale == pattern_scale
    assert not hatch.dxf.solid_fill


def test_visible_regular_hatch_pattern_is_preserved_in_adaptive_mode(tmp_path):
    doc, _ = patterned_document(20)
    adaptive = render.render_document(doc, tmp_path / "regular.svg", bbox=[0, 0, 1000, 1000])
    accurate = render.render_document(doc, tmp_path / "accurate.svg", bbox=[0, 0, 1000, 1000],
                                      hatch_mode="accurate")
    assert not any(item["code"] == "HATCH_SIMPLIFIED" for item in adaptive["diagnostics"])
    assert adaptive["render_stats"]["records_after_clip"] == accurate["render_stats"]["records_after_clip"]
    assert abs(adaptive["bytes"] - accurate["bytes"]) < 100


def test_accurate_hatch_rejects_excessive_pattern_without_publishing_partial_svg(tmp_path):
    doc, _ = patterned_document(0.001)
    target = tmp_path / "accurate-existing.svg"
    target.write_text("old", encoding="utf-8")
    with pytest.raises(CadError) as error:
        render.render_document(doc, target, bbox=[0, 0, 1000, 1000],
                               overwrite=True, hatch_mode="accurate")
    assert error.value.code == "RENDER_COMPLEXITY_EXCEEDED"
    assert target.read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize("option", [
    {"bbox": [0, 0, 0, 1]}, {"bbox": [0, 0, 1, float("inf")]},
    {"background": "pink"}, {"foreground": "invalid"},
    {"lineweight_scale": 0}, {"lineweight_scale": float("nan")},
    {"hatch_mode": "unknown"},
])
def test_invalid_render_options_reject_before_input_reads(tmp_path, option):
    with pytest.raises(CadError) as error:
        render.render_drawing(tmp_path / "missing.dxf", tmp_path / "out.svg", **option)
    assert error.value.code == "INVALID_ARGUMENT"
    assert not (tmp_path / "out.svg").exists()


def test_empty_clip_region_does_not_replace_existing_output(tmp_path):
    doc = ezdxf.new()
    line = doc.modelspace().add_line((100, 100), (200, 200))
    target = tmp_path / "existing.svg"
    target.write_text("old", encoding="utf-8")
    with pytest.raises(CadError) as error:
        render.render_document(doc, target, bbox=[0, 0, 10, 10], entities=[line], overwrite=True)
    assert error.value.code == "EMPTY_DRAWING"
    assert target.read_text(encoding="utf-8") == "old"
