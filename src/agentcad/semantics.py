# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Evidence-based label association and orthogonal axis grids, with explicit ambiguity."""

from collections import defaultdict
import json
import math
from pathlib import Path
import re

from .errors import CadError
from .io import atomic_output, file_sha256, jsonable, read_document


def _pattern(value, default=None):
    if value is None:
        value = default
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 256:
        raise CadError("INVALID_ARGUMENT", "Patterns must be strings of at most 256 characters.")
    try:
        return re.compile(value)
    except re.error as exc:
        raise CadError(
            "INVALID_PATTERN", "Invalid regular expression.", {"reason": str(exc)}
        ) from exc


def _positive(value, name, allow_zero=False):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or (value < 0 if allow_zero else value <= 0)
    ):
        raise CadError("INVALID_ARGUMENT", f"{name} must be finite and positive.")


def _layers(value):
    if value is None:
        return None
    if isinstance(value, str):
        return {value}
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) or not v for v in value):
        raise CadError("INVALID_ARGUMENT", "Layer selections must be lists of names.")
    return set(value)


def _identity(path, source_info=None):
    identity = source_info or {
        "path": str(Path(path).expanduser().resolve()),
        "sha256": file_sha256(path),
    }
    if not isinstance(identity, dict) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", str(identity.get("sha256", ""))
    ):
        raise CadError("INVALID_ARGUMENT", "Source identity requires a SHA-256 digest.")
    return identity


def _unchanged(path, digest, identity):
    if file_sha256(path) != digest:
        raise CadError("SOURCE_CHANGED", "Drawing changed during semantic analysis.")
    if (
        identity.get("path")
        and Path(identity["path"]).is_file()
        and file_sha256(identity["path"]).lower() != identity["sha256"].lower()
    ):
        raise CadError("SOURCE_CHANGED", "Original source changed during semantic analysis.")


def _axis_ranges(axis):
    return axis.get("ranges", [axis["range"]])


def _range_covers(axis, value, tolerance):
    return any(low - tolerance <= value <= high + tolerance for low, high in _axis_ranges(axis))


def _validate_saved_grid(result):
    if not isinstance(result, dict):
        raise CadError("INVALID_AXES", "Saved axes must be a JSON object.")
    grid = result.get("axes")
    if not isinstance(grid, dict) or set(grid) != {"vertical", "horizontal"}:
        raise CadError("INVALID_AXES", "Saved axes must contain vertical and horizontal arrays.")
    ids = set()
    for orientation in ("vertical", "horizontal"):
        values = grid[orientation]
        if not isinstance(values, list) or len(values) > 1000:
            raise CadError(
                "INVALID_AXES", "Saved axis orientation arrays must contain at most 1000 axes."
            )
        for axis in values:
            if (
                not isinstance(axis, dict)
                or not isinstance(axis.get("id"), str)
                or axis["id"] in ids
                or type(axis.get("position")) not in (int, float)
                or not math.isfinite(axis["position"])
            ):
                raise CadError(
                    "INVALID_AXES",
                    "Saved axes contain an invalid or duplicate axis identity/position.",
                )
            ids.add(axis["id"])
            if "ranges" in axis and not axis["ranges"]:
                raise CadError("INVALID_AXES", "Saved axis ranges must not be empty.")
            intervals = (
                [axis.get("range"), *axis.get("ranges", [])]
                if isinstance(axis.get("ranges", []), list)
                else [None]
            )
            if any(
                not isinstance(interval, (list, tuple))
                or len(interval) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in interval)
                or interval[0] > interval[1]
                for interval in intervals
            ):
                raise CadError("INVALID_AXES", "Saved axes contain an invalid range.")
    return grid


def _anchor(record):
    entity = record["entity"]
    if entity.dxftype() in {"TEXT", "ATTRIB", "ATTDEF"}:
        try:
            return list(entity.ocs().to_wcs(entity.get_placement()[1]))[:2]
        except (AttributeError, ValueError):
            pass
    if record.get("world_insert") is not None:
        return record["world_insert"][:2]
    point = entity.dxf.get("insert")
    return list(point)[:2] if point is not None else None


def _label(record):
    entity = record["entity"]
    value = entity.plain_text() if hasattr(entity, "plain_text") else entity.dxf.get("text", "")
    return str(value)[:2048]


def _evidence(record):
    return {
        "entity_key": record["entity_key"],
        "handle": record["handle"],
        "root_handle": record["root_handle"],
        "instance_path": record["instance_path"],
        "layer": record["layer"],
        "type": record["entity"].dxftype(),
    }


def _publish(result, output, path, overwrite=False, unmatched_output=None, protected_paths=()):
    protected = {Path(v).expanduser().resolve() for v in (path, *protected_paths)}
    if isinstance(result.get("source"), dict) and result["source"].get("path"):
        protected.add(Path(result["source"]["path"]).expanduser().resolve())
    destinations = [Path(v).expanduser().resolve() for v in (output, unmatched_output) if v]
    if len(set(destinations)) != len(destinations) or any(p in protected for p in destinations):
        raise CadError(
            "OUTPUT_CONFLICT",
            "Analysis outputs must be distinct from their input drawing and one another.",
        )
    for target in destinations:
        if target.exists() and not overwrite:
            raise CadError(
                "OUTPUT_EXISTS", "Analysis output already exists.", {"path": str(target)}
            )
    raw = json.dumps(jsonable(result), ensure_ascii=False, indent=2).encode("utf-8")
    if len(raw) > 64 * 1024 * 1024:
        raise CadError(
            "OUTPUT_TOO_LARGE", "Analysis evidence exceeds 64 MiB; narrow the selection."
        )
    if unmatched_output:
        with atomic_output(unmatched_output, overwrite=overwrite) as temporary:
            temporary.write_text(
                json.dumps(
                    jsonable(
                        {
                            "source": result["source"],
                            "unmatched": result.get("unmatched", []),
                            "ambiguous": result.get("ambiguous", []),
                        }
                    ),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
    if output:
        with atomic_output(output, overwrite=overwrite) as temporary:
            temporary.write_bytes(raw)
        return {
            "output": str(Path(output).expanduser().resolve()),
            "sha256": file_sha256(output),
            "source": result["source"],
            "summary": result.get("summary", {}),
            "examples": result.get("pairs", [])[:5],
            "warnings": result.get("warnings", [])[:100],
            "complete_artifact": True,
        }
    # Files retain complete evidence. Inline list bounds also apply recursively
    # to conflict candidates, axis line handles and duplicated labels.
    omitted = []

    def bounded(value, field="result"):
        if isinstance(value, list):
            if len(value) > 100:
                omitted.append({"field": field, "count": len(value), "returned": 100})
            return [bounded(item, f"{field}[{i}]") for i, item in enumerate(value[:100])]
        if isinstance(value, dict):
            return {k: bounded(v, f"{field}.{k}") for k, v in value.items()}
        return value

    response = bounded(result)
    response["evidence"] = {
        "truncated": bool(omitted),
        "list_limit": 100,
        "omissions": omitted[:100],
        "full_artifact": False,
    }
    return response


def associate_entities(
    path,
    layout="Model",
    label_layers=None,
    target_layers=None,
    label_types=None,
    target_types=None,
    label_regex=None,
    method="inside",
    max_distance=500.0,
    one_to_one=False,
    bbox=None,
    flatten_tolerance=0.1,
    output=None,
    unmatched_output=None,
    overwrite=False,
    source_info=None,
    max_entities=250000,
    document=None,
    roots=None,
    include_hidden=False,
):
    """Associate actual text anchors with valid polygons; ties are never silently chosen."""
    from shapely import Point, STRtree, box
    from .geometry import entity_to_polygon
    from .spatial import iter_world_entities

    if method not in {"inside", "nearest", "leader"}:
        raise CadError("INVALID_ARGUMENT", "Association method must be inside, nearest or leader.")
    _positive(max_distance, "max_distance")
    _positive(flatten_tolerance, "flatten_tolerance")
    label_layers, target_layers = _layers(label_layers), _layers(target_layers)
    label_types = _layers(label_types) or {"TEXT", "MTEXT", "ATTRIB"}
    target_types = _layers(target_types) or {
        "LWPOLYLINE",
        "POLYLINE",
        "HATCH",
        "CIRCLE",
        "ELLIPSE",
        "SPLINE",
    }
    regex = _pattern(label_regex)
    identity, source_digest = _identity(path, source_info), file_sha256(path)
    doc = document if document is not None else read_document(path)
    diagnostics = {}
    needed_layers = (
        label_layers | target_layers
        if label_layers is not None and target_layers is not None and method != "leader"
        else None
    )
    needed_types = label_types | target_types | ({"LEADER"} if method == "leader" else set())
    records = [
        r
        for r in iter_world_entities(
            doc,
            layout,
            roots=roots,
            layers=needed_layers,
            types=needed_types,
            bbox=bbox,
            max_entities=max_entities,
            diagnostics=diagnostics,
        )
        if include_hidden or r.get("visible", True)
    ]
    labels, targets, geometries, invalid = [], [], [], []
    for record in records:
        kind = record["entity"].dxftype()
        if kind in label_types and (label_layers is None or record["layer"] in label_layers):
            if _anchor(record) is not None and (regex is None or regex.search(_label(record))):
                labels.append(record)
        if kind in target_types and (target_layers is None or record["layer"] in target_layers):
            polygon = entity_to_polygon(record["entity"], flatten_tolerance=flatten_tolerance)
            if polygon.geometry is None or polygon.invalid_reason or polygon.geometry.is_empty:
                invalid.append(
                    {**_evidence(record), "reason": polygon.invalid_reason or "no_closed_area"}
                )
            else:
                targets.append(record)
                geometries.append(polygon.geometry)
    tree = STRtree(geometries) if geometries else None
    leaders = (
        [r for r in records if r["entity"].dxftype() == "LEADER"] if method == "leader" else []
    )
    pairs, unmatched, ambiguous = [], [], []
    relation_count = 0
    for label in sorted(labels, key=lambda r: r["entity_key"]):
        point = Point(_anchor(label))
        candidates = []
        leader_evidence = {}
        if tree is not None and method == "inside":
            candidates = [
                (0, 0.0, int(i)) for i in tree.query(point) if geometries[int(i)].covers(point)
            ]
        elif tree is not None and method == "nearest":
            candidates = [
                (0, float(geometries[int(i)].distance(point)), int(i))
                for i in tree.query(
                    box(
                        point.x - max_distance,
                        point.y - max_distance,
                        point.x + max_distance,
                        point.y + max_distance,
                    )
                )
            ]
            candidates = [c for c in candidates if c[1] <= max_distance]
        elif tree is not None:
            for leader in leaders:
                vertices = list(leader["entity"].vertices)
                if len(vertices) < 2:
                    continue
                tip, tail = (
                    Point(vertices[0].x, vertices[0].y),
                    Point(vertices[-1].x, vertices[-1].y),
                )
                annotation = leader["entity"].dxf.get("annotation_handle")
                exact = (
                    annotation == label["handle"]
                    and leader["instance_path"] == label["instance_path"]
                )
                if not exact and tail.distance(point) > max_distance:
                    continue
                for index in tree.query(tip):
                    index = int(index)
                    if geometries[index].covers(tip):
                        rank, distance = (0 if exact else 1), float(tail.distance(point))
                        candidates.append((rank, distance, index))
                        old = leader_evidence.get(index)
                        if old is None or (rank, distance, leader["entity_key"]) < old[0]:
                            leader_evidence[index] = (
                                (rank, distance, leader["entity_key"]),
                                {
                                    **_evidence(leader),
                                    "link_kind": "annotation_handle"
                                    if exact
                                    else "tail_anchor_proximity",
                                },
                            )
        best_by_target = {}
        for rank, distance, index in candidates:
            best_by_target[index] = min(
                (rank, distance), best_by_target.get(index, (math.inf, math.inf))
            )
        candidates = sorted(
            ((rank, distance, index) for index, (rank, distance) in best_by_target.items()),
            key=lambda c: (c[0], c[1], targets[c[2]]["entity_key"]),
        )
        relation_count += len(candidates)
        if relation_count > 100000:
            raise CadError("ASSOCIATION_LIMIT", "Candidate relations exceed their safety budget.")
        evidence = {**_evidence(label), "text": _label(label), "anchor": list(point.coords[0])}
        if not candidates:
            unmatched.append(
                {"label": evidence, "reason": "no_valid_target_within_method_and_distance"}
            )
            continue
        best_rank, best, _ = candidates[0]
        tied = [c for c in candidates if c[0] == best_rank and abs(c[1] - best) <= 1e-7]
        if len(tied) > 1:
            ambiguous.append(
                {
                    "label": evidence,
                    "reason": "multiple_equal_candidates",
                    "candidates": [
                        {"target": _evidence(targets[i]), "distance": d} for _, d, i in tied
                    ],
                }
            )
            continue
        _, distance, index = tied[0]
        pair = {
            "label": evidence,
            "target": _evidence(targets[index]),
            "method": method,
            "distance": distance,
            "confidence": "unique_geometric_evidence",
            "candidate_count": len(candidates),
        }
        if index in leader_evidence:
            pair["leader"] = leader_evidence[index][1]
        pairs.append(pair)
    if one_to_one:
        targets_to_pairs = defaultdict(list)
        for pair in pairs:
            targets_to_pairs[pair["target"]["entity_key"]].append(pair)
        conflicts = {key for key, values in targets_to_pairs.items() if len(values) > 1}
        for pair in pairs:
            if pair["target"]["entity_key"] in conflicts:
                ambiguous.append(
                    {
                        "label": pair["label"],
                        "reason": "one_to_one_conflict",
                        "candidates": [{"target": pair["target"], "distance": pair["distance"]}],
                    }
                )
        pairs = [p for p in pairs if p["target"]["entity_key"] not in conflicts]
    result = {
        "source": identity,
        "layout": layout,
        "bbox": bbox,
        "method": method,
        "distance_unit": "drawing_unit",
        "pairs": pairs,
        "selection_complete": diagnostics.get("complete", True),
        "include_hidden": include_hidden,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "invalid_targets": invalid,
        "world_traversal": diagnostics,
        "summary": {
            "label_count": len(labels),
            "target_count": len(targets),
            "pair_count": len(pairs),
            "unmatched_count": len(unmatched),
            "ambiguous_count": len(ambiguous),
            "invalid_target_count": len(invalid),
        },
        "warnings": [
            "Confidence records geometric uniqueness, not a calibrated probability or engineering interpretation.",
            "Label anchors are associated; text extents and drawing units are not inferred.",
        ],
    }
    _unchanged(path, source_digest, identity)
    return _publish(result, output, path, overwrite, unmatched_output)


def axes_entities(
    path,
    layout="Model",
    line_layers=None,
    label_layers=None,
    label_regex=None,
    bubble_block_regex=None,
    tolerance=1.0,
    max_distance=1000.0,
    bbox=None,
    point=None,
    axes=None,
    output=None,
    overwrite=False,
    source_info=None,
    max_axes=1000,
    max_intersections=100000,
    document=None,
    roots=None,
    include_hidden=False,
):
    """Extract an orthogonal axis grid, or query a revision-bound saved grid."""
    from .spatial import iter_world_entities, validated_bbox

    _positive(tolerance, "tolerance")
    _positive(max_distance, "max_distance")
    if type(max_axes) is not int or not 1 <= max_axes <= 1000:
        raise CadError("INVALID_ARGUMENT", "max_axes must be between 1 and 1000.")
    if type(max_intersections) is not int or not 0 <= max_intersections <= 100000:
        raise CadError("INVALID_ARGUMENT", "max_intersections must be between 0 and 100000.")
    identity = _identity(path, source_info)
    source_digest = file_sha256(path)
    if axes:
        source = Path(axes)
        if source.stat().st_size > 8 * 1024 * 1024:
            raise CadError("INPUT_TOO_LARGE", "Saved axes JSON exceeds 8 MiB.")
        from .transport import loads_json

        result = loads_json(source.read_text(encoding="utf-8-sig"))
        if not isinstance(result, dict):
            raise CadError("INVALID_AXES", "Saved axes must be a JSON object.")
        if result.get("source", {}).get("sha256") != identity.get("sha256"):
            raise CadError("STALE", "Saved axes were extracted from a different source revision.")
        grid = _validate_saved_grid(result)
        if result.get("layout") != layout:
            raise CadError("INVALID_AXES", "Saved axes belong to a different layout.")
        response = {"source": identity, "axes_file": str(source.resolve()), "axes": grid}
        if point is not None:
            if (
                not isinstance(point, (list, tuple))
                or len(point) != 2
                or any(type(n) not in (int, float) or not math.isfinite(n) for n in point)
            ):
                raise CadError("INVALID_ARGUMENT", "point must be two finite coordinates.")
            response["point"] = list(point)
            response["nearest"] = {}
            for orientation, value in (("vertical", point[0]), ("horizontal", point[1])):
                distances = sorted(
                    ((abs(a["position"] - value), a) for a in grid[orientation]),
                    key=lambda p: (p[0], p[1]["id"]),
                )
                best = distances[0][0] if distances else math.inf
                along = point[1] if orientation == "vertical" else point[0]
                response["nearest"][orientation] = [
                    {
                        "axis": a,
                        "distance": d,
                        "within_axis_extent": _range_covers(a, along, tolerance),
                        "on_axis": d <= tolerance and _range_covers(a, along, tolerance),
                    }
                    for d, a in distances
                    if abs(d - best) <= tolerance * 1e-6
                ]
            crossings, crossing_count, best_distance = [], 0, math.inf
            for v in grid["vertical"]:
                for h in grid["horizontal"]:
                    if _range_covers(v, h["position"], tolerance) and _range_covers(
                        h, v["position"], tolerance
                    ):
                        distance = math.hypot(v["position"] - point[0], h["position"] - point[1])
                        if distance < best_distance - tolerance * 1e-6:
                            crossings, crossing_count, best_distance = [], 0, distance
                        if abs(distance - best_distance) > tolerance * 1e-6:
                            continue
                        crossing_count += 1
                        if len(crossings) < 100:
                            crossings.append(
                                {
                                    "vertical": v["id"],
                                    "horizontal": h["id"],
                                    "point": [v["position"], h["position"]],
                                    "labels": [v.get("label"), h.get("label")],
                                    "distance": distance,
                                }
                            )
            response["nearest_intersections"] = crossings
            response["nearest_intersection_count"] = crossing_count
            response["nearest_intersections_complete"] = crossing_count == len(crossings)
        if bbox is not None:
            region = validated_bbox(bbox)
            response["bbox"] = region
            response["axes_in_region"] = {
                orientation: [
                    a
                    for a in values
                    if region[0 if orientation == "vertical" else 1]
                    <= a["position"]
                    <= region[2 if orientation == "vertical" else 3]
                    and any(
                        low <= region[3 if orientation == "vertical" else 2]
                        and high >= region[1 if orientation == "vertical" else 0]
                        for low, high in _axis_ranges(a)
                    )
                ]
                for orientation, values in grid.items()
            }
        response["summary"] = {
            "vertical_count": len(grid["vertical"]),
            "horizontal_count": len(grid["horizontal"]),
        }
        _unchanged(path, source_digest, identity)
        return _publish(response, output, path, overwrite, protected_paths=(source,))
    if point is not None:
        raise CadError("INVALID_ARGUMENT", "Point queries require a saved --axes file.")
    regex = _pattern(
        label_regex, r"^(?:[A-Za-z]{1,3}|\d+[A-Za-z]?(?:/\d+)?|[A-Za-z]{1,3}-[A-Za-z0-9]{1,6})$"
    )
    block_regex = _pattern(bubble_block_regex)
    line_layers, label_layers = _layers(line_layers), _layers(label_layers)
    doc = document if document is not None else read_document(path)
    diagnostics = {}
    records = [
        r
        for r in iter_world_entities(
            doc,
            layout,
            roots=roots,
            bbox=bbox,
            max_entities=250000,
            types={"LINE", "TEXT", "ATTRIB", "MTEXT", "CIRCLE", "ELLIPSE"},
            diagnostics=diagnostics,
        )
        if include_hidden or r.get("visible", True)
    ]
    segments, labels, bubbles = [], [], []
    warnings = []
    for record in records:
        entity, kind = record["entity"], record["entity"].dxftype()
        if kind == "LINE" and (line_layers is None or record["layer"] in line_layers):
            a, b = entity.dxf.start, entity.dxf.end
            if abs(a.x - b.x) <= tolerance and abs(a.y - b.y) > tolerance:
                segments.append(("vertical", (a.x + b.x) / 2, min(a.y, b.y), max(a.y, b.y), record))
            elif abs(a.y - b.y) <= tolerance and abs(a.x - b.x) > tolerance:
                segments.append(
                    ("horizontal", (a.y + b.y) / 2, min(a.x, b.x), max(a.x, b.x), record)
                )
        if kind in {"TEXT", "ATTRIB", "MTEXT"} and (
            label_layers is None or record["layer"] in label_layers
        ):
            if regex.fullmatch(_label(record).strip()) and _anchor(record) is not None:
                labels.append(record)
        if kind == "CIRCLE":
            names = [doc.entitydb.get(h.split("[", 1)[0]) for h in record["instance_path"]]
            if block_regex is None or any(
                e is not None and e.dxftype() == "INSERT" and block_regex.search(e.dxf.name)
                for e in names
            ):
                bubbles.append(record)
    grid = {"vertical": [], "horizontal": []}
    for orientation in grid:
        groups = []
        for _, position, low, high, record in sorted(
            (s for s in segments if s[0] == orientation),
            key=lambda s: (s[1], s[2], s[4]["entity_key"]),
        ):
            if groups and abs(position - groups[-1]["position"]) <= tolerance:
                group = groups[-1]
                group["range"] = [min(low, group["range"][0]), max(high, group["range"][1])]
                group["ranges"].append([low, high])
                group["lines"].append(_evidence(record))
            else:
                groups.append(
                    {
                        "position": position,
                        "range": [low, high],
                        "ranges": [[low, high]],
                        "lines": [_evidence(record)],
                        "labels": [],
                    }
                )
        if len(groups) > max_axes:
            raise CadError("AXIS_LIMIT", "Axis groups exceed their safety budget.")
        for index, group in enumerate(groups, 1):
            group["id"] = f"{'X' if orientation == 'vertical' else 'Y'}-{index:03d}"
            merged = []
            for low, high in sorted(group["ranges"]):
                if merged and low <= merged[-1][1] + tolerance:
                    merged[-1][1] = max(merged[-1][1], high)
                else:
                    merged.append([low, high])
            group["ranges"] = merged
        grid[orientation] = groups
    unmatched, ambiguous = [], []
    for label in labels:
        x, y = _anchor(label)
        local_bubbles = []
        for bubble in bubbles:
            center, radius = (
                bubble["entity"].ocs().to_wcs(bubble["entity"].dxf.center),
                bubble["entity"].dxf.radius,
            )
            if math.hypot(center.x - x, center.y - y) <= radius + tolerance:
                local_bubbles.append(bubble)
        if bubbles and not local_bubbles:
            unmatched.append(
                {
                    **_evidence(label),
                    "text": _label(label).strip(),
                    "anchor": [x, y],
                    "reason": "no_containing_axis_bubble",
                }
            )
            continue
        # A left-aligned axis number is usually offset from its axis. The
        # containing bubble centre supplies the geometric axis location;
        # the text placement remains evidence for label-to-bubble membership.
        anchors = [
            bubble["entity"].ocs().to_wcs(bubble["entity"].dxf.center) for bubble in local_bubbles
        ]
        anchors = [(p.x, p.y) for p in anchors] or [(x, y)]
        candidate_map = {}
        for anchor_x, anchor_y in anchors:
            for orientation, groups in grid.items():
                perpendicular, along = (
                    (anchor_x, anchor_y) if orientation == "vertical" else (anchor_y, anchor_x)
                )
                for axis in groups:
                    distance = abs(perpendicular - axis["position"])
                    if (
                        distance <= tolerance
                        and axis["range"][0] - max_distance
                        <= along
                        <= axis["range"][1] + max_distance
                    ):
                        endpoint = min(
                            abs(along - bound) for interval in axis["ranges"] for bound in interval
                        )
                        if endpoint <= max_distance:
                            candidate = (
                                distance,
                                endpoint,
                                orientation,
                                axis,
                                [anchor_x, anchor_y],
                            )
                            old = candidate_map.get(axis["id"])
                            if old is None or candidate[:2] < old[:2]:
                                candidate_map[axis["id"]] = candidate
        candidates = list(candidate_map.values())
        candidates.sort(key=lambda c: (c[0], c[1], c[3]["id"]))
        evidence = {
            **_evidence(label),
            "text": _label(label).strip(),
            "anchor": [x, y],
            "bubbles": [_evidence(b) for b in local_bubbles],
        }
        if not candidates:
            unmatched.append(evidence)
            continue
        best = candidates[0]
        ties = [
            c
            for c in candidates
            if abs(c[0] - best[0]) <= tolerance * 1e-6 and abs(c[1] - best[1]) <= tolerance * 1e-6
        ]
        if len(ties) > 1:
            ambiguous.append({"label": evidence, "axis_ids": [c[3]["id"] for c in ties]})
        else:
            evidence["axis_anchor"] = best[4]
            evidence["axis_anchor_kind"] = (
                "containing_bubble_center" if local_bubbles else "text_placement"
            )
            best[3]["labels"].append(evidence)
    for orientation, axes_list in grid.items():
        labels_to_axes = defaultdict(list)
        for axis in axes_list:
            names = sorted({label["text"] for label in axis["labels"]})
            axis["label"] = names[0] if len(names) == 1 else None
            axis["confidence"] = (
                "conflicting_endpoint_labels"
                if len(names) > 1
                else "matching_endpoint_labels"
                if names
                else "unlabeled_geometry"
            )
            if not names:
                warnings.append({"code": "AXIS_UNLABELED", "axis_id": axis["id"]})
            if len(names) > 1:
                warnings.append(
                    {"code": "AXIS_LABEL_CONFLICT", "axis_id": axis["id"], "labels": names}
                )
            for name in names:
                labels_to_axes[name].append(axis["id"])
        for label, ids in labels_to_axes.items():
            if len(ids) > 1:
                warnings.append(
                    {
                        "code": "DUPLICATE_AXIS_LABEL",
                        "orientation": orientation,
                        "label": label,
                        "axis_ids": ids,
                    }
                )
    intersections = []
    count = 0
    for vertical in grid["vertical"]:
        for horizontal in grid["horizontal"]:
            x, y = vertical["position"], horizontal["position"]
            if _range_covers(vertical, y, tolerance) and _range_covers(horizontal, x, tolerance):
                count += 1
                if len(intersections) < max_intersections:
                    intersections.append(
                        {
                            "x": x,
                            "y": y,
                            "vertical": vertical["id"],
                            "horizontal": horizontal["id"],
                            "labels": [vertical["label"], horizontal["label"]],
                        }
                    )
    bounds = [
        min((a["position"] for a in grid["vertical"]), default=None),
        min((a["position"] for a in grid["horizontal"]), default=None),
        max((a["position"] for a in grid["vertical"]), default=None),
        max((a["position"] for a in grid["horizontal"]), default=None),
    ]
    result = {
        "source": identity,
        "layout": layout,
        "bbox": bbox,
        "axes": grid,
        "grid_bbox": bounds,
        "intersections": intersections,
        "intersections_complete": len(intersections) == count,
        "bubbles": [_evidence(b) for b in bubbles],
        "unmatched_labels": unmatched,
        "ambiguous_labels": ambiguous,
        "world_traversal": diagnostics,
        "tolerance": tolerance,
        "distance_unit": "drawing_unit",
        "selection_complete": diagnostics.get("complete", True),
        "include_hidden": include_hidden,
        "summary": {
            "vertical_count": len(grid["vertical"]),
            "horizontal_count": len(grid["horizontal"]),
            "labeled_axis_count": sum(bool(a["label"]) for values in grid.values() for a in values),
            "intersection_count": count,
            "unmatched_label_count": len(unmatched),
            "ambiguous_label_count": len(ambiguous),
        },
        "warnings": warnings
        + [
            "Only orthogonal line axes with geometric endpoint evidence are supported; unnumbered/ambiguous axes are not guessed."
        ],
    }
    _unchanged(path, source_digest, identity)
    return _publish(result, output, path, overwrite)
