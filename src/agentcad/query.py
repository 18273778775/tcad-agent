# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Lightweight native query contract; warm pages need no CAD-library import."""

import math
from .errors import CadError

_QUERY_FIELDS = {
    "handle", "type", "layer", "color", "start", "end", "coordinate_system", "vertices",
    "invisible_edges", "center", "radius", "extrusion", "start_angle", "end_angle", "points",
    "point_format", "closed", "elevation", "bulges", "is_2d", "is_3d", "text", "insert",
    "height", "rotation", "char_height", "width", "plain_text", "bounds", "bounds_kind",
    "point_count", "text_length", "name", "xscale", "yscale", "zscale", "geometry_page",
    "truncation",
}



def _validate_handles(handles, allow_empty=False):
    if (
        not isinstance(handles, list)
        or (not handles and not allow_empty)
        or any(
            not isinstance(handle, str)
            or not handle
            or any(c not in "0123456789abcdefABCDEF" for c in handle)
            for handle in handles
        )
    ):
        raise CadError("INVALID_ARGUMENT", "Handles must be a list of hexadecimal handle strings.")
    normalized = [handle.upper() for handle in handles]
    if len(set(normalized)) != len(normalized):
        raise CadError("INVALID_ARGUMENT", "Handles must be unique ignoring case.")
    return normalized



def query_entities(
    path, layout="Model", types=None, layer=None, text=None, handles=None, bbox=None,
    offset=0, limit=100, detail="full", fields=None, cursor=None, max_output_bytes=32768,
    vertex_offset=0, vertex_limit=128,
):
    """Query native entities using a bounded, persistent SQLite query snapshot.

    First use of a source or a new projection/filter parses the source once.
    Following pages reuse the prepared snapshot, including across CLI processes.
    INSERTs remain native entities; query does not expand block definitions.
    """
    from .query_cache import query_snapshot

    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 10000:
        raise CadError("INVALID_ARGUMENT", "Offset must be nonnegative and limit 1..10000.")
    if detail not in {"summary", "full"}:
        raise CadError("INVALID_ARGUMENT", "Detail must be summary or full.")
    if fields is not None and (
        not isinstance(fields, list) or not fields
        or any(not isinstance(field, str) or field not in _QUERY_FIELDS for field in fields)
    ):
        raise CadError("INVALID_ARGUMENT", "Fields must be known entity field names.",
                       {"available": sorted(_QUERY_FIELDS)})
    if types is not None and (
        not isinstance(types, list) or any(not isinstance(t, str) or not t for t in types)
    ):
        raise CadError("INVALID_ARGUMENT", "Types must be a list of entity names.")
    if layer is not None and not isinstance(layer, str):
        raise CadError("INVALID_ARGUMENT", "Layer must be a string.")
    if text is not None and not isinstance(text, str):
        raise CadError("INVALID_ARGUMENT", "Text filter must be a string.")
    normalized = None if handles is None else _validate_handles(handles, allow_empty=True)
    if bbox is not None and (
        not isinstance(bbox, (list, tuple)) or len(bbox) != 4
        or any(not _finite_number(v) for v in bbox)
        or bbox[0] > bbox[2] or bbox[1] > bbox[3]
    ):
        raise CadError("INVALID_ARGUMENT", "Bounding box must contain four finite ordered numbers.")
    if type(max_output_bytes) is not int or not 4096 <= max_output_bytes <= 1048576:
        raise CadError("INVALID_ARGUMENT", "max_output_bytes must be 4096..1048576.")
    if (type(vertex_offset) is not int or vertex_offset < 0 or type(vertex_limit) is not int
            or not 1 <= vertex_limit <= 10000):
        raise CadError("INVALID_ARGUMENT", "Vertex offset must be nonnegative and limit 1..10000.")
    if vertex_offset and (normalized is None or len(normalized) != 1):
        raise CadError("INVALID_ARGUMENT", "Vertex pagination requires exactly one handle.")
    if cursor is not None and (not isinstance(cursor, str) or not cursor or offset):
        raise CadError("INVALID_ARGUMENT", "Cursor must be a nonempty string without offset.")
    return query_snapshot(
        path=path, layout=layout, types=None if types is None else sorted({t.upper() for t in types}),
        layer=layer, text=text, handles=normalized, bbox=bbox, offset=offset, limit=limit,
        detail=detail, fields=fields, cursor=cursor, max_output_bytes=max_output_bytes,
        vertex_offset=vertex_offset, vertex_limit=vertex_limit,
    )



def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False
