# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Visible pixels, resource containment, and honest per-selection quality."""

from io import BytesIO
from shutil import copyfile
from xml.etree import ElementTree as ET

import ezdxf
from ezdxf.fonts import fonts
from PIL import Image
import pytest

from agentcad.errors import CadError
from agentcad.render import render_document, render_drawing


def image_document(tmp_path, image_name="proof.png", insertion=(0, 0), layer="0"):
    document = ezdxf.new()
    document.filename = str(tmp_path / "drawing.dxf")
    image_def = document.add_image_def(image_name, (16, 16))
    document.modelspace().add_image(image_def, insertion, (10, 10), dxfattribs={"layer": layer})
    return document


def pixels(path, width=256):
    resvg = pytest.importorskip("resvg_py")
    return Image.open(BytesIO(resvg.svg_to_bytes(svg_path=str(path), width=width, dpi=96))).convert("RGB")


def assert_red(image):
    assert sum(red > 200 and green < 50 and blue < 50 for red, green, blue in image.getdata()) > image.width * image.height / 3


def test_raster_is_actually_visible_and_inline_without_local_links(tmp_path):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "proof.png")
    target = tmp_path / "preview.svg"
    result = render_document(image_document(tmp_path), target, strict=True)
    images = ET.parse(target).getroot().findall(".//{*}image")
    assert len(images) == 1
    assert images[0].attrib["href"].startswith("data:image/png;base64,")
    assert "file:" not in target.read_text(encoding="utf-8")
    assert "proof.png" not in target.read_text(encoding="utf-8")
    assert result["preview_quality"]["complete"] is True
    assert result["resources"]["embedded_images"] == 1
    assert result["bytes"] == target.stat().st_size
    assert_red(pixels(target))


def test_relocated_absolute_link_uses_only_explicit_resource_root(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    Image.new("RGB", (16, 16), "red").save(resources / "proof.png")
    target = tmp_path / "relocated.svg"
    result = render_document(image_document(tmp_path, "Z:/private/unavailable/proof.png"), target,
                             resource_paths=[str(resources)])
    assert result["resources"]["embedded_images"] == 1
    assert_red(pixels(target))


def test_cli_png_and_split_thumbnails_keep_embedded_raster(tmp_path):
    from agentcad.protocol import execute_request
    Image.new("RGB", (16, 16), "red").save(tmp_path / "proof.png")
    document = image_document(tmp_path)
    source = tmp_path / "drawing.dxf"
    document.saveas(source)
    rendered = execute_request({"command": "render", "args": {"input": str(source),
        "output": str(tmp_path / "preview.png"), "width": 256, "bbox": [0, 0, 10, 10], "strict": True}})
    assert rendered["ok"], rendered
    assert_red(Image.open(tmp_path / "preview.png").convert("RGB"))
    split = execute_request({"command": "split", "args": {"input": str(source),
        "output_dir": str(tmp_path / "pages"), "frames": [{"bbox": [0, 0, 10, 10], "name": "Image"}],
        "render_options": {"strict": True}}})
    assert split["ok"], split
    png = next((tmp_path / "pages").glob("*.png"))
    assert_red(Image.open(png).convert("RGB"))


@pytest.mark.parametrize("name", ["../proof.png", "https://example.org/proof.png",
                                   "//server/share/proof.png", "\\\\server\\proof.png", "proof.svg"])
def test_untrusted_raster_paths_do_not_read_files_or_network(tmp_path, monkeypatch, name):
    import agentcad.render_assets as assets
    calls = []
    monkeypatch.setattr(assets.PreviewResources, "_read", lambda self, path: calls.append(path))
    result = render_document(image_document(tmp_path, name), tmp_path / "denied.svg")
    assert not calls
    assert result["preview_quality"]["status"] == "incomplete"
    assert any(item["code"] in {"RESOURCE_ACCESS_DENIED", "RESOURCE_FORMAT_UNSUPPORTED"}
               for item in result["diagnostics"])
    assert not ET.parse(tmp_path / "denied.svg").getroot().findall(".//{*}image")


def test_outside_absolute_path_is_not_read_without_permission(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    drawing = tmp_path / "drawing"
    drawing.mkdir()
    Image.new("RGB", (16, 16), "red").save(outside / "proof.png")
    result = render_document(image_document(drawing, str(outside / "proof.png")), drawing / "denied.svg")
    assert result["resources"]["bytes_read"] == 0
    assert "RESOURCE_NOT_FOUND" in result["preview_quality"]["content_issues"]


def test_partial_image_crop_keeps_visible_pixels_and_region_not_entire_image(tmp_path):
    image = Image.new("RGB", (16, 16), "blue")
    for x in range(8):
        for y in range(16):
            image.putpixel((x, y), (255, 0, 0))
    image.save(tmp_path / "proof.png")
    target = tmp_path / "half.svg"
    result = render_document(image_document(tmp_path), target, bbox=[0, 0, 5, 10])
    assert result["render_stats"]["geometry_bounds"] == pytest.approx([0, 0, 5, 10])
    actual = pixels(target)
    assert_red(actual)
    assert sum(blue > 200 and red < 50 for red, green, blue in actual.getdata()) < 0.02 * actual.width * actual.height
    assert actual.height / actual.width == pytest.approx(2, abs=0.05)


def test_raster_rotation_orientation_and_later_vector_draw_order(tmp_path):
    image = Image.new("RGB", (16, 16), "blue")
    for x in range(16):
        for y in range(8):
            image.putpixel((x, y), (255, 0, 0))
    image.save(tmp_path / "proof.png")
    document = image_document(tmp_path)
    document.modelspace().add_solid([(0, 0), (5, 0), (0, 10), (5, 10)], dxfattribs={"color": 3})
    target = tmp_path / "order.svg"
    render_document(document, target, bbox=[0, 0, 10, 10])
    actual = pixels(target)
    assert actual.getpixel((int(actual.width * .25), int(actual.height * .25)))[1] > 200
    assert actual.getpixel((int(actual.width * .75), int(actual.height * .25)))[0] > 200
    assert actual.getpixel((int(actual.width * .75), int(actual.height * .75)))[2] > 200


def test_missing_content_strict_does_not_replace_existing_artifact(tmp_path):
    target = tmp_path / "existing.svg"
    target.write_text("previous verified preview", encoding="utf-8")
    with pytest.raises(CadError) as error:
        render_document(image_document(tmp_path, "absent.png"), target, overwrite=True, strict=True)
    assert error.value.code == "PREVIEW_INCOMPLETE"
    assert error.value.details["preview_quality"]["content_complete"] is False
    assert target.read_text(encoding="utf-8") == "previous verified preview"


def test_image_decode_and_cumulative_resource_budgets(tmp_path):
    Image.new("RGB", (64, 64), "red").save(tmp_path / "proof.png")
    result = render_document(image_document(tmp_path), tmp_path / "budget.svg", max_resource_bytes=1024)
    assert "RESOURCE_BUDGET_EXCEEDED" in result["preview_quality"]["content_issues"]
    assert result["resources"]["embedded_images"] == 0
    (tmp_path / "proof.png").write_bytes(b"x" * 1500)
    result = render_document(image_document(tmp_path), tmp_path / "input-budget.svg", max_resource_bytes=1024)
    assert "RESOURCE_BUDGET_EXCEEDED" in result["preview_quality"]["content_issues"]
    assert result["resources"]["bytes_read"] == 0


def test_quality_excludes_hidden_layers_and_distant_nested_resources(tmp_path):
    doc = image_document(tmp_path, "absent.png", layer="IGNORED")
    doc.layers.new("VISIBLE")
    doc.modelspace().add_line((0, 0), (10, 10), dxfattribs={"layer": "VISIBLE"})
    block = doc.blocks.new("OUTSIDE")
    image_def = doc.add_image_def("also-absent.png", (16, 16))
    block.add_image(image_def, (1000, 1000), (10, 10))
    doc.modelspace().add_blockref("OUTSIDE", (0, 0))
    result = render_document(doc, tmp_path / "selection.svg", layers=["VISIBLE"],
                             bbox=[0, 0, 10, 10], strict=True)
    assert result["preview_quality"]["complete"] is True
    assert not any(item["code"].startswith("RESOURCE_") for item in result["diagnostics"])


def available_font():
    face = fonts.font_manager.get_font_face(fonts.font_manager.fallback_font_name())
    return fonts.font_manager._font_cache.get(face.filename, face.filename).file_path


def test_explicit_font_mapping_renders_paths_and_restores_global_registry(tmp_path):
    original = fonts.font_manager
    file = available_font()
    doc = ezdxf.new()
    doc.styles.new("ENGINEERING", dxfattribs={"font": "absent-cad.shx"})
    doc.modelspace().add_text("AB123", dxfattribs={"style": "ENGINEERING", "height": 10})
    target = tmp_path / "mapped.svg"
    result = render_document(doc, target, font_map={"ENGINEERING": str(file)},
                             max_resource_bytes=64 * 1024 * 1024)
    assert fonts.font_manager is original
    assert result["resources"]["explicit_fonts_loaded"] == 1
    assert "FONT_MAPPED" in result["preview_quality"]["fidelity_issues"]
    assert "FONT_FILE_UNAVAILABLE" not in {item["code"] for item in result["diagnostics"]}
    assert ET.parse(target).getroot().findall(".//{*}path")
    assert not ET.parse(target).getroot().findall(".//{*}text")
    with pytest.raises(CadError) as error:
        render_document(doc, tmp_path / "strict-map.svg", font_map={"ENGINEERING": str(file)},
                         max_resource_bytes=64 * 1024 * 1024, strict=True)
    assert error.value.code == "PREVIEW_INCOMPLETE"
    assert fonts.font_manager is original
    assert not (tmp_path / "strict-map.svg").exists()


def test_explicit_font_directory_has_no_recursive_or_global_side_effects(tmp_path):
    original = fonts.font_manager
    directory = tmp_path / "fonts"
    directory.mkdir()
    source = available_font()
    copyfile(source, directory / source.name)
    doc = ezdxf.new()
    doc.modelspace().add_text("AB123", dxfattribs={"height": 10})
    result = render_document(doc, tmp_path / "fonts.svg", font_paths=[str(directory)],
                             max_resource_bytes=64 * 1024 * 1024)
    assert result["resources"]["explicit_fonts_loaded"] == 1
    assert fonts.font_manager is original


@pytest.mark.parametrize("options", [{"strict": "yes"}, {"resource_paths": "folder"},
                                      {"font_map": []}, {"max_resource_bytes": 10}])
def test_resource_options_validate_before_source_read(tmp_path, options):
    with pytest.raises(CadError) as error:
        render_drawing(tmp_path / "missing.dxf", tmp_path / "output.svg", **options)
    assert error.value.code == "INVALID_ARGUMENT"
