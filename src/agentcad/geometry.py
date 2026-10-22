# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0
"""Deterministic planar boundary extraction, independent of CLI selection.

Boolean operations use WCS XY; measurement may explicitly use the entity plane.
Curve tessellation is an approximation and never advertises a certified area bound.
"""

import math
from dataclasses import dataclass, field

from ezdxf.math import Vec3, bulge_to_arc
from shapely import Polygon, GeometryCollection, unary_union
from shapely.validation import explain_validity

from .errors import CadError

FLATTEN_RULE_VERSION = "tcad-planar-2"
MAX_BOUNDARY_VERTICES = 1_000_000


@dataclass
class BoundaryGeometry:
    geometry: object
    closed: bool
    approximate: bool = False
    vertex_count: int = 0
    warnings: list = field(default_factory=list)
    invalid_reason: str | None = None
    length: float | None = None
    closure_evidence: str | None = None


def _bounded_vertices(values):
    points = []
    for point in values:
        if len(points) >= MAX_BOUNDARY_VERTICES:
            raise CadError("GEOMETRY_LIMIT", "Boundary exceeds the vertex budget.")
        points.append(point)
    return points


def _step(radius, tolerance, sweep=math.tau):
    if radius <= 0 or not math.isfinite(radius):
        raise ValueError("Curve radius must be positive and finite.")
    angle = 2 * math.acos(max(-1.0, min(1.0, 1 - tolerance / radius)))
    # asin avoids catastrophic cancellation for very small tolerance / radius.
    if angle == 0:
        angle = 4 * math.asin(min(1.0, math.sqrt(tolerance / (2 * radius))))
    count = max(4, math.ceil(abs(sweep) / angle))
    if count + 1 > MAX_BOUNDARY_VERTICES:
        raise CadError("GEOMETRY_LIMIT", "Curve exceeds the boundary vertex budget.")
    return count


def _arc(center, radius, start, sweep, tolerance):
    count = _step(radius, tolerance, sweep)
    points = [
        (
            center[0] + radius * math.cos(start + sweep * i / count),
            center[1] + radius * math.sin(start + sweep * i / count),
            0,
        )
        for i in range(count + 1)
    ]
    if math.isclose(abs(sweep), math.tau, abs_tol=1e-12):
        points[-1] = points[0]
    return points


def _poly_vertices(vertices, closed, tolerance):
    result = []
    curved = False
    count = len(vertices) if closed else max(0, len(vertices) - 1)
    for i in range(count):
        a, b = vertices[i], vertices[(i + 1) % len(vertices)]
        if a[2] and (a[0], a[1]) != (b[0], b[1]):
            curved = True
            center, start, end, radius = bulge_to_arc(a[:2], b[:2], a[2])
            points = _arc(center, radius, start, (end - start) % math.tau, tolerance)
            if a[2] < 0:
                points.reverse()
        else:
            points = [(a[0], a[1], 0), (b[0], b[1], 0)]
        result.extend(points[:-1])
        if len(result) > MAX_BOUNDARY_VERTICES:
            raise CadError("GEOMETRY_LIMIT", "Boundary exceeds the vertex budget.")
    if count:
        result.append(result[0] if closed else (vertices[-1][0], vertices[-1][1], 0))
    return result, curved


def _ellipse(tool, tolerance):
    sweep = (tool.end_param - tool.start_param) % math.tau
    if math.isclose(sweep, 0, abs_tol=1e-12):
        sweep = math.tau
    count = _step(max(tool.major_axis.magnitude, tool.minor_axis.magnitude), tolerance, sweep)
    points = list(tool.vertices(tool.start_param + sweep * i / count for i in range(count + 1)))
    if math.isclose(sweep, math.tau, abs_tol=1e-12):
        points[-1] = points[0]
    return points


def _hatch_loops(entity, tolerance):
    loops = []
    curved = False
    total_vertices = 0
    for boundary in entity.paths:
        if hasattr(boundary, "vertices"):
            points, has_curve = _poly_vertices(boundary.vertices, boundary.is_closed, tolerance)
            if not boundary.is_closed:
                raise ValueError("HATCH contains an open polyline boundary.")
            curved |= has_curve
        else:
            points = []
            for edge in boundary.edges:
                kind = edge.EDGE_TYPE
                if kind == "LineEdge":
                    part = [Vec3(edge.start), Vec3(edge.end)]
                elif kind == "ArcEdge":
                    sweep = math.radians((edge.end_angle - edge.start_angle) % 360)
                    if math.isclose(sweep, 0, abs_tol=1e-12):
                        sweep = math.tau
                    part = _arc(
                        edge.center, edge.radius, math.radians(edge.start_angle), sweep, tolerance
                    )
                    if not edge.ccw:
                        part.reverse()
                    curved = True
                elif kind == "EllipseEdge":
                    part = _ellipse(edge.construction_tool(), tolerance)
                    if not edge.ccw:
                        part.reverse()
                    curved = True
                elif kind == "SplineEdge":
                    part = _bounded_vertices(
                        edge.construction_tool().flattening(tolerance, segments=8)
                    )
                    curved = True
                else:
                    raise ValueError(f"Unsupported HATCH edge: {kind}.")
                if not part:
                    raise ValueError("Empty HATCH boundary edge.")
                if points and (Vec3(points[-1]) - Vec3(part[0])).magnitude > 1e-7:
                    gap = (Vec3(points[-1]) - Vec3(part[0])).magnitude
                    raise ValueError(
                        f"HATCH boundary edges are disconnected (gap={gap:.12g} drawing units)."
                    )
                points.extend(part if not points else part[1:])
                if len(points) > MAX_BOUNDARY_VERTICES:
                    raise CadError("GEOMETRY_LIMIT", "HATCH exceeds the boundary vertex budget.")
            if len(points) < 4:
                raise ValueError("HATCH boundary has fewer than three distinct vertices.")
            gap = (Vec3(points[0]) - Vec3(points[-1])).magnitude
            if gap > 1e-7:
                raise ValueError(f"HATCH boundary is open (gap={gap:.12g} drawing units).")
        loops.append(points)
        total_vertices += len(points)
        if total_vertices > MAX_BOUNDARY_VERTICES:
            raise CadError("GEOMETRY_LIMIT", "HATCH exceeds the total boundary vertex budget.")
    return loops, curved


def _map_points(points, entity, *, ocs, elevation, plane, transform, origin):
    converted = []
    entity_ocs = entity.ocs() if hasattr(entity, "ocs") else None
    for point in points:
        value = Vec3(point)
        if ocs:
            value = entity_ocs.to_wcs((value.x, value.y, elevation))
        if transform is not None:
            value = transform.transform(value)
        if plane == "entity" and entity_ocs is not None:
            value = entity_ocs.from_wcs(value)
        if not all(math.isfinite(coordinate) for coordinate in value):
            raise ValueError("Boundary contains a nonfinite coordinate.")
        converted.append(value)
    if not converted:
        return []
    z0 = converted[0].z
    if any(abs(p.z - z0) > 1e-7 for p in converted):
        raise ValueError(
            "Boundary is not parallel to WCS XY; projection is not an area measurement."
        )
    return [(p.x - origin[0], p.y - origin[1]) for p in converted]


def _hatch_polygon(polygons, style):
    # Intersecting, non-nested boundaries have ambiguous fill semantics.
    from shapely import STRtree

    tree = STRtree(polygons)
    depths = [0] * len(polygons)
    for i, polygon in enumerate(polygons):
        for j in tree.query(polygon):
            j = int(j)
            if i == j:
                continue
            other = polygons[j]
            if other.equals(polygon):
                raise ValueError("HATCH has duplicate boundary rings.")
            if other.covers(polygon):
                depths[i] += 1
            elif j > i and polygon.intersection(other).area > 0 and not polygon.covers(other):
                raise ValueError("HATCH boundary rings overlap without containment.")
    if style == 2:
        return unary_union([p for p, depth in zip(polygons, depths) if depth == 0])
    value = GeometryCollection()
    for p, depth in sorted(zip(polygons, depths), key=lambda pair: pair[1]):
        if style == 1 and depth > 1:
            continue
        value = value.union(p) if depth % 2 == 0 else value.difference(p)
    return value


def entity_to_polygon(
    entity, *, flatten_tolerance=0.1, transform=None, origin=(0.0, 0.0), plane="world"
):
    """Extract a closed polygon; invalid/open inputs remain explicit rejections.

    ``transform`` is an optional ezdxf Matrix44 after native OCS to WCS conversion.
    ``plane='entity'`` is for single entity measurement, never cross-entity union.
    """
    if (
        type(flatten_tolerance) not in {int, float}
        or not math.isfinite(flatten_tolerance)
        or flatten_tolerance <= 0
    ):
        raise CadError("INVALID_ARGUMENT", "flatten_tolerance must be positive and finite.")
    if plane not in {"world", "entity"}:
        raise CadError("INVALID_ARGUMENT", "plane must be world or entity.")
    if transform is not None:
        # Frobenius norm bounds the largest linear amplification, so curve
        # tessellation remains at least as fine after nonuniform transforms.
        amplification = math.sqrt(sum(transform[i, j] ** 2 for i in range(3) for j in range(3)))
        if amplification > 0:
            flatten_tolerance /= amplification
    kind = entity.dxftype()
    approximate = False
    closed = True
    closure_evidence = "native_closed_definition"
    ocs = False
    elevation = 0.0
    try:
        if kind == "LWPOLYLINE":
            vertices = list(entity.get_points("xyb"))
            native_closed = bool(entity.closed)
            repeated_endpoint = len(vertices) >= 4 and vertices[0][:2] == vertices[-1][:2]
            closed = native_closed or repeated_endpoint
            closure_evidence = (
                "native_closed_flag"
                if native_closed
                else "exact_repeated_endpoint"
                if repeated_endpoint
                else "open"
            )
            points, approximate = _poly_vertices(vertices, native_closed, flatten_tolerance)
            loops = [points]
            ocs, elevation = True, float(entity.dxf.elevation)
        elif kind == "POLYLINE" and entity.is_2d_polyline:
            native_closed = bool(entity.is_closed)
            values = [
                (v.dxf.location.x, v.dxf.location.y, float(v.dxf.bulge)) for v in entity.vertices
            ]
            if any(abs(v.dxf.location.z) > 1e-12 for v in entity.vertices):
                raise ValueError("2D POLYLINE has ambiguous nonzero vertex Z coordinates.")
            repeated_endpoint = len(values) >= 4 and values[0][:2] == values[-1][:2]
            closed = native_closed or repeated_endpoint
            closure_evidence = (
                "native_closed_flag"
                if native_closed
                else "exact_repeated_endpoint"
                if repeated_endpoint
                else "open"
            )
            points, approximate = _poly_vertices(values, native_closed, flatten_tolerance)
            loops = [points]
            ocs, elevation = True, float(entity.dxf.elevation.z)
        elif kind == "CIRCLE":
            loops = [
                _arc(entity.dxf.center, float(entity.dxf.radius), 0, math.tau, flatten_tolerance)
            ]
            ocs, elevation = True, float(entity.dxf.center.z)
            approximate = True
        elif kind == "ELLIPSE":
            tool = entity.construction_tool()
            closed = math.isclose((tool.end_param - tool.start_param) % math.tau, 0, abs_tol=1e-10)
            loops = [_ellipse(tool, flatten_tolerance)]
            approximate = True
        elif kind == "SPLINE":
            points = _bounded_vertices(entity.flattening(flatten_tolerance, segments=8))
            closed = bool(
                entity.closed or (len(points) > 2 and points[0].isclose(points[-1], abs_tol=1e-9))
            )
            if closed and points and points[0].isclose(points[-1], abs_tol=1e-9):
                points[-1] = points[0]
            loops, approximate = [points], True
        elif kind == "HATCH":
            loops, approximate = _hatch_loops(entity, flatten_tolerance)
            ocs, elevation = True, float(entity.dxf.elevation.z)
        else:
            return BoundaryGeometry(
                None, False, invalid_reason=f"Unsupported area entity type: {kind}."
            )
        mapped = [
            _map_points(
                p,
                entity,
                ocs=ocs,
                elevation=elevation,
                plane=plane,
                transform=transform,
                origin=origin,
            )
            for p in loops
        ]
        length = math.fsum(math.dist(a, b) for loop in mapped for a, b in zip(loop, loop[1:]))
        count = sum(len(loop) for loop in mapped)
        if not closed:
            return BoundaryGeometry(
                None, False, approximate, count, invalid_reason="Boundary is open.", length=length
            )
        if not mapped or any(len(loop) < 4 for loop in mapped):
            raise ValueError("Boundary has fewer than three distinct vertices.")
        polygons = [Polygon(loop) for loop in mapped]
        invalid = next((explain_validity(p) for p in polygons if not p.is_valid), None)
        if invalid:
            # Preserve invalid geometry for an explicitly requested make_valid.
            return BoundaryGeometry(
                polygons[0] if len(polygons) == 1 else None,
                True,
                approximate,
                count,
                invalid_reason=invalid,
                length=length,
            )
        value = _hatch_polygon(polygons, entity.dxf.hatch_style) if kind == "HATCH" else polygons[0]
        if value.is_empty or value.area <= 0:
            raise ValueError("Boundary has zero area.")
        return BoundaryGeometry(
            value, True, approximate, count, length=length, closure_evidence=closure_evidence
        )
    except CadError:
        raise
    except (ValueError, TypeError, AttributeError, ZeroDivisionError) as exc:
        return BoundaryGeometry(None, closed, approximate, invalid_reason=str(exc))


def measurement_extension(entity, tolerance=0.1):
    """Return measurement supplements, preserving exact polyline length in engine.

    For LWPOLYLINE/POLYLINE return validity fields only unless area is invalid.
    HATCH perimeter counts every source ring (including islands).
    """
    kind = entity.dxftype()
    if kind not in {"LWPOLYLINE", "POLYLINE", "HATCH", "ELLIPSE", "SPLINE"}:
        return None
    if kind == "POLYLINE" and not entity.is_2d_polyline:
        return None
    value = entity_to_polygon(entity, flatten_tolerance=tolerance, plane="entity")
    meta = {
        "area_valid": value.invalid_reason is None and value.closed,
        "flatten_tolerance": tolerance,
        "flatten_rule_version": FLATTEN_RULE_VERSION,
        "area_error_bound": None,
        "area_error_bound_status": "not_certified"
        if value.approximate
        else "floating_point_not_certified",
    }
    meta["closure_evidence"] = value.closure_evidence
    if value.closed:
        meta["closed"] = True
    if kind in {"LWPOLYLINE", "POLYLINE"}:
        if value.invalid_reason:
            meta.update(
                area=None,
                signed_area=None,
                area_kind="invalid_boundary" if value.closed else "not_defined_for_open_polyline",
                warnings=[value.invalid_reason],
            )
        else:
            meta["warnings"] = []
        return meta
    meta.update(
        supported=value.length is not None,
        closed=value.closed,
        length=value.length,
        length_complete=value.length is not None,
        length_kind="flattened_boundary" if value.approximate else "exact_linear_boundary",
        area=value.geometry.area
        if value.geometry is not None and not value.invalid_reason
        else None,
        area_kind="flattened_entity_plane" if value.approximate else "polygon_entity_plane",
        warnings=value.warnings + ([value.invalid_reason] if value.invalid_reason else []),
    )
    if kind == "SPLINE" and value.length is None:
        points = _bounded_vertices(entity.flattening(tolerance, segments=8))
        meta.update(
            supported=True,
            length=math.fsum((b - a).magnitude for a, b in zip(points, points[1:])),
            length_complete=True,
            length_kind="flattened_3d_centerline",
        )
    if kind == "ELLIPSE" and value.closed and value.invalid_reason is None:
        tool = entity.construction_tool()
        meta.update(
            area=math.pi * tool.major_axis.magnitude * tool.minor_axis.magnitude,
            area_kind="exact_ellipse_in_entity_plane",
        )
    return meta
