import json

import ezdxf
from PIL import Image

from agentcad.protocol import execute_request
from agentcad.render import render_drawing


def drawing(tmp_path):
    doc = ezdxf.new()
    for layer in ("AXIS", "COLUMN", "BEAM", "NOTES"):
        doc.layers.add(layer)
    block = doc.blocks.new("mixed")
    block.add_line((0, 0), (100, 0))  # inherits COLUMN
    block.add_circle((10, 10), 3, dxfattribs={"layer": "BEAM"})
    block.add_text("KZ1", dxfattribs={"layer": "NOTES", "height": 2, "insert": (4, 4)})
    msp = doc.modelspace()
    msp.add_blockref("mixed", (10, 10), dxfattribs={"layer": "COLUMN"})
    msp.add_line((0, 0), (0, 120), dxfattribs={"layer": "AXIS"})
    msp.add_lwpolyline([(40, 40), (50, 40), (50, 60), (40, 60)], close=True,
                       dxfattribs={"layer": "COLUMN"})
    source = tmp_path / "source.dxf"
    doc.saveas(source)
    return source


def test_leaf_layers_include_nested_inheritance_and_exclude_explicit(tmp_path):
    source = drawing(tmp_path)
    result = render_drawing(source, tmp_path / "selected.svg", layers=["COLUMN", "AXIS", "NOTES"],
                            exclude_layers=["NOTES"], annotate="areas", legend=True)
    assert set(result["render_stats"]["drawn_layers"]) == {"COLUMN", "AXIS"}
    assert result["render_stats"]["excluded_layers"] == {"BEAM": 1, "NOTES": 1}
    assert result["annotations"]["candidate_count"] == 1
    assert result["annotations"]["items"][0]["label"] == "200.000 du2"
    assert len(ezdxf.readfile(source).modelspace().query("TEXT")) == 0


def test_png_cli_protocol_is_code_rendered_and_source_intact(tmp_path):
    source = drawing(tmp_path)
    before = source.read_bytes()
    response = execute_request({"command": "render", "args": {"input": str(source),
        "output": str(tmp_path / "preview.png"), "layers": ["COLUMN"], "width": 640}})
    assert response["ok"], response
    result = response["result"]
    assert result["format"] == "png" and result["renderer"] == "resvg_py"
    with Image.open(result["output"]) as image:
        assert max(image.size) == 640
    assert source.read_bytes() == before


def test_sheet_redraw_uses_source_bound_manifest(tmp_path):
    source = drawing(tmp_path)
    from agentcad.io import file_sha256
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"source": {"sha256": file_sha256(source)}, "sheets": [
        {"id": "sheet-001", "title": "Columns", "bbox": [0, 0, 150, 150]}]}), encoding="utf-8")
    result = render_drawing(source, tmp_path / "one.svg", sheet="sheet-001", manifest=manifest)
    assert result["sheet"]["id"] == "sheet-001"
    result = render_drawing(source, sheet="all", manifest=manifest, output_dir=tmp_path / "batch")
    assert result["sheet_count"] == 1 and result["document_reads"] == 1
    assert (tmp_path / "batch/sheet-001.png").is_file()
    source.write_bytes(source.read_bytes() + b"\n")
    response = execute_request({"command": "render", "args": {"input": str(source),
        "output": str(tmp_path / "stale.svg"), "sheet": "sheet-001", "manifest": str(manifest)}})
    assert not response["ok"] and response["error"]["code"] == "STALE_MANIFEST"


def test_new_command_schema_and_cli_flags():
    from agentcad.cli import parser
    for command in ("index", "area", "associate", "axes"):
        assert parser().parse_args([command, "source.dxf"]).command == command
    arguments = parser().parse_args(["split", "source.dxf", "--output-dir", "out", "--title-regex", ".*",
                                    "--layers", "COLUMN", "AXIS", "--no-thumbnail"])
    assert arguments.layers == ["COLUMN", "AXIS"] and not arguments.thumbnail


def test_feature_report_preserves_manifest_and_escapes_untrusted_title(tmp_path):
    from agentcad.sheet_report import write_sheet_report
    from agentcad.errors import CadError
    import pytest
    manifest = {"feature_acceptance": {"column": {"svg": "column#1.svg", "png": "column#1.png"},
        "checks": [{"name": "</script><script>unsafe()</script>", "status": "review", "detail": "evidence"}]}}
    write_sheet_report(manifest, tmp_path / "index.html")
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "</script><script>unsafe()" not in page
    assert "column%231.svg" in page
    assert manifest["feature_acceptance"]["column"]["svg"] == "column#1.svg"
    manifest["feature_acceptance"]["column"]["svg"] = "../private.svg"
    with pytest.raises(CadError, match="relative"):
        write_sheet_report(manifest, tmp_path / "bad.html")
