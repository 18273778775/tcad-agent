# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

import ezdxf
import pytest

from agentcad.cli import parser
from agentcad.errors import CadError
from agentcad.protocol import execute_request
from agentcad.resources import _cleanup_atomic_temporaries
from agentcad.sheet_report import write_sheet_report


def test_discovery_and_split_protocol_artifacts(tmp_path):
    source = tmp_path / "source.dxf"
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (1000, 0), (1000, 700), (0, 700)], close=True)
    msp.add_text("Foundation plan", dxfattribs={"insert": (600, 40), "height": 20})
    msp.add_circle((200, 300), 30)
    doc.saveas(source)
    before = source.read_bytes()
    discovered = execute_request({"command": "sheets", "args": {
        "input": str(source), "output": str(tmp_path / "frames.json")}})
    assert discovered["ok"], discovered
    result = execute_request({"command": "split", "args": {
        "input": str(source), "output_dir": str(tmp_path / "sheets"),
        "frames": [{"bbox": [0, 0, 1000, 700], "name": "Foundation plan", "number": "S-01"}],
        "preview": True}})
    assert result["ok"], result
    manifest = json.loads((tmp_path / "sheets" / "manifest.json").read_text("utf-8"))
    assert len(manifest["sheets"]) == 1
    sheet = manifest["sheets"][0]
    assert (tmp_path / "sheets" / sheet["dxf"]).is_file()
    assert (tmp_path / "sheets" / sheet["svg"]).is_file()
    assert (tmp_path / "sheets" / "index.html").is_file()
    assert source.read_bytes() == before
    refused = execute_request({"command": "split", "args": {
        "input": str(source), "output_dir": str(tmp_path / "sheets"), "preview": False}})
    assert not refused["ok"] and refused["error"]["code"] == "OUTPUT_EXISTS"


def test_cli_frame_json_and_preview_switch():
    args = parser().parse_args(["split", "source.dwg", "--output-dir", "parts", "--frames", "frames.json", "--no-preview"])
    assert args.frames == "frames.json" and args.preview is False
    invalid = execute_request({"command": "split", "args": {
        "input": "source.dwg", "output_dir": "parts", "frames": [{"bbox": [0, 0, 5]}]}})
    assert not invalid["ok"] and invalid["error"]["code"] == "INVALID_REQUEST"


def test_discovery_json_never_overwrites_source_drawing(tmp_path):
    source = tmp_path / "source.dxf"
    doc = ezdxf.new()
    doc.modelspace().add_line((0, 0), (1, 1))
    doc.saveas(source)
    original = source.read_bytes()
    result = execute_request({"command": "sheets", "args": {
        "input": str(source), "output": str(source), "overwrite": True}})
    assert not result["ok"] and result["error"]["code"] == "OUTPUT_CONFLICT"
    assert source.read_bytes() == original
    from agentcad.sheets import discover_drawing
    original_dwg = tmp_path / "original.dwg"
    original_dwg.write_bytes(b"Original source, not a converter test")
    with pytest.raises(CadError) as error:
        discover_drawing(source, output=original_dwg, overwrite=True,
                         source_info={"path": str(original_dwg)})
    assert error.value.code == "OUTPUT_CONFLICT"
    assert original_dwg.read_bytes() == b"Original source, not a converter test"


def test_gallery_treats_drawing_titles_as_data_and_rejects_external_assets(tmp_path):
    title = '</script><script>alert("CAD")</script>'
    manifest = {"source": {"path": title}, "sheets": [{"id": "01", "name": title,
                "dxf": "sheets/图纸 #1.dxf", "svg": "sheets/图纸 #1.svg"}]}
    output = tmp_path / "index.html"
    write_sheet_report(manifest, output)
    page = output.read_text("utf-8")
    assert title not in page
    assert "\\u003c/script\\u003e" in page
    assert "%23" in page and "%E5%9B%BE" in page
    manifest["sheets"][0]["svg"] = "../source.svg"
    with pytest.raises(CadError):
        write_sheet_report(manifest, output)
    manifest["sheets"][0]["svg"] = "https://example.com/source.svg"
    with pytest.raises(CadError):
        write_sheet_report(manifest, output)


def test_split_cancellation_cleanup_only_removes_current_worker_staging(tmp_path):
    operation = "a" * 32
    owned = tmp_path / f".tcad-{operation}-sheet-123"
    owned.mkdir()
    (owned / "partial.svg").write_text("partial", encoding="utf-8")
    other = tmp_path / f".tcad-{'b' * 32}-sheet-456"
    other.mkdir()
    committed = tmp_path / "parts"
    committed.mkdir()
    (committed / "manifest.json").write_text("{}", encoding="utf-8")
    _cleanup_atomic_temporaries({"args": {"output_dir": str(committed)}}, operation)
    assert not owned.exists()
    assert other.is_dir() and committed.is_dir()
    assert Path(committed, "manifest.json").read_text("utf-8") == "{}"
