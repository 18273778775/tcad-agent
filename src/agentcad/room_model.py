# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Rectangular room solids and verification of the serialized geometry.

The mesh coordinate frame is local: the clear interior southwest corner at the
finished floor is (0, 0, 0). Internal, OBJ and DXF coordinates use Z up. GLB
uses the glTF right-handed Y-up frame: (x, y, z) becomes (x, z, -y), in metres.
DXF coordinates retain the explicitly declared drawing units. Every solid is a separate box;
the assembly is deliberately not represented as one Boolean-unioned solid.
"""

import math
from pathlib import Path
import re

import ezdxf
import numpy as np

from .errors import CadError
from .io import atomic_output
from .room import extract_room, source_identity, validate_room_spec


_UNITS = {"mm": (0.001, 4), "cm": (0.01, 5), "m": (1.0, 6),
          "in": (0.0254, 1), "ft": (0.3048, 2)}
_ROLE = re.compile(r"tcad_wall_(south|north|west|east)_[0-9]+\Z")
_SIDES = ("south", "north", "west", "east")
# A proper rotation (determinant +1), preserving winding and volume.
ARCHITECTURAL_TO_GLTF = np.array([[1., 0., 0., 0.], [0., 0., 1., 0.],
                                  [0., -1., 0., 0.], [0., 0., 0., 1.]])
GLTF_TO_ARCHITECTURAL = ARCHITECTURAL_TO_GLTF.T.copy()
_GLB_CONVENTION = {"schema_version": "tcad-room-2.0",
                   "coordinate_system": "gltf_y_up_right_handed", "units": "m"}


def _trimesh():
    try:
        import trimesh
    except ImportError as exc:
        raise CadError("DEPENDENCY_MISSING", "Room models require the trimesh package.") from exc
    return trimesh


def _number(value, label, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CadError("INVALID_ROOM_SPEC", f"{label} must be a finite number.")
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0) or (nonnegative and value < 0):
        raise CadError("INVALID_ROOM_SPEC", f"{label} has an invalid value.")
    return value


def _validate_spec(spec):
    """Use the public schema and reject geometrically unidentifiable roles."""
    validate_room_spec(spec)
    dimensions = spec["dimensions"]
    intervals = {side: [] for side in _SIDES}
    for opening in spec["openings"]:
        wall, offset, width = opening["wall"], opening["offset"], opening["width"]
        span = dimensions["length" if wall in ("south", "north") else "width"]
        if width == span and opening["height"] == dimensions["height"]:
            raise CadError("INVALID_ROOM_SPEC", "An opening cannot remove an entire wall.")
        for start, end in intervals[wall]:
            if offset == end or offset + width == start:
                raise CadError("INVALID_ROOM_SPEC",
                               "Touching openings have no independently measurable divider.")
        intervals[wall].append((offset, offset + width))
    return float(spec["drawing_unit_to_m"])


def _box(name, low, high):
    mesh = _trimesh().creation.box(extents=np.subtract(high, low))
    mesh.apply_translation((np.asarray(high) + low) / 2)
    mesh.units = "m"
    if name == "tcad_floor":
        color = [155, 143, 125, 255]
    elif name == "tcad_ceiling":
        color = [230, 233, 239, 90]
    else:
        color = [208, 214, 222, 255]
    # Vertex colors avoid trimesh's optional SciPy face-to-vertex conversion.
    mesh.visual.vertex_colors = np.tile(color, (len(mesh.vertices), 1))
    return mesh


def _parts(spec, scale):
    length, width, height = (float(spec["dimensions"][key]) * scale
                             for key in ("length", "width", "height"))
    thickness, floor, ceiling = (float(spec["construction"][key]) * scale
                                  for key in ("wall_thickness", "floor_thickness",
                                              "ceiling_thickness"))
    parts = {
        "tcad_floor": _box("tcad_floor", [-thickness, -thickness, -floor],
                           [length + thickness, width + thickness, 0]),
        "tcad_ceiling": _box("tcad_ceiling", [-thickness, -thickness, height],
                             [length + thickness, width + thickness, height + ceiling]),
    }
    for side in _SIDES:
        along_x = side in ("south", "north")
        start, end = (0, length) if along_x else (-thickness, width + thickness)
        openings = sorted((item for item in spec["openings"] if item["wall"] == side),
                          key=lambda item: item["offset"])
        rectangles, cursor = [], start
        for opening in openings:
            a, b = opening["offset"] * scale, (opening["offset"] + opening["width"]) * scale
            sill, head = opening["sill"] * scale, (opening["sill"] + opening["height"]) * scale
            if a > cursor:
                rectangles.append((cursor, a, 0, height))
            if sill > 0:
                rectangles.append((a, b, 0, sill))
            if head < height:
                rectangles.append((a, b, head, height))
            cursor = b
        if cursor < end:
            rectangles.append((cursor, end, 0, height))
        for index, (a, b, bottom, top) in enumerate(rectangles):
            name = f"tcad_wall_{side}_{index:03d}"
            if side == "south":
                low, high = [a, -thickness, bottom], [b, 0, top]
            elif side == "north":
                low, high = [a, width, bottom], [b, width + thickness, top]
            elif side == "west":
                low, high = [-thickness, a, bottom], [0, b, top]
            else:
                low, high = [length, a, bottom], [length + thickness, b, top]
            parts[name] = _box(name, low, high)
    return parts


def _write_obj(parts, destination):
    # OBJ itself has no unit field.  The public API contract fixes metres.
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("# TCAD room model; coordinates in metres; Z is up\n")
        offset = 1
        for name, mesh in parts.items():
            stream.write(f"o {name}\n")
            for vertex in mesh.vertices:
                stream.write("v " + " ".join(format(float(v), ".17g") for v in vertex) + "\n")
            for face in mesh.faces:
                stream.write("f " + " ".join(str(int(v) + offset) for v in face) + "\n")
            offset += len(mesh.vertices)


def _write_dxf(parts, destination, units, scale):
    doc = ezdxf.new("R2013", units=_UNITS[units][1])
    space = doc.modelspace()
    for name, mesh in parts.items():
        doc.layers.new(name)
        vertices = np.asarray(mesh.vertices) / scale
        for face in mesh.faces:
            space.add_3dface(vertices[face].tolist(), dxfattribs={"layer": name})
    doc.saveas(destination)


def build_room_model(spec, output, overwrite=False):
    """Atomically build and independently verify a GLB, OBJ, or 3D DXF model."""
    scale = _validate_spec(spec)
    target = Path(output).expanduser().resolve()
    extension = target.suffix.lower()
    if extension not in (".glb", ".obj", ".dxf"):
        raise CadError("UNSUPPORTED_FORMAT", "Room model output must be .glb, .obj, or .dxf.")
    parts = _parts(spec, scale)
    with atomic_output(target, overwrite=overwrite) as temporary:
        if extension == ".glb":
            scene = _trimesh().Scene()
            scene.metadata["tcad_room"] = dict(_GLB_CONVENTION)
            for name, mesh in parts.items():
                transformed = mesh.copy()
                transformed.apply_transform(ARCHITECTURAL_TO_GLTF)
                scene.add_geometry(transformed, node_name=name, geom_name=name)
            temporary.write_bytes(scene.export(file_type="glb"))
        elif extension == ".obj":
            _write_obj(parts, temporary)
        else:
            _write_dxf(parts, temporary, spec["units"], scale)
        verification = verify_room_model(spec, temporary)
    verification["model_path"] = str(target)
    return {
        "written": True, "output": str(target), "format": extension[1:],
        "model_units": spec["units"] if extension == ".dxf" else "m",
        "source_units": spec["units"], "drawing_unit_to_m": scale,
        "coordinate_frame": ("glTF right-handed Y-up; metres; architectural XYZ maps to (X,Z,-Y)"
                             if extension == ".glb" else "local Z-up XYZ; southwest floor origin"),
        "model_schema_version": "tcad-room-2.0",
        "source_origin": list(spec["origin"]), "geometry_count": len(parts),
        "verification": verification,
    }


def _load_obj(path):
    vertices, objects, current = [], {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        tag = fields[0]
        if tag in ("o", "g"):
            if len(fields) != 2:
                raise ValueError("OBJ objects must have one unambiguous role name.")
            current = fields[1]
            if current in objects:
                raise ValueError("Duplicate OBJ object role.")
            objects[current] = []
        elif tag == "v":
            if len(fields) != 4:
                raise ValueError("OBJ vertices must contain exactly XYZ coordinates.")
            vertices.append([float(item) for item in fields[1:]])
        elif tag == "f":
            if current is None or len(fields) != 4:
                raise ValueError("OBJ room geometry requires named triangular objects.")
            face = []
            for item in fields[1:]:
                index = int(item.split("/")[0])
                index = index - 1 if index > 0 else len(vertices) + index
                if not 0 <= index < len(vertices):
                    raise ValueError("OBJ face references an absent vertex.")
                face.append(index)
            objects[current].append(face)
        elif tag not in ("vn", "vt", "s", "mtllib", "usemtl"):
            raise ValueError(f"Unsupported OBJ geometry directive: {tag}")
    vertices = np.asarray(vertices, dtype=float)
    result = {}
    for name, faces in objects.items():
        if not faces:
            raise ValueError("Empty OBJ role.")
        faces = np.asarray(faces, dtype=int)
        indices, inverse = np.unique(faces, return_inverse=True)
        result[name] = (vertices[indices], inverse.reshape((-1, 3)))
    return result, "m", 1.0


def load_glb_room_components(path):
    """Load marked glTF Y-up room geometry into architectural Z-up metres.

    Old TCAD GLBs did not mark their nonstandard Z-up frame. Reject these rather
    than silently interpreting them as standard glTF. Rebuild from their spec.
    The marker selects the frame only; geometric verification never trusts it
    for dimensions, positions, openings or component orientation.
    """
    trimesh = _trimesh()
    scene = trimesh.load_scene(path, process=False)
    if scene.metadata.get("tcad_room") != _GLB_CONVENTION:
        raise CadError("UNSUPPORTED_MODEL_CONVENTION",
                       "GLB needs the TCAD room-2.0 Y-up coordinate marker; rebuild legacy GLBs from their room spec.",
                       {"required": _GLB_CONVENTION,
                        "found": scene.metadata.get("tcad_room"),
                        "recovery": "room-model --spec <room.json> --output <new.glb>"})
    result = {}
    for node in scene.graph.nodes_geometry:
        transform, geometry = scene.graph[node]
        # Use the scene node's world transform; raw geometry vertices alone are
        # insufficient when a serializer or downstream editor moves a node.
        name = str(node)
        if name in result:
            raise ValueError("Duplicate GLB node role.")
        mesh = scene.geometry[geometry]
        if not isinstance(mesh, trimesh.Trimesh):
            raise ValueError("Every named room component must be a triangle mesh.")
        vertices = trimesh.transform_points(np.asarray(mesh.vertices), transform)
        vertices = trimesh.transform_points(vertices, GLTF_TO_ARCHITECTURAL)
        result[name] = (vertices, np.asarray(mesh.faces))
    return result, "m", 1.0


_load_glb = load_glb_room_components


def _load_dxf(path):
    doc = ezdxf.readfile(path)
    unit = next((name for name, (_, code) in _UNITS.items() if code == doc.units), None)
    if unit is None:
        raise ValueError("3D DXF has missing or unsupported INSUNITS.")
    parts = {}
    for entity in doc.modelspace():
        if entity.dxftype() != "3DFACE":
            raise ValueError("3D room DXF must contain only named 3DFACE components.")
        points = [tuple(entity.dxf.get(f"vtx{index}")) for index in range(4)]
        if points[2] != points[3]:
            raise ValueError("3D room DXF requires triangular 3DFACE entities.")
        parts.setdefault(entity.dxf.layer, []).append(points[:3])
    result = {}
    for name, triangles in parts.items():
        vertices = np.asarray(triangles, dtype=float).reshape((-1, 3))
        result[name] = (vertices, np.arange(len(vertices)).reshape((-1, 3)))
    return result, unit, _UNITS[unit][0]


def _box_bounds(vertices, faces, epsilon):
    """Establish a solid box from actual triangles, before using its intervals."""
    if vertices.ndim != 2 or vertices.shape[1] != 3 or not np.isfinite(vertices).all():
        raise ValueError("Non-finite or malformed mesh vertices.")
    if faces.shape != (12, 3) or faces.min() < 0 or faces.max() >= len(vertices):
        raise ValueError("A room component must have twelve box triangles.")
    unique, inverse = np.unique(vertices, axis=0, return_inverse=True)
    if len(unique) != 8:
        raise ValueError("A room component must have eight distinct box corners.")
    low, high = unique.min(axis=0), unique.max(axis=0)
    if np.any(high - low <= epsilon):
        raise ValueError("A room component has a degenerate dimension.")
    on_low = np.abs(unique - low) <= epsilon
    on_high = np.abs(unique - high) <= epsilon
    if not np.all(on_low | on_high):
        raise ValueError("A room component is not an axis-aligned rectangular solid.")
    if len(set(map(tuple, on_high.tolist()))) != 8:
        raise ValueError("A room component is missing a box corner.")
    indexed = inverse[faces]
    mesh = _trimesh().Trimesh(vertices=unique, faces=indexed, process=False)
    if not mesh.is_watertight or not mesh.is_winding_consistent:
        raise ValueError("A room component is not a consistently oriented closed solid.")
    triangles = unique[indexed]
    areas = np.linalg.norm(np.cross(triangles[:, 1] - triangles[:, 0],
                                    triangles[:, 2] - triangles[:, 0]), axis=1) / 2
    accounted = np.zeros(12, dtype=bool)
    for axis in range(3):
        other = [index for index in range(3) if index != axis]
        expected = float(np.prod((high - low)[other]))
        for coordinate in (low[axis], high[axis]):
            on_plane = np.all(np.abs(triangles[:, :, axis] - coordinate) <= epsilon, axis=1)
            accounted |= on_plane
            if not math.isclose(float(areas[on_plane].sum()), expected,
                                rel_tol=1e-7, abs_tol=epsilon * max(high - low)):
                raise ValueError("The triangles do not cover all six rectangular box faces.")
    if not accounted.all() or np.any(areas <= 0):
        raise ValueError("A component contains internal or degenerate triangles.")
    center = (low + high) / 2
    normals = np.cross(triangles[:, 1] - triangles[:, 0],
                       triangles[:, 2] - triangles[:, 0])
    outward = np.einsum("ij,ij->i", normals, triangles.mean(axis=1) - center)
    if not np.all(outward > 0):
        raise ValueError("A room component must have outward face normals.")
    relative = triangles - center
    signed_volume = float(np.einsum("ij,ij->i", relative[:, 0],
                                    np.cross(relative[:, 1], relative[:, 2])).sum() / 6)
    if signed_volume <= 0 or not math.isclose(signed_volume, float(np.prod(high - low)),
                                             rel_tol=1e-7, abs_tol=1e-15):
        raise ValueError("A room component must have positive solid volume.")
    return np.asarray([low, high])


def _coordinates(values, epsilon):
    result = []
    for value in sorted(float(item) for item in values):
        if not result or value - result[-1] > epsilon:
            result.append(value)
    return result


def _voids(rectangles, limits, epsilon):
    """Recover actual connected empty rectangles from serialized wall intervals."""
    u0, u1, z0, z1 = limits
    us = _coordinates([u0, u1] + [v for r in rectangles for v in r[:2]
                                 if u0 < v < u1], epsilon)
    zs = _coordinates([z0, z1] + [v for r in rectangles for v in r[2:]
                                 if z0 < v < z1], epsilon)
    empty = set()
    for i in range(len(us) - 1):
        for j in range(len(zs) - 1):
            u, z = (us[i] + us[i + 1]) / 2, (zs[j] + zs[j + 1]) / 2
            count = sum(a < u < b and c < z < d for a, b, c, d in rectangles)
            if count > 1:
                raise ValueError("Wall components overlap with positive volume.")
            if count == 0:
                empty.add((i, j))
    gaps = []
    while empty:
        seed = empty.pop()
        region, pending = {seed}, [seed]
        while pending:
            i, j = pending.pop()
            for neighbor in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)):
                if neighbor in empty:
                    empty.remove(neighbor)
                    region.add(neighbor)
                    pending.append(neighbor)
        a = min(us[i] for i, j in region)
        b = max(us[i + 1] for i, j in region)
        c = min(zs[j] for i, j in region)
        d = max(zs[j + 1] for i, j in region)
        area = sum((us[i + 1] - us[i]) * (zs[j + 1] - zs[j]) for i, j in region)
        if not math.isclose(area, (b - a) * (d - c), rel_tol=1e-7,
                            abs_tol=epsilon * max(b - a, d - c)):
            raise ValueError("A wall void is not rectangular, or a panel blocks part of it.")
        gaps.append((a, b, c, d))
    return sorted(gaps)


def _measurement(expected_source, actual_m, source_scale, model_scale, tolerance_mm):
    expected_mm, actual_mm = float(expected_source) * source_scale * 1000, float(actual_m) * 1000
    error = abs(actual_mm - expected_mm)
    return {"expected_source_units": float(expected_source), "expected_mm": expected_mm,
            "actual_model_units": float(actual_m) / model_scale, "actual_mm": actual_mm,
            "absolute_error_mm": error, "passed": error <= tolerance_mm}


def verify_room_model(spec, model_path, tolerance_mm=0.01):
    """Measure reopened meshes, including opening voids; raise on any mismatch.

    Component names identify roles only.  Coordinates, transforms, dimensions,
    wall coverage, and voids come from the reopened artifact, never its extras.
    """
    source_scale = _validate_spec(spec)
    tolerance_mm = _number(tolerance_mm, "tolerance_mm", positive=True)
    path = Path(model_path).expanduser().resolve()
    if not path.is_file():
        raise CadError("FILE_NOT_FOUND", "Room model does not exist.", {"path": str(path)})
    loaders = {".glb": _load_glb, ".obj": _load_obj, ".dxf": _load_dxf}
    if path.suffix.lower() not in loaders:
        raise CadError("UNSUPPORTED_FORMAT", "Room model must be .glb, .obj, or .dxf.")
    result = {"passed": False, "verified": False, "model_verified": False,
              "source_verified": False, "verification_scope": "model_against_spec",
              "model_path": str(path),
              "source_units": spec["units"], "drawing_unit_to_m": source_scale,
              "tolerance_mm": tolerance_mm, "dimensions": {}, "openings": [], "checks": []}

    def check(name, passed, **details):
        result["checks"].append({"name": name, "passed": bool(passed), **details})

    def mismatch(reason):
        check("serialized_geometry", False, reason=str(reason))
        raise CadError("MODEL_DIMENSION_MISMATCH",
                       "Serialized room geometry does not match the room specification.", result)

    try:
        parts, model_units, model_scale = loaders[path.suffix.lower()](path)
        result.update(model_units=model_units, model_unit_to_m=model_scale)
        epsilon = max(1e-12, tolerance_mm / 1000)
        bounds, walls = {}, {side: [] for side in _SIDES}
        for name, (vertices, faces) in parts.items():
            match = _ROLE.fullmatch(name)
            if name not in ("tcad_floor", "tcad_ceiling") and match is None:
                raise ValueError(f"Unknown or absent component role: {name}")
            bounds[name] = _box_bounds(np.asarray(vertices) * model_scale,
                                       np.asarray(faces), epsilon)
            if match:
                walls[match.group(1)].append(bounds[name])
        if not all(name in bounds for name in ("tcad_floor", "tcad_ceiling")):
            raise ValueError("Named floor and ceiling components are required.")
        if not all(walls.values()):
            raise ValueError("At least one named solid for each wall is required.")
        floor, ceiling = bounds["tcad_floor"], bounds["tcad_ceiling"]
        west = max(b[1, 0] for b in walls["west"])
        east = min(b[0, 0] for b in walls["east"])
        south = max(b[1, 1] for b in walls["south"])
        north = min(b[0, 1] for b in walls["north"])
        bottom, top = floor[1, 2], ceiling[0, 2]
        measured = {"length": east - west, "width": north - south, "height": top - bottom}
        for name, value in measured.items():
            result["dimensions"][name] = _measurement(spec["dimensions"][name], value,
                                                     source_scale, model_scale, tolerance_mm)
        check("source_and_serialized_units", path.suffix.lower() != ".dxf" or
              model_units == spec["units"])
        check("local_interior_origin", max(abs(west), abs(south), abs(bottom)) <= epsilon)
        length, width, height = (spec["dimensions"][key] * source_scale
                                 for key in ("length", "width", "height"))
        thick, floor_t, ceiling_t = (spec["construction"][key] * source_scale
                                     for key in ("wall_thickness", "floor_thickness",
                                                 "ceiling_thickness"))
        expected_floor = [[-thick, -thick, -floor_t], [length + thick, width + thick, 0]]
        expected_ceiling = [[-thick, -thick, height],
                            [length + thick, width + thick, height + ceiling_t]]
        check("floor_extent_and_thickness", np.max(np.abs(floor - expected_floor)) <= epsilon)
        check("ceiling_extent_and_thickness", np.max(np.abs(ceiling - expected_ceiling)) <= epsilon)
        for side in _SIDES:
            along_x = side in ("south", "north")
            normal = 1 if along_x else 0
            along = 0 if along_x else 1
            normal_range = {"south": (-thick, 0), "north": (width, width + thick),
                            "west": (-thick, 0), "east": (length, length + thick)}[side]
            start, end = (west, east) if along_x else (south - thick, north + thick)
            rectangles = []
            valid = True
            for box in walls[side]:
                valid &= bool(np.max(np.abs(box[:, normal] - normal_range)) <= epsilon)
                valid &= bool(box[0, along] >= start - epsilon and
                              box[1, along] <= end + epsilon and
                              box[0, 2] >= bottom - epsilon and box[1, 2] <= top + epsilon)
                rectangles.append((box[0, along], box[1, along], box[0, 2], box[1, 2]))
            check(f"{side}_wall_placement_and_thickness", valid)
            gaps = _voids(rectangles, (start, end, bottom, top), epsilon)
            expected = sorted((o for o in spec["openings"] if o["wall"] == side),
                              key=lambda item: item["offset"])
            check(f"{side}_opening_count", len(gaps) == len(expected),
                  expected=len(expected), actual=len(gaps))
            for index, opening in enumerate(expected):
                entry = {"id": opening["id"], "kind": opening["kind"], "wall": side,
                         "passed": False, "void_clear": False, "measurements": {}}
                result["openings"].append(entry)
                if index >= len(gaps):
                    continue
                a, b, c, d = gaps[index]
                # For west/east, offsets still start at the clear south interior
                # plane, not at the outer corner's negative coordinate.
                reference = west if along_x else south
                actual = {"offset": a - reference, "width": b - a,
                          "height": d - c, "sill": c - bottom}
                entry["measurements"] = {
                    key: _measurement(opening[key], value, source_scale, model_scale, tolerance_mm)
                    for key, value in actual.items()}
                entry["void_clear"] = True
                entry["passed"] = all(item["passed"] for item in entry["measurements"].values())
        result["components"] = {"count": len(parts), "each_component_watertight": True,
                                "each_component_outward_normals": True,
                                "each_component_positive_volume": True,
                                "assembly_boolean_union": False}
        result["limitations"] = [
            "Verification supports named, closed, axis-aligned rectangular components.",
            "Components are separate solids; no fused watertight assembly is claimed.",
            "OBJ has no standardized unit field; its API coordinate contract is metres.",
            "This result verifies the model against the supplied spec, without rereading source CAD.",
        ]
    except CadError:
        raise
    except (ValueError, OSError, KeyError, IndexError, TypeError, ezdxf.DXFError) as exc:
        mismatch(exc)
    passed = (all(item["passed"] for item in result["dimensions"].values()) and
              all(item["passed"] for item in result["openings"]) and
              all(item["passed"] for item in result["checks"]))
    result["passed"] = result["verified"] = result["model_verified"] = passed
    if not passed:
        raise CadError("MODEL_DIMENSION_MISMATCH",
                       "Serialized room dimensions, openings, or construction differ.", result)
    return result


def verify_room_model_against_source(spec, model_path, source, mapping, tolerance_mm=0.01):
    """Verify both current source CAD -> spec and serialized model -> spec.

    A spec must carry its original source SHA-256. DWG conversion is temporary;
    compare the actual DWG identity, never a conversion's timestamps or paths.
    The source is hashed again after extraction to reject concurrent mutations.
    """
    from .converters import readable_dxf

    scale = _validate_spec(spec)
    tolerance_mm = _number(tolerance_mm, "tolerance_mm", positive=True)
    before = source_identity(source)
    expected_sha = spec.get("source", {}).get("sha256")
    if not isinstance(expected_sha, str) or expected_sha != before["sha256"]:
        raise CadError("ROOM_SOURCE_MISMATCH",
                       "Current source hash differs from the room specification; extract a new spec.",
                       {"expected_sha256": expected_sha, "actual_sha256": before["sha256"],
                        "source_verified": False, "model_verified": False})
    recorded_mapping = spec.get("evidence", {}).get("interpretation", {}).get("mapping")
    if recorded_mapping is not None and recorded_mapping != mapping:
        raise CadError("ROOM_SOURCE_MISMATCH", "Verification mapping differs from the extraction mapping.",
                       {"source_verified": False, "model_verified": False})
    with readable_dxf(source) as dxf:
        current = extract_room(dxf, mapping, source_info=before)["spec"]
    after = source_identity(source)
    if after["sha256"] != before["sha256"]:
        raise CadError("ROOM_SOURCE_MISMATCH", "Source changed during strict verification.",
                       {"source_verified": False, "model_verified": False})
    checks = []

    def compare(label, expected, actual):
        if isinstance(expected, (float, int)) and not isinstance(expected, bool):
            error_mm = abs(float(expected) - float(actual)) * scale * 1000
            checks.append({"name": label, "passed": error_mm <= tolerance_mm,
                           "absolute_error_mm": error_mm})
        elif isinstance(expected, dict):
            checks.append({"name": label + ":keys", "passed": set(expected) == set(actual)})
            for key in expected.keys() & actual.keys():
                compare(f"{label}.{key}", expected[key], actual[key])
        elif isinstance(expected, list):
            checks.append({"name": label + ":count", "passed": len(expected) == len(actual)})
            for index, (left, right) in enumerate(zip(expected, actual)):
                compare(f"{label}[{index}]", left, right)
        else:
            checks.append({"name": label, "passed": expected == actual})

    # Compare IDs independent of mapping order, while geometry remains measured.
    for key in ("units", "drawing_unit_to_m", "origin", "dimensions", "construction"):
        compare(key, spec[key], current[key])
    compare("openings", sorted(spec["openings"], key=lambda o: o["id"]),
            sorted(current["openings"], key=lambda o: o["id"]))
    if not all(item["passed"] for item in checks):
        raise CadError("ROOM_SOURCE_MISMATCH", "Current source CAD does not match the supplied room spec.",
                       {"source_verified": False, "model_verified": False, "source_checks": checks})
    try:
        result = verify_room_model(spec, model_path, tolerance_mm=tolerance_mm)
    except CadError as exc:
        details = dict(exc.details) if isinstance(exc.details, dict) else {}
        details.update(source_verified=True, model_verified=False,
                       verification_scope="source_and_model_against_spec", source_checks=checks)
        raise CadError(exc.code, exc.message, details) from exc
    if source_identity(source)["sha256"] != before["sha256"]:
        raise CadError("ROOM_SOURCE_MISMATCH", "Source changed during strict verification.",
                       {"source_verified": False, "model_verified": result["model_verified"]})
    result.update(source_verified=True, verification_scope="source_and_model_against_spec",
                  source=before, source_checks=checks)
    result["limitations"] = [item for item in result["limitations"]
                             if "without rereading" not in item]
    return result
