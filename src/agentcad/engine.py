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
    elif kind in {"TEXT", "MTEXT"}:
        raw = entity.dxf.text if kind == "TEXT" else entity.text
        put("text_length", len(raw))
        put("text", raw)
        put("plain_text", entity.plain_text)
        put("insert", lambda: _point(entity.dxf.insert))
        put("coordinate_system", "OCS" if kind == "TEXT" else "WCS")
        if kind == "TEXT":
            put("height", lambda: float(entity.dxf.height))
            put("rotation", lambda: float(entity.dxf.rotation))
            put("extrusion", lambda: _point(entity.dxf.extrusion))
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
    return {
        "path": str(Path(path).expanduser().resolve()),
        "source_sha256": before_sha,
        "dxf_version": doc.dxfversion,
        "units": _units(doc),
        "layout": space.name,
        "layouts": list(doc.layouts.names()),
        "entity_count": len(entities),
        "entity_types": dict(sorted(Counter(e.dxftype() for e in entities).items())),
        "entity_layers": dict(sorted(Counter(e.dxf.get("layer", "0") for e in entities).items())),
        "layers": [
            {
                "name": layer.dxf.name,
                "color": int(layer.dxf.color),
                "off": bool(layer.is_off()),
                "frozen": bool(layer.is_frozen()),
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
    target = Path(output).expanduser().resolve()
    if target.suffix.lower() not in {".jsonl", ".ndjson"}:
        raise CadError("UNSUPPORTED_FORMAT", "Entity export requires .jsonl or .ndjson output.")
    count = size = 0
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with revision_guard(path, target):
        # Atomic output checks no-clobber before opening/parsing the input.
        with atomic_output(target, overwrite=overwrite) as temporary:
            doc = read_document(path)
            space = _layout(doc, layout)
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
    return {"output": str(target), "entity_count": count, "bytes": size,
            "sha256": digest.hexdigest(), "format": "jsonl", "layout": layout,
            "warnings": ["Native supported query records only; INSERTs and unsupported entity geometry are not expanded."]}




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
            "Closed-polyline area is the absolute signed integral; self-intersections are not resolved into a union area."
        ]
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
    return jsonable(result)


def measure_entities(path, handles, layout="Model"):
    doc = read_document(path)
    space = _layout(doc, layout)
    measurements = [_measure(e) for e in _selected_entities(doc, space, handles)]
    incomplete = [item["handle"] for item in measurements if item.get("length") is None]
    return {
        "measurements": measurements,
        "layout": space.name,
        "units": _units(doc),
        "totals": {
            "length": sum(item.get("length") or 0 for item in measurements),
            "length_complete": not incomplete,
            "area": sum(item.get("area") or 0 for item in measurements),
            "area_kind": "sum_of_available_entity_areas_overlaps_counted_separately",
            "area_entity_count": sum(item.get("area") is not None for item in measurements),
        },
        "unsupported_handles": incomplete,
        "warnings": [
            "Lengths use drawing units; areas use squared drawing units. Entity areas are summed without Boolean union."
        ],
    }


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
