# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0
"""One-document sheet redraw and code-only PNG export."""

import json
from math import isfinite
import os
import re
from pathlib import Path
import tempfile

from .errors import CadError
from .io import file_sha256


def _read_manifest(path):
    target = Path(path).expanduser().resolve()
    try:
        if target.stat().st_size > 16 * 1024 * 1024:
            raise CadError("MANIFEST_TOO_LARGE", "Sheet manifest exceeds 16 MiB.")
        value = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise CadError("INVALID_MANIFEST", "Cannot read the sheet manifest.",
                       {"path": str(target), "reason": str(exc)}) from exc
    if (not isinstance(value, dict) or not isinstance(value.get("sheets"), list) or
            len(value["sheets"]) > 200 or
            any(not isinstance(item, dict) for item in value["sheets"])):
        raise CadError("INVALID_MANIFEST", "Expected a bounded sheet manifest.")
    return target, value


def _sheet_bbox(item):
    bbox = item.get("bbox")
    if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or
            any(type(v) not in (int, float) or not isfinite(v) for v in bbox) or
            bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
        raise CadError("INVALID_MANIFEST", "Sheet bbox must have four finite coordinates and positive size.")
    return list(bbox)


def _split_artifact_sheet(path, manifest_path, value, layout):
    """Use an exported page only when its own path and serialized SHA match.

    The source_handles in a split manifest belong to the original drawing. They
    must never be applied to the exported page, whose native handles can change.
    """
    source = Path(path).expanduser().resolve()
    matches = []
    for item in value["sheets"]:
        name = item.get("dxf")
        if not isinstance(name, str):
            continue
        # Split artifacts are plain filenames. Reject traversal rather than
        # accidentally accepting a source or a different page as the artifact.
        if (not name or Path(name).name != name or "/" in name or "\\" in name or
                ":" in name or name in (".", "..")):
            raise CadError("INVALID_MANIFEST", "Split DXF artifact names must be plain local filenames.")
        candidate = manifest_path.parent / name
        if candidate.resolve() == source:
            if candidate.is_symlink() or candidate.resolve().parent != manifest_path.parent:
                raise CadError("INVALID_MANIFEST", "Split DXF artifact must stay in its manifest directory.")
            matches.append(item)
    if not matches:
        return None
    if len(matches) != 1:
        raise CadError("INVALID_MANIFEST", "Split DXF artifact must identify exactly one sheet.")
    item = matches[0]
    expected = item.get("dxf_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise CadError("STALE_MANIFEST", "Split artifact requires its own dxf_sha256 before a frame can be reused.")
    actual = file_sha256(source)
    if actual != expected.lower():
        raise CadError("STALE_MANIFEST", "Split DXF differs from its manifest; its saved frame is not trusted.",
                       {"expected": expected.lower(), "actual": actual})
    if value.get("layout", "Model") != layout:
        raise CadError("INVALID_MANIFEST", "Split frame belongs to a different layout.",
                       {"frame_layout": value.get("layout", "Model"), "requested_layout": layout})
    selected = dict(item)
    selected.pop("source_handles", None)
    selected["bbox"] = _sheet_bbox(item)
    selected["frame_context"] = {
        "scope": "split_artifact", "manifest": str(manifest_path),
        "artifact_sha256": actual, "artifact_sha256_verified": True,
        "native_handles": "current split DXF; original source_handles are not reused",
    }
    source_info = value.get("source")
    if isinstance(source_info, dict):
        selected["frame_context"]["original_source_sha256"] = source_info.get("sha256")
    return selected


def split_preview_frame(path, layout="Model"):
    """Resolve the adjacent split manifest without discovering guessed frames."""
    target = Path(path).expanduser().resolve().parent / "manifest.json"
    if not target.is_file():
        return None
    manifest_path, value = _read_manifest(target)
    return _split_artifact_sheet(path, manifest_path, value, layout)


def resolve_sheets(document, path, selection, manifest=None, layout="Model", source_info=None):
    from .sheets import discover_sheets
    if manifest:
        if isinstance(manifest, dict):
            # Internal, already SHA-verified workset selection. Never reuse its
            # original drawing handles on the exported subset.
            value = manifest
            if (not isinstance(value.get("sheets"), list) or len(value["sheets"]) > 200
                    or any(not isinstance(item, dict) for item in value["sheets"])):
                raise CadError("INVALID_MANIFEST", "Expected bounded workset sheet metadata.")
            split_sheet = None
        else:
            target, value = _read_manifest(manifest)
            split_sheet = _split_artifact_sheet(path, target, value, layout)
        source = value.get("source", {})
        digests = {file_sha256(path)}
        if source_info:
            digests.add(source_info.get("sha256"))
        if split_sheet is not None:
            sheets = [split_sheet]
        elif not isinstance(source, dict):
            raise CadError("INVALID_MANIFEST", "Manifest source identity must be an object.")
        elif source.get("sha256") not in digests and source.get("normalized_dxf_sha256") not in digests:
            raise CadError("STALE_MANIFEST", "Manifest source SHA256 does not match this drawing.")
        elif source.get("normalized_dxf_sha256") and source["normalized_dxf_sha256"] != file_sha256(path):
            raise CadError("STALE_MANIFEST", "Converted DXF revision differs from the manifest; native handles may have changed.")
        else:
            sheets = value["sheets"]
    else:
        sheets = discover_sheets(document, layout=layout)["sheets"]
    selected = sheets if selection == "all" else [item for item in sheets if selection in
        {item.get("id"), item.get("title"), item.get("name"), item.get("primary_title")}]
    if not selected:
        raise CadError("SHEET_NOT_FOUND", "No sheet matches the requested id or title.")
    if selection != "all" and len(selected) != 1:
        raise CadError("AMBIGUOUS_SHEET", "Title matches multiple sheets; select by id.")
    identifiers = [item.get("id", "") for item in selected]
    if (any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value)
            for value in identifiers) or len(set(identifiers)) != len(identifiers)):
        raise CadError("INVALID_MANIFEST", "Sheet ids must be unique safe filename components.")
    for item in selected:
        _sheet_bbox(item)
    if document is not None:
        owner = document.layouts.get(layout).layout_key
        for item in selected:
            handles = item.get("source_handles")
            if handles is not None:
                from .engine import _validate_handles
                handles = _validate_handles(handles)
                if len(handles) != len(set(handles)) or any(
                        (document.entitydb.get(h) is None or document.entitydb[h].dxf.get("owner") != owner)
                        for h in handles):
                    raise CadError("INVALID_MANIFEST", "Sheet handles must be unique native handles in the selected layout.")
    return selected


def _one(document, target, overwrite, width, options):
    from .render import render_document
    from .thumbnails import create_thumbnail
    target = Path(target).expanduser().resolve()
    if target.suffix.lower() == ".svg":
        return render_document(document, target, overwrite=overwrite, **options)
    if target.suffix.lower() != ".png":
        raise CadError("UNSUPPORTED_FORMAT", "Preview output must be SVG or PNG.")
    if target.exists() and not overwrite:
        raise CadError("OUTPUT_EXISTS", "Preview output exists.")
    with tempfile.TemporaryDirectory(prefix="tcad-preview-") as folder:
        temporary = Path(folder) / "preview.svg"
        result = render_document(document, temporary, **options)
        png = create_thumbnail(temporary, target, max_side=width, overwrite=overwrite)
    result.update(png)
    result["format"] = "png"
    return result


def render_selection(document, path, output, *, sheet=None, manifest=None,
                     output_dir=None, source_info=None, width=1600, overwrite=False, **options):
    if type(width) is not int or not 16 <= width <= 4096:
        raise CadError("INVALID_ARGUMENT", "width must be between 16 and 4096 pixels (longest side).")
    if output and Path(output).expanduser().resolve() == Path(path).expanduser().resolve():
        raise CadError("OUTPUT_CONFLICT", "Preview must not replace its source.")
    if sheet is None:
        if not output or output_dir or manifest:
            raise CadError("INVALID_ARGUMENT", "Use output for a single preview; manifest requires sheet.")
        # A split DXF contains whole intersecting entities, so fitting its full
        # extents can include neighbouring sheets or shrink the intended page.
        # Explicit user bbox always wins and does not consult a nearby manifest.
        automatic = None if options.get("bbox") is not None else split_preview_frame(
            path, options.get("layout", "Model"))
        if automatic is not None:
            options["bbox"] = automatic["bbox"]
        result = _one(document, output, overwrite, width, options)
        if automatic is not None:
            result["sheet"] = {k: automatic.get(k) for k in ("id", "title", "bbox")}
            result["sheet_context"] = dict(automatic["frame_context"], automatic=True)
        elif options.get("bbox") is not None:
            result["sheet_context"] = {"scope": "explicit_bbox", "automatic": False}
        elif options.get("layers") is not None or options.get("exclude_layers") is not None:
            result["sheet_context"] = {"scope": "layout_extents", "automatic": False,
                                       "verified_split_frame": False}
            result["warnings"].append(
                "No verified adjacent split manifest identifies this DXF; preview fits visible selected geometry. "
                "Keep manifest.json beside split DXFs or provide bbox to preserve the intended sheet frame.")
        return result
    if options.get("bbox") is not None and sheet == "all":
        raise CadError("INVALID_ARGUMENT", "Select one sheet before refining it with bbox; use workset for multiple regions.")
    sheets = resolve_sheets(document, path, sheet, manifest, options.get("layout", "Model"), source_info)
    if sheet != "all":
        if not output or output_dir:
            raise CadError("INVALID_ARGUMENT", "A selected sheet needs output.")
        frame = list(sheets[0]["bbox"])
        if options.get("bbox") is not None:
            from .spatial import validated_bbox
            box = validated_bbox(options["bbox"])
            frame = [max(frame[0], box[0]), max(frame[1], box[1]),
                     min(frame[2], box[2]), min(frame[3], box[3])]
            if frame[0] > frame[2] or frame[1] > frame[3]:
                raise CadError("EMPTY_SELECTION", "bbox does not intersect the selected sheet.")
        options["bbox"] = frame
        handles = sheets[0].get("source_handles")
        if handles:
            options["entities"] = [document.entitydb[h] for h in handles]
        result = _one(document, output, overwrite, width, options)
        result["sheet"] = {k: sheets[0].get(k) for k in ("id", "title", "bbox")}
        result["sheet"]["selection_bbox"] = frame
        if sheets[0].get("frame_context"):
            result["sheet_context"] = dict(sheets[0]["frame_context"], automatic=False)
        return result
    if not output_dir or output:
        raise CadError("INVALID_ARGUMENT", "sheet=all needs a new output_dir.")
    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise CadError("OUTPUT_EXISTS", "Batch redraw needs a new output directory.")
    if not target.parent.is_dir():
        raise CadError("DIRECTORY_NOT_FOUND", "Output parent directory is missing.")
    import uuid
    # atomic staging is owned by this worker; supervised cancellation cleans it.
    operation = os.environ.get("TCAD_OPERATION_ID") or uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix=f".tcad-{operation}-sheet-", dir=target.parent) as folder:
        staging = Path(folder) / "publish"
        staging.mkdir()
        records = []
        total_bytes = 0
        for item in sheets:
            selected = dict(options, bbox=item["bbox"])
            if item.get("source_handles"):
                selected["entities"] = [document.entitydb[h] for h in item["source_handles"]]
            name = item["id"] + ".svg"
            receipt = _one(document, staging / name, False, width, selected)
            receipt["output"] = name
            png = create_batch_thumbnail(staging / name, staging / (item["id"] + ".png"), width)
            total_bytes += receipt["bytes"] + png["bytes"]
            if total_bytes > 512 * 1024 * 1024:
                raise CadError("OUTPUT_TOO_LARGE", "Batch redraw exceeds the 512 MiB total artifact budget.")
            records.append({"id": item["id"], "title": item.get("title"), "svg": name,
                            "png": png, "thumbnail": png, "bbox": item["bbox"], "render": receipt})
        report = {"source": source_info or {"sha256": file_sha256(path)}, "sheets": records,
                  "sheet_count": len(records), "document_reads": 1}
        (staging / "render-manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        from .sheet_report import write_sheet_report
        write_sheet_report(report, staging / "index.html", manifest_filename="render-manifest.json")
        if sum(item.stat().st_size for item in staging.iterdir()) > 512 * 1024 * 1024:
            raise CadError("OUTPUT_TOO_LARGE", "Batch redraw including report exceeds its total artifact budget.")
        os.rename(staging, target)
    return {"output_dir": str(target), "manifest": str(target / "render-manifest.json"),
            "html": str(target / "index.html"),
            "sheet_count": len(records), "document_reads": 1}


def create_batch_thumbnail(svg, png, width):
    from .thumbnails import create_thumbnail
    result = create_thumbnail(svg, png, max_side=width)
    result["output"] = Path(png).name
    return result
