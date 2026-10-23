# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Revision-bound geometric association and straight-axis evidence, without BIM inference."""

from collections import defaultdict
from bisect import bisect_right
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
    intervals = _axis_ranges(axis)
    index = bisect_right(intervals, value + tolerance, key=lambda interval: interval[0]) - 1
    return index >= 0 and intervals[index][1] >= value - tolerance


def _validate_saved_grid(result):
    if not isinstance(result, dict):
        raise CadError("INVALID_AXES", "Saved axes must be a JSON object.")
    grid = result.get("axes")
    if not isinstance(grid, dict) or not {"vertical", "horizontal"} <= set(grid):
        raise CadError("INVALID_AXES", "Saved axes must contain vertical and horizontal arrays.")
    ids = set()
    if set(grid) - {"vertical", "horizontal", "oblique"}:
        raise CadError("INVALID_AXES", "Saved axes contain an unknown orientation.")
    for orientation in grid:
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
            if len(ids) > 1000:
                raise CadError("INVALID_AXES", "Saved grid exceeds 1000 total axes.")
            if orientation == "oblique" and any(k not in axis for k in ("origin", "direction")):
                raise CadError("INVALID_AXES", "Oblique axes require origin and direction.")
            for key in ("origin", "direction"):
                if key in axis and (not isinstance(axis[key], (list, tuple))
                    or len(axis[key]) != 2
                    or any(type(v) not in (int, float) or not math.isfinite(v)
                           for v in axis[key])):
                    raise CadError("INVALID_AXES", "Saved axes contain invalid geometry.")
            if "direction" in axis and abs(math.hypot(*axis["direction"]) - 1) > 1e-8:
                raise CadError("INVALID_AXES", "Saved axis direction must be a unit vector.")
            if ("origin" in axis) != ("direction" in axis):
                raise CadError("INVALID_AXES", "Axis origin and direction must occur together.")
            if "origin" in axis:
                origin, direction = axis["origin"], axis["direction"]
                frame_tolerance = 1e-6 + math.hypot(*origin) * 1e-12
                if abs(sum(origin[i] * direction[i] for i in (0, 1))) > frame_tolerance:
                    raise CadError("INVALID_AXES", "Axis origin must be its perpendicular foot from WCS zero.")
                if orientation in {"vertical", "horizontal"}:
                    expected = ([0, 1], [axis["position"], 0]) if orientation == "vertical" else (
                        [1, 0], [0, axis["position"]])
                    if any(abs(a - b) > 1e-8 for a, b in zip(list(direction) + list(origin),
                           expected[0] + expected[1])):
                        raise CadError("INVALID_AXES", "Orthogonal axis geometry conflicts with its orientation.")
                elif abs(-direction[1] * origin[0] + direction[0] * origin[1]
                         - axis["position"]) > frame_tolerance:
                    raise CadError("INVALID_AXES", "Oblique axis position conflicts with its geometry.")
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
            ranges = _axis_ranges(axis)
            if any(ranges[i][0] <= ranges[i - 1][1] for i in range(1, len(ranges))):
                raise CadError("INVALID_AXES", "Axis ranges must be sorted and disjoint.")
            if axis["range"] != [ranges[0][0], ranges[-1][1]]:
                raise CadError("INVALID_AXES", "Axis extent must contain exactly its finite ranges.")
    return grid


def _anchor(record):
    entity = record["entity"]
    if entity.dxftype() in {"MLEADER", "MULTILEADER"}:
        content = entity.context.mtext
        return list(content.insert)[:2] if content is not None else None
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
    if entity.dxftype() in {"MLEADER", "MULTILEADER"}:
        from ezdxf.tools.text import plain_mtext

        return plain_mtext(entity.get_mtext_content())[:2048]
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


def _polyline_points(entity):
    """Return native WCS vertices; curved/closed paths are not straight leaders/axes."""
    if entity.dxftype() == "LWPOLYLINE":
        if entity.closed or any(abs(p[4]) > 1e-12 for p in entity.get_points("xyseb")):
            return None
        return [list(p)[:2] for p in entity.vertices_in_wcs()]
    if entity.dxftype() == "POLYLINE":
        if entity.is_closed or not (entity.is_2d_polyline or entity.is_3d_polyline):
            return None
        if any(abs(v.dxf.get("bulge", 0)) > 1e-12 for v in entity.vertices):
            return None
        return [list(p)[:2] for p in entity.points_in_wcs()]
    return None


def _leader_paths(record, polyline_layers, diagnostics):
    entity, kind = record["entity"], record["entity"].dxftype()
    paths = []
    if kind == "LEADER":
        points = [list(p)[:2] for p in entity.vertices]
        if len(points) >= 2:
            paths.append((points, "native_arrow_first_vertex", entity.dxf.get("annotation_handle")))
    elif kind in {"MLEADER", "MULTILEADER"}:
        for leader in entity.context.leaders:
            for line in leader.lines:
                points = [list(p)[:2] for p in line.vertices]
                if len(points) >= 2:
                    paths.append((points, "native_arrow_first_vertex", record["handle"]))
    elif polyline_layers is not None and record["layer"] in polyline_layers:
        points = _polyline_points(entity)
        if points and len(points) >= 2:
            # A plain polyline stores no arrow direction. Both endpoint hypotheses
            # remain explicit and compete; proximity alone is never semantic proof.
            paths.extend([(points, "polyline_endpoint_hypothesis", None),
                          (list(reversed(points)), "polyline_endpoint_hypothesis", None)])
    if not paths:
        diagnostics.append({**_evidence(record), "reason": "no_supported_open_leader_path"})
    return [
        {"record": record, "tip": points[0], "tail": points[-1], "vertices": points,
         "direction_evidence": direction, "annotation_handle": annotation}
        for points, direction, annotation in paths
    ]


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
    leader_layers=None,
    ambiguity_tolerance=1e-7,
):
    """Associate actual text anchors with valid polygons; ties are never silently chosen."""
    from shapely import Point, STRtree, box
    from .geometry import entity_to_polygon
    from .spatial import iter_world_entities

    if method not in {"inside", "nearest", "leader"}:
        raise CadError("INVALID_ARGUMENT", "Association method must be inside, nearest or leader.")
    _positive(max_distance, "max_distance")
    _positive(flatten_tolerance, "flatten_tolerance")
    _positive(ambiguity_tolerance, "ambiguity_tolerance", allow_zero=True)
    label_layers, target_layers = _layers(label_layers), _layers(target_layers)
    leader_layers = _layers(leader_layers)
    label_types = _layers(label_types) or {"TEXT", "MTEXT", "ATTRIB", "MLEADER", "MULTILEADER"}
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
    needed_types = label_types | target_types
    if method == "leader":
        needed_types |= {"LEADER", "MLEADER", "MULTILEADER"}
        if leader_layers is not None:
            needed_types |= {"LWPOLYLINE", "POLYLINE"}
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
    leader_diagnostics, leader_paths = [], []
    if method == "leader":
        for record in records:
            kind = record["entity"].dxftype()
            if kind in {"LEADER", "MLEADER", "MULTILEADER"} or (
                kind in {"LWPOLYLINE", "POLYLINE"}
                and leader_layers is not None and record["layer"] in leader_layers
            ):
                leader_paths.extend(_leader_paths(record, leader_layers, leader_diagnostics))
    leader_tree = STRtree([Point(p["tail"]) for p in leader_paths]) if leader_paths else None
    linked_paths = defaultdict(list)
    for index, path_evidence in enumerate(leader_paths):
        if path_evidence["annotation_handle"]:
            linked_paths[(tuple(path_evidence["record"]["instance_path"]),
                          path_evidence["annotation_handle"])].append(index)
    pairs, unmatched, ambiguous = [], [], []
    relation_count, candidate_work = 0, 0
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
            nearby = set(int(i) for i in leader_tree.query(
                box(point.x - max_distance, point.y - max_distance,
                    point.x + max_distance, point.y + max_distance)
            )) if leader_tree is not None else set()
            nearby.update(linked_paths[(tuple(label["instance_path"]), label["handle"])])
            for leader_index in sorted(nearby):
                path_evidence = leader_paths[leader_index]
                leader = path_evidence["record"]
                tip, tail = Point(path_evidence["tip"]), Point(path_evidence["tail"])
                exact = (path_evidence["annotation_handle"] == label["handle"]
                         and leader["instance_path"] == label["instance_path"])
                if not exact and tail.distance(point) > max_distance:
                    continue
                # Intrinsic MLEADER content belongs to its own leader paths;
                # do not borrow another MLEADER's nearby annotation.
                if leader["entity"].dxftype() in {"MLEADER", "MULTILEADER"} and not exact:
                    continue
                for index in tree.query(tip):
                    candidate_work += 1
                    if candidate_work > 100000:
                        raise CadError("ASSOCIATION_LIMIT", "Leader-target candidate tests exceed their safety budget.")
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
                                    "tip": path_evidence["tip"],
                                    "tail": path_evidence["tail"],
                                    "vertices": path_evidence["vertices"],
                                    "direction_evidence": path_evidence["direction_evidence"],
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
        candidate_evidence = [
            {"target": _evidence(targets[i]), "distance": d, "evidence_rank": rank,
             **({"leader": leader_evidence[i][1]} if i in leader_evidence else {})}
            for rank, d, i in candidates
        ]
        tied = [c for c in candidates
                if c[0] == best_rank and abs(c[1] - best) <= ambiguity_tolerance]
        if len(tied) > 1:
            ambiguous.append(
                {
                    "label": evidence,
                    "reason": "multiple_equal_candidates" if ambiguity_tolerance <= 1e-7
                    else "candidates_within_ambiguity_tolerance",
                    "candidates": candidate_evidence,
                    "competing_candidate_count": len(tied),
                }
            )
            continue
        _, distance, index = tied[0]
        pair = {
            "label": evidence,
            "target": _evidence(targets[index]),
            "method": method,
            "distance": distance,
            "confidence": "unique_nearest_geometry" if method == "nearest"
            else "unique_geometric_evidence",
            "candidate_count": len(candidates),
            "candidates": candidate_evidence,
            "distance_margin": candidates[1][1] - best
            if len(candidates) > 1 and candidates[1][0] == best_rank else None,
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
                        "candidates": pair["candidates"],
                    }
                )
        pairs = [p for p in pairs if p["target"]["entity_key"] not in conflicts]
    result = {
        "source": identity,
        "layout": layout,
        "bbox": bbox,
        "method": method,
        "ambiguity_tolerance": ambiguity_tolerance,
        "leader_layers": sorted(leader_layers) if leader_layers is not None else None,
        "distance_unit": "drawing_unit",
        "pairs": pairs,
        "selection_complete": diagnostics.get("complete", True),
        "include_hidden": include_hidden,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "invalid_targets": invalid,
        "unsupported_leaders": leader_diagnostics,
        "candidate_relations_complete": True,
        "world_traversal": diagnostics,
        "summary": {
            "label_count": len(labels),
            "target_count": len(targets),
            "pair_count": len(pairs),
            "unmatched_count": len(unmatched),
            "ambiguous_count": len(ambiguous),
            "invalid_target_count": len(invalid),
            "leader_path_count": len(leader_paths),
            "unsupported_leader_count": len(leader_diagnostics),
            "candidate_relation_count": relation_count,
        },
        "warnings": [
            "Confidence records geometric uniqueness, not a calibrated probability or engineering interpretation.",
            "Label anchors are associated; text extents and drawing units are not inferred.",
            "Polyline leaders require explicitly selected leader layers; both endpoint directions remain geometric hypotheses.",
        ],
    }
    _unchanged(path, source_digest, identity)
    return _publish(result, output, path, overwrite, unmatched_output)


def _axis_frame(axis, orientation):
    if "origin" in axis:
        return axis["origin"], axis["direction"]
    return (([axis["position"], 0], [0, 1]) if orientation == "vertical"
            else ([0, axis["position"]], [1, 0]))


def _axis_projection(axis, orientation, point):
    origin, direction = _axis_frame(axis, orientation)
    delta = [point[i] - origin[i] for i in (0, 1)]
    return (abs(delta[0] * direction[1] - delta[1] * direction[0]),
            sum(delta[i] * direction[i] for i in (0, 1)))


def _axis_point(axis, orientation, along):
    origin, direction = _axis_frame(axis, orientation)
    return [origin[i] + direction[i] * along for i in (0, 1)]


def _axis_crossings(grid, tolerance):
    """At most 499,500 pair tests for the validated 1,000-axis budget."""
    flat = [(orientation, axis) for orientation, axes in grid.items() for axis in axes]
    for index, (orientation, first) in enumerate(flat):
        p, u = _axis_frame(first, orientation)
        for other_orientation, second in flat[index + 1:]:
            q, v = _axis_frame(second, other_orientation)
            determinant = u[0] * v[1] - u[1] * v[0]
            if abs(determinant) < 1e-12:
                continue
            delta = [q[i] - p[i] for i in (0, 1)]
            a = (delta[0] * v[1] - delta[1] * v[0]) / determinant
            b = (delta[0] * u[1] - delta[1] * u[0]) / determinant
            if not (_range_covers(first, a, tolerance) and _range_covers(second, b, tolerance)):
                continue
            point = _axis_point(first, orientation, a)
            result = {"x": point[0], "y": point[1],
                      "axis_ids": [first["id"], second["id"]],
                      "labels": [first.get("label"), second.get("label")]}
            # Preserve the original orthogonal query contract.
            if {orientation, other_orientation} == {"vertical", "horizontal"}:
                vertical, horizontal = ((first, second) if orientation == "vertical"
                                        else (second, first))
                result.update(vertical=vertical["id"], horizontal=horizontal["id"],
                              labels=[vertical.get("label"), horizontal.get("label")])
            yield result


def _axis_intersects_region(axis, orientation, region):
    from shapely import LineString, box

    shape = box(*region)
    return any(LineString([_axis_point(axis, orientation, low),
                           _axis_point(axis, orientation, high)]).intersects(shape)
               for low, high in _axis_ranges(axis))


def axes_entities(
    path, layout="Model", line_layers=None, label_layers=None, label_regex=None,
    bubble_block_regex=None, tolerance=1.0, max_distance=1000.0, bbox=None,
    point=None, axes=None, output=None, overwrite=False, source_info=None,
    max_axes=1000, max_intersections=100000, document=None, roots=None,
    include_hidden=False,
):
    """Extract/query finite straight axes in WCS; retain numbering and ambiguity evidence."""
    from shapely import LineString, Point, STRtree, box
    from .spatial import iter_world_entities, validated_bbox

    _positive(tolerance, "tolerance")
    _positive(max_distance, "max_distance")
    if type(max_axes) is not int or not 1 <= max_axes <= 1000:
        raise CadError("INVALID_ARGUMENT", "max_axes must be between 1 and 1000.")
    if type(max_intersections) is not int or not 0 <= max_intersections <= 100000:
        raise CadError("INVALID_ARGUMENT", "max_intersections must be between 0 and 100000.")
    identity, source_digest = _identity(path, source_info), file_sha256(path)
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
        response = {"source": identity, "axes_file": str(source.resolve()), "axes": grid,
                    "selection_complete": result.get("selection_complete", False),
                    "axis_geometry_complete": result.get("axis_geometry_complete", False)}
        if point is not None:
            if (not isinstance(point, (list, tuple)) or len(point) != 2
                or any(type(n) not in (int, float) or not math.isfinite(n) for n in point)):
                raise CadError("INVALID_ARGUMENT", "point must be two finite coordinates.")
            response.update(point=list(point), nearest={})
            for orientation, values in grid.items():
                distances = sorted(
                    ((_axis_projection(a, orientation, point)[0], a) for a in values),
                    key=lambda item: (item[0], item[1]["id"]),
                )
                best = distances[0][0] if distances else math.inf
                response["nearest"][orientation] = []
                for distance, axis in distances:
                    if abs(distance - best) > tolerance * 1e-6:
                        continue
                    along = _axis_projection(axis, orientation, point)[1]
                    extent = _range_covers(axis, along, tolerance)
                    response["nearest"][orientation].append(
                        {"axis": axis, "distance": distance, "within_axis_extent": extent,
                         "on_axis": distance <= tolerance and extent})
            crossings, count, best = [], 0, math.inf
            for crossing in _axis_crossings(grid, tolerance):
                distance = math.hypot(crossing["x"] - point[0], crossing["y"] - point[1])
                if distance < best - tolerance * 1e-6:
                    crossings, count, best = [], 0, distance
                if abs(distance - best) > tolerance * 1e-6:
                    continue
                count += 1
                if len(crossings) < min(100, max_intersections):
                    crossings.append({**crossing, "point": [crossing["x"], crossing["y"]],
                                      "distance": distance})
            response.update(nearest_intersections=crossings, nearest_intersection_count=count,
                            nearest_intersections_complete=count == len(crossings))
        if bbox is not None:
            region = validated_bbox(bbox)
            response.update(bbox=region, axes_in_region={
                orientation: [a for a in values if _axis_intersects_region(a, orientation, region)]
                for orientation, values in grid.items()})
        response["summary"] = {f"{k}_count": len(v) for k, v in grid.items()}
        _unchanged(path, source_digest, identity)
        return _publish(response, output, path, overwrite, protected_paths=(source,))
    if point is not None:
        raise CadError("INVALID_ARGUMENT", "Point queries require a saved --axes file.")
    regex = _pattern(label_regex,
                     r"^(?:[A-Za-z]{1,3}|\d+[A-Za-z]?(?:/\d+)?|[A-Za-z]{1,3}-[A-Za-z0-9]{1,6})$")
    block_regex = _pattern(bubble_block_regex)
    line_layers, label_layers = _layers(line_layers), _layers(label_layers)
    doc = document if document is not None else read_document(path)
    diagnostics = {}
    records = [r for r in iter_world_entities(
        doc, layout, roots=roots, bbox=bbox, max_entities=250000,
        types={"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "SPLINE", "TEXT", "ATTRIB",
               "MTEXT", "CIRCLE", "ELLIPSE"}, diagnostics=diagnostics)
        if include_hidden or r.get("visible", True)]
    segments, labels, bubbles, unsupported = [], [], [], []
    for record in records:
        entity, kind = record["entity"], record["entity"].dxftype()
        if kind in {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "SPLINE"} and (
            line_layers is None or record["layer"] in line_layers
        ):
            points = ([list(entity.dxf.start)[:2], list(entity.dxf.end)[:2]] if kind == "LINE"
                      else _polyline_points(entity) if kind in {"LWPOLYLINE", "POLYLINE"}
                      else None)
            reason = None
            if not points or len(points) < 2:
                reason = "curved_closed_or_unsupported_axis_path"
            else:
                a, b = points[0], points[-1]
                length = math.dist(a, b)
                if length <= tolerance:
                    reason = "axis_endpoint_separation_below_tolerance"
                else:
                    u = [(b[i] - a[i]) / length for i in (0, 1)]
                    if u[1] < -1e-12 or (abs(u[1]) <= 1e-12 and u[0] < 0):
                        u = [-u[0], -u[1]]
                    if any(abs((p[0] - a[0]) * u[1] - (p[1] - a[1]) * u[0]) > tolerance
                           for p in points):
                        reason = "non_collinear_polyline_axis"
                    else:
                        orientation = ("vertical" if abs(b[0] - a[0]) <= tolerance else
                                       "horizontal" if abs(b[1] - a[1]) <= tolerance else "oblique")
                        if orientation == "vertical":
                            u, normal = [0.0, 1.0], [1.0, 0.0]
                        elif orientation == "horizontal":
                            u, normal = [1.0, 0.0], [0.0, 1.0]
                        else:
                            normal = [-u[1], u[0]]
                        position = sum((a[i] + b[i]) * normal[i] / 2 for i in (0, 1))
                        along = [sum(p[i] * u[i] for i in (0, 1)) for p in points]
                        segments.append({"orientation": orientation, "position": position,
                                         "direction": u, "normal": normal,
                                         "range": [min(along), max(along)], "record": record,
                                         "endpoints": [a, b], "length": length,
                                         "extent_points": [points[along.index(min(along))],
                                                           points[along.index(max(along))]]})
            if reason:
                unsupported.append({**_evidence(record), "reason": reason})
        if kind in {"TEXT", "ATTRIB", "MTEXT"} and (
            label_layers is None or record["layer"] in label_layers
        ) and regex.fullmatch(_label(record).strip()) and _anchor(record) is not None:
            labels.append(record)
        if kind == "CIRCLE":
            names = [doc.entitydb.get(h.split("[", 1)[0]) for h in record["instance_path"]]
            if block_regex is None or any(e is not None and e.dxftype() == "INSERT"
                                         and block_regex.search(e.dxf.name) for e in names):
                bubbles.append(record)
    grid = {"vertical": [], "horizontal": [], "oblique": []}
    group_tests = 0
    for segment in sorted(segments, key=lambda s: (s["orientation"],
                          math.atan2(s["direction"][1], s["direction"][0]), s["position"],
                          s["record"]["entity_key"])):
        groups = grid[segment["orientation"]]
        matching = []
        for group in groups:
            group_tests += 1
            if group_tests > 5000000:
                raise CadError("AXIS_LIMIT", "Axis grouping exceeds five million candidate tests; narrow the selection.")
            u, v = group["direction"], segment["direction"]
            if abs(u[0] * v[1] - u[1] * v[0]) * segment["length"] > tolerance:
                continue
            distances = [_axis_projection(group, segment["orientation"], p)[0]
                         for p in segment["endpoints"]]
            if max(distances) <= tolerance:
                matching.append(group)
        if len(matching) > 1:
            unsupported.append({**_evidence(segment["record"]),
                                "reason": "multiple_axis_groups_within_tolerance"})
            continue
        if matching:
            group = matching[0]
            interval = [_axis_projection(group, segment["orientation"], p)[1]
                        for p in segment["extent_points"]]
            group["ranges"].append([min(interval), max(interval)])
            group["lines"].append({**_evidence(segment["record"]),
                                   "endpoints": segment["endpoints"]})
        else:
            if sum(len(values) for values in grid.values()) >= max_axes:
                raise CadError("AXIS_LIMIT", "Axis groups exceed their safety budget.")
            groups.append({"position": segment["position"], "direction": segment["direction"],
                           "normal": segment["normal"],
                           "origin": [n * segment["position"] for n in segment["normal"]],
                           "range": segment["range"], "ranges": [segment["range"]],
                           "lines": [{**_evidence(segment["record"]),
                                      "endpoints": segment["endpoints"]}], "labels": []})
    for orientation, groups in grid.items():
        groups.sort(key=lambda a: (math.atan2(a["direction"][1], a["direction"][0]), a["position"]))
        for index, group in enumerate(groups, 1):
            prefix = {"vertical": "X", "horizontal": "Y", "oblique": "O"}[orientation]
            group["id"] = f"{prefix}-{index:03d}"
            merged = []
            for low, high in sorted(group["ranges"]):
                if merged and low <= merged[-1][1] + tolerance:
                    merged[-1][1] = max(merged[-1][1], high)
                else:
                    merged.append([low, high])
            group["ranges"] = merged
            group["range"] = [merged[0][0], merged[-1][1]]
    bubble_centers = [b["entity"].ocs().to_wcs(b["entity"].dxf.center) for b in bubbles]
    bubble_radii = [b["entity"].dxf.radius for b in bubbles]
    bubble_tree = STRtree([box(p.x - r - tolerance, p.y - r - tolerance,
                              p.x + r + tolerance, p.y + r + tolerance)
                          for p, r in zip(bubble_centers, bubble_radii)]) if bubbles else None
    flat_axes = [(orientation, axis) for orientation, values in grid.items() for axis in values]
    endpoint_tree = STRtree([LineString([
        _axis_point(axis, orientation, axis["range"][0] - max_distance),
        _axis_point(axis, orientation, axis["range"][1] + max_distance)]).buffer(tolerance)
        for orientation, axis in flat_axes]) if flat_axes else None
    unmatched, ambiguous = [], []
    relation_count = 0
    for label in labels:
        x, y = _anchor(label)
        local = [int(i) for i in bubble_tree.query(Point(x, y))
                 if math.hypot(bubble_centers[int(i)].x - x, bubble_centers[int(i)].y - y)
                 <= bubble_radii[int(i)] + tolerance] if bubble_tree is not None else []
        evidence = {**_evidence(label), "text": _label(label).strip(), "anchor": [x, y],
                    "bubbles": [_evidence(bubbles[i]) for i in local]}
        if bubbles and not local:
            unmatched.append({**evidence, "reason": "no_containing_axis_bubble"})
            continue
        anchors = [[bubble_centers[i].x, bubble_centers[i].y] for i in local] or [[x, y]]
        candidates = {}
        for anchor in anchors:
            nearby = endpoint_tree.query(Point(anchor)) if endpoint_tree is not None else []
            for axis_index in nearby:
                orientation, axis = flat_axes[int(axis_index)]
                distance, along = _axis_projection(axis, orientation, anchor)
                endpoint = min(abs(along - bound) for interval in axis["ranges"]
                               for bound in interval)
                if distance <= tolerance and endpoint <= max_distance:
                    candidate = (distance, endpoint, axis, anchor)
                    if axis["id"] not in candidates or candidate[:2] < candidates[axis["id"]][:2]:
                        candidates[axis["id"]] = candidate
        candidates = sorted(candidates.values(), key=lambda c: (c[0], c[1], c[2]["id"]))
        relation_count += len(candidates)
        if relation_count > 100000:
            raise CadError("AXIS_LIMIT", "Axis-label candidates exceed their safety budget.")
        evidence["candidates"] = [{"axis_id": c[2]["id"], "perpendicular_distance": c[0],
                                   "endpoint_distance": c[1], "axis_anchor": c[3]}
                                  for c in candidates]
        if not candidates:
            unmatched.append({**evidence, "reason": "no_axis_endpoint_within_tolerance"})
            continue
        best = candidates[0]
        ties = [c for c in candidates if abs(c[0] - best[0]) <= tolerance * 1e-6
                and abs(c[1] - best[1]) <= tolerance * 1e-6]
        if len(ties) > 1:
            ambiguous.append({"label": evidence, "axis_ids": [c[2]["id"] for c in ties]})
        else:
            evidence.update(axis_anchor=best[3], axis_anchor_kind=(
                "containing_bubble_center" if local else "text_placement"))
            best[2]["labels"].append(evidence)
    warnings = []
    names_to_axes = defaultdict(list)
    for orientation, groups in grid.items():
        for axis in groups:
            names = sorted({label["text"] for label in axis["labels"]})
            axis["label"] = names[0] if len(names) == 1 else None
            axis["confidence"] = ("conflicting_endpoint_labels" if len(names) > 1 else
                                  "matching_endpoint_labels" if names and len(axis["labels"]) > 1 else
                                  "single_endpoint_label" if names else "unlabeled_geometry")
            if not names:
                warnings.append({"code": "AXIS_UNLABELED", "axis_id": axis["id"]})
            if len(names) > 1:
                warnings.append({"code": "AXIS_LABEL_CONFLICT", "axis_id": axis["id"], "labels": names})
            for name in names:
                names_to_axes[(orientation, name)].append(axis["id"])
    for (orientation, name), ids in names_to_axes.items():
        if len(ids) > 1:
            warnings.append({"code": "DUPLICATE_AXIS_LABEL", "orientation": orientation,
                             "label": name, "axis_ids": ids})
    intersections, count = [], 0
    for crossing in _axis_crossings(grid, tolerance):
        count += 1
        if len(intersections) < max_intersections:
            intersections.append(crossing)
    vertices = [_axis_point(a, orientation, bound) for orientation, values in grid.items()
                for a in values for interval in a["ranges"] for bound in interval]
    bounds = ([min(p[0] for p in vertices), min(p[1] for p in vertices),
               max(p[0] for p in vertices), max(p[1] for p in vertices)] if vertices else [None] * 4)
    if unsupported:
        warnings.append({"code": "UNSUPPORTED_AXIS_GEOMETRY", "count": len(unsupported)})
    result = {"source": identity, "layout": layout, "bbox": bbox, "axes": grid,
              "axis_format": "straight-wcs-v2", "grid_bbox": bounds,
              "intersections": intersections, "intersections_complete": len(intersections) == count,
              "bubbles": [_evidence(b) for b in bubbles], "unmatched_labels": unmatched,
              "ambiguous_labels": ambiguous, "unsupported_axes": unsupported,
              "world_traversal": diagnostics, "tolerance": tolerance,
              "distance_unit": "drawing_unit", "include_hidden": include_hidden,
              "axis_geometry_complete": not unsupported,
              "selection_complete": diagnostics.get("complete", True) and not unsupported,
              "summary": {**{f"{k}_count": len(v) for k, v in grid.items()},
                  "labeled_axis_count": sum(bool(a["label"]) for v in grid.values() for a in v),
                  "intersection_count": count, "unmatched_label_count": len(unmatched),
                  "ambiguous_label_count": len(ambiguous), "unsupported_axis_count": len(unsupported)},
              "warnings": warnings + ["Straight LINE and collinear open polyline axes use WCS endpoint evidence; curved axes are reported as unsupported. Axis labels are geometric evidence, not component semantics."]}
    _unchanged(path, source_digest, identity)
    return _publish(result, output, path, overwrite)
