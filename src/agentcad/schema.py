# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""The deliberately small, data-only CAD plan language."""

from __future__ import annotations

import math

from jsonschema import Draft202012Validator

from .errors import CadError


_NUMBER = {"type": "number"}
_POSITIVE = {"type": "number", "exclusiveMinimum": 0}
_VECTOR = {"type": "array", "items": _NUMBER, "minItems": 2, "maxItems": 3}
_POINT2 = {"type": "array", "items": _NUMBER, "minItems": 2, "maxItems": 2}
_NAME = {"type": "string", "minLength": 1, "maxLength": 255}
_COLOR = {"type": "integer", "minimum": 1, "maximum": 255}
_HANDLES = {
    "type": "array",
    "items": {"type": "string", "pattern": "^[0-9A-Fa-f]+$", "maxLength": 32},
    "minItems": 1,
    "maxItems": 10000,
    "uniqueItems": True,
}


def _object(properties, required):
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _entity(kind, properties, required):
    return _object(
        {"type": {"const": kind}, "layer": _NAME, "color": _COLOR, **properties},
        ["type", *required],
    )


PLAN_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "AgentCAD plan version 1",
    **_object(
        {
            "version": {"const": 1, "type": "integer"},
            "units": {"enum": ["mm", "cm", "m", "in", "ft", "unitless"]},
            "dxf_version": {"enum": ["R2010", "R2013", "R2018"]},
            "operations": {
                "type": "array",
                "maxItems": 10000,
                "items": {
                    "oneOf": [
                        _object(
                            {"op": {"const": "layer"}, "name": _NAME, "color": _COLOR},
                            ["op", "name"],
                        ),
                        _object(
                            {
                                "op": {"const": "add"},
                                "entity": {
                                    "oneOf": [
                                        _entity(
                                            "LINE",
                                            {"start": _VECTOR, "end": _VECTOR},
                                            ["start", "end"],
                                        ),
                                        _entity(
                                            "CIRCLE",
                                            {"center": _VECTOR, "radius": _POSITIVE},
                                            ["center", "radius"],
                                        ),
                                        _entity(
                                            "ARC",
                                            {
                                                "center": _VECTOR,
                                                "radius": _POSITIVE,
                                                "start_angle": _NUMBER,
                                                "end_angle": _NUMBER,
                                            },
                                            ["center", "radius", "start_angle", "end_angle"],
                                        ),
                                        _entity(
                                            "LWPOLYLINE",
                                            {
                                                "points": {
                                                    "type": "array",
                                                    "items": _POINT2,
                                                    "minItems": 2,
                                                    "maxItems": 100000,
                                                },
                                                "closed": {"type": "boolean"},
                                            },
                                            ["points"],
                                        ),
                                        _entity(
                                            "TEXT",
                                            {
                                                "text": {"type": "string", "maxLength": 1000000},
                                                "insert": _VECTOR,
                                                "height": _POSITIVE,
                                                "rotation": _NUMBER,
                                            },
                                            ["text", "insert", "height"],
                                        ),
                                        _entity(
                                            "MTEXT",
                                            {
                                                "text": {"type": "string", "maxLength": 1000000},
                                                "insert": _VECTOR,
                                                "char_height": _POSITIVE,
                                                "width": _POSITIVE,
                                            },
                                            ["text", "insert", "char_height"],
                                        ),
                                        _entity(
                                            "RECTANGLE",
                                            {
                                                "insert": _VECTOR,
                                                "width": _POSITIVE,
                                                "height": _POSITIVE,
                                            },
                                            ["insert", "width", "height"],
                                        ),
                                    ]
                                },
                            },
                            ["op", "entity"],
                        ),
                        _object(
                            {
                                "op": {"const": "update"},
                                "handles": _HANDLES,
                                "attributes": {
                                    **_object(
                                        {
                                            "layer": _NAME,
                                            "color": _COLOR,
                                            "text": {"type": "string", "maxLength": 1000000},
                                        },
                                        [],
                                    ),
                                    "minProperties": 1,
                                },
                            },
                            ["op", "handles", "attributes"],
                        ),
                        _object(
                            {"op": {"const": "delete"}, "handles": _HANDLES}, ["op", "handles"]
                        ),
                        {
                            **_object(
                                {
                                    "op": {"const": "transform"},
                                    "handles": _HANDLES,
                                    "translate": _VECTOR,
                                    "rotate": _NUMBER,
                                    "scale": _POSITIVE,
                                },
                                ["op", "handles"],
                            ),
                            "anyOf": [
                                {"required": ["translate"]},
                                {"required": ["rotate"]},
                                {"required": ["scale"]},
                            ],
                        },
                    ]
                },
            },
        },
        ["version", "operations"],
    ),
}


_VALIDATOR = Draft202012Validator(PLAN_SCHEMA)


def _reject_nonfinite(value, path="$"):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise CadError(
                "INVALID_PLAN", "Plan numbers must be finite IEEE-754 values.", {"path": path}
            )
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{path}[{index}]")


def validate_plan(plan):
    """Validate without inserting defaults or modifying the caller's plan."""
    _reject_nonfinite(plan)
    errors = list(_VALIDATOR.iter_errors(plan))
    if errors:
        # A oneOf error's children contain the useful field-level explanation.
        first = errors[0]
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in first.absolute_path
        )
        details = {"path": path, "reason": first.message[:1000]}
        if first.context:
            details["field_errors"] = [
                {"path": "/".join(str(p) for p in item.absolute_path), "reason": item.message[:300]}
                for item in first.context[:12]
            ]
        raise CadError("INVALID_PLAN", "Plan does not match the version 1 schema.", details)
    # JSON Schema's integer comparison can make 1.0 match const:1. The public
    # version discriminator is deliberately an integer, not a bool or float.
    if type(plan["version"]) is not int:
        raise CadError("INVALID_PLAN", "Plan version must be the integer 1.")
    for index, operation in enumerate(plan["operations"]):
        handles = operation.get("handles", [])
        if len({handle.upper() for handle in handles}) != len(handles):
            raise CadError(
                "INVALID_PLAN", "Handles must be unique ignoring case.", {"operation_index": index}
            )
    return plan
