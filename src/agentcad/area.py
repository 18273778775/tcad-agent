# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0
"""Planar area accounting with GEOS union and bounded agent-facing evidence."""

import hashlib
import json
import math
from pathlib import Path

from shapely import STRtree, make_valid as repair_geometry, set_precision, unary_union
from shapely.affinity import translate
from shapely.geometry import mapping
from shapely.errors import GEOSException

from .errors import CadError
from .geometry import FLATTEN_RULE_VERSION, entity_to_polygon
from .io import atomic_output, file_sha256, revision_guard

_LINEAR_TO_METRES = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "ft": 0.3048}


def _polygonal(geometry):
    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        return geometry
    return unary_union(
        [p for p in getattr(geometry, "geoms", ()) if p.geom_type in {"Polygon", "MultiPolygon"}]
    )


def _native_origin(entity):
    """Choose a local origin before GEOS sees large WCS coordinates."""
    kind = entity.dxftype()
    if kind == "LWPOLYLINE" and len(entity):
        p = entity.ocs().to_wcs((*entity.get_points("xy")[0], entity.dxf.elevation))
        return p.x, p.y
    if kind == "POLYLINE" and entity.vertices:
        p = entity.ocs().to_wcs(entity.vertices[0].dxf.location)
        return p.x, p.y
    for name in ("center", "insert", "start"):
        if entity.dxf.hasattr(name):
            p = entity.dxf.get(name)
            return float(p[0]), float(p[1])
    if kind == "HATCH" and entity.paths:
        path = entity.paths[0]
        if hasattr(path, "vertices") and path.vertices:
            p = entity.ocs().to_wcs((*path.vertices[0][:2], entity.dxf.elevation.z))
            return p.x, p.y
        if getattr(path, "edges", None):
            edge = path.edges[0]
            point = getattr(edge, "real_start_point", getattr(edge, "start", None))
            if point is not None:
                p = entity.ocs().to_wcs((point[0], point[1], entity.dxf.elevation.z))
                return p.x, p.y
    if kind == "SPLINE" and len(entity.control_points):
        p = entity.control_points[0]
        return float(p[0]), float(p[1])
    return 0.0, 0.0


def _world_bounds(geometry, origin):
    b = geometry.bounds
    return [b[0] + origin[0], b[1] + origin[1], b[2] + origin[0], b[3] + origin[1]]


def _metrics(geometries, tolerance):
    unsnapped = unary_union(geometries)
    # GEOS unary_union can return a sole input unchanged; set_precision makes the
    # explicitly requested grid apply even for a one-polygon selection.
    merged = (
        unary_union([set_precision(g, tolerance) for g in geometries], grid_size=tolerance)
        if tolerance
        else unsnapped
    )
    summed = math.fsum(g.area for g in geometries)
    return {
        "sum_area": summed,
        "union_area": float(merged.area),
        "overlap_area": max(0.0, summed - float(unsnapped.area)),
        "unsnapped_union_area": float(unsnapped.area),
        "precision_area_delta": float(merged.area - unsnapped.area),
        "perimeter": float(merged.length),
    }, merged


def area_selected(
    entities,
    *,
    mode="union",
    by=None,
    tolerance=0.0,
    flatten_tolerance=0.1,
    linear_unit=None,
    make_valid=False,
    output=None,
    overwrite=False,
    evidence_limit=100,
    max_relations=100000,
    sheet=None,
    sheet_id=None,
    source=None,
    source_info=None,
    include_hidden=False,
    expected_source_sha256=None,
):
    """Union selected WCS entities or ``spatial.iter_world_entities`` records.

    ``tolerance`` is an explicit GEOS precision grid, *not* a closeness threshold.
    It may alter area; unsnapped area and the delta are always recorded. By default
    no grid snapping or invalid-boundary repair takes place.
    """
    if mode not in {"union", "sum", "overlap"} or by not in {None, "layer", "block", "sheet"}:
        raise CadError(
            "INVALID_ARGUMENT",
            "Area mode must be union/sum/overlap and by must be layer/block/sheet.",
        )
    sheet = sheet_id or sheet
    if by == "sheet" and not sheet:
        raise CadError(
            "INVALID_ARGUMENT", "Grouping by sheet requires one explicitly selected sheet identity."
        )
    if type(tolerance) not in {int, float} or not math.isfinite(tolerance) or tolerance < 0:
        raise CadError(
            "INVALID_ARGUMENT", "tolerance must be a finite nonnegative precision grid size."
        )
    if (
        type(flatten_tolerance) not in {int, float}
        or not math.isfinite(flatten_tolerance)
        or flatten_tolerance <= 0
    ):
        raise CadError("INVALID_ARGUMENT", "flatten_tolerance must be positive and finite.")
    if type(evidence_limit) is not int or not 0 <= evidence_limit <= 1000:
        raise CadError("INVALID_ARGUMENT", "evidence_limit must be between 0 and 1000.")
    if type(max_relations) is not int or not 1 <= max_relations <= 1_000_000:
        raise CadError("INVALID_ARGUMENT", "max_relations must be between 1 and 1000000.")
    if linear_unit not in {None, *_LINEAR_TO_METRES}:
        raise CadError("INVALID_ARGUMENT", "linear_unit must be mm, cm, m, in or ft.")
    if type(make_valid) is not bool or type(include_hidden) is not bool:
        raise CadError("INVALID_ARGUMENT", "make_valid and include_hidden must be booleans.")
    source_digest = expected_source_sha256 or (file_sha256(source) if source is not None else None)
    origin = None
    records, rejected, repaired, geometries, groups = [], [], [], [], {}
    approximation = False
    selected_count = 0
    total_vertices = 0
    hidden_excluded_count = 0
    for item in entities:
        if selected_count >= 250000:
            raise CadError("GEOMETRY_LIMIT", "Area selection exceeds 250000 leaf entities.")
        selected_count += 1
        wrapper = item if isinstance(item, dict) else {"entity": item}
        if not include_hidden and not wrapper.get("visible", True):
            hidden_excluded_count += 1
            continue
        entity = wrapper["entity"]
        handle = wrapper.get("handle", wrapper.get("source_handle", entity.dxf.get("handle")))
        key = wrapper.get("entity_key") or handle or f"virtual-{selected_count}"
        layer = wrapper.get("layer", wrapper.get("effective_layer", entity.dxf.get("layer", "0")))
        instance_path = wrapper.get("instance_path", [])
        evidence = {
            "entity_key": key,
            "handle": handle,
            "type": entity.dxftype(),
            "layer": layer,
            "instance_path": instance_path,
            "root_handle": wrapper.get("root_handle", handle),
        }
        if origin is None:
            origin = _native_origin(entity)
            if tolerance:
                origin = tuple(math.floor(v / tolerance) * tolerance for v in origin)
        value = entity_to_polygon(entity, flatten_tolerance=flatten_tolerance, origin=origin)
        total_vertices += value.vertex_count
        if total_vertices > 2_000_000:
            raise CadError(
                "GEOMETRY_LIMIT",
                "Area selection exceeds the total boundary vertex budget.",
                {"max_vertices": 2000000, "vertices": total_vertices},
            )
        geometry = value.geometry
        if value.invalid_reason and make_valid and geometry is not None and value.closed:
            original_area = geometry.area
            geometry = _polygonal(repair_geometry(geometry))
            if not geometry.is_empty and geometry.is_valid and geometry.area > 0:
                repaired.append(
                    {
                        **evidence,
                        "reason": value.invalid_reason,
                        "original_algebraic_area": original_area,
                        "repaired_area": geometry.area,
                        "area_delta": geometry.area - original_area,
                        "repair_method": "GEOS_make_valid_linework",
                    }
                )
            else:
                geometry = None
        elif value.invalid_reason:
            geometry = None
        if geometry is None:
            rejected.append(
                {
                    **evidence,
                    "reason": value.invalid_reason or "No polygonal area.",
                    "closed": value.closed,
                }
            )
            continue
        approximation |= value.approximate
        evidence.update(
            area=float(geometry.area),
            bbox=_world_bounds(geometry, origin),
            approximate=value.approximate,
            vertex_count=value.vertex_count,
            closure_evidence=value.closure_evidence,
        )
        records.append(evidence)
        geometries.append(geometry)
        if by:
            group = (
                layer
                if by == "layer"
                else str(instance_path[0])
                if by == "block" and instance_path
                else "layout"
                if by == "block"
                else sheet or "unassigned"
            )
            groups.setdefault(str(group), []).append(geometry)
    origin = origin or (0.0, 0.0)
    try:
        totals, merged = _metrics(geometries, tolerance)
    except GEOSException as exc:
        raise CadError(
            "GEOMETRY_FAILED",
            "GEOS could not union the selected boundaries.",
            {"reason": str(exc)[:1024]},
        ) from exc
    duplicates, contained, overlaps = [], [], []
    unique, members, hashes = [], [], {}
    for i, geometry in enumerate(geometries):
        # Zero-distance simplification removes redundant collinear vertices
        # without a snapping tolerance, so equivalent HATCH/polyline outlines
        # with different edge subdivisions form one duplicate group.
        digest = hashlib.sha256(
            geometry.simplify(0, preserve_topology=True).normalize().wkb
        ).hexdigest()
        if digest in hashes:
            members[hashes[digest]].append(i)
        else:
            hashes[digest] = len(unique)
            unique.append(geometry)
            members.append([i])
    for indices in members:
        if len(indices) > 1:
            duplicates.append(
                {
                    "entity_keys": [records[i]["entity_key"] for i in indices],
                    "count": len(indices),
                    "area_each": records[indices[0]]["area"],
                    "layers": sorted({records[i]["layer"] for i in indices}),
                    "cross_layer": len({records[i]["layer"] for i in indices}) > 1,
                }
            )
    tree = STRtree(unique)
    comparisons = 0
    relations_complete = True
    for i, geometry in enumerate(unique):
        for j in sorted(int(n) for n in tree.query(geometry)):
            if j <= i:
                continue
            if comparisons >= max_relations:
                relations_complete = False
                break
            comparisons += 1
            other = unique[j]
            if not geometry.intersects(other):
                continue
            common = geometry.intersection(other)
            if common.area <= 0:
                continue
            left = [records[k]["entity_key"] for k in members[i]]
            right = [records[k]["entity_key"] for k in members[j]]
            if geometry.covers(other):
                contained.append(
                    {
                        "container_keys": left,
                        "contained_keys": right,
                        "area": other.area,
                        "bbox": _world_bounds(other, origin),
                    }
                )
            elif other.covers(geometry):
                contained.append(
                    {
                        "container_keys": right,
                        "contained_keys": left,
                        "area": geometry.area,
                        "bbox": _world_bounds(geometry, origin),
                    }
                )
            else:
                overlaps.append(
                    {
                        "left_keys": left,
                        "right_keys": right,
                        "area": common.area,
                        "bbox": _world_bounds(common, origin),
                    }
                )
        if not relations_complete:
            break
    warnings = []
    if rejected:
        warnings.append(
            "Open, invalid, nonplanar or unsupported entities were excluded; selection area is incomplete."
        )
    if approximation:
        warnings.append(
            "Curves were deterministically tessellated. No certified area error bound is available."
        )
    if duplicates:
        warnings.append(
            "Duplicate contours exist; sum_area counts their multiplicity, union_area does not."
        )
    if not relations_complete:
        warnings.append(
            "Pair relation evidence reached max_relations; union totals still cover all accepted boundaries."
        )
    if tolerance:
        warnings.append(
            "Explicit precision-grid snapping may alter union area; inspect precision_area_delta."
        )
    result = {
        "mode": mode,
        "area": totals[{"union": "union_area", "sum": "sum_area", "overlap": "overlap_area"}[mode]],
        **totals,
        "area_complete": not rejected,
        "selected_count": selected_count,
        "accepted_count": len(records),
        "rejected_count": len(rejected),
        "repaired_count": len(repaired),
        "repair_requested": make_valid,
        "approximate": approximation or bool(tolerance),
        "include_hidden": include_hidden,
        "hidden_excluded_count": hidden_excluded_count,
        "units": "drawing-unit²",
        "linear_unit": linear_unit,
        "local_origin": list(origin),
        "coordinate_system": "WCS_XY",
        "tolerance": tolerance,
        "tolerance_kind": "explicit_precision_grid",
        "flatten_tolerance": flatten_tolerance,
        "flatten_rule_version": FLATTEN_RULE_VERSION,
        "area_error_bound": None,
        "area_error_bound_status": "not_certified",
        "overlap_area_kind": "sum_area_minus_unsnapped_union_area_multiplicity_excess",
        "relation_candidate_count": comparisons,
        "relations_complete": relations_complete,
        "duplicate_group_count": len(duplicates),
        "contained_count": len(contained),
        "overlap_pair_count": len(overlaps),
        "group_by": by,
        "group_kind": "root_block_instance" if by == "block" else by,
        "groups": [
            {"key": key, **_metrics(values, tolerance)[0], "count": len(values)}
            for key, values in sorted(groups.items())
        ],
        "warnings": warnings,
    }
    if linear_unit:
        factor = _LINEAR_TO_METRES[linear_unit] ** 2
        result["conversion"] = {
            "explicit_linear_unit": linear_unit,
            "factor_to_m2": factor,
            "sum_area_m2": totals["sum_area"] * factor,
            "union_area_m2": totals["union_area"] * factor,
            "area_m2": result["area"] * factor,
            "overlap_area_m2": totals["overlap_area"] * factor,
        }
    if source_info is not None:
        result["source_identity"] = source_info
        if (
            source_info.get("path")
            and Path(source_info["path"]).is_file()
            and file_sha256(source_info["path"]) != source_info.get("sha256")
        ):
            raise CadError("SOURCE_CHANGED", "Original source changed during area analysis.")
    if source is not None and file_sha256(source) != source_digest:
        raise CadError("SOURCE_CHANGED", "Drawing changed during area analysis.")
    full = {
        **result,
        "entities": records,
        "rejected": rejected,
        "repaired": repaired,
        "duplicates": duplicates,
        "contained": contained,
        "overlaps": overlaps,
    }
    if output is not None:
        full["union_geometry"] = mapping(translate(merged, xoff=origin[0], yoff=origin[1]))
        target = Path(output).expanduser().resolve()
        protected = [source] if source is not None else []
        if isinstance(source_info, dict) and source_info.get("path"):
            protected.append(source_info["path"])
        if any(target == Path(p).expanduser().resolve() for p in protected):
            raise CadError("OUTPUT_CONFLICT", "Area evidence must not overwrite the input drawing.")
        raw = json.dumps(full, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(raw) > 64 * 1024 * 1024:
            raise CadError(
                "OUTPUT_TOO_LARGE", "Area evidence exceeds 64 MiB; narrow the selection."
            )
        with revision_guard(source, target, expected_source_sha256=source_digest):
            with atomic_output(target, overwrite=overwrite) as temporary:
                Path(temporary).write_bytes(raw)
        result["artifact"] = {
            "path": str(target),
            "format": "json",
            "bytes": len(raw),
            "sha256": file_sha256(target),
            "evidence_complete": relations_complete,
        }
    for name in ("entities", "rejected", "repaired", "duplicates", "contained", "overlaps"):
        result[name] = [dict(item) for item in full[name][:evidence_limit]]
        for item in result[name]:
            for key in (
                "entity_keys",
                "container_keys",
                "contained_keys",
                "left_keys",
                "right_keys",
                "layers",
            ):
                if key in item and len(item[key]) > evidence_limit:
                    item[key + "_count"] = len(item[key])
                    item[key] = item[key][:evidence_limit]
                    item[key + "_complete"] = False
    result["evidence"] = {
        "limit_per_list": evidence_limit,
        "truncated": any(
            len(full[name]) > evidence_limit
            for name in ("entities", "rejected", "repaired", "duplicates", "contained", "overlaps")
        ),
        "full_artifact": output is not None,
        "relations_complete": relations_complete,
    }
    if any(
        any(key.endswith("_complete") and value is False for key, value in item.items())
        for name in ("duplicates", "contained", "overlaps")
        for item in result[name]
    ):
        result["evidence"]["truncated"] = True
    # Group counts can also be arbitrarily large; retain complete data in artifact.
    if len(result["groups"]) > evidence_limit:
        result["group_count"] = len(result["groups"])
        result["groups"] = result["groups"][:evidence_limit]
        result["evidence"]["truncated"] = True
    if source:
        result["source"] = str(source)
    return result


def area_entities(
    path,
    *,
    handles=None,
    layout="Model",
    layers=None,
    entity_types=None,
    bbox=None,
    source_info=None,
    **options,
):
    """Standalone adapter; root CLI can pass its shared selector to area_selected."""
    from .engine import read_document, _layout, _selected_entities
    from .spatial import iter_world_entities, select_handles

    source_digest = file_sha256(path)
    selection = None
    if bbox is not None and handles is None:
        # Select conservative native root bounds before recursively inspecting
        # their leaves. Layer/type predicates belong to leaves, since a selected
        # INSERT can contain geometry on multiple inherited/explicit layers.
        selection = select_handles(path, layout=layout, bbox=bbox)
        handles = selection["handles"]
    document = read_document(path)
    space = _layout(document, layout)
    roots = (
        _selected_entities(document, space, handles)
        if handles
        else ([] if handles is not None else None)
    )
    if isinstance(layers, str):
        layers = [layers]
    if isinstance(entity_types, str):
        entity_types = [entity_types]
    diagnostics = {}
    entities = iter_world_entities(
        document,
        layout=layout,
        roots=roots,
        layers=layers,
        types=entity_types,
        bbox=bbox,
        diagnostics=diagnostics,
    )
    result = area_selected(
        entities,
        source=path,
        source_info=source_info,
        expected_source_sha256=source_digest,
        **options,
    )
    result["selection_diagnostics"] = diagnostics
    if selection is not None:
        result["selection"] = {
            key: selection[key]
            for key in ("source_revision", "source_sha256", "cache", "entity_count")
        }
    if diagnostics.get("omitted_count") or diagnostics.get("omitted"):
        result["area_complete"] = False
    return result
