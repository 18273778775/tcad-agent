# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Reusable, verified native DXF selections for agent analysis.

Preparation parses the source once. Reuse checks file hashes without parsing the
source and returns only the smaller exported drawing. Bounding boxes select whole
native entities; they never imply geometric clipping or component recognition.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

from .converters import readable_dxf
from .errors import CadError
from .io import destination_lock, file_sha256, jsonable, read_document
from .spatial import entity_bounds, intersects, validated_bbox

WORKSET_VERSION = 1
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")
_KINDS = {"plan", "detail", "schedule", "notes", "unknown"}


def _strings(value, name, maximum=100000):
    if value is None:
        return None
    if (not isinstance(value, (list, tuple)) or len(value) > maximum or not value
            or any(not isinstance(item, str) or not item or len(item) > 512 for item in value)):
        raise CadError("INVALID_ARGUMENT", f"{name} must be a nonempty bounded list of strings.")
    return sorted(set(value))


def _hash(path, code="STALE_WORKSET"):
    try:
        return file_sha256(path)
    except OSError as exc:
        raise CadError(code, "A workset source or artifact is unavailable.",
                       {"path": str(path)}) from exc


def _read(path):
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_MANIFEST_BYTES:
            raise CadError("INVALID_WORKSET", "Workset manifest must be an ordinary file of at most 32 MiB.")
        value = json.loads(path.read_text("utf-8-sig"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise CadError("INVALID_WORKSET", "Cannot read the workset manifest.") from exc
    if not isinstance(value, dict) or value.get("version") != WORKSET_VERSION:
        raise CadError("INVALID_WORKSET", "Unsupported workset manifest.")
    return value


def _source_identity(source, normalized, source_info):
    normalized_sha = file_sha256(normalized)
    identity = {"input": str(source), "path": str(source), "sha256": file_sha256(source), "normalized_input": str(normalized),
                "normalized_dxf_sha256": normalized_sha}
    if source_info is not None:
        if not isinstance(source_info, dict):
            raise CadError("INVALID_ARGUMENT", "source_info must be an identity object.")
        original = source_info.get("input", source_info.get("path"))
        if source_info.get("input") is not None and source_info.get("path") is not None:
            if not isinstance(source_info["input"], str) or not isinstance(source_info["path"], str):
                raise CadError("INVALID_ARGUMENT", "source_info input/path aliases must be paths.")
            if Path(source_info["input"]).expanduser().resolve() != Path(source_info["path"]).expanduser().resolve():
                raise CadError("INVALID_ARGUMENT", "source_info input/path aliases must identify the same source.")
        expected = source_info.get("sha256")
        if original is not None or expected is not None:
            if (not isinstance(original, str) or not isinstance(expected, str)
                    or not _SHA.fullmatch(expected)):
                raise CadError("INVALID_ARGUMENT", "source_info requires input and SHA256 together.")
            original = Path(original).expanduser().resolve()
            if _hash(original, "REVISION_CONFLICT") != expected:
                raise CadError("REVISION_CONFLICT", "Original source differs from its supplied identity.")
            identity.update(input=str(original), path=str(original), sha256=expected)
        supplied_normalized = source_info.get("normalized_dxf_sha256")
        if supplied_normalized is not None and supplied_normalized != normalized_sha:
            raise CadError("REVISION_CONFLICT", "Normalized drawing differs from its supplied identity.")
    return identity


def _select_sheets(document, normalized, manifest, sheets, building, kind, title_regex, identity, layout):
    from .render_jobs import resolve_sheets
    if manifest is None and sheets is None and building is None and kind is None and title_regex is None:
        return []
    available = resolve_sheets(document, normalized, "all", manifest, layout, identity)
    if any(any(item.get(key) is not None and (not isinstance(item[key], str) or len(item[key]) > 4096)
                   for key in ("title", "name", "primary_title", "building", "kind")) for item in available):
        raise CadError("INVALID_MANIFEST", "Sheet title/name/metadata must be bounded text or null.")
    if sheets is not None:
        matches = []
        for key in sheets:
            found = [item for item in available if key in {
                item.get("id"), item.get("title"), item.get("name"), item.get("primary_title")}]
            if not found:
                raise CadError("SHEET_NOT_FOUND", "No sheet matches the requested id or title.", {"sheet": key})
            if len(found) != 1:
                raise CadError("AMBIGUOUS_SHEET", "Title matches several sheets; use sheet ids.", {"sheet": key})
            matches.extend(found)
        available = [item for item in available if item.get("id") in {m["id"] for m in matches}]
    if building is not None:
        available = [item for item in available if item.get("building") == building]
    if kind is not None:
        available = [item for item in available if item.get("kind") == kind]
    if title_regex is not None:
        pattern = re.compile(title_regex)
        available = [item for item in available if pattern.search(item.get("title") or "")]
    if not available:
        raise CadError("SHEET_NOT_FOUND", "No sheet matches all requested sheet metadata filters.")
    return available


def _handle_map(originals, exported, source_document, target_document):
    """Pair only after the serialized native geometry verification has passed."""
    records, visited = [], set()

    def pairs(first, second, scope):
        for original, copy in zip(first, second):
            records.append({"source_handle": original.dxf.handle, "exported_handle": copy.dxf.handle,
                            "type": original.dxftype(), "scope": scope})
            if original.dxftype() == "INSERT":
                pairs(original.attribs, copy.attribs, scope + "/attributes")
            if original.dxftype() == "POLYLINE":
                pairs(original.vertices, copy.vertices, scope + "/vertices")
            if original.dxftype() in {"INSERT", "DIMENSION"}:
                attr = "name" if original.dxftype() == "INSERT" else "geometry"
                names = (original.dxf.get(attr), copy.dxf.get(attr))
                if all(names) and names not in visited:
                    visited.add(names)
                    pairs(source_document.blocks.get(names[0]), target_document.blocks.get(names[1]),
                          "block:" + names[0])
    pairs(originals, exported, "layout")
    return records


def _safe_existing(target):
    if not target.is_dir() or target.is_symlink():
        raise CadError("UNSAFE_PATH", "Only an ordinary existing workset directory can be replaced.")
    if any(item.is_symlink() or item.name not in {"subset.dxf", "workset.json"}
           or not item.is_file() for item in target.iterdir()):
        raise CadError("UNSAFE_PATH", "Existing directory contains files outside this workset; choose a new directory.")
    _read(target / "workset.json")


def _publish(stage, target, overwrite):
    if not target.exists():
        os.rename(stage, target)
        return
    if not overwrite:
        raise CadError("OUTPUT_EXISTS", "Workset destination exists; set overwrite explicitly.")
    _safe_existing(target)
    backup = Path(tempfile.mkdtemp(prefix=".tcad-workset-backup-", dir=target.parent))
    backup.rmdir()
    os.rename(target, backup)
    try:
        os.rename(stage, target)
    except BaseException:
        os.rename(backup, target)
        raise
    finally:
        if backup.exists() and target.exists():
            if backup.is_symlink() or backup.resolve().parent != target.parent.resolve():
                raise CadError("UNSAFE_PATH", "Workset backup left its verified publication directory.")
            shutil.rmtree(backup)


def create_workset(input_path, output_dir, layout="Model", manifest=None, sheets=None, bboxes=None,
                   building=None, kind=None, title_regex=None, handles=None, layers=None, types=None,
                   source_info=None, max_entities=100000, max_bytes=256 * 1024 * 1024, overwrite=False):
    """Export a reusable union of selected pages/regions, filtered by native roots.

    Sheet metadata filters are conjunctive. Their boxes and explicit ``bboxes``
    form a union; handles/layers/types then intersect that spatial selection.
    Unknown bounds remain in full-layout or handle selections, but cannot be
    silently assigned to a spatial selection. Block definitions stay whole.
    """
    started = time.perf_counter()
    if type(max_entities) is not int or not 1 <= max_entities <= 1000000:
        raise CadError("INVALID_ARGUMENT", "max_entities must be between 1 and 1000000.")
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 2 * 1024**3:
        raise CadError("INVALID_ARGUMENT", "max_bytes must be between 1024 and 2147483648.")
    if type(overwrite) is not bool or not isinstance(layout, str) or not layout:
        raise CadError("INVALID_ARGUMENT", "overwrite must be boolean and layout must be nonempty.")
    sheets = _strings(sheets, "sheets", 200)
    handles = _strings(handles, "handles")
    layers = _strings(layers, "layers", 10000)
    types = _strings(types, "types", 256)
    if handles is not None:
        from .query import _validate_handles
        handles = sorted(set(_validate_handles(handles)))
    if types is not None:
        types = sorted({value.upper() for value in types})
    if kind is not None and (not isinstance(kind, str) or kind not in _KINDS):
        raise CadError("INVALID_ARGUMENT", "kind must be plan, detail, schedule, notes or unknown.")
    if building is not None and (not isinstance(building, str) or not building or len(building) > 512):
        raise CadError("INVALID_ARGUMENT", "building must be an exact nonempty metadata value.")
    if title_regex is not None:
        if not isinstance(title_regex, str) or len(title_regex) > 256:
            raise CadError("INVALID_ARGUMENT", "title_regex must be a string of at most 256 characters.")
        try:
            re.compile(title_regex)
        except re.error as exc:
            raise CadError("INVALID_ARGUMENT", "title_regex is invalid.") from exc
    if bboxes is not None and (not isinstance(bboxes, (list, tuple)) or not bboxes or len(bboxes) > 200):
        raise CadError("INVALID_ARGUMENT", "bboxes must contain between 1 and 200 boxes.")
    boxes = [validated_bbox(value) for value in (bboxes or [])]
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise CadError("FILE_NOT_FOUND", "Drawing does not exist.")
    unresolved_target = Path(output_dir).expanduser().absolute()
    if unresolved_target.is_symlink():
        raise CadError("UNSAFE_PATH", "Workset output directory must not be a symlink.")
    target = unresolved_target.resolve()
    if target == source or target in source.parents:
        raise CadError("UNSAFE_PATH", "Workset output directory must not contain its source.")
    if not target.parent.is_dir():
        raise CadError("DIRECTORY_NOT_FOUND", "Workset output parent must already exist.")
    with destination_lock(target):
        if target.exists() and not overwrite:
            raise CadError("OUTPUT_EXISTS", "Workset destination exists; set overwrite explicitly.")
        if target.exists():
            _safe_existing(target)
        operation = os.environ.get("TCAD_OPERATION_ID", "")
        stage = Path(tempfile.mkdtemp(prefix=f".tcad-{operation}-workset-", dir=target.parent))
        try:
            with readable_dxf(source) as normalized:
                identity = _source_identity(source, normalized, source_info)
                original_source = Path(identity["input"])
                if target == original_source or target in original_source.parents:
                    raise CadError("UNSAFE_PATH", "Workset output must not contain the original source.")
                document_read_requests = {"source": 0, "serialized_subset": 0}
                document_read_requests["source"] += 1
                document = read_document(normalized)
                from .engine import _layout, _units
                selected_layout = _layout(document, layout)
                selected_sheets = _select_sheets(document, normalized, manifest, sheets, building, kind,
                                                 title_regex, identity, layout)
                boxes.extend(validated_bbox(item["bbox"]) for item in selected_sheets)
                # Preserve disjoint selections rather than their large outer envelope.
                boxes = [list(value) for value in dict.fromkeys(tuple(value) for value in boxes)]
                if len(boxes) > 200:
                    raise CadError("INVALID_ARGUMENT", "Selected sheet and explicit regions together must not exceed 200.")
                native = list(selected_layout)
                if handles is not None:
                    absent = set(handles) - {entity.dxf.handle for entity in native}
                    if absent:
                        raise CadError("ENTITY_NOT_FOUND", "Handles must be native roots in this layout.",
                                       {"handles": sorted(absent)[:32]})
                wanted, allowed_layers, allowed_types = (None if handles is None else set(handles),
                    None if layers is None else set(layers), None if types is None else set(types))
                eligible = [entity for entity in native if
                    (wanted is None or entity.dxf.handle in wanted)
                    and (allowed_layers is None or entity.dxf.get("layer", "0") in allowed_layers)
                    and (allowed_types is None or entity.dxftype() in allowed_types)]
                unknown, partial, crossing = [], [], []
                bounds_statistics = {"method": "not required for a full-layout/native-filter selection"}
                if boxes:
                    bounded, unknown, bounds_statistics = entity_bounds(eligible)
                    partial = bounds_statistics.get("partial_bounds_handles", [])
                    chosen = []
                    for entity, extent in bounded:
                        matching = [box for box in boxes if intersects(extent, box)]
                        if not matching:
                            continue
                        chosen.append(entity)
                        if not any(extent[0] >= box[0] and extent[1] >= box[1]
                                   and extent[2] <= box[2] and extent[3] <= box[3] for box in matching):
                            crossing.append(entity.dxf.handle)
                else:
                    chosen = eligible
                if len(chosen) > max_entities:
                    raise CadError("WORKSET_TOO_LARGE", "Selected native entities exceed the workset budget.",
                                   {"selected_entity_count": len(chosen), "max_entities": max_entities})
                if not chosen:
                    raise CadError("EMPTY_SELECTION", "No native entities match the workset selection.",
                                   {"unknown_bounds_count": len(unknown)})
                from .sheets import _export_entities, _verify_dxf_inventory
                exported, image_preservation = _export_entities(document, selected_layout, chosen)
                artifact = stage / "subset.dxf"
                exported.saveas(artifact)
                if artifact.stat().st_size > max_bytes:
                    raise CadError("WORKSET_OUTPUT_TOO_LARGE", "Workset DXF exceeds the output byte budget.")
                document_read_requests["serialized_subset"] += 1
                reopened = read_document(artifact)
                verification = _verify_dxf_inventory(document, reopened, chosen)
                if not verification["ok"]:
                    raise CadError("WORKSET_VERIFICATION_FAILED", "Serialized workset differs from selected source geometry/resources.",
                                   {"verification": verification})
                mapping = _handle_map(chosen, list(reopened.modelspace()), document, reopened)
                warnings = []
                if crossing:
                    warnings.append("Crossing native entities are retained whole; selection is not geometric clipping.")
                if unknown:
                    warnings.append("Entities without computable bounds were excluded from spatial selection; inspect workset.json.")
                if bounds_statistics.get("partial_bounds_count", 0):
                    warnings.append("Some block bounds omit unsupported children; spatial selection completeness is not established.")
                if any(entity.dxftype() == "INSERT" for entity in chosen) and (layers is not None or types is not None):
                    warnings.append("Layer/type filters select native roots; selected INSERT definitions retain all their child layers/types.")
                if original_source.suffix.lower() == ".dwg":
                    warnings.append("Preservation is relative to normalized DXF; it does not establish lossless DWG conversion.")
                summary = {"source_layout_entity_count": len(native), "eligible_native_entity_count": len(eligible),
                           "selected_entity_count": len(chosen), "sheet_count": len(selected_sheets),
                           "region_count": len(boxes), "crossing_entity_count": len(crossing),
                           "unknown_bounds_count": len(unknown),
                           "partial_bounds_count": bounds_statistics.get("partial_bounds_count", 0),
                           "spatial_selection_complete": not unknown and not bounds_statistics.get("partial_bounds_count", 0),
                           "serialized_geometry_verified": True}
                record = {"version": WORKSET_VERSION, "source": identity, "layout": layout,
                          "artifact_layout": "Model", "coordinate_system": "source WCS, no translation or scaling",
                          "units": _units(document), "units_note": "INSUNITS metadata is preserved, never inferred; unitless remains unitless.",
                          "selection": {"sheets": [{key: item.get(key) for key in
                              ("id", "title", "name", "building", "kind", "bbox", "metadata_confidence")}
                              for item in selected_sheets], "bboxes": boxes, "sheet_selectors": sheets,
                              "building": building, "kind": kind, "title_regex": title_regex,
                              "handles": handles, "layers": layers, "types": types,
                              "rule": "union of selected sheet/explicit boxes, intersected with native root filters; whole crossing entities retained",
                              "unknown_bounds_policy": "exclude and report for spatial selection; retain for nonspatial selection",
                              "unknown_bounds_handles": unknown, "partial_bounds_handles": partial,
                              "partial_bounds_handles_complete": bounds_statistics.get("partial_bounds_handles_complete", True),
                              "crossing_handles": crossing, "native_filters_scope": "native roots; block definitions stay whole"},
                          "artifact": {"dxf": "subset.dxf", "sha256": file_sha256(artifact),
                                       "bytes": artifact.stat().st_size}, "summary": summary,
                          "handle_map": mapping, "handle_map_complete": True,
                          "verification": verification, "image_preservation": image_preservation,
                          "bounds_statistics": bounds_statistics, "warnings": warnings,
                          "statistics": {"source_document_read_requests": document_read_requests["source"],
                              "serialized_subset_read_requests": document_read_requests["serialized_subset"],
                              "count_scope": "calls made by workset preparation; per-request document caching may avoid physical reparsing",
                              "prepare_seconds": round(time.perf_counter() - started, 6)},
                          "limits": {"max_entities": max_entities, "max_bytes": max_bytes}}
                manifest_output = stage / "workset.json"
                manifest_output.write_text(json.dumps(jsonable(record), ensure_ascii=False, indent=2), "utf-8")
                if manifest_output.stat().st_size > MAX_MANIFEST_BYTES or sum(
                        item.stat().st_size for item in stage.iterdir()) > max_bytes:
                    raise CadError("WORKSET_OUTPUT_TOO_LARGE", "Workset DXF and provenance exceed the byte budget.")
                if (_hash(original_source, "REVISION_CONFLICT") != identity["sha256"]
                        or _hash(normalized, "REVISION_CONFLICT") != identity["normalized_dxf_sha256"]):
                    raise CadError("REVISION_CONFLICT", "Source changed while workset preparation was running.")
                _publish(stage, target, overwrite)
                return {"output_dir": str(target), "workset": str(target / "workset.json"),
                        "manifest": str(target / "workset.json"), "drawing": str(target / "subset.dxf"),
                        "summary": summary, "units": record["units"], "warnings": warnings,
                        "statistics": record["statistics"],
                        "handle_map": {"artifact": str(target / "workset.json"), "count": len(mapping),
                                       "inline": False}}
        finally:
            if stage.exists():
                if stage.is_symlink() or stage.resolve().parent != target.parent.resolve():
                    raise CadError("UNSAFE_PATH", "Workset staging left its verified publication directory.")
                shutil.rmtree(stage)


def resolve_workset(workset_path, input_path=None):
    """Return ``(verified_subset_path, bounded_context)`` without parsing any CAD.

    Saved original source bytes must still match. A missing or changed original,
    altered subset, or mismatched explicitly supplied source is STALE_WORKSET.
    Cooperating readers share the preparation destination lock during validation.
    """
    requested = Path(workset_path).expanduser().absolute()
    if requested.is_symlink():
        raise CadError("UNSAFE_PATH", "Workset path must not be a symbolic link.")
    requested = requested.resolve()
    manifest_path = requested / "workset.json" if requested.is_dir() else requested
    if manifest_path.name != "workset.json":
        raise CadError("INVALID_WORKSET", "Use the workset directory or its workset.json.")
    root = manifest_path.parent
    with destination_lock(root):
        value = _read(manifest_path)
        source, artifact = value.get("source"), value.get("artifact")
        if (not isinstance(source, dict) or not isinstance(artifact, dict)
                or not isinstance(source.get("input"), str) or not source["input"]
                or not isinstance(source.get("sha256"), str) or not _SHA.fullmatch(source["sha256"])
                or not isinstance(source.get("normalized_dxf_sha256"), str)
                or not _SHA.fullmatch(source["normalized_dxf_sha256"])
                or artifact.get("dxf") != "subset.dxf" or not isinstance(artifact.get("sha256"), str)
                or not _SHA.fullmatch(artifact["sha256"])):
            raise CadError("INVALID_WORKSET", "Workset requires fixed local artifact and complete source hashes.")
        drawing = root / "subset.dxf"
        if drawing.is_symlink() or drawing.resolve().parent != root:
            raise CadError("UNSAFE_PATH", "Workset DXF must remain in its own directory.")
        original = Path(source["input"]).expanduser().resolve()
        if _hash(original) != source["sha256"]:
            raise CadError("STALE_WORKSET", "Original drawing changed; prepare the workset again.")
        if input_path is not None:
            supplied = Path(input_path).expanduser().resolve()
            if _hash(supplied) not in {source["sha256"], source["normalized_dxf_sha256"]}:
                raise CadError("STALE_WORKSET", "Requested input does not match this workset source.")
        if _hash(drawing) != artifact["sha256"]:
            raise CadError("STALE_WORKSET", "Workset subset changed; prepare it again before analysis.")
        verification, summary = value.get("verification"), value.get("summary")
        if (not isinstance(verification, dict) or verification.get("ok") is not True
                or not isinstance(summary, dict) or value.get("artifact_layout") != "Model"):
            raise CadError("INVALID_WORKSET", "Workset requires successful serialized geometry verification.")
        saved_selection = value.get("selection")
        if not isinstance(saved_selection, dict) or not isinstance(saved_selection.get("bboxes"), list) or len(saved_selection["bboxes"]) > 200:
            raise CadError("INVALID_WORKSET", "Workset selection requires bounded regions.")
        boxes = [validated_bbox(box) for box in saved_selection["bboxes"]]
        selected_sheets = saved_selection.get("sheets")
        if (not isinstance(selected_sheets, list) or len(selected_sheets) > 200
                or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                       or len(item["id"]) > 128 or any(item.get(key) is not None
                           and (not isinstance(item[key], str) or len(item[key]) > 4096)
                           for key in ("title", "name")) for item in selected_sheets)):
            raise CadError("INVALID_WORKSET", "Selected workset sheets must have bounded ids, names and titles.")
        selected_sheets = [{"id": item["id"], "title": item.get("title"), "name": item.get("name"),
                            "bbox": validated_bbox(item.get("bbox"))} for item in selected_sheets]
        summary_keys = ("source_layout_entity_count", "eligible_native_entity_count", "selected_entity_count", "sheet_count",
                        "region_count", "crossing_entity_count", "unknown_bounds_count", "partial_bounds_count",
                        "spatial_selection_complete", "serialized_geometry_verified")
        if any(key not in summary or type(summary[key]) not in (int, bool) or summary[key] < 0 for key in summary_keys):
            raise CadError("INVALID_WORKSET", "Workset summary must contain bounded scalar counts and verification flags.")
        summary = {key: summary[key] for key in summary_keys}
        warnings = value.get("warnings", [])
        if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            raise CadError("INVALID_WORKSET", "Workset warnings must be text.")
        normalized_input = source.get("normalized_input")
        if normalized_input is not None and not isinstance(normalized_input, str):
            raise CadError("INVALID_WORKSET", "Workset normalized source path must be text.")
        context = {"manifest": str(manifest_path), "drawing": str(drawing),
                   "source_input": str(original), "normalized_input": normalized_input,
                   "source_sha256": source["sha256"],
                   "normalized_dxf_sha256": source["normalized_dxf_sha256"],
                   "artifact_sha256": artifact["sha256"], "artifact_sha256_verified": True,
                   "source_sha256_verified": True, "source_document_parse_count": 0,
                   "layout": "Model", "original_layout": value.get("layout"),
                   "units": value.get("units"), "summary": summary,
                   "selected_sheets": selected_sheets, "regions": boxes,
                   "selection": {"sheet_count": summary.get("sheet_count"), "region_count": summary.get("region_count"),
                                 "preview_bbox": boxes[0] if len(boxes) == 1 else None,
                                 "preview_regions": boxes, "geometric_clipping": False, "native_filters_scope": "roots"},
                   "handle_map": {"artifact": str(manifest_path), "inline": False},
                   "warnings": [item[:1024] for item in warnings[:8]]}
        return drawing, context
