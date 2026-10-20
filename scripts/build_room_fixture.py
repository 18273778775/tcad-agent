# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Create a controlled complete-room acceptance drawing, not an external project.

This independent fixture factory is deliberately separate from the room extractor
and model builder. Geometry specifications also use the public declarative plan
language so the CLI can perform a complete, atomic dimensional revision.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import ezdxf


def room_mapping() -> dict:
    return {
        "version": 1,
        "name": "A101",
        "units": "mm",
        "axes": "drawing_x_length_y_width",
        "height_meaning": "finished_floor_to_ceiling_underside",
        "roles": {
            "footprint": {"layer": "ROOM_CLEAR"},
            "height": {"layer": "ROOM_HEIGHT"},
            "wall_thickness": {"layer": "WALL_THICKNESS"},
            "floor_thickness": {"layer": "FLOOR_THICKNESS"},
            "ceiling_thickness": {"layer": "CEILING_THICKNESS"},
        },
        "openings": [
            {"id": "D1", "kind": "door", "wall": "south",
             "plan": {"layer": "DOOR_PLAN"}, "elevation": {"layer": "DOOR_ELEV"}},
            {"id": "W1", "kind": "window", "wall": "north",
             "plan": {"layer": "WINDOW_PLAN"}, "elevation": {"layer": "WINDOW_ELEV"}},
        ],
        "dimension_labels": {
            "length": {"layer": "DIM_LENGTH"},
            "width": {"layer": "DIM_WIDTH"},
            "height": {"layer": "DIM_HEIGHT"},
        },
        "units_label": {"layer": "DRAWING_UNITS"},
    }


def expected_oracle(length=6000, width=4500, height=3000) -> dict:
    """Independent known inputs, never a source for extraction or model building."""
    return {
        "provenance": "controlled complete-room acceptance drawing; not an external building project",
        "units": "mm",
        "name": "A101",
        "dimensions": {"length": length, "width": width, "height": height},
        "construction": {"wall_thickness": 200, "floor_thickness": 150,
                         "ceiling_thickness": 150},
        "clear_area_mm2": length * width,
        "clear_perimeter_mm": 2 * (length + width),
        "openings": [
            {"id": "D1", "kind": "door", "wall": "south", "offset": 1000,
             "width": 900, "height": 2100, "sill": 0},
            {"id": "W1", "kind": "window", "wall": "north", "offset": 2200,
             "width": 1600, "height": 1200, "sill": 900},
        ],
        "height_evidence": "explicit elevation/section geometry measured from finished floor to ceiling underside",
    }


def geometry_specs(length=6000, width=4500, height=3000) -> list[dict]:
    """Public CLI entity specifications for a complete plan and two elevations."""
    if any(not math.isfinite(v) or v <= 0 for v in (length, width, height)):
        raise ValueError("Room dimensions must be finite and positive.")
    if length < 4000 or width < 2000 or height < 2400:
        raise ValueError("Fixture dimensions cannot truncate the fixed door and window.")
    result = []

    def line(layer, a, b):
        result.append({"type": "LINE", "layer": layer, "start": list(a), "end": list(b)})

    def rectangle(layer, x, y, w, h):
        result.append({"type": "LWPOLYLINE", "layer": layer, "closed": True,
                       "points": [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]})

    def text(layer, value, position, size=170, rotation=0):
        result.append({"type": "TEXT", "layer": layer, "text": value,
                       "insert": list(position), "height": size, "rotation": rotation})

    # Exactly one unambiguous geometric entity for each mapping role.
    rectangle("ROOM_CLEAR", 0, 0, length, width)
    line("ROOM_HEIGHT", (9500, 0), (9500, height))
    line("WALL_THICKNESS", (-200, width / 2), (0, width / 2))
    line("FLOOR_THICKNESS", (9400, -150), (9400, 0))
    line("CEILING_THICKNESS", (9400, height), (9400, height + 150))
    line("DOOR_PLAN", (1000, 0), (1900, 0))
    line("WINDOW_PLAN", (2200, width), (3800, width))
    rectangle("DOOR_ELEV", 11000, 0, 900, 2100)
    rectangle("WINDOW_ELEV", 22200, 900, 1600, 1200)

    # Plan wall outlines: break the southern wall at D1 and northern wall at W1.
    for y in (-200, 0):
        line("WALL_OUTLINE", (-200, y), (1000, y))
        line("WALL_OUTLINE", (1900, y), (length + 200, y))
    for y in (width, width + 200):
        line("WALL_OUTLINE", (-200, y), (2200, y))
        line("WALL_OUTLINE", (3800, y), (length + 200, y))
    for x in (-200, 0, length, length + 200):
        line("WALL_OUTLINE", (x, -200), (x, width + 200))
    for x, y in ((1000, -200), (1900, -200), (2200, width), (3800, width)):
        line("WALL_OUTLINE", (x, y), (x, y + 200))
    line("DOOR_DETAIL", (1000, 0), (1000, 900))
    result.append({"type": "ARC", "layer": "DOOR_DETAIL", "center": [1000, 0],
                   "radius": 900, "start_angle": 0, "end_angle": 90})
    for dy in (60, 100, 140):
        line("WINDOW_DETAIL", (2200, width + dy), (3800, width + dy))
    line("WINDOW_DETAIL", (3000, width), (3000, width + 200))
    text("ROOM_LABEL", "A101 / CONTROLLED ROOM", (1000, width / 2), 220)
    text("VIEW_LABEL", "PLAN / CLEAR INTERNAL DIMENSIONS", (0, -1450))
    text("OPENING_LABEL", "D1 900 x 2100 / SILL 0", (500, -650), 120)
    text("OPENING_LABEL", "W1 1600 x 1200 / SILL 900", (1800, width + 450), 120)

    # Dimension chains are ordinary lines/ticks and explicit millimetre labels.
    line("DIM_LINES", (0, -950), (length, -950))
    for x in (0, length):
        line("DIM_LINES", (x, -250), (x, -1100))
        line("DIM_LINES", (x - 70, -1020), (x + 70, -880))
    text("DIM_LENGTH", f"{length:g} mm", (length / 2 - 400, -900))
    line("DIM_LINES", (-900, 0), (-900, width))
    for y in (0, width):
        line("DIM_LINES", (-250, y), (-1050, y))
        line("DIM_LINES", (-970, y - 70), (-830, y + 70))
    text("DIM_WIDTH", f"{width:g} mm", (-1050, width / 2 - 400), rotation=90)

    # South and north elevations include slabs, datum, window and door frames.
    for origin, name in ((10000, "SOUTH ELEVATION / SECTION DATUM"),
                         (20000, "NORTH ELEVATION / SECTION DATUM")):
        rectangle("ELEVATION_OUTLINE", origin, 0, length, height)
        rectangle("SLAB_OUTLINE", origin - 200, -150, length + 400, 150)
        rectangle("SLAB_OUTLINE", origin - 200, height, length + 400, 150)
        line("FLOOR_DATUM", (origin - 300, 0), (origin + length + 300, 0))
        text("VIEW_LABEL", name, (origin, -650), 140)
        text("LEVEL_LABEL", "FFL +/-0 / CEILING UNDERSIDE", (origin, height + 430), 120)
    line("DOOR_DETAIL", (11100, 1000), (11220, 1000))
    line("WINDOW_DETAIL", (23000, 900), (23000, 2100))
    line("WINDOW_DETAIL", (22200, 1500), (23800, 1500))
    for y in (0, height):
        line("DIM_LINES", (9300, y), (9900, y))
        line("DIM_LINES", (9430, y - 70), (9570, y + 70))
    text("DIM_HEIGHT", f"{height:g} mm", (9250, height / 2 - 400), rotation=90)
    text("CONSTRUCTION_LABEL", "WALL 200 / FLOOR 150 / CEILING 150 mm", (0, -1850), 140)
    text("DRAWING_UNITS", "UNITS: mm", (0, width + 1100), 180)
    text("SHEET_TITLE", "CONTROLLED COMPLETE-ROOM ACCEPTANCE DRAWING / A101", (0, width + 1550), 260)
    text("SHEET_NOTE", "TEST FIXTURE - NOT AN EXTERNAL BUILDING PROJECT", (0, width + 850), 150)
    return result


def create_room_drawing(output, length=6000, width=4500, height=3000) -> dict:
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = ezdxf.new("R2000", setup=True)
    doc.units = ezdxf.units.MM
    space = doc.modelspace()
    for spec in geometry_specs(length, width, height):
        layer = spec["layer"]
        if layer not in doc.layers:
            doc.layers.new(layer, dxfattribs={"color": 7})
        attrs = {"layer": layer}
        kind = spec["type"]
        if kind == "LINE":
            space.add_line(spec["start"], spec["end"], dxfattribs=attrs)
        elif kind == "LWPOLYLINE":
            space.add_lwpolyline(spec["points"], close=spec["closed"], dxfattribs=attrs)
        elif kind == "ARC":
            space.add_arc(spec["center"], spec["radius"], spec["start_angle"],
                          spec["end_angle"], dxfattribs=attrs)
        elif kind == "TEXT":
            space.add_text(spec["text"], dxfattribs={**attrs, "insert": spec["insert"],
                           "height": spec["height"], "rotation": spec["rotation"]})
        else:
            raise AssertionError(kind)
    doc.saveas(output)
    return {"output": str(output.resolve()), "entity_count": len(space),
            "oracle": expected_oracle(length, width, height)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mapping")
    parser.add_argument("--oracle")
    parser.add_argument("--length", type=float, default=6000)
    parser.add_argument("--width", type=float, default=4500)
    parser.add_argument("--height", type=float, default=3000)
    args = parser.parse_args()
    result = create_room_drawing(args.output, args.length, args.width, args.height)
    for target, value in ((args.mapping, room_mapping()), (args.oracle, result["oracle"])):
        if target:
            path = Path(target)
            if path.exists():
                raise FileExistsError(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
