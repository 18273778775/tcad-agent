# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0
"""One-document sheet redraw and code-only PNG export."""

import json
import os
import re
from pathlib import Path
import tempfile

from .errors import CadError
from .io import file_sha256


def resolve_sheets(document, path, selection, manifest=None, layout="Model", source_info=None):
    from .sheets import discover_sheets
    if manifest:
        target = Path(manifest).expanduser().resolve()
        if target.stat().st_size > 16 * 1024 * 1024:
            raise CadError("MANIFEST_TOO_LARGE", "Sheet manifest exceeds 16 MiB.")
        value = json.loads(target.read_text(encoding="utf-8-sig"))
        source = value.get("source", {})
        digests = {file_sha256(path)}
        if source_info:
            digests.add(source_info.get("sha256"))
        if source.get("sha256") not in digests and source.get("normalized_dxf_sha256") not in digests:
            raise CadError("STALE_MANIFEST", "Manifest source SHA256 does not match this drawing.")
        if source.get("normalized_dxf_sha256") and source["normalized_dxf_sha256"] != file_sha256(path):
            raise CadError("STALE_MANIFEST", "Converted DXF revision differs from the manifest; native handles may have changed.")
        sheets = value.get("sheets", [])
    else:
        sheets = discover_sheets(document, layout=layout)["sheets"]
    selected = sheets if selection == "all" else [item for item in sheets if selection in
        {item.get("id"), item.get("title"), item.get("name"), item.get("primary_title")}]
    if not selected:
        raise CadError("SHEET_NOT_FOUND", "No sheet matches the requested id or title.")
    if selection != "all" and len(selected) != 1:
        raise CadError("AMBIGUOUS_SHEET", "Title matches multiple sheets; select by id.")
    identifiers = [item.get("id", "") for item in selected]
    if len(set(identifiers)) != len(identifiers) or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) for value in identifiers):
        raise CadError("INVALID_MANIFEST", "Sheet ids must be unique safe filename components.")
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
        return _one(document, output, overwrite, width, options)
    if options.get("bbox") is not None:
        raise CadError("INVALID_ARGUMENT", "Use sheet or bbox, not both.")
    sheets = resolve_sheets(document, path, sheet, manifest, options.get("layout", "Model"), source_info)
    if sheet != "all":
        if not output or output_dir:
            raise CadError("INVALID_ARGUMENT", "A selected sheet needs output.")
        options["bbox"] = sheets[0]["bbox"]
        handles = sheets[0].get("source_handles")
        if handles:
            options["entities"] = [document.entitydb[h] for h in handles]
        result = _one(document, output, overwrite, width, options)
        result["sheet"] = {k: sheets[0].get(k) for k in ("id", "title", "bbox")}
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
                            "png": png, "render": receipt})
        report = {"source": source_info or {"sha256": file_sha256(path)}, "sheets": records,
                  "sheet_count": len(records), "document_reads": 1}
        (staging / "render-manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.rename(staging, target)
    return {"output_dir": str(target), "manifest": str(target / "render-manifest.json"),
            "sheet_count": len(records), "document_reads": 1}


def create_batch_thumbnail(svg, png, width):
    from .thumbnails import create_thumbnail
    result = create_thumbnail(svg, png, max_side=width)
    result["output"] = Path(png).name
    return result
