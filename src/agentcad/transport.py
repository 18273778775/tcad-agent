# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Lightweight strict JSON transport; importing the CLI does not load CAD libraries."""

import json
from .errors import CadError

MAX_JSON_BYTES = 8 * 1024 * 1024

def loads_json(raw):
    if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise CadError("REQUEST_TOO_LARGE", "JSON input exceeds 8 MiB.")

    def reject_constant(value):
        raise CadError("INVALID_JSON", f"Non-finite JSON number is not allowed: {value}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CadError("INVALID_JSON", f"Duplicate object key: {key}")
            result[key] = value
        return result

    try:
        result = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except (ValueError, RecursionError) as exc:
        raise CadError("INVALID_JSON", "Input is not valid JSON.", {"reason": str(exc)}) from exc
    _finite_numbers(result)
    return result


def _finite_numbers(value, depth=0):
    import math

    if depth > 64:
        raise CadError("INVALID_JSON", "JSON nesting exceeds 64 levels.")
    if isinstance(value, str) and any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise CadError("INVALID_JSON", "JSON strings must not contain unpaired Unicode surrogates.")
    if isinstance(value, float) and not math.isfinite(value):
        raise CadError("INVALID_JSON", "JSON numbers must be finite.")
    if isinstance(value, dict):
        for key, item in value.items():
            _finite_numbers(key, depth + 1)
            _finite_numbers(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _finite_numbers(item, depth + 1)
