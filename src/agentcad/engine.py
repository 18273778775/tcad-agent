# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""A local, bounded DXF engine. Plans are data, never executable code.

Creation and plan edits use Model space. DXF coordinates retain their native
WCS/OCS meaning, exposed in query records. Linear units are drawing units;
changing INSUNITS changes metadata and does not rescale existing geometry.
"""

from __future__ import annotations

from collections import Counter
from itertools import islice
import math
from pathlib import Path

import ezdxf
from ezdxf import bbox as ezbbox
from ezdxf.math import Matrix44, Vec3, arc_angle_span_deg

from .errors import CadError
from .io import jsonable, read_document, revision_guard, save_document, validate_finite_entity
from .schema import validate_plan
from .query import (query_entities as query_entities, _QUERY_FIELDS, _validate_handles)


_UNIT_CODES = {"unitless": 0, "in": 1, "ft": 2, "mm": 4, "cm": 5, "m": 6}
_UNIT_NAMES = {value: key for key, value in _UNIT_CODES.items()}
_DXF_VERSIONS = {"R2010": "AC1024", "R2013": "AC1027", "R2018": "AC1032"}
_BOUND_WARNING = "Bounds are approximate geometric extents; text extents depend on available fonts."


def _units(doc):
    code = int(doc.header.get("$INSUNITS", 0))
    return {"name": _UNIT_NAMES.get(code, "other"), "code": code}


def _layout(doc, name):
    if not isinstance(name, str) or not name:
        raise CadError("INVALID_ARGUMENT", "Layout must be a nonempty name.")
    try:
        return doc.layouts.get(name)
    except KeyError as exc:
        raise CadError(
            "INVALID_ARGUMENT",
            "Layout does not exist.",
            {"layout": name, "available": list(doc.layouts.names())},
        ) from exc


def _point(value):
    return [float(v) for v in Vec3(value)]


def _bounds(entity):
    try:
        extent = ezbbox.extents([entity], fast=False)
        if extent.has_data:
            return {"min": _point(extent.extmin), "max": _point(extent.extmax)}
    except Exception:
        # Unsupported bounds must not hide an otherwise queryable entity.
        pass
    return None


def _drawing_bounds(entities):
    result = None
    missing = 0
    for entity in entities:
        extent = _bounds(entity)
        if extent is None:
            missing += 1
        elif result is None:
            result = extent
        else:
            for axis in range(3):
                result["min"][axis] = min(result["min"][axis], extent["min"][axis])
                result["max"][axis] = max(result["max"][axis], extent["max"][axis])
    return result, missing


def _base_record(entity):
    return {
        "handle": entity.dxf.handle,
        "type": entity.dxftype(),
        "layer": entity.dxf.get("layer", "0"),
        "color": entity.dxf.get("color", 256),
    }


# Field projection is evaluated before walking vertices, text formatting, or bounds.
_SUMMARY_FIELDS = {
    "handle", "type", "layer", "color", "start", "end", "center", "radius", "insert",
    "closed", "point_count", "text_length", "name", "coordinate_system",
}


def _record(entity, fields=None, detail="full", vertex_offset=0, vertex_limit=None):
    wanted = set(fields) if fields is not None else (
        _SUMMARY_FIELDS if detail == "summary" else _QUERY_FIELDS
    )
    record = {}

    def put(name, value):
        if name in wanted:
            record[name] = value() if callable(value) else value

    for name, value in _base_record(entity).items():
        put(name, value)
    kind = entity.dxftype()
    if kind == "LINE":
        put("start", lambda: _point(entity.dxf.start))
        put("end", lambda: _point(entity.dxf.end))
        put("coordinate_system", "WCS")
    elif kind == "3DFACE":
        put("vertices", lambda: [_point(entity.dxf.get(f"vtx{i}")) for i in range(4)])
        put("coordinate_system", "WCS")
        put("invisible_edges", lambda: int(entity.dxf.get("invisible_edges", 0)))
    elif kind in {"CIRCLE", "ARC"}:
        put("center", lambda: _point(entity.dxf.center))
        put("radius", lambda: float(entity.dxf.radius))
        put("extrusion", lambda: _point(entity.dxf.extrusion))
        put("coordinate_system", "OCS")
        if kind == "ARC":
            put("start_angle", lambda: float(entity.dxf.start_angle))
            put("end_angle", lambda: float(entity.dxf.end_angle))
    elif kind in {"LWPOLYLINE", "POLYLINE"}:
        lw = kind == "LWPOLYLINE"
        total = len(entity) if lw else len(entity.vertices)
        put("point_count", total)
        put("closed", bool(entity.closed if lw else entity.is_closed))
        put("coordinate_system", "OCS" if lw or entity.is_2d_polyline else "WCS")
        if lw:
            put("point_format", "x,y,start_width,end_width,bulge")
            put("elevation", lambda: float(entity.dxf.elevation))
            put("extrusion", lambda: _point(entity.dxf.extrusion))
        else:
            put("is_2d", bool(entity.is_2d_polyline))
            put("is_3d", bool(entity.is_3d_polyline))
            if entity.is_2d_polyline:
                put("elevation", lambda: _point(entity.dxf.elevation))
                put("extrusion", lambda: _point(entity.dxf.extrusion))
        if "points" in wanted or (not lw and "bulges" in wanted):
            stop = None if vertex_limit is None else vertex_offset + vertex_limit
            # get_points() eagerly duplicates the whole LWPOLYLINE. Iteration keeps
            # a 100k-vertex entity bounded when only a vertex page was requested.
            vertices = islice(iter(entity) if lw else iter(entity.vertices), vertex_offset, stop)
            if lw:
                put("points", lambda: [[float(v) for v in point] for point in vertices])
            else:
                chosen = list(vertices)
                put("points", lambda: [_point(v.dxf.location) for v in chosen])
                put("bulges", lambda: [float(v.dxf.bulge) for v in chosen])
            if vertex_limit is not None:
                returned = min(max(total - vertex_offset, 0), vertex_limit)
                record["geometry_page"] = {
                    "offset": vertex_offset, "returned": returned, "total": total,
                    "has_more": vertex_offset + returned < total,
                    "next_offset": vertex_offset + returned if vertex_offset + returned < total else None,
                }
    elif kind in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
        raw = entity.text if kind == "MTEXT" else entity.dxf.text
        put("text_length", len(raw))
        put("text", raw)
        put("plain_text", entity.plain_text)
        put("insert", lambda: _point(entity.dxf.insert))
        put("coordinate_system", "WCS" if kind == "MTEXT" else "OCS")
        if kind != "MTEXT":
            put("height", lambda: float(entity.dxf.height))
            put("rotation", lambda: float(entity.dxf.rotation))
            put("extrusion", lambda: _point(entity.dxf.extrusion))
            if kind in {"ATTRIB", "ATTDEF"}:
                put("tag", lambda: entity.dxf.tag)
        else:
            put("char_height", lambda: float(entity.dxf.char_height))
            put("width", lambda: float(entity.dxf.get("width", 0) or 0))
    elif kind == "INSERT":
        put("name", lambda: entity.dxf.name)
        put("insert", lambda: _point(entity.dxf.insert))
        put("rotation", lambda: float(entity.dxf.rotation))
        for axis in ("xscale", "yscale", "zscale"):
            put(axis, lambda axis=axis: float(entity.dxf.get(axis, 1)))
        put("coordinate_system", "OCS")
        put("extrusion", lambda: _point(entity.dxf.extrusion))
        put("attribute_count", lambda: len(entity.attribs))
        if "attributes" in wanted:
            attributes = [
                _record(attribute, fields=["handle", "type", "tag", "text", "plain_text", "insert", "height"],
                        vertex_limit=128)
                for attribute in islice(entity.attribs, 128)
            ]
            record["attributes"] = attributes
            record["attribute_count"] = len(entity.attribs)
            capped = len(attributes) != len(entity.attribs) or any(a.get("truncation") for a in attributes)
            undecoded = any(a.get("text_encoding", {}).get("encoding_complete") is False for a in attributes)
            record["attribute_complete"] = not capped and not undecoded
            if not record["attribute_complete"]:
                record.setdefault("truncation", {})["attributes"] = {
                    "returned": len(attributes), "total": len(entity.attribs), "complete": False,
                    "reason": "annotation_cap_and_text_encoding" if capped and undecoded else "annotation_cap" if capped else "text_encoding",
                    "hint": ("At most 128 attached attributes and 1024 characters per string are exposed; inspect the source for remaining evidence. " if capped else "")
                            + ("Attached attribute text is not fully decoded; inspect child text_encoding. Source bytes are unchanged." if undecoded else ""),
                }
        elif "attribute_complete" in wanted:
            # No attributes were requested; their detail cannot be called complete.
            record["attribute_complete"] = not entity.attribs
        block_fields = {
            "block_base_point", "block_entity_count", "matrix44", "matrix44_convention",
            "block_entities", "block_complete", "block_coordinate_system", "block_omissions",
            "block_omission_count", "block_omissions_complete",
        }
        if block_fields & wanted:
            try:
                block = entity.block()
            except (AttributeError, KeyError, TypeError, ValueError):
                block = None
            total = len(block) if block is not None else None
            put("block_base_point", lambda: _point(block.block.dxf.base_point) if block is not None else None)
            put("block_entity_count", total)
            put("matrix44_convention", "row_vector: [x,y,z,1] @ matrix44; block definition Cartesian coordinates to drawing WCS; includes block base point, scale, rotation, OCS and translation.")
            put("block_coordinate_system", "Native block definition coordinates. Child WCS/OCS are relative to the definition, not the drawing; convert child OCS to definition Cartesian coordinates before applying matrix44.")
            if "matrix44" in wanted:
                try:
                    record["matrix44"] = [list(row) for row in entity.matrix44().rows()] if block is not None else None
                except (AttributeError, KeyError, TypeError, ValueError, ArithmeticError):
                    record["matrix44"] = None
                    record.setdefault("warnings", []).append("Block transformation is unavailable or invalid.")
                if entity.mcount > 1:
                    record.setdefault("warnings", []).append("MINSERT matrix44 describes the first grid cell only; additional grid instances are not expanded.")
            # Only an explicit field selection can inspect first-level children.
            # detail=full still returns a native INSERT without block expansion.
            if fields is not None and "block_entities" in wanted:
                children = []
                omissions = []
                omission_count = 0

                def omit(handle, reason):
                    nonlocal omission_count
                    omission_count += 1
                    if len(omissions) < 8:
                        omissions.append({"handle": handle, "reason": reason})

                supported = {"LINE", "3DFACE", "CIRCLE", "ARC", "LWPOLYLINE", "POLYLINE",
                             "TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION", "INSERT"}
                child_fields = sorted(_QUERY_FIELDS - block_fields - {"bounds", "bounds_kind"})
                if block is None:
                    omit(None, "Block definition is unavailable.")
                else:
                    if block.block.is_xref or block.block.is_xref_overlay:
                        omit(None, "External block reference; external drawing content is not resolved.")
                    if entity.mcount > 1:
                        omit(None, "MINSERT grid instances are not expanded; matrix44 describes the first grid cell only.")
                    for child in islice(block, 128):
                        child_kind = child.dxftype()
                        if child_kind == "INSERT":
                            child_record = _record(child, fields=[
                                "handle", "type", "layer", "color", "name", "insert", "rotation",
                                "xscale", "yscale", "zscale", "extrusion", "coordinate_system",
                            ], vertex_limit=128)
                            omit(child.dxf.handle, "Nested INSERT retained as native reference; its definition is not expanded.")
                        else:
                            child_record = _record(child, fields=child_fields, vertex_limit=128)
                            if child_kind not in supported:
                                omit(child.dxf.handle, f"Unsupported child geometry: {child_kind}.")
                            elif child_record.get("truncation") or child_record.get("geometry_page", {}).get("has_more"):
                                omit(child.dxf.handle, "Child native detail is truncated; inspect its truncation/geometry_page.")
                            elif child_record.get("text_encoding"):
                                omit(child.dxf.handle, "Child contains undecodable text; inspect its text_encoding evidence.")
                        children.append(child_record)
                    if len(children) < total:
                        omit(None, "Only the first 128 native block children are exposed; child pagination is unavailable.")
                record.update(block_entities=children, block_entity_count=total,
                              block_complete=not omission_count,
                              block_omissions=omissions, block_omission_count=omission_count,
                              block_omissions_complete=omission_count == len(omissions))
                record["block_coordinate_system"] = "Native block definition coordinates. Child WCS/OCS are relative to the definition, not the drawing; convert child OCS to definition Cartesian coordinates before applying matrix44."
            elif "block_complete" in wanted:
                record["block_complete"] = block is not None and total == 0
                if not record["block_complete"]:
                    record["block_omissions"] = [{"handle": None, "reason": "Block child detail was not explicitly requested or its definition is unavailable."}]
                    record["block_omission_count"] = 1
                    record["block_omissions_complete"] = True
    elif kind == "DIMENSION":
        # get_measurement() is a geometric value, not rendered dimension text.
        # ezdxf returns unscaled linear WCS units, degrees for angular types,
        # and a WCS displacement vector for ordinate dimensions.
        warnings = []

        def optional(name, transform=None):
            try:
                value = entity.dxf.get(name)
                value = transform(value) if value is not None and transform else value
                numbers = value if isinstance(value, list) else [value]
                if any(isinstance(v, float) and not math.isfinite(v) for v in numbers):
                    raise ValueError()
                return value
            except (AttributeError, TypeError, ValueError, ArithmeticError):
                warnings.append(f"Dimension field {name} is unavailable or invalid.")
                return None

        dimtype = int(entity.dimtype)
        put("dimension_type", dimtype)
        put("dimension_type_raw", lambda: int(entity.dxf.dimtype))
        for name in ("defpoint", "defpoint2", "defpoint3", "defpoint4", "defpoint5"):
            put(name, lambda name=name: optional(name, _point))
        put("coordinate_system", "WCS")
        put("defpoint5_coordinate_system", "OCS")
        put("actual_measurement", lambda: optional("actual_measurement", float))
        put("text_override", lambda: optional("text"))
        put("dimstyle", lambda: optional("dimstyle"))
        put("geometry_block", lambda: optional("geometry"))
        put("angle", lambda: optional("angle", float))
        if "dimlfac" in wanted:
            try:
                factor = float(entity.override().get("dimlfac", 1.0))
                if not math.isfinite(factor):
                    raise ValueError()
                record["dimlfac"] = factor
            except (AttributeError, TypeError, ValueError, ArithmeticError):
                record["dimlfac"] = None
                warnings.append("Effective dimlfac could not be resolved from the dimension style and overrides.")
        if {"measurement", "measure_kind"} & wanted:
            kinds = {0: "linear_distance", 1: "linear_distance", 2: "angular_degrees",
                     3: "diameter", 4: "radius", 5: "angular_degrees", 6: "ordinate_vector_wcs"}
            required = {0: ("defpoint2", "defpoint3"), 1: ("defpoint2", "defpoint3"),
                        2: ("defpoint", "defpoint2", "defpoint3", "defpoint4"),
                        3: ("defpoint", "defpoint4"), 4: ("defpoint", "defpoint4"),
                        5: ("defpoint2", "defpoint3", "defpoint4"), 6: ("defpoint", "defpoint2")}
            measurement = None
            measure_kind = kinds.get(dimtype, "unsupported")
            try:
                if dimtype not in required or any(not entity.dxf.hasattr(name) for name in required[dimtype]):
                    raise ValueError()
                value = entity.get_measurement()
                measurement = _point(value) if isinstance(value, Vec3) else float(value)
                values = measurement if isinstance(measurement, list) else [measurement]
                if not all(math.isfinite(v) for v in values):
                    measurement = None
                    raise ValueError()
            except (AttributeError, TypeError, ValueError, ArithmeticError):
                warnings.append("Geometry measurement is unavailable: unsupported type, missing definition points, or degenerate geometry.")
                if dimtype in kinds:
                    measure_kind = "unavailable"
            put("measurement", measurement)
            put("measure_kind", measure_kind)
        if {"measurement", "measure_kind", "actual_measurement", "dimlfac", "text_override"} & wanted:
            warnings.append("measurement is derived from definition geometry without dimlfac scaling; actual_measurement is stored group 42 and may be stale. text_override and style formatting can differ from both; rendered display text is not evaluated.")
        if warnings:
            record["warnings"] = warnings
    if "bounds" in wanted or "bounds_kind" in wanted:
        extent = None if kind == "INSERT" else _bounds(entity)
        put("bounds", extent)
        put("bounds_kind", "approximate" if extent else "unavailable")
    if vertex_limit is not None:
        # Full text lives in the source. Query output always has an explicit cap.
        for key, value in list(record.items()):
            if isinstance(value, str) and len(value) > 1024:
                record[key] = value[:1024]
                record.setdefault("truncation", {})[key] = {
                    "returned_characters": 1024, "total_characters": len(value),
                    "complete": False,
                }
    return jsonable(record)


def inspect_drawing(path, layout="Model", include_bounds=True):
    import re
    from .io import file_sha256
    before_sha = file_sha256(path) if Path(path).is_file() else None
    doc = read_document(path)
    space = _layout(doc, layout)
    entities = space
    bounds, missing = _drawing_bounds(entities) if include_bounds else (None, 0)
    warnings = [_BOUND_WARNING] if include_bounds and len(space) else []
    if missing:
        warnings.append(
            f"Bounds unavailable for {missing} entities; drawing bounds may be incomplete."
        )
    if file_sha256(path) != before_sha:
        raise CadError("SOURCE_CHANGED", "Source changed while drawing information was being read.")
    references = Counter(e.dxf.name for e in space if e.dxftype() == "INSERT")
    definitions = []
    for block in doc.blocks:
        if len(definitions) < 128:
            definitions.append({"name": block.name, "entity_count": len(block),
                                "native_reference_count": references[block.name],
                                "external_reference": bool(block.block.is_xref)})
    unit_annotations = []
    unit_pattern = re.compile(r"(?:单位|尺寸|标高).{0,40}(?:毫米|厘米|米|mm|cm|m)|\b(?:millimetres?|millimeters?|centimetres?|meters?|metres?)\b", re.I)
    unit_matches = 0
    for entity in space:
        if entity.dxftype() not in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            continue
        label = entity.plain_text()
        if unit_pattern.search(label):
            unit_matches += 1
            if len(unit_annotations) < 16:
                unit_annotations.append({"handle": entity.dxf.handle, "layer": entity.dxf.layer,
                                         "text": label[:240], "text_complete": len(label) <= 240})
    return {
        "path": str(Path(path).expanduser().resolve()),
        "source_sha256": before_sha,
        "dxf_version": doc.dxfversion,
        "units": _units(doc),
        "unit_evidence": {"metadata": "$INSUNITS", "annotations": unit_annotations,
                          "annotation_count": unit_matches, "complete": unit_matches <= 16,
                          "interpretation": "Annotation candidates are evidence for review; physical units are never inferred or rescaled automatically."},
        "layout": space.name,
        "layouts": list(doc.layouts.names()),
        "entity_count": len(entities),
        "entity_types": dict(sorted(Counter(e.dxftype() for e in entities).items())),
        "entity_layers": dict(sorted(Counter(e.dxf.get("layer", "0") for e in entities).items())),
        "blocks": {"definition_count": len(doc.blocks), "native_reference_count": sum(references.values()),
                   "definitions": definitions, "definitions_complete": len(doc.blocks) <= 128,
                   "scope": "native layout references; nested references are not counted as native instances"},
        "layers": [
            {
                "name": layer.dxf.name,
                "color": int(layer.dxf.color),
                "off": bool(layer.is_off()),
                "frozen": bool(layer.is_frozen()),
                "linetype": layer.dxf.get("linetype", "Continuous"),
                "lineweight": int(layer.dxf.get("lineweight", -3)),
                "plot": bool(layer.dxf.get("plot", 1)),
            }
            for layer in doc.layers
        ],
        "bounds": bounds,
        "bounds_kind": ("approximate" if bounds else "unavailable") if include_bounds else "not_requested",
        "bounds_included": bool(include_bounds),
        "warnings": warnings,
    }







def export_entities(
    path, output, layout="Model", types=None, layer=None, handles=None, fields=None,
    max_entities=100000, max_bytes=64 * 1024 * 1024, overwrite=False,
    layers=None, exclude_layers=None, bbox=None, expected_source_revision=None,
):
    """Write complete selected native entity records as bounded, atomic JSONL.

    This is the complete-data file boundary. Query pagination and result-file
    output retain their own page/detail caps. Export does not expand INSERTs,
    resolve unsupported entity data, or accumulate all selected rows in memory.
    """
    import hashlib
    import json
    from .io import atomic_output

    if (type(max_entities) is not int or not 1 <= max_entities <= 1000000
            or type(max_bytes) is not int or not 1 <= max_bytes <= 1024 * 1024 * 1024):
        raise CadError("INVALID_ARGUMENT", "Export limits must be 1..1000000 entities and 1..1073741824 bytes.")
    if type(overwrite) is not bool:
        raise CadError("INVALID_ARGUMENT", "Overwrite must be boolean.")
    if types is not None and (
        not isinstance(types, list) or any(not isinstance(kind, str) or not kind for kind in types)
    ):
        raise CadError("INVALID_ARGUMENT", "Types must be a list of entity names.")
    if layer is not None and not isinstance(layer, str):
        raise CadError("INVALID_ARGUMENT", "Layer must be a string.")
    if fields is not None and (
        not isinstance(fields, list) or not fields
        or any(not isinstance(field, str) or field not in _QUERY_FIELDS for field in fields)
    ):
        raise CadError("INVALID_ARGUMENT", "Fields must be known entity field names.",
                       {"available": sorted(_QUERY_FIELDS)})
    selected_handles = None if handles is None else set(_validate_handles(handles, allow_empty=True))
    selected_types = None if types is None else {kind.upper() for kind in types}
    for value in (layers, exclude_layers):
        if value is not None and (not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value)):
            raise CadError("INVALID_ARGUMENT", "Layers must be lists of nonempty layer names.")
    selection = None
    target = Path(output).expanduser().resolve()
    if target.suffix.lower() not in {".jsonl", ".ndjson"}:
        raise CadError("UNSUPPORTED_FORMAT", "Entity export requires .jsonl or .ndjson output.")
    count = size = 0
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with revision_guard(path, target):
        # Atomic output checks no-clobber before opening/parsing the input.
        with atomic_output(target, overwrite=overwrite) as temporary:
            if bbox is not None or layers is not None or exclude_layers is not None or expected_source_revision is not None:
                from .spatial import select_handles
                selection = select_handles(path, layout=layout, types=types, layer=layer, handles=handles,
                                           layers=layers, exclude_layers=exclude_layers, bbox=bbox,
                                           expected_source_revision=expected_source_revision)
                selected_handles = set(selection["handles"])
            doc = read_document(path)
            space = _layout(doc, layout)
            export_bounds = None
            if fields is None or set(fields) & {"bounds", "bounds_kind", "world_bbox"}:
                if selection is not None:
                    export_bounds = {row["handle"]: row["world_bbox"] for row in selection["entities"]}
                else:
                    from .spatial import entity_bounds
                    native_bounded, _, _ = entity_bounds(space)
                    export_bounds = {entity.dxf.handle: extent for entity, extent in native_bounded}
            with temporary.open("wb") as stream:
                for entity in space:
                    kind = entity.dxftype()
                    if selected_types is not None and kind not in selected_types:
                        continue
                    if layer is not None and entity.dxf.get("layer", "0") != layer:
                        continue
                    if selected_handles is not None and entity.dxf.handle.upper() not in selected_handles:
                        continue
                    if count >= max_entities:
                        raise CadError("EXPORT_ENTITY_LIMIT", "Selected entities exceed the export entity budget.",
                                       {"max_entities": max_entities, "hint": "Narrow types/layer/handles or increase max_entities."})
                    # A conservative lower bound prevents copying a massive
                    # vertex array when even its shortest encoding cannot fit.
                    needs_points = fields is None or "points" in fields
                    if needs_points and kind in {"LWPOLYLINE", "POLYLINE"}:
                        points = len(entity) if kind == "LWPOLYLINE" else len(entity.vertices)
                        minimum = points * (21 if kind == "LWPOLYLINE" else 13)
                        if minimum > max_bytes - size:
                            raise CadError("EXPORT_BYTE_LIMIT", "A complete entity cannot fit the remaining export byte budget.",
                                           {"max_bytes": max_bytes, "entity_handle": entity.dxf.handle,
                                            "minimum_geometry_bytes": minimum})
                    record = _record(entity, fields=fields, detail="full", vertex_limit=None)
                    if export_bounds is not None:
                        extent = export_bounds.get(entity.dxf.handle)
                        if fields is None or "bounds" in fields:
                            record["bounds"] = {"min": extent[:2], "max": extent[2:]} if extent else None
                        if fields is None or "bounds_kind" in fields:
                            record["bounds_kind"] = "conservative_wcs_2d" if extent else "unavailable"
                    if fields is not None and set(fields) & {"entity_key", "instance_path", "root_handle", "scope", "visible", "world_bbox"}:
                        from .spatial import entity_visible
                        identity = {"entity_key": entity.dxf.handle, "instance_path": [],
                                    "root_handle": entity.dxf.handle, "scope": "layout", "visible": entity_visible(entity)}
                        if "world_bbox" in fields:
                            identity["world_bbox"] = export_bounds.get(entity.dxf.handle)
                        record.update({key: value for key, value in identity.items() if key in fields})
                    for chunk in encoder.iterencode(record):
                        block = chunk.encode("utf-8")
                        if size + len(block) + 1 > max_bytes:
                            raise CadError("EXPORT_BYTE_LIMIT", "Complete entity data exceeds the export byte budget.",
                                           {"max_bytes": max_bytes, "entity_handle": entity.dxf.handle,
                                            "hint": "Narrow fields/selection or increase max_bytes."})
                        stream.write(block)
                        digest.update(block)
                        size += len(block)
                    stream.write(b"\n")
                    digest.update(b"\n")
                    size += 1
                    count += 1
                if count == 0:
                    raise CadError("EMPTY_SELECTION", "No native entities match the export selection; no file was published.")
    result = {"output": str(target), "entity_count": count, "bytes": size,
            "sha256": digest.hexdigest(), "format": "jsonl", "layout": layout,
            "warnings": ["Native supported query records only; INSERTs and unsupported entity geometry are not expanded."]}
    if selection is not None:
        result["selection"] = {key: value for key, value in selection.items() if key not in {"handles", "entities"}}
    return result




def _selected_entities(doc, space, handles):
    handles = _validate_handles(handles)
    # Native handle lookup is O(selected handles), not a second full layout scan.
    owner = space.layout_key
    available = {}
    for handle in handles:
        entity = doc.entitydb.get(handle)
        if entity is not None and entity.is_alive and entity.dxf.get("owner") == owner:
            available[handle] = entity
    missing = [handle for handle in handles if handle not in available]
    if missing:
        raise CadError(
            "ENTITY_NOT_FOUND",
            "Handles are absent from the selected layout.",
            {"handles": missing, "layout": space.name},
        )
    return [available[handle] for handle in handles]


def _polyline_measure(points, bulges, closed):
    repeated_endpoint = not closed and len(points) >= 4 and points[0][:2] == points[-1][:2]
    closed = closed or repeated_endpoint
    lengths = []
    areas = []
    origin_x, origin_y = points[0][:2] if points else (0.0, 0.0)
    count = len(points) if closed else max(len(points) - 1, 0)
    for index in range(count):
        start, end = points[index], points[(index + 1) % len(points)]
        chord = math.hypot(end[0] - start[0], end[1] - start[1])
        bulge = bulges[index]
        # Local coordinates avoid cancellation for small objects far from the origin.
        start_x, start_y = start[0] - origin_x, start[1] - origin_y
        end_x, end_y = end[0] - origin_x, end[1] - origin_y
        areas.append((start_x * end_y - end_x * start_y) / 2)
        if bulge and chord:
            theta = 4 * math.atan(bulge)
            radius = chord * (1 + bulge * bulge) / (4 * abs(bulge))
            lengths.append(abs(theta) * radius)
            areas.append(radius * radius * (theta - math.sin(theta)) / 2)
        else:
            lengths.append(chord)
    length = math.fsum(lengths)
    signed_area = math.fsum(areas)
    result = {"length": length, "length_kind": "exact_centerline", "closed": closed}
    if closed:
        result.update(
            area=abs(signed_area),
            signed_area=signed_area,
            area_kind="absolute_algebraic_area_in_entity_plane",
        )
        result["warnings"] = [
            "Area is the individual entity integral; overlaps with other entities are counted separately."
        ]
        if repeated_endpoint:
            result["warnings"].append("Closure uses exactly repeated first and last XY coordinates; no gap tolerance was applied.")
    else:
        result.update(area=None, area_kind="not_defined_for_open_polyline")
    return result


def _measure(entity):
    result = _base_record(entity)
    kind = entity.dxftype()
    result["supported"] = True
    if kind == "LINE":
        result.update(
            length=(Vec3(entity.dxf.end) - Vec3(entity.dxf.start)).magnitude,
            length_kind="exact_3d",
            area=None,
            area_kind="not_defined_for_line",
        )
    elif kind in {"CIRCLE", "ARC"}:
        radius = float(entity.dxf.radius)
        if radius <= 0:
            raise CadError(
                "INVALID_DXF",
                "Circle or arc has a nonpositive radius.",
                {"handle": entity.dxf.handle},
            )
        if kind == "CIRCLE":
            result.update(
                radius=radius,
                length=math.tau * radius,
                length_kind="exact",
                area=math.pi * radius * radius,
                area_kind="exact_disk_in_entity_plane",
            )
        else:
            sweep = arc_angle_span_deg(entity.dxf.start_angle, entity.dxf.end_angle)
            result.update(
                radius=radius,
                sweep_degrees=sweep,
                length=math.radians(sweep) * radius,
                length_kind="exact",
                area=None,
                area_kind="not_defined_for_open_arc",
            )
    elif kind == "LWPOLYLINE":
        points = list(entity.get_points("xyb"))
        result.update(_polyline_measure(points, [p[2] for p in points], bool(entity.closed)))
        if entity.has_width:
            result.setdefault("warnings", []).append(
                "Polyline width is excluded; length and area use its centerline."
            )
    elif kind == "POLYLINE" and entity.is_2d_polyline:
        points = [_point(v.dxf.location) for v in entity.vertices]
        elevation = float(entity.dxf.elevation.z)
        if any(abs(p[2]) > 1e-12 and abs(p[2] - elevation) > 1e-12 for p in points):
            result.update(
                supported=False,
                length=None,
                area=None,
                reason="2D POLYLINE has nonzero vertex Z coordinates; planar interpretation is ambiguous.",
            )
        else:
            result.update(
                _polyline_measure(
                    points, [float(v.dxf.bulge) for v in entity.vertices], bool(entity.is_closed)
                )
            )
    elif kind == "POLYLINE" and entity.is_3d_polyline:
        points = [Vec3(v.dxf.location) for v in entity.vertices]
        pairs = list(zip(points, points[1:]))
        if entity.is_closed and len(points) > 1:
            pairs.append((points[-1], points[0]))
        result.update(
            length=sum((b - a).magnitude for a, b in pairs),
            length_kind="exact_3d",
            closed=bool(entity.is_closed),
            area=None,
            area_kind="unsupported_3d_polyline_area",
            warnings=[
                "3D polyline area is unsupported; no projection or planarity assumption is made."
            ],
        )
    else:
        result.update(
            supported=False,
            length=None,
            area=None,
            reason=f"Measurement is unsupported for {kind}.",
        )
    from .geometry import measurement_extension
    extension = measurement_extension(entity)
    if extension is not None:
        supplement = dict(extension)
        extra_warnings = supplement.pop("warnings", [])
        result.update(supplement)
        if extra_warnings:
            result.setdefault("warnings", []).extend(extra_warnings)
        if result.get("supported") and "reason" in result:
            del result["reason"]
    return jsonable(result)


def measure_entities(path, handles=None, layout="Model", types=None, layer=None,
                     layers=None, exclude_layers=None, bbox=None, max_entities=100000,
                     expected_source_revision=None):
    if type(max_entities) is not int or not 1 <= max_entities <= 1000000:
        raise CadError("INVALID_ARGUMENT", "max_entities must be 1..1000000.")
    from .query_cache import _source
    source_revision = _source(path)[1]
    selection = None
    if handles is None or any(value is not None for value in (types, layer, layers, exclude_layers, bbox, expected_source_revision)):
        from .spatial import select_handles
        selection = select_handles(path, layout=layout, types=types, layer=layer, handles=handles,
                                   layers=layers, exclude_layers=exclude_layers, bbox=bbox,
                                   expected_source_revision=expected_source_revision)
        handles = selection["handles"]
        if not handles:
            raise CadError("EMPTY_SELECTION", "No native entities match the measurement selection.")
    if len(handles) > max_entities:
        raise CadError("MEASURE_ENTITY_LIMIT", "Selection exceeds measurement entity budget.",
                       {"max_entities": max_entities, "selected_count": len(handles)})
    doc = read_document(path)
    space = _layout(doc, layout)
    measurements = [_measure(e) for e in _selected_entities(doc, space, handles)]
    incomplete = [item["handle"] for item in measurements if item.get("length") is None]
    invalid_areas = [item["handle"] for item in measurements if item.get("area_kind") == "invalid_boundary"
                     or item.get("closed") and item.get("area_valid") is False]
    incomplete_areas = [item["handle"] for item in measurements if item.get("area") is None
                        and (not item.get("supported") or item.get("closed") or item.get("area_kind") in
                             {"unsupported_3d_polyline_area", "invalid_boundary"})]
    result = {
        "measurements": measurements,
        "layout": space.name,
        "units": _units(doc),
        "totals": {
            "length": sum(item.get("length") or 0 for item in measurements),
            "length_complete": not incomplete,
            "area": sum(item.get("area") or 0 for item in measurements),
            "area_kind": "sum_of_available_entity_areas_overlaps_counted_separately",
            "area_entity_count": sum(item.get("area") is not None for item in measurements),
            "area_complete": not incomplete_areas,
            "invalid_area_count": len(invalid_areas),
        },
        "unsupported_handles": incomplete,
        "invalid_area_handles": invalid_areas,
        "incomplete_area_handles": incomplete_areas,
        "warnings": [
            "Lengths use drawing units; areas use squared drawing units. Entity areas are summed without Boolean union."
        ],
    }
    if selection is not None:
        result["selection"] = {key: value for key, value in selection.items() if key not in {"handles", "entities"}}
    if _source(path)[1] != source_revision:
        raise CadError("SOURCE_CHANGED", "Source changed during measurement.")
    return result


def _ensure_layer(doc, name, color=7):
    if name not in doc.layers:
        try:
            doc.layers.add(name, color=color)
        except (ezdxf.DXFError, ValueError) as exc:
            raise CadError(
                "INVALID_PLAN",
                "Invalid layer name or attributes.",
                {"name": name, "reason": str(exc)},
            ) from exc
        return True
    return False


def _add_entity(doc, space, spec):
    attrs = {key: spec[key] for key in ("layer", "color") if key in spec}
    _ensure_layer(doc, attrs.get("layer", "0"))
    kind = spec["type"]
    if kind == "LINE":
        return space.add_line(spec["start"], spec["end"], dxfattribs=attrs)
    if kind == "CIRCLE":
        return space.add_circle(spec["center"], spec["radius"], dxfattribs=attrs)
    if kind == "ARC":
        return space.add_arc(
            spec["center"], spec["radius"], spec["start_angle"], spec["end_angle"], dxfattribs=attrs
        )
    if kind == "LWPOLYLINE":
        return space.add_lwpolyline(
            spec["points"], format="xy", close=spec.get("closed", False), dxfattribs=attrs
        )
    if kind == "TEXT":
        attrs.update(insert=spec["insert"], height=spec["height"], rotation=spec.get("rotation", 0))
        return space.add_text(spec["text"], dxfattribs=attrs)
    if kind == "MTEXT":
        attrs.update(insert=spec["insert"], char_height=spec["char_height"])
        if "width" in spec:
            attrs["width"] = spec["width"]
        return space.add_mtext(spec["text"], dxfattribs=attrs)
    if kind == "RECTANGLE":
        x, y, z = _point(spec["insert"])
        width, height = spec["width"], spec["height"]
        attrs["elevation"] = z
        return space.add_lwpolyline(
            [(x, y), (x + width, y), (x + width, y + height), (x, y + height)],
            close=True,
            dxfattribs=attrs,
        )
    raise CadError("INVALID_PLAN", "Unknown entity type.", {"type": kind})


def _apply_operation(doc, space, operation):
    kind = operation["op"]
    if kind == "layer":
        created = _ensure_layer(doc, operation["name"], operation.get("color", 7))
        if "color" in operation:
            doc.layers.get(operation["name"]).color = operation["color"]
        return {"op": kind, "name": operation["name"], "created": created}
    if kind == "add":
        entity = _add_entity(doc, space, operation["entity"])
        return {"op": kind, "handle": entity.dxf.handle, "type": entity.dxftype()}
    selected = _selected_entities(doc, space, operation["handles"])
    handles = [e.dxf.handle for e in selected]
    if kind == "delete":
        for entity in selected:
            space.delete_entity(entity)
    elif kind == "update":
        attrs = operation["attributes"]
        if "text" in attrs:
            unsupported = [e.dxf.handle for e in selected if e.dxftype() not in {"TEXT", "MTEXT"}]
            if unsupported:
                raise CadError(
                    "UNSUPPORTED_GEOMETRY",
                    "The text attribute is available only for TEXT and MTEXT.",
                    {"handles": unsupported},
                )
        if "layer" in attrs:
            _ensure_layer(doc, attrs["layer"])
        for entity in selected:
            for key, value in attrs.items():
                if key == "text" and entity.dxftype() == "MTEXT":
                    entity.text = value
                else:
                    entity.dxf.set(key, value)
    elif kind == "transform":
        # Row-vector matrices are applied left-to-right: scale, rotate, translate.
        transforms = []
        if "scale" in operation:
            transforms.append(Matrix44.scale(operation["scale"]))
        if "rotate" in operation:
            transforms.append(Matrix44.z_rotate(math.radians(operation["rotate"])))
        if "translate" in operation:
            transforms.append(Matrix44.translate(*_point(operation["translate"])))
        matrix = Matrix44.chain(*transforms)
        for entity in selected:
            try:
                entity.transform(matrix)
            except (
                NotImplementedError,
                AttributeError,
                ezdxf.DXFError,
                ValueError,
                TypeError,
            ) as exc:
                raise CadError(
                    "UNSUPPORTED_GEOMETRY",
                    "Entity cannot be transformed as requested.",
                    {"handle": entity.dxf.handle, "type": entity.dxftype(), "reason": str(exc)},
                ) from exc
    return {"op": kind, "handles": handles}


def apply_plan(
    plan, output, input_path=None, overwrite=False, dry_run=False,
    expected_source_sha256=None, expected_output_sha256=None,
):
    """Serialize the complete read/edit/publish transaction for this destination.

    Expected revisions are optional compare-and-swap preconditions. Even without
    them, cooperating in-place writers read only after taking the destination
    lock, so a second edit starts from the first writer's published document.
    """
    with revision_guard(input_path, output, expected_source_sha256, expected_output_sha256):
        return _apply_plan_unlocked(plan, output, input_path, overwrite, dry_run)


def _apply_plan_unlocked(plan, output, input_path=None, overwrite=False, dry_run=False):
    validate_plan(plan)
    if type(overwrite) is not bool or type(dry_run) is not bool:
        raise CadError("INVALID_ARGUMENT", "Overwrite and dry_run must be booleans.")
    target = Path(output).expanduser().resolve()
    if target.suffix.lower() != ".dxf":
        raise CadError("UNSUPPORTED_FORMAT", "Drawing output must have a .dxf extension.")
    if not target.parent.is_dir():
        raise CadError(
            "DIRECTORY_NOT_FOUND",
            "Output parent directory does not exist.",
            {"path": str(target.parent)},
        )
    if target.exists() and not overwrite:
        raise CadError(
            "OUTPUT_EXISTS", "Output exists; set overwrite explicitly.", {"path": str(target)}
        )
    if input_path is None:
        doc = ezdxf.new(
            plan.get("dxf_version", "R2013"), units=_UNIT_CODES[plan.get("units", "mm")]
        )
    else:
        doc = read_document(input_path)
        if "dxf_version" in plan and _DXF_VERSIONS[plan["dxf_version"]] != doc.dxfversion:
            raise CadError(
                "UNSUPPORTED_VERSION_CHANGE",
                "Changing an existing drawing's DXF version may discard geometry and is disabled.",
                {"source_version": doc.dxfversion, "requested_version": plan["dxf_version"]},
            )
        if "units" in plan:
            doc.units = _UNIT_CODES[plan["units"]]
    space = doc.modelspace()
    results = []
    for index, operation in enumerate(plan["operations"]):
        try:
            result = _apply_operation(doc, space, operation)
            result["operation_index"] = index
            results.append(result)
        except CadError as exc:
            details = dict(exc.details) if isinstance(exc.details, dict) else {}
            details["operation_index"] = index
            raise CadError(exc.code, exc.message, details) from exc
        except (ezdxf.DXFError, ValueError, TypeError, OverflowError) as exc:
            raise CadError(
                "INVALID_PLAN",
                "Operation could not be applied.",
                {"operation_index": index, "reason": str(exc)},
            ) from exc
    # Check that finite input did not overflow during geometry operations.
    for entity in space:
        try:
            validate_finite_entity(entity, doc.dxfversion)
        except CadError as exc:
            if exc.code == "NON_FINITE_RESULT":
                raise CadError(
                    "INVALID_PLAN",
                    "Geometry operations produced a non-finite result.",
                    {"handle": entity.dxf.handle},
                ) from exc
            raise
    result = {
        "output": str(target),
        "dry_run": dry_run,
        "written": not dry_run,
        "dxf_version": doc.dxfversion,
        "units": _units(doc),
        "layout": "Model",
        "entity_count": len(space),
        "operations": results,
        "summary": {
            "added": sum(r["op"] == "add" for r in results),
            "updated": sum(len(r["handles"]) for r in results if r["op"] == "update"),
            "deleted": sum(len(r["handles"]) for r in results if r["op"] == "delete"),
            "transformed": sum(len(r["handles"]) for r in results if r["op"] == "transform"),
        },
        "warnings": [
            "Setting units changes INSUNITS metadata only; existing geometry is not rescaled."
        ]
        if input_path is not None and "units" in plan
        else [],
    }
    if not dry_run:
        result["output"] = save_document(doc, target, overwrite=overwrite)
    return result
