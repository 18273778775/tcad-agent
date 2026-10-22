# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0
"""Integration guards for shared native regions and source-bound manifests."""
import json
from unittest.mock import patch

import ezdxf
import pytest

from agentcad.errors import CadError
from agentcad.io import file_sha256
from agentcad.query import query_entities
from agentcad.spatial import index_drawing


def _drawing(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    doc = ezdxf.new("R2013")
    line = doc.modelspace().add_line((0, 0), (10, 10))
    source = tmp_path / "source.dxf"
    doc.saveas(source)
    return source, doc, line


def _manifest(tmp_path, source, handles=None):
    manifest = tmp_path / "manifest.json"
    sheet = {"id": "sheet-001", "title": "Plan", "bbox": [-1, -1, 11, 11]}
    if handles is not None:
        sheet["source_handles"] = handles
    manifest.write_text(json.dumps({"source": {"sha256": file_sha256(source)}, "sheets": [sheet]}), encoding="utf-8")
    return manifest


def test_source_bound_manifest_query_reuses_warm_index_without_parser(tmp_path, monkeypatch):
    from agentcad.protocol import dispatch
    source, _, line = _drawing(tmp_path, monkeypatch)
    index_drawing(source, text_scope="layout")
    manifest = _manifest(tmp_path, source)
    with patch("agentcad.io.read_document", side_effect=AssertionError("manifest query reparsed CAD")):
        result = dispatch("query", {"input": str(source), "sheet": "sheet-001", "manifest": str(manifest),
                                    "fields": ["handle"], "limit": 10})
    assert result["entities"] == [{"handle": line.dxf.handle}]


def test_render_bbox_unknown_extents_policy_matches_query(tmp_path, monkeypatch):
    from agentcad.render import render_document
    source, doc, _ = _drawing(tmp_path, monkeypatch)
    doc.modelspace().add_xline((100, 100), (1, 0))
    doc.saveas(source)
    bbox = [-1, -1, 11, 11]
    page = query_entities(source, bbox=bbox, fields=["handle"])
    result = render_document(doc, tmp_path / "preview.svg", bbox=bbox)
    assert page["total"] == 1
    assert result["entity_count"] == page["total"]


def test_manifest_source_handles_cannot_inject_definition_entities(tmp_path, monkeypatch):
    from agentcad.render_jobs import render_selection
    source, doc, _ = _drawing(tmp_path, monkeypatch)
    definition = doc.blocks.new("DEFINITION")
    child = definition.add_line((1, 0), (9, 8))
    doc.saveas(source)
    manifest = _manifest(tmp_path, source, [child.dxf.handle])
    with pytest.raises(CadError) as error:
        render_selection(doc, source, tmp_path / "preview.svg", sheet="sheet-001", manifest=str(manifest))
    assert error.value.code in {"INVALID_MANIFEST", "ENTITY_NOT_FOUND", "INVALID_ARGUMENT"}
    assert not (tmp_path / "preview.svg").exists()


def test_manifest_sheet_id_cannot_escape_atomic_output_directory(tmp_path, monkeypatch):
    from agentcad.render_jobs import render_selection
    source, doc, _ = _drawing(tmp_path, monkeypatch)
    manifest = _manifest(tmp_path, source)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["sheets"][0]["id"] = "../../escaped"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CadError) as error:
        render_selection(doc, source, None, sheet="all", manifest=str(manifest),
                         output_dir=tmp_path / "gallery")
    assert error.value.code in {"INVALID_MANIFEST", "INVALID_ARGUMENT"}
    assert not (tmp_path / "escaped.svg").exists()
    assert not (tmp_path / "gallery").exists()


def test_manifest_normalized_hash_is_required_even_if_original_dwg_hash_matches(tmp_path, monkeypatch):
    from agentcad.render_jobs import resolve_sheets
    source, doc, _ = _drawing(tmp_path, monkeypatch)
    manifest = _manifest(tmp_path, source)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    original_hash = "f" * 64
    data["source"] = {"sha256": original_hash, "normalized_dxf_sha256": "0" * 64}
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CadError) as error:
        resolve_sheets(doc, source, "sheet-001", str(manifest),
                       source_info={"sha256": original_hash, "normalized_dxf_sha256": file_sha256(source)})
    assert error.value.code == "STALE_MANIFEST"
