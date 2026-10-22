# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0
"""Portable preview overlays. Virtual entities never enter the source document."""

import re


def draw_annotations(frontend, document, roots, bbox, bounds, mode, legend,
                     layers, exclude_layers, font, maximum):
    from ezdxf.entities import Text
    from ezdxf.addons.drawing.properties import Properties
    from .spatial import iter_world_entities
    from .geometry import entity_to_polygon

    span = max(bounds.size.x, bounds.size.y, 1)
    height = span / 180
    items, total, rejected = [], 0, 0
    diagnostics = {}

    def draw(text, point, color="#dc2626"):
        entity = Text.new(dxfattribs={"text": text, "insert": (point[0], point[1], 0), "height": height})
        entity.doc = document
        properties = Properties()
        properties.color, properties.font = color, font
        frontend.draw_text_entity(entity, properties)

    if mode:
        for record in iter_world_entities(document, roots=roots, bbox=bbox,
                layers=list(layers) if layers is not None else None,
                exclude_layers=list(exclude_layers), diagnostics=diagnostics):
            if not record.get("visible", True):
                continue
            entity = record["entity"]
            point, text = None, None
            if mode == "axes":
                if entity.dxftype() not in {"TEXT", "MTEXT", "ATTRIB"}:
                    continue
                value = entity.plain_text().strip()
                if re.fullmatch(r"[A-Za-z]{1,3}|\d{1,3}(?:/[A-Za-z0-9]+)?", value):
                    point = record.get("world_insert")
                    text = f"axis-label {value}"
            else:
                if entity.dxftype() not in {"LWPOLYLINE", "POLYLINE", "HATCH", "CIRCLE", "ELLIPSE", "SPLINE"}:
                    continue
                boundary = entity_to_polygon(entity)
                if boundary.invalid_reason or boundary.geometry is None or boundary.geometry.is_empty:
                    rejected += 1
                    continue
                anchor = boundary.geometry.representative_point()
                point = (anchor.x, anchor.y)
                text = (f"{boundary.geometry.area:.3f} du2" if mode == "areas"
                        else record["entity_key"])
            if text is None or point is None:
                continue
            total += 1
            if len(items) < maximum:
                draw(text, point)
                items.append({"entity_key": record["entity_key"], "label": text, "point": list(point)[:2]})
    if legend:
        names = f"{len(layers)} selected layers" if layers is not None else "all visible layers"
        text = f"TCAD | {names} | {mode or 'source'} | area: du2"
        draw(text, (bounds.extmin.x, bounds.extmin.y - height * 3), "#1d4ed8")
    return {"mode": mode, "count": len(items) + int(legend), "candidate_count": total,
            "complete": total <= maximum and diagnostics.get("complete", True),
            "omitted_count": max(total - maximum, 0), "invalid_boundaries": rejected,
            "items": items, "traversal": diagnostics, "legend": legend,
            "scope": "preview overlays only; axis-label mode marks source text candidates, not inferred axis intersections"}
