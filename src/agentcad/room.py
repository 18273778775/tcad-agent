# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Explicit, evidence-bearing rectangular-room extraction. No inferred heights."""

import hashlib
import json
import math
from pathlib import Path
import re

from jsonschema import Draft202012Validator

from . import engine
from .errors import CadError
from .io import atomic_output, jsonable, read_document

UNIT_TO_M = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "ft": 0.3048}


def _object(properties, required):
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_STRING = {"type": "string", "minLength": 1, "maxLength": 255}
_POSITIVE = {"type": "number", "exclusiveMinimum": 0}
_SELECTOR = {
    **_object({"layer": _STRING, "handle": {"type": "string", "pattern": "^[0-9a-fA-F]+$"}}, []),
    "anyOf": [{"required": ["layer"]}, {"required": ["handle"]}],
}
_ROLE_NAMES = ["footprint", "height", "wall_thickness", "floor_thickness", "ceiling_thickness"]
ROOM_MAPPING_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Explicit rectangular room role mapping",
    **_object(
        {
            "version": {"const": 1, "type": "integer"},
            "name": _STRING,
            "layout": _STRING,
            "units": {"enum": list(UNIT_TO_M)},
            "axes": {"const": "drawing_x_length_y_width"},
            "height_meaning": {"const": "finished_floor_to_ceiling_underside"},
            "roles": _object({name: _SELECTOR for name in _ROLE_NAMES}, _ROLE_NAMES),
            "openings": {
                "type": "array",
                "maxItems": 100,
                "items": _object(
                    {
                        "id": _STRING,
                        "kind": {"enum": ["door", "window"]},
                        "wall": {"enum": ["south", "north", "west", "east"]},
                        "plan": _SELECTOR,
                        "elevation": _SELECTOR,
                    },
                    ["id", "kind", "wall", "plan", "elevation"],
                ),
            },
            "dimension_labels": _object(
                {name: _SELECTOR for name in ["length", "width", "height"]},
                ["length", "width", "height"],
            ),
            "units_label": _SELECTOR,
        },
        ["version", "name", "units", "axes", "height_meaning", "roles", "openings"],
    ),
}
ROOM_SPEC_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Extracted rectangular room specification",
    **_object(
        {
            "schema_version": {"const": "room-1.0"},
            "name": _STRING,
            "units": {"enum": list(UNIT_TO_M)},
            "drawing_unit_to_m": _POSITIVE,
            "origin": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
            "dimensions": _object(
                {name: _POSITIVE for name in ["length", "width", "height"]},
                ["length", "width", "height"],
            ),
            "construction": _object(
                {
                    name: _POSITIVE
                    for name in ["wall_thickness", "floor_thickness", "ceiling_thickness"]
                },
                ["wall_thickness", "floor_thickness", "ceiling_thickness"],
            ),
            "openings": {
                "type": "array",
                "maxItems": 100,
                "items": _object(
                    {
                        "id": _STRING,
                        "kind": {"enum": ["door", "window"]},
                        "wall": {"enum": ["south", "north", "west", "east"]},
                        "offset": {"type": "number", "minimum": 0},
                        "width": _POSITIVE,
                        "height": _POSITIVE,
                        "sill": {"type": "number", "minimum": 0},
                    },
                    ["id", "kind", "wall", "offset", "width", "height", "sill"],
                ),
            },
            "evidence": {"type": "object"},
            "source": {"type": "object"},
            "checks": {"type": "array"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        [
            "schema_version",
            "name",
            "units",
            "drawing_unit_to_m",
            "origin",
            "dimensions",
            "construction",
            "openings",
        ],
    ),
}


def _validate(schema, value, code):
    try:
        jsonable(value)
    except CadError as exc:
        raise CadError(code, "Room data must contain only finite JSON values.") from exc
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        raise CadError(
            code,
            "Room data does not match the schema.",
            {
                "errors": [
                    {"path": list(error.absolute_path), "message": error.message[:400]}
                    for error in errors[:10]
                ]
            },
        )


def validate_room_spec(spec):
    _validate(ROOM_SPEC_SCHEMA, spec, "INVALID_ROOM_SPEC")
    if not math.isclose(spec["drawing_unit_to_m"], UNIT_TO_M[spec["units"]], rel_tol=1e-12):
        raise CadError("INVALID_ROOM_SPEC", "Drawing unit scale conflicts with the declared unit.")
    identifiers = set()
    for opening in spec["openings"]:
        if opening["id"] in identifiers:
            raise CadError("INVALID_ROOM_SPEC", "Opening identifiers must be unique.")
        identifiers.add(opening["id"])
        span = spec["dimensions"]["length" if opening["wall"] in {"south", "north"} else "width"]
        if (
            opening["offset"] + opening["width"] > span
            or opening["sill"] + opening["height"] > spec["dimensions"]["height"]
        ):
            raise CadError(
                "INVALID_ROOM_SPEC",
                "Opening extends outside the clear wall dimensions.",
                {"opening": opening["id"]},
            )
        if opening["kind"] == "door" and opening["sill"] != 0:
            raise CadError(
                "INVALID_ROOM_SPEC", "This room model requires doors to start at finished floor."
            )
        if (
            opening["offset"] == 0
            and opening["width"] == span
            and opening["sill"] == 0
            and opening["height"] == spec["dimensions"]["height"]
        ):
            raise CadError(
                "INVALID_ROOM_SPEC", "An opening cannot remove the entire wall in this model."
            )
    for wall in ["south", "north", "west", "east"]:
        intervals = sorted(
            (item["offset"], item["offset"] + item["width"])
            for item in spec["openings"]
            if item["wall"] == wall
        )
        if any(right >= next_left for (_, right), (next_left, _) in zip(intervals, intervals[1:])):
            raise CadError(
                "INVALID_ROOM_SPEC",
                "Openings on one wall must be separated by a measurable wall pier.",
                {"wall": wall},
            )
    return spec


def source_identity(path):
    source = Path(path).expanduser().resolve()
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(source),
        "sha256": digest.hexdigest(),
        "format": source.suffix.lower().lstrip("."),
    }


def _rectangle(record, tolerance, role):
    if record["type"] not in {"LWPOLYLINE", "POLYLINE"} or not record.get("closed"):
        raise CadError(
            "UNSUPPORTED_ROOM_GEOMETRY",
            "Room roles require a closed rectangular polyline.",
            {"role": role},
        )
    if record.get("is_3d") or record.get("extrusion", [0, 0, 1]) != [0, 0, 1]:
        raise CadError(
            "UNSUPPORTED_ROOM_GEOMETRY",
            "Only standard-OCS 2D rectangles are supported.",
            {"role": role},
        )
    if record["type"] == "LWPOLYLINE":
        points = [[point[0], point[1]] for point in record["points"]]
        bulges = [point[4] for point in record["points"]]
        elevation = record.get("elevation", 0)
    else:
        points = [[point[0], point[1]] for point in record["points"]]
        bulges = record.get("bulges", [])
        elevation = record.get("elevation", [0, 0, 0])[2]
        if any(abs(point[2]) > tolerance for point in record["points"]):
            raise CadError(
                "UNSUPPORTED_ROOM_GEOMETRY", "2D polyline has nonzero vertex Z.", {"role": role}
            )
    if abs(elevation) > tolerance or any(abs(bulge) > 1e-12 for bulge in bulges):
        raise CadError(
            "UNSUPPORTED_ROOM_GEOMETRY",
            "Room role must have no bulges and lie in Z=0.",
            {"role": role},
        )
    if len(points) == 5 and math.dist(points[0], points[-1]) <= tolerance:
        points.pop()
    if len(points) != 4:
        raise CadError(
            "UNSUPPORTED_ROOM_GEOMETRY",
            "A rectangle must have four corner vertices.",
            {"role": role},
        )
    low = [min(point[axis] for point in points) for axis in [0, 1]]
    high = [max(point[axis] for point in points) for axis in [0, 1]]
    if any(high[axis] - low[axis] <= tolerance for axis in [0, 1]):
        raise CadError("UNSUPPORTED_ROOM_GEOMETRY", "Rectangle is degenerate.", {"role": role})
    expected = [(low[0], low[1]), (high[0], low[1]), (high[0], high[1]), (low[0], high[1])]
    used = set()
    for point in points:
        matches = [
            index for index, corner in enumerate(expected) if math.dist(point, corner) <= tolerance
        ]
        if len(matches) != 1 or matches[0] in used:
            raise CadError(
                "UNSUPPORTED_ROOM_GEOMETRY",
                "Rectangle must be axis-aligned with unique corners.",
                {"role": role},
            )
        used.add(matches[0])
    for start, end in zip(points, points[1:] + points[:1]):
        if min(abs(end[0] - start[0]), abs(end[1] - start[1])) > tolerance:
            raise CadError(
                "UNSUPPORTED_ROOM_GEOMETRY",
                "Diagonal edges are not rectangular room boundaries.",
                {"role": role},
            )
    return low, high


def _line(record, axis, tolerance, role):
    if record["type"] != "LINE":
        raise CadError(
            "UNSUPPORTED_ROOM_GEOMETRY", "Reference role must select one LINE.", {"role": role}
        )
    start, end = record["start"], record["end"]
    if (
        any(abs(end[index] - start[index]) > tolerance for index in range(3) if index != axis)
        or abs(start[2]) > tolerance
    ):
        raise CadError(
            "UNSUPPORTED_ROOM_GEOMETRY",
            "Reference line is not aligned to its declared view axis.",
            {"role": role},
        )
    value = abs(end[axis] - start[axis])
    if value <= tolerance:
        raise CadError("UNSUPPORTED_ROOM_GEOMETRY", "Reference line is degenerate.", {"role": role})
    return value, min(start[axis], end[axis]), max(start[axis], end[axis])


def extract_room(path, mapping, output=None, overwrite=False, source_info=None):
    _validate(ROOM_MAPPING_SCHEMA, mapping, "INVALID_ROOM_MAPPING")
    document = read_document(path)
    space = engine._layout(document, mapping.get("layout", "Model"))
    declared_units = mapping["units"]
    factor = UNIT_TO_M[declared_units]
    tolerance = 0.001 / (factor * 1000)  # 0.001 mm for native CAD geometry.
    native_units = engine._units(document)
    evidence = {
        "interpretation": {
            "axes": mapping["axes"],
            "height_meaning": mapping["height_meaning"],
            "mapping": mapping,
        }
    }
    checks = []
    warnings = [
        "Room/elevation association and layer meanings are explicitly supplied by the mapping.",
        "Axis-aligned rectangular clear rooms only; this is not automatic plan recognition.",
    ]

    def select(selector, role):
        matches = [
            entity
            for entity in space
            if ("layer" not in selector or entity.dxf.get("layer", "0") == selector["layer"])
            and (
                "handle" not in selector or entity.dxf.handle.upper() == selector["handle"].upper()
            )
        ]
        if len(matches) != 1:
            raise CadError(
                "ROOM_EVIDENCE_MISSING" if not matches else "ROOM_EVIDENCE_AMBIGUOUS",
                "Each room role must identify exactly one entity.",
                {"role": role, "count": len(matches), "selector": selector},
            )
        entity = matches[0]
        record = engine._record(entity)
        evidence[role] = {
            "layout": space.name,
            "selector": selector,
            "entity": record,
            "measurement": engine._measure(entity),
        }
        return record

    unit_record = (
        select(mapping["units_label"], "units_label") if "units_label" in mapping else None
    )
    if unit_record is not None:
        declared_text = unit_record.get("plain_text", "").strip()
        if declared_text.lower() != f"units: {declared_units}":
            raise CadError(
                "ROOM_UNITS_MISMATCH", "Unit declaration text conflicts with the mapping."
            )
    if native_units["name"] == "unitless":
        if unit_record is None:
            raise CadError(
                "ROOM_UNITS_MISSING",
                "Unitless drawings require an explicit drawing unit declaration.",
            )
        warnings.append(
            "INSUNITS is unitless; units are taken from the mapped drawing declaration, not inferred from scale."
        )
    elif native_units["name"] != declared_units:
        raise CadError(
            "ROOM_UNITS_MISMATCH",
            "Drawing INSUNITS conflicts with the mapped unit.",
            {"native": native_units, "declared": declared_units},
        )

    roles = {role: select(selector, role) for role, selector in mapping["roles"].items()}
    low, high = _rectangle(roles["footprint"], tolerance, "footprint")
    length, width = high[0] - low[0], high[1] - low[1]
    height, floor_level, ceiling_level = _line(roles["height"], 1, tolerance, "height")
    construction = {}
    for role, axis in [("wall_thickness", 0), ("floor_thickness", 1), ("ceiling_thickness", 1)]:
        value, start, end = _line(roles[role], axis, tolerance, role)
        construction[role] = value
        if role == "floor_thickness" and abs(end - floor_level) > tolerance:
            raise CadError(
                "ROOM_REFERENCE_MISMATCH",
                "Floor slab does not end at the finished floor reference.",
            )
        if role == "ceiling_thickness" and abs(start - ceiling_level) > tolerance:
            raise CadError(
                "ROOM_REFERENCE_MISMATCH",
                "Ceiling slab does not start at the clear height reference.",
            )
    dimensions = {"length": length, "width": width, "height": height}
    footprint_measure = evidence["footprint"]["measurement"]
    if not math.isclose(
        footprint_measure.get("area", -1), length * width, rel_tol=1e-10, abs_tol=tolerance**2
    ):
        raise CadError(
            "ROOM_DIMENSION_MISMATCH", "Native polyline area conflicts with rectangle coordinates."
        )
    checks.append(
        {
            "name": "native_footprint_area",
            "actual": footprint_measure["area"],
            "expected": length * width,
            "passed": True,
        }
    )

    openings = []
    for item in mapping["openings"]:
        role = f"opening:{item['id']}"
        plan = select(item["plan"], role + ":plan")
        elevation = select(item["elevation"], role + ":elevation")
        wall = item["wall"]
        axis = 0 if wall in {"north", "south"} else 1
        opening_width, opening_start, _ = _line(plan, axis, tolerance, role + ":plan")
        wall_coordinate = {"south": low[1], "north": high[1], "west": low[0], "east": high[0]}[wall]
        if abs(plan["start"][1 - axis] - wall_coordinate) > tolerance:
            raise CadError(
                "ROOM_REFERENCE_MISMATCH",
                "Opening plan line does not lie on its declared inner wall.",
                {"opening": item["id"]},
            )
        elow, ehigh = _rectangle(elevation, tolerance, role + ":elevation")
        if abs((ehigh[0] - elow[0]) - opening_width) > tolerance:
            raise CadError(
                "ROOM_DIMENSION_MISMATCH",
                "Opening plan and elevation widths disagree.",
                {"opening": item["id"]},
            )
        sill = elow[1] - floor_level
        if abs(sill) <= tolerance:
            sill = 0.0
        openings.append(
            {
                "id": item["id"],
                "kind": item["kind"],
                "wall": wall,
                "offset": opening_start - low[axis],
                "width": opening_width,
                "height": ehigh[1] - elow[1],
                "sill": sill,
            }
        )
        checks.append(
            {
                "name": role + ":width_plan_vs_elevation",
                "actual": opening_width,
                "expected": ehigh[0] - elow[0],
                "passed": True,
            }
        )

    for axis, selector in mapping.get("dimension_labels", {}).items():
        label = select(selector, "dimension_label:" + axis)
        text = label.get("plain_text", "")
        match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", text)
        units_match = re.search(r"\b(mm|cm|m|in|ft)\b", text.lower())
        if not match or not units_match:
            raise CadError(
                "ROOM_DIMENSION_MISMATCH",
                "Dimension label needs an explicit value and supported unit.",
                {"axis": axis, "text": text},
            )
        label_m = float(match.group()) * UNIT_TO_M[units_match.group()]
        error_mm = abs(label_m - dimensions[axis] * factor) * 1000
        if not math.isfinite(label_m) or error_mm > 0.001:
            raise CadError(
                "ROOM_DIMENSION_MISMATCH",
                "Dimension label disagrees with actual CAD geometry.",
                {
                    "axis": axis,
                    "label": text,
                    "geometry_value": dimensions[axis],
                    "absolute_error_mm": error_mm,
                },
            )
        checks.append(
            {"name": "dimension_label:" + axis, "absolute_error_mm": error_mm, "passed": True}
        )

    spec = {
        "schema_version": "room-1.0",
        "name": mapping["name"],
        "units": declared_units,
        "drawing_unit_to_m": factor,
        "origin": [low[0], low[1], 0.0],
        "dimensions": dimensions,
        "construction": construction,
        "openings": openings,
        "evidence": evidence,
        "source": source_info or source_identity(path),
        "checks": checks,
        "warnings": warnings,
    }
    validate_room_spec(spec)
    result = {"spec": spec, "warnings": warnings.copy()}
    if output is not None:
        if Path(output).suffix.lower() != ".json":
            raise CadError(
                "UNSUPPORTED_FORMAT", "Extracted room specification output must be .json."
            )
        with atomic_output(output, overwrite=overwrite) as temporary:
            temporary.write_text(
                json.dumps(spec, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8"
            )
        result["output"] = str(Path(output).expanduser().resolve())
    return result
