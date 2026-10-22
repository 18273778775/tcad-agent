# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Deterministic frame discovery and transactional multi-sheet DXF export.

Coordinates remain in the source WCS. Detection is deliberately geometric: no
OCR, language model, or title-derived guess is used to manufacture a frame.
"""

import json
import math
import os
import re
import shutil
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from .converters import readable_dxf
from .errors import CadError
from .io import atomic_output, destination_lock, file_sha256, jsonable, read_document


_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)
}
_TITLE_SUFFIX = re.compile(r"(?:图|详图|说明|目录|表|做法|设计总说明)(?:[（(][^）)]*[）)])?$")
_NUMBER = re.compile(r"^(?:(?:结施|建施|结构|建筑|结|建|施)[\s-]*|[A-Z]{1,4}[-.])[0-9一二三四五六七八九十][A-Z0-9一二三四五六七八九十\s.\-/]{0,23}$", re.I)


def _area(box):
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _contains(outer, inner, tolerance=0.0):
    return (inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance
            and inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance)


def _intersects(a, b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def _validated_box(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(item) not in (int, float) or not math.isfinite(item) for item in value)):
        raise CadError("INVALID_ARGUMENT", "A frame bbox requires four finite numbers.")
    box = [float(item) for item in value]
    if box[2] <= box[0] or box[3] <= box[1]:
        raise CadError("INVALID_ARGUMENT", "Frame bbox must have positive width and height.")
    return box


def _rectangle(points):
    """Return bounds only when four corners form an axis-aligned rectangle."""
    if len(points) == 5 and (points[0] - points[-1]).magnitude < 1e-7:
        points = points[:-1]
    if len(points) != 4:
        return None
    xs, ys = [p.x for p in points], [p.y for p in points]
    box = [min(xs), min(ys), max(xs), max(ys)]
    tolerance = max(box[2] - box[0], box[3] - box[1], 1.0) * 1e-7
    if _area(box) <= 0:
        return None
    seen = set()
    for first, second in zip(points, points[1:] + points[:1]):
        if abs(first.x - second.x) > tolerance and abs(first.y - second.y) > tolerance:
            return None
        corner = (min((0, 2), key=lambda i: abs(first.x - box[i])),
                  min((1, 3), key=lambda i: abs(first.y - box[i])))
        if abs(first.x - box[corner[0]]) > tolerance or abs(first.y - box[corner[1]]) > tolerance:
            return None
        seen.add(corner)
    return box if len(seen) == 4 else None


def _poly_rectangle(entity):
    try:
        if entity.dxftype() == "LWPOLYLINE":
            if not entity.closed or any(abs(p[4]) > 1e-10 for p in entity):
                return None
            return _rectangle(list(entity.vertices_in_wcs()))
        if entity.dxftype() == "POLYLINE" and entity.is_closed:
            if any(abs(v.dxf.get("bulge", 0)) > 1e-10 for v in entity.vertices):
                return None
            return _rectangle(list(entity.points_in_wcs()))
    except (AttributeError, ValueError, TypeError):
        return None
    return None


def _line_rectangles(entities):
    """Find four-side rectangles by matching merged collinear intervals.

    This avoids enumerating every pair of vertical lines in a structural plan.
    Interrupted borders with genuine gaps are intentionally not synthesized.
    """
    horizontal, vertical = defaultdict(list), defaultdict(list)
    for entity in entities:
        if entity.dxftype() != "LINE":
            continue
        a, b = entity.dxf.start, entity.dxf.end
        if abs(a.y - b.y) < 1e-6 and abs(a.x - b.x) > 1e-6:
            horizontal[round(a.y, 5)].append((min(a.x, b.x), max(a.x, b.x), entity.dxf.handle))
        elif abs(a.x - b.x) < 1e-6 and abs(a.y - b.y) > 1e-6:
            vertical[round(a.x, 5)].append((min(a.y, b.y), max(a.y, b.y), entity.dxf.handle))

    def merge(groups):
        result = {}
        for coordinate, intervals in groups.items():
            merged = []
            for lo, hi, handle in sorted(intervals):
                if merged and lo <= merged[-1][1] + 1e-5:
                    merged[-1][1] = max(hi, merged[-1][1])
                    merged[-1][2].append(handle)
                else:
                    merged.append([lo, hi, [handle]])
            result[coordinate] = merged
        return result

    horizontal, vertical = merge(horizontal), merge(vertical)
    pairs = defaultdict(list)
    for y, intervals in horizontal.items():
        for x0, x1, handles in intervals:
            pairs[(round(x0, 5), round(x1, 5))].append((y, handles))
    for (x0, x1), levels in pairs.items():
        lefts, rights = vertical.get(x0, []), vertical.get(x1, [])
        if not lefts or not rights:
            continue
        ordered = sorted(levels)
        # A page may contain title-block horizontals. Find the outer spanning
        # pair for each actual vertical span, rather than O(levels squared).
        for y0, y1, left_handles in lefts:
            inside = [(y, handles) for y, handles in ordered if y0 - 1e-5 <= y <= y1 + 1e-5]
            if len(inside) < 2:
                continue
            bottom, top = inside[0], inside[-1]
            right = next((r for r in rights if r[0] <= bottom[0] + 1e-5
                          and r[1] >= top[0] - 1e-5), None)
            if right is not None and top[0] > bottom[0]:
                yield ([x0, bottom[0], x1, top[0]],
                       bottom[1] + top[1] + left_handles + right[2])


def _local_candidates(entities, frame_layer):
    selected = [e for e in entities if frame_layer is None or e.dxf.get("layer", "0") == frame_layer]
    for entity in selected:
        box = _poly_rectangle(entity)
        if box:
            yield {"bbox": box, "frame_handles": [entity.dxf.handle], "method": "closed_rectangle"}
    for box, handles in _line_rectangles(selected):
        yield {"bbox": box, "frame_handles": sorted(set(handles)), "method": "four_sides"}


def _text_record(entity, matrix=None, owner=None):
    kind = entity.dxftype()
    if kind not in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
        return None
    try:
        text = entity.plain_text() if hasattr(entity, "plain_text") else entity.dxf.get("text", "")
        text = re.sub(r"\s+", " ", text).strip()
        if not text or len(text) > 300:
            return None
        point = entity.dxf.insert
        if kind != "MTEXT":
            point = entity.ocs().to_wcs(point)
        if matrix is not None:
            point = matrix.transform(point)
        return {"text": text, "x": point.x, "y": point.y,
                "height": float(entity.dxf.get("char_height" if kind == "MTEXT" else "height", 0)),
                "handle": entity.dxf.handle, "owner_handle": owner,
                "layer": entity.dxf.get("layer", "0"),
                "kind": kind, "tag": entity.dxf.get("tag", "") if kind in {"ATTRIB", "ATTDEF"} else ""}
    except (ValueError, TypeError, AttributeError):
        return None


def _scan(document, selected, frame_layer):
    """Probe local definitions once, then transform rectangles and text per INSERT."""
    from ezdxf.math import Vec3
    local_cache, candidates, texts, warnings = {}, [], [], []
    visits = 0

    def local(block, inherited=False):
        probe_layer = None if inherited else frame_layer
        key = (block.name, probe_layer)
        if key not in local_cache:
            entities = list(block)
            local_cache[key] = (list(_local_candidates(entities, probe_layer)), entities)
        return local_cache[key]

    def insert(entity, parent=None, owner=None, stack=(), inherited=False):
        nonlocal visits
        visits += 1
        if visits > 100000 or len(stack) > 16:
            raise CadError("SHEET_SCAN_LIMIT", "Block frame/text inspection exceeds its safety budget.",
                           {"max_insert_visits": 100000, "max_depth": 16})
        name = entity.dxf.name
        if name in stack:
            warnings.append(f"Cyclic block reference skipped: {name}")
            return
        block = document.blocks.get(name)
        if block is None:
            warnings.append(f"Missing block definition: {name}")
            return
        if block.block.is_xref:
            warnings.append(f"External reference is unresolved: {name}")
            return
        instances = entity.multi_insert() if entity.mcount > 1 else [entity]
        for instance in instances:
            matrix = instance.matrix44()
            if parent is not None:
                matrix = matrix @ parent
            root_owner = owner or entity.dxf.handle
            # Attached attributes are in the containing coordinate system,
            # already positioned by their own INSERT, unlike definition text.
            for attribute in instance.attribs:
                item = _text_record(attribute, parent, root_owner)
                if item:
                    texts.append(item)
            eligible = inherited or (frame_layer is not None and entity.dxf.get("layer", "0") == frame_layer)
            shapes, children = local(block, eligible)
            for candidate in shapes:
                a, b, c, d = candidate["bbox"]
                box = _rectangle([matrix.transform(Vec3(x, y)) for x, y in
                                  [(a, b), (c, b), (c, d), (a, d)]])
                if box:
                    candidates.append({**candidate, "bbox": box, "block_name": name,
                                       "parent_handle": root_owner,
                                       "definition_handles": candidate["frame_handles"],
                                       "frame_handles": [root_owner], "method": "block_" + candidate["method"]})
            for child in children:
                if child.dxftype() == "INSERT":
                    insert(child, matrix, root_owner, stack + (name,), eligible)
                elif child.dxftype() != "ATTDEF":
                    item = _text_record(child, matrix, root_owner)
                    if item:
                        texts.append(item)

    entities = list(selected)
    candidates.extend(_local_candidates(entities, frame_layer))
    for entity in entities:
        if entity.dxftype() == "INSERT":
            insert(entity)
        else:
            item = _text_record(entity)
            if item:
                texts.append(item)
    return candidates, texts, sorted(set(warnings))


def sanitize_name(value, fallback="sheet"):
    """Portable filename stem; never allow path traversal or Windows devices."""
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f\ud800-\udfff]", "_", str(value))
    value = re.sub(r"\s+", " ", value).strip(" ._")[:100].rstrip(" .")
    if not value:
        value = fallback
    if value.split(".")[0].upper() in _RESERVED:
        value = "_" + value
    return value


def _name_sheet(frame, texts, title_layer=None, title_regex=None, number_layer=None):
    box = frame["bbox"]
    width, height = box[2] - box[0], box[3] - box[1]
    inside = [t for t in texts if box[0] <= t["x"] <= box[2] and box[1] <= t["y"] <= box[3]]
    title_region = [t for t in inside if t["x"] >= box[0] + width * .45
                    and t["y"] <= box[1] + height * .32]
    eligible = [t for t in inside if title_layer is None or t["layer"] == title_layer]
    pattern = re.compile(title_regex) if title_regex else None
    titles = [t for t in title_region if _TITLE_SUFFIX.search(t["text"])
              and 2 <= len(t["text"]) <= 70
              and not any(s in t["text"] for s in ("参见", "详见", "见图", "图名", "图纸名称"))]
    # Explicit title-block tags have priority over typographic heuristics.
    tagged = [t for t in inside if re.search(r"(?:图名|图纸名称|DRAWING.?TITLE|SHEET.?NAME)", t["tag"], re.I)]
    dedicated = [t for t in inside if re.search(r"(?:图名|DRAWING[ _-]?TITLE|SHEET[ _-]?TITLE)", t["layer"], re.I)
                 and _TITLE_SUFFIX.search(t["text"]) and 2 <= len(t["text"]) <= 90]
    titles = tagged or dedicated or titles
    if title_layer is not None or pattern is not None:
        titles = [t for t in eligible if (pattern.search(t["text"]) if pattern else _TITLE_SUFFIX.search(t["text"]))]
    if not titles and title_layer is None and pattern is None:
        titles = [t for t in inside if _TITLE_SUFFIX.search(t["text"])
                  and 2 <= len(t["text"]) <= 50 and t["y"] <= box[1] + height * .25]
    titles.sort(key=lambda t: (-t["height"], -t["y"], t["x"], t["text"]))
    numbers = [t for t in (inside if number_layer else title_region)
               if (number_layer is None or t["layer"] == number_layer) and _NUMBER.fullmatch(t["text"])]
    numbers.sort(key=lambda t: (-t["x"], t["y"], t["text"]))
    major = [t for t in titles if t["height"] >= titles[0]["height"] * .75][:6] if titles else []
    title = "；".join(dict.fromkeys(t["text"] for t in major)) if titles else None
    if not dedicated and not tagged and title_layer is None and pattern is None:
        directories = [t for t in inside if t["text"] in {"目录", "图纸目录", "图纸目次"}]
        if directories:
            projects = [t for t in inside if re.search(r"\d+#.*(?:楼|栋)", t["text"])]
            projects.sort(key=lambda t: (-t["y"], t["x"]))
            building = re.search(r"\d+#[^\s]{0,12}(?:楼|栋)", projects[0]["text"]) if projects else None
            title = (building.group() if building else "") + "图纸目录"
            major = directories[:1] + projects[:1]
    number = numbers[0]["text"] if numbers else None
    evidence = [{k: item[k] for k in ("handle", "owner_handle", "kind", "text", "x", "y")}
                for item in (major + (numbers[:1] if numbers else []))]
    return title, number, evidence, len(inside)


def _discovery_options(frame_block_regex, frame_size, title_layer, title_regex, number_layer,
                       shared_frame, names):
    for label, value in (("frame_block_regex", frame_block_regex), ("title_regex", title_regex)):
        if value is not None:
            if not isinstance(value, str) or not value or len(value) > 512:
                raise CadError("INVALID_ARGUMENT", f"{label} requires a nonempty regular expression, at most 512 characters.")
            try:
                re.compile(value)
            except re.error as exc:
                raise CadError("INVALID_ARGUMENT", f"Invalid {label} regular expression.", {"reason": str(exc)}) from exc
    for label, value in (("title_layer", title_layer), ("number_layer", number_layer)):
        if value is not None and (not isinstance(value, str) or not value):
            raise CadError("INVALID_ARGUMENT", f"{label} requires a nonempty layer name.")
    if shared_frame not in {"largest", "split"}:
        raise CadError("INVALID_ARGUMENT", "shared_frame must be largest or split.")
    if isinstance(frame_size, str):
        try:
            frame_size = [float(v) for v in re.split(r"[xX×,]", frame_size)]
        except ValueError as exc:
            raise CadError("INVALID_ARGUMENT", "frame_size must be WIDTHxHEIGHT.") from exc
    if frame_size is not None and (not isinstance(frame_size, (list, tuple)) or len(frame_size) != 2
            or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in frame_size)):
        raise CadError("INVALID_ARGUMENT", "frame_size must contain two finite positive sizes.")
    if names is not None and (not isinstance(names, dict) or len(names) > 400
            or any(not isinstance(k, str) or not k for k in names)):
        raise CadError("INVALID_ARGUMENT", "names must map sheet IDs or native frame handles to strings or name objects.")
    if names:
        for value in names.values():
            if isinstance(value, str) and value.strip():
                continue
            if (isinstance(value, dict) and value and set(value) <= {"name", "title", "number"}
                    and all(isinstance(v, str) and v.strip() for v in value.values())):
                continue
            raise CadError("INVALID_ARGUMENT", "Each name override requires a nonempty string or name/title/number object.")
    return frame_size


def _sheet_metadata(frame, texts, title):
    box = frame["bbox"]
    inside = [t for t in texts if box[0] <= t["x"] <= box[2] and box[1] <= t["y"] <= box[3]]
    title_parts = set((title or "").split("；"))
    preferred = [t for t in inside if t["text"] in title_parts]
    building_pattern = r"(?:\d+\s*[#＃号](?:楼|栋)?|\d+栋)"
    building_candidates = [t for t in preferred if re.search(building_pattern, t["text"])] or [
        t for t in inside if re.search(building_pattern, t["text"])]
    buildings = {re.search(building_pattern, t["text"]).group() for t in building_candidates}
    building = building_candidates[0] if len(buildings) == 1 else None
    elevation_pattern = r"(?:标高\s*[+-]?\d+(?:\.\d+)?(?:\s*[~～至]\s*[+-]?\d+(?:\.\d+)?)?|[+-]?\d+(?:\.\d+)?\s*[~～至]\s*[+-]?\d+(?:\.\d+)?\s*(?:m|米|标高))"
    elevation_candidates = [t for t in preferred if re.search(elevation_pattern, t["text"], re.I)] or [
        t for t in inside if re.search(elevation_pattern, t["text"], re.I)]
    elevations = {re.search(elevation_pattern, t["text"], re.I).group() for t in elevation_candidates}
    elevation = elevation_candidates[0] if len(elevations) == 1 else None
    value = title or ""
    kind = "schedule" if re.search(r"(?:目录|表|清单)", value) else "plan" if re.search(r"(?:平面|布置|柱定位|结构图)", value) else "detail" if re.search(r"(?:详图|节点|剖面|大样)", value) else "notes" if "说明" in value else "unknown"
    def evidence(item):
        return None if item is None else {k: item[k] for k in ("handle", "owner_handle", "text", "x", "y")}
    return {"building": re.search(building_pattern, building["text"]).group() if building else None,
            "elevation_range": re.search(elevation_pattern, elevation["text"], re.I).group() if elevation else None,
            "kind": kind, "metadata_confidence": {
                "building": "literal_unambiguous" if building else "ambiguous" if buildings else "unresolved",
                "elevation_range": "literal_unambiguous" if elevation else "ambiguous" if elevations else "unresolved",
                "kind": "literal_title_keywords" if kind != "unknown" else "unresolved"},
            "metadata_evidence": {"building": evidence(building), "elevation_range": evidence(elevation),
                "kind": {"title": title, "rule": "literal title keywords"} if kind != "unknown" else None}}


def discover_sheets(document, layout="Model", frames=None, frame_layer=None,
                    min_frame_area=10000, max_sheets=200, *, frame_block_regex=None,
                    frame_size=None, title_layer=None, title_regex=None, number_layer=None,
                    shared_frame="largest", names=None):
    frame_size = _discovery_options(frame_block_regex, frame_size, title_layer, title_regex,
                                    number_layer, shared_frame, names)
    if type(max_sheets) is not int or not 1 <= max_sheets <= 200:
        raise CadError("INVALID_ARGUMENT", "max_sheets must be between 1 and 200.")
    if type(min_frame_area) not in (int, float) or not math.isfinite(min_frame_area) or min_frame_area <= 0:
        raise CadError("INVALID_ARGUMENT", "min_frame_area must be a finite positive drawing-unit area.")
    if frame_layer is not None and not isinstance(frame_layer, str):
        raise CadError("INVALID_ARGUMENT", "frame_layer must be a layer name.")
    try:
        selected = document.layouts.get(layout)
    except KeyError as exc:
        raise CadError("LAYOUT_NOT_FOUND", "The requested layout does not exist.") from exc
    candidates, texts, warnings = _scan(document, selected, frame_layer)
    for candidate in candidates:
        candidate["hint_score"] = 0
        candidate["hint_reasons"] = []
        if frame_block_regex and re.search(frame_block_regex, candidate.get("block_name", "")):
            candidate["hint_score"] += 1
            candidate["hint_reasons"].append("frame block name matches the configured expression")
        if frame_size:
            box = candidate["bbox"]
            ratio = (box[2] - box[0]) / (box[3] - box[1])
            desired = frame_size[0] / frame_size[1]
            if min(abs(ratio / desired - 1), abs(ratio * desired - 1)) <= .03:
                candidate["hint_score"] += 1
                candidate["hint_reasons"].append("frame aspect ratio matches the configured size within 3%; no unit/scale inference")
    if frames is not None:
        if not isinstance(frames, list) or not frames:
            raise CadError("INVALID_ARGUMENT", "frames must be a nonempty list of handles or bbox objects.")
        manual = []
        for frame in frames:
            if isinstance(frame, str):
                matches = [c for c in candidates if frame.upper() in
                           [str(h).upper() for h in c["frame_handles"]]]
                if not matches:
                    raise CadError("FRAME_NOT_FOUND", "No rectangular frame matches this native handle.",
                                   {"handle": frame})
                manual.extend(matches)
            elif isinstance(frame, dict) and "bbox" in frame:
                manual.append({"bbox": _validated_box(frame["bbox"]), "frame_handles": [],
                               "method": "explicit_bbox", "explicit_name": frame.get("name"),
                               "explicit_number": frame.get("number")})
            else:
                raise CadError("INVALID_ARGUMENT", "Each frame must be a handle or an object with bbox.")
        candidates = manual
    else:
        candidates = [c for c in candidates if _area(c["bbox"]) >= min_frame_area
                      and .35 <= (c["bbox"][2] - c["bbox"][0]) /
                      (c["bbox"][3] - c["bbox"][1]) <= 3.5]
    candidates.sort(key=lambda c: (-_area(c["bbox"]), -c.get("hint_score", 0), c["bbox"]))
    if shared_frame == "split" and frames is None:
        replaced = set()
        for outer in list(candidates):
            children = [c for c in candidates if c is not outer and _contains(outer["bbox"], c["bbox"])
                        and .1 <= _area(c["bbox"]) / _area(outer["bbox"]) <= .95]
            children.sort(key=lambda c: -_area(c["bbox"]))
            bands = []
            for child in children:
                if any(_contains(b["bbox"], child["bbox"]) for b in bands):
                    continue
                if _name_sheet(child, texts, title_layer, title_regex, number_layer)[0]:
                    bands.append(child)
            overlap = any(_area([max(a["bbox"][0], b["bbox"][0]), max(a["bbox"][1], b["bbox"][1]),
                                       min(a["bbox"][2], b["bbox"][2]), min(a["bbox"][3], b["bbox"][3])]) >
                          _area(outer["bbox"]) * 1e-7
                          for i, a in enumerate(bands) for b in bands[i + 1:])
            if len(bands) >= 2 and not overlap and sum(_area(b["bbox"]) for b in bands) >= .85 * _area(outer["bbox"]):
                replaced.add(id(outer))
                for band in bands:
                    band["shared_frame_parent"] = outer["frame_handles"]
            elif len(bands) >= 2:
                warnings.append("SHARED_FRAME_AMBIGUOUS: candidate page bands overlap or cover less than 85% of the enclosing frame; largest frame retained.")
            elif "；" in (_name_sheet(outer, texts, title_layer, title_regex, number_layer)[0] or ""):
                warnings.append("SHARED_FRAME_AMBIGUOUS: multiple title texts exist without disjoint closed subframes; no page band was invented.")
        candidates = [c for c in candidates if id(c) not in replaced]
    kept = []
    rejected = 0
    for candidate in candidates:
        box = candidate["bbox"]
        # Outer/inner plotted borders normally differ by a narrow margin. Also
        # reject smaller rectangles wholly inside an accepted sheet: these are
        # title cells, plan outlines, or detail boxes, not additional sheets.
        enclosing = None if candidate["method"] == "explicit_bbox" else next(
            (c for c in kept if _contains(c["bbox"], box, max(c["bbox"][2] - c["bbox"][0],
             c["bbox"][3] - c["bbox"][1]) * 1e-5)), None)
        if enclosing:
            rejected += 1
            continue
        kept.append(candidate)
    orphan_rejected = []
    if frames is None and len(kept) >= 3:
        largest = max(_area(c["bbox"]) for c in kept)
        families = defaultdict(int)
        for item in kept:
            b = item["bbox"]
            families[(round((b[2] - b[0]) / 10), round((b[3] - b[1]) / 10))] += 1
        filtered = []
        for item in kept:
            b = item["bbox"]
            family = (round((b[2] - b[0]) / 10), round((b[3] - b[1]) / 10))
            if _area(b) < largest * .01 and families[family] == 1 and not _name_sheet(item, texts)[0]:
                orphan_rejected.append(item)
            else:
                filtered.append(item)
        kept = filtered
    kept.sort(key=lambda c: (-round(c["bbox"][3], 3), c["bbox"][0]))
    if len(kept) > max_sheets:
        raise CadError("TOO_MANY_SHEETS", "Detected sheets exceed max_sheets; refine frame_layer or frames.",
                       {"detected": len(kept), "max_sheets": max_sheets})
    used_names, sheets = set(), []
    for index, candidate in enumerate(kept, 1):
        title, number, evidence, text_count = _name_sheet(candidate, texts, title_layer, title_regex, number_layer)
        capture = next((match for item in evidence
                        if title_regex and (match := re.search(title_regex, item["text"]))), None)
        capture_groups = {key: value for key, value in (capture.groupdict() if capture else {}).items() if value}
        if capture_groups.get("title"):
            title = capture_groups["title"]
        if capture_groups.get("number"):
            number = capture_groups["number"]
        title = candidate.get("explicit_name") or title
        number = candidate.get("explicit_number") or number
        identifier = f"sheet-{index:03d}"
        keys = [identifier] + candidate["frame_handles"]
        overrides = [names[key] for key in keys if names and key in names]
        if len(overrides) > 1 and any(value != overrides[0] for value in overrides):
            raise CadError("INVALID_ARGUMENT", "Conflicting name overrides target the same sheet.", {"sheet": identifier})
        override = overrides[0] if overrides else None
        if isinstance(override, str):
            override = {"name": override, "title": override}
        if override:
            title, number = override.get("title", title), override.get("number", number)
        primary = (title or "").split("；", 1)[0] or None
        proposed = (override or {}).get("name") or "_".join(str(value) for value in (number, primary) if value) or identifier
        name = sanitize_name(proposed)
        stem, suffix = name, 2
        while name.casefold() in used_names:
            name = f"{stem}_{suffix:02d}"
            suffix += 1
        used_names.add(name.casefold())
        notes = []
        if not title:
            notes.append("No reliable title was found; the filename uses an explicit number or stable sheet ID.")
        metadata = _sheet_metadata(candidate, texts, title)
        if capture:
            for key in ("building", "elevation_range", "kind", "number"):
                if capture_groups.get(key):
                    if key != "number":
                        metadata[key] = capture_groups[key]
                        metadata["metadata_confidence"][key] = "configured_named_capture"
                    metadata["metadata_evidence"][key] = {"text": capture.string, "rule": "configured named regular-expression capture"}
        sheets.append({"id": identifier, "name": name, "title": title, "number": number,
                       "primary_title": primary, "subtitles": (title or "").split("；")[1:],
                       "title_capture_groups": capture_groups,
                       "bbox": candidate["bbox"], "frame_handles": candidate["frame_handles"],
                       "parent_handle": candidate.get("parent_handle"), "block_name": candidate.get("block_name"),
                       "shared_frame_parent": candidate.get("shared_frame_parent"),
                       "hint_score": candidate.get("hint_score", 0), "hint_reasons": candidate.get("hint_reasons", []),
                       "detection_method": candidate["method"], "title_evidence": evidence,
                       "title_confidence": "explicit" if candidate.get("explicit_name") or override else
                       ("heuristic" if title else "unresolved"), "text_count": text_count,
                       "confidence": {"level": "explicit" if candidate.get("explicit_name") or override else "heuristic" if title else "unresolved",
                                      "reasons": (["explicit title/name override"] if candidate.get("explicit_name") or override else
                                                  ["in-frame title text selected by layer/tag/position/height rules"] if title else ["no matching in-frame title"])
                                                 + candidate.get("hint_reasons", [])},
                       **metadata,
                       "warnings": notes})
    if not sheets:
        warnings.append("No suitable axis-aligned rectangular frames were detected; supply explicit bboxes or handles.")
    warnings.append("Automatic sheet/title discovery is heuristic; inspect the manifest and HTML before relying on names or completeness.")
    unused = set(names or {}) - {key for sheet in sheets for key in [sheet["id"], *sheet["frame_handles"]]}
    if unused:
        raise CadError("NAME_OVERRIDE_NOT_FOUND", "Name overrides do not match any discovered sheet.", {"keys": sorted(unused)[:20]})
    return {"layout": layout, "sheets": sheets, "discovery_options": {"frame_block_regex": frame_block_regex,
            "frame_size": frame_size, "frame_size_rule": "aspect-ratio hint only; no unit/scale inference",
            "title_layer": title_layer, "title_regex": title_regex, "number_layer": number_layer,
            "shared_frame": shared_frame},
            "summary": {"sheet_count": len(sheets), "all_sheet_count": len(sheets),
                        "frame_candidate_count": len(candidates), "nested_candidates_removed": rejected,
                        "small_untitled_orphans_removed": len(orphan_rejected),
                        "small_orphan_rule": "Unique untitled rectangles below 1% of largest frame area are excluded when at least three frames exist; explicit frames bypass this rule.",
                        "layout_entity_count": len(selected), "title_count": sum(bool(s["title"]) for s in sheets)},
            "warnings": sorted(set(warnings))}


def discover_drawing(input_path, layout="Model", frames=None, frame_layer=None,
                     min_frame_area=10000, max_sheets=200, output=None, source_info=None, overwrite=False,
                     **discovery_options):
    source = Path(input_path).expanduser().resolve()
    with readable_dxf(source) as dxf_path:
        document = read_document(dxf_path)
        result = discover_sheets(document, layout, frames, frame_layer, min_frame_area, max_sheets, **discovery_options)
    result["source"] = source_info or {"input": str(source), "sha256": file_sha256(source)}
    if output is not None:
        target = Path(output).expanduser().resolve()
        protected = {source}
        if isinstance(source_info, dict):
            for key in ("path", "input"):
                if isinstance(source_info.get(key), str):
                    protected.add(Path(source_info[key]).expanduser().resolve())
        if target in protected:
            raise CadError("OUTPUT_CONFLICT", "Sheet discovery JSON must not replace the source drawing.")
        with atomic_output(target, overwrite=overwrite) as temporary:
            temporary.write_text(json.dumps(jsonable(result), ensure_ascii=False, indent=2), encoding="utf-8")
        return {"source": result["source"], "summary": result["summary"], "manifest": str(target),
                "examples": result["sheets"][:3], "warnings": result["warnings"]}
    return result


def _entity_bounds(entities):
    """Compatibility wrapper for common native WCS bounds selection."""
    from .spatial import entity_bounds
    return entity_bounds(entities)


def _preservation_names(source_document):
    names = {"layers": [name for name in ("0", "Defpoints") if source_document.layers.has_entry(name)],
             "styles": ["Standard"] if source_document.styles.has_entry("Standard") else [],
             "dimstyles": ["Standard"] if source_document.dimstyles.has_entry("Standard") else []}
    for header, table in [("$DIMSTYLE", "dimstyles"), ("$TEXTSTYLE", "styles"),
                          ("$CLAYER", "layers")]:
        value = source_document.header.get(header)
        if isinstance(value, str) and getattr(source_document, table).has_entry(value):
            if value.casefold() not in {n.casefold() for n in names[table]}:
                names[table].append(value)
    return names


def _queue_preservation_resources(loader, source_document):
    names = _preservation_names(source_document)
    for table, method in [("layers", "load_layers"), ("styles", "load_text_styles"),
                          ("dimstyles", "load_dim_styles")]:
        if names[table]:
            getattr(loader, method)(names[table])
    linetype = source_document.header.get("$CELTYPE")
    if isinstance(linetype, str) and source_document.linetypes.has_entry(linetype):
        loader.load_linetypes([linetype])


def preserve_builtin_resources(source_document, target_document, load_missing=True):
    """Copy semantic default values in place, keeping target resource handles.

    ezdxf's KEEP policy intentionally keeps fresh Standard/Defpoints defaults.
    Preserve those existing record handles and update their scalar/name fields.
    Only missing dependencies are loaded; NUM_PREFIX avoids an upstream KEEP
    collision bug when a copied BLOCK_RECORD has not yet acquired its block.
    """
    from ezdxf import xref
    names = _preservation_names(source_document)
    if load_missing:
        loader = xref.Loader(source_document, target_document, conflict_policy=xref.ConflictPolicy.NUM_PREFIX)
        queued = False
        for table, method in [("layers", "load_layers"), ("styles", "load_text_styles"),
                              ("dimstyles", "load_dim_styles")]:
            missing = [name for name in names[table] if not getattr(target_document, table).has_entry(name)]
            if missing:
                getattr(loader, method)(missing)
                queued = True
        for style_name in names["dimstyles"]:
            style = source_document.dimstyles.get(style_name)
            text_style = style.dxf.get("dimtxsty")
            if text_style and not target_document.styles.has_entry(text_style):
                loader.load_text_styles([text_style])
                queued = True
            for field in ("dimltype", "dimltex1", "dimltex2"):
                linetype = style.dxf.get(field)
                if linetype and not target_document.linetypes.has_entry(linetype):
                    loader.load_linetypes([linetype])
                    queued = True
            for field in ("dimblk", "dimblk1", "dimblk2", "dimldrblk"):
                block_name = style.dxf.get(field)
                source_block = source_document.blocks.get(block_name) if block_name else None
                if source_block is not None and target_document.blocks.get(block_name) is None:
                    loader.load_block_layout(source_block)
                    queued = True
        linetype = source_document.header.get("$CELTYPE")
        if (linetype and source_document.linetypes.has_entry(linetype)
                and not target_document.linetypes.has_entry(linetype)):
            loader.load_linetypes([linetype])
            queued = True
        if queued:
            loader.execute()
    imported = dict(names)
    selected_styles = {}
    for header, table, method in [("$DIMSTYLE", "dimstyles", "load_dim_styles"),
                                  ("$TEXTSTYLE", "styles", "load_text_styles"),
                                  ("$CLAYER", "layers", "load_layers"),
                                  ("$CELTYPE", "linetypes", "load_linetypes")]:
        value = source_document.header.get(header)
        if isinstance(value, str) and getattr(source_document, table).has_entry(value):
            selected_styles[header] = value
    for table, entries in names.items():
        for name in entries:
            source_entry = getattr(source_document, table).get(name)
            target_entry = getattr(target_document, table).get(name)
            attributes = source_entry.dxf.all_existing_dxf_attribs()
            for key in list(target_entry.dxf.all_existing_dxf_attribs()):
                if key not in {"handle", "owner", "name"} and not key.endswith("_handle"):
                    target_entry.dxf.discard(key)
            target_entry.dxf.update({key: value for key, value in attributes.items()
                                     if key not in {"handle", "owner"} and not key.endswith("_handle")})
            if table == "styles":
                target_entry.discard_extended_font_data()
                if source_entry.has_extended_font_data:
                    # get_extended_font_data()/set_extended_font_data() expose
                    # only family/italic/bold and discard charset/pitch bits in
                    # the original 1071 value (e.g. SimSun 34306 becomes 34).
                    # Keep the complete native font record verbatim instead.
                    target_entry.set_xdata("ACAD", [(tag.code, tag.value)
                                                    for tag in source_entry.get_xdata("ACAD")])
            elif table == "dimstyles":
                target_entry.discard_handles()
    imported["record_handles"] = "existing target handles retained; DIMSTYLE named references regenerate handles on serialization"
    safe_headers = (
        "$LTSCALE", "$CELTSCALE", "$MEASUREMENT", "$LWDISPLAY", "$PDMODE", "$PDSIZE",
        "$INSBASE", "$PLINEGEN", "$LUNITS", "$LUPREC", "$AUNITS", "$AUPREC", "$PSLTSCALE",
        "$TEXTSIZE", "$CECOLOR", "$CELWEIGHT", "$CMLJUST", "$CMLSCALE", "$MIRRTEXT",
        "$QTEXTMODE", "$FILLMODE", "$ATTMODE", "$ANGBASE", "$ANGDIR", "$INSUNITS", "$MSLTSCALE",
    )
    copied_headers = []
    for key in safe_headers + tuple(selected_styles):
        if key in source_document.header:
            target_document.header[key] = source_document.header[key]
            copied_headers.append(key)
    # Header dimension defaults are scalar values/names in ezdxf, not raw DXF
    # pointers. Resource-bearing arrow/text names are loaded via DIMSTYLE above.
    for key in source_document.header.varnames():
        if not key.startswith("$DIM") or key in {"$DIMSTYLE", "$DIMTXSTY", "$DIMBLK", "$DIMBLK1", "$DIMBLK2", "$DIMLDRBLK", "$DIMLTYPE", "$DIMLTEX1", "$DIMLTEX2"}:
            continue
        value = source_document.header[key]
        if type(value) in (int, float, str):
            target_document.header[key] = value
            copied_headers.append(key)
    imported["presentation_headers"] = copied_headers
    return imported


def _new_export_document(source_document):
    import ezdxf
    target = ezdxf.new(source_document.dxfversion)
    target.units = source_document.units
    return target


def _image_geometry(entity):
    values = [entity.dxf.get("layer", "0")]
    for attribute in ("insert", "u_pixel", "v_pixel", "image_size"):
        values.append(tuple(entity.dxf.get(attribute)))
    for attribute, default in [("flags", 3), ("clipping", 0), ("clipping_boundary_type", 1),
                               ("brightness", 50), ("contrast", 50), ("fade", 0)]:
        values.append(entity.dxf.get(attribute, default))
    values.append(tuple(tuple(point) for point in entity.boundary_path))
    return tuple(values)


def _same_values(first, second):
    if isinstance(first, (list, tuple)) and isinstance(second, (list, tuple)):
        return len(first) == len(second) and all(_same_values(a, b) for a, b in zip(first, second))
    if isinstance(first, (int, float)) and isinstance(second, (int, float)):
        return math.isclose(first, second, rel_tol=1e-12, abs_tol=1e-7)
    return first == second


def _image_definition_values(definition):
    values = []
    for key in ("class_version", "filename", "image_size", "pixel_size", "loaded", "resolution_units"):
        value = definition.dxf.get(key, definition.DXFATTRIBS.get(key).default)
        values.append((key, tuple(value) if hasattr(value, "x") else value))
    return tuple(values)


def repair_image_references(source_document, target_document, source_entities=None,
                            target_entities=None, include_blocks=True):
    """Preserve distinct source IMAGEDEF identities and valid reactor links.

    IMAGE associations use full placement/pixel/clip geometry, never handles or
    accidental target ordering. Indistinguishable overlapping images fail rather
    than risk assigning the wrong external file. No image file is opened.
    """
    source_entities = list(source_document.modelspace() if source_entities is None else source_entities)
    target_entities = list(target_document.modelspace() if target_entities is None else target_entities)
    groups = [(source_entities, target_entities, "layout")]
    if include_blocks:
        for block in target_document.blocks:
            if block.is_any_layout:
                continue
            source_block = source_document.blocks.get(block.name)
            if source_block is not None:
                groups.append((list(source_block), list(block), block.name))
    paired = []
    for originals, copies, scope in groups:
        originals = [e for e in originals if e.dxftype() == "IMAGE"]
        copies = [e for e in copies if e.dxftype() == "IMAGE"]
        if len(originals) != len(copies):
            raise CadError("IMAGE_REFERENCE_MISMATCH", "Image inventory differs after export.",
                           {"scope": scope, "source_count": len(originals), "target_count": len(copies)})
        available = list(copies)
        for original in originals:
            matches = [copy for copy in available if _same_values(_image_geometry(original), _image_geometry(copy))]
            if len(matches) != 1:
                raise CadError("IMAGE_ASSOCIATION_AMBIGUOUS", "Cannot uniquely match an exported image by placement and clipping geometry.",
                               {"scope": scope, "source_handle": original.dxf.handle, "matches": len(matches)})
            copy = matches[0]
            available.remove(copy)
            paired.append((original, copy))
    definitions, created, repaired = {}, 0, 0
    for original, copy in paired:
        source_definition = original.image_def
        if source_definition is None or not source_definition.is_alive:
            raise CadError("IMAGE_REFERENCE_MISSING", "Source IMAGE has no valid IMAGEDEF.",
                           {"source_handle": original.dxf.handle})
        source_key = source_definition.dxf.handle
        target_definition = definitions.get(source_key)
        if target_definition is None:
            image_dict = target_document.rootdict.get_required_dict("ACAD_IMAGE_DICT")
            name = f"__TCAD_IMAGEDEF_{source_key}"
            suffix = 1
            while name in image_dict:
                existing = image_dict[name]
                if _same_values(_image_definition_values(existing), _image_definition_values(source_definition)):
                    target_definition = existing
                    break
                suffix += 1
                name = f"__TCAD_IMAGEDEF_{source_key}_{suffix}"
            if target_definition is None:
                size = source_definition.dxf.image_size
                target_definition = target_document.add_image_def(source_definition.dxf.filename,
                                                                   (size.x, size.y), name=name)
                for key, value in source_definition.dxf.all_existing_dxf_attribs().items():
                    if key not in {"handle", "owner"}:
                        target_definition.dxf.set(key, value)
                created += 1
            definitions[source_key] = target_definition
        reactor = copy.image_def_reactor
        if reactor is None or not reactor.is_alive:
            raise CadError("IMAGE_REACTOR_MISSING", "Exported IMAGE has no valid definition reactor.",
                           {"target_handle": copy.dxf.handle})
        previous = copy.image_def
        if previous is not target_definition:
            if previous is not None and previous.is_alive:
                previous.discard_reactor_handle(reactor.dxf.handle)
            copy.image_def = target_definition
            repaired += 1
        target_definition.append_reactor_handle(reactor.dxf.handle)
        reactor.dxf.image_handle = copy.dxf.handle
        reactor.dxf.owner = copy.dxf.handle
        if (copy.dxf.image_def_handle != target_definition.dxf.handle
                or copy.dxf.image_def_reactor_handle != reactor.dxf.handle
                or reactor.dxf.handle not in target_definition.get_reactors()):
            raise CadError("IMAGE_REFERENCE_MISMATCH", "Image definition/reactor links failed verification.")
    return {"image_count": len(paired), "source_definition_count": len(definitions),
            "definitions_created": created, "references_repaired": repaired,
            "association": "unique full native placement/pixel vectors/size/clipping geometry; definition identity retained"}


def _export_entities(source_document, selected_layout, entities):
    from ezdxf import xref
    target = _new_export_document(source_document)
    handles = {e.dxf.handle for e in entities}
    loader = xref.Loader(source_document, target)
    _queue_preservation_resources(loader, source_document)
    if selected_layout.name == "Model":
        loader.load_modelspace(filter_fn=lambda e: e.dxf.handle in handles)
    else:
        loader.load_paperspace_layout_into(selected_layout, target.modelspace(),
                                          filter_fn=lambda e: e.dxf.handle in handles)
    loader.execute()
    preserve_builtin_resources(source_document, target, load_missing=False)
    if len(target.modelspace()) != len(entities):
        raise CadError("SHEET_EXPORT_INCOMPLETE", "Native entity count changed during export.",
                       {"selected": len(entities), "exported": len(target.modelspace())})
    images = repair_image_references(source_document, target, entities, target.modelspace())
    return target, images


def _semantic_tags(entity):
    """Serialized native values with remappable identity pointers excluded.

    This is a geometric/resource check, not an assertion about CAD application
    object graphs, material dictionaries or dimension associativity.
    """
    from ezdxf.lldxf.tagwriter import TagCollector
    collector = TagCollector(optional=False)
    entity.export_dxf(collector)
    tags, subclass, columns, kind = [], "", False, entity.dxftype()
    for index, tag in enumerate(collector.tags):
        if tag.code == 0:
            kind, subclass = tag.value, ""
        if tag.code == 100:
            subclass = tag.value
        if subclass == "AcDbXrecord":
            continue
        if tag.code == 1000 and tag.value == "ACAD_MTEXT_COLUMNS_BEGIN":
            columns = True
        if columns:
            if tag.code == 1000 and tag.value == "ACAD_MTEXT_COLUMNS_END":
                columns = False
            continue
        if tag.code in {5, 102, 105, 1005} or 320 <= tag.code <= 369 or 390 <= tag.code <= 399:
            continue
        if tag.code == 2 and kind in {"INSERT", "DIMENSION"}:
            continue
        if kind == "LTYPE" and tag.code == 3:
            continue
        if kind == "HATCH" and tag.code == 97 and (tag.value == 0 or
                (index + 1 < len(collector.tags) and collector.tags[index + 1].code == 330)):
            continue
        value = tag.value.upper() if tag.code == 6 and isinstance(tag.value, str) else tag.value
        tags.append((tag.code, value))
    return tags


def _tags_equal(first, second):
    return _same_values(_semantic_tags(first), _semantic_tags(second))


def _verify_dxf_inventory(source_document, target_document, originals):
    """Check every selected native entity and recursively referenced block."""
    copies = list(target_document.modelspace())
    failures, resources, blocks, image_ids = [], set(), set(), {}
    source_definition_ids = set()
    def check_pairs(first, second, scope):
        if len(first) != len(second):
            failures.append({"check": "entity_count", "scope": scope, "expected": len(first), "actual": len(second)})
            return
        for a, b in zip(first, second):
            if a.dxftype() != b.dxftype() or not _tags_equal(a, b):
                failures.append({"check": "native_geometry", "scope": scope, "source_handle": a.dxf.handle,
                                 "target_handle": b.dxf.handle, "source_type": a.dxftype(), "target_type": b.dxftype()})
            for attr, table in (("layer", "layers"), ("style", "styles"), ("dimstyle", "dimstyles"), ("linetype", "linetypes")):
                if a.dxf.is_supported(attr) and b.dxf.is_supported(attr):
                    x, y = a.dxf.get(attr), b.dxf.get(attr)
                    if x and y and str(x).upper() not in {"BYLAYER", "BYBLOCK"}:
                        resources.add((table, x, y))
            if a.dxftype() in {"INSERT", "DIMENSION"} and a.dxftype() == b.dxftype():
                attr = "name" if a.dxftype() == "INSERT" else "geometry"
                na, nb = a.dxf.get(attr), b.dxf.get(attr)
                if na and nb and (na, nb) not in blocks:
                    blocks.add((na, nb))
                    ba, bb = source_document.blocks.get(na), target_document.blocks.get(nb)
                    if ba is None or bb is None:
                        failures.append({"check": "missing_block", "source": na, "target": nb})
                    else:
                        if not _same_values(tuple(ba.base_point), tuple(bb.base_point)):
                            failures.append({"check": "block_base_point", "source": na, "target": nb})
                        check_pairs(list(ba), list(bb), f"block:{na}")
            if a.dxftype() == "IMAGE" and b.dxftype() == "IMAGE":
                da, db, reactor = a.image_def, b.image_def, b.image_def_reactor
                if da is None or db is None or not _same_values(_image_definition_values(da), _image_definition_values(db)):
                    failures.append({"check": "image_definition", "source_handle": a.dxf.handle})
                elif (reactor is None or reactor.dxf.get("image_handle") != b.dxf.handle or
                        reactor.dxf.get("owner") != b.dxf.handle or reactor.dxf.handle not in db.get_reactors()):
                    failures.append({"check": "image_reactor", "source_handle": a.dxf.handle})
                else:
                    source_definition_ids.add(da.dxf.handle)
                    if db.dxf.handle in image_ids and image_ids[db.dxf.handle] != da.dxf.handle:
                        failures.append({"check": "image_identity_collapsed", "target_definition": db.dxf.handle})
                    image_ids[db.dxf.handle] = da.dxf.handle
    check_pairs(originals, copies, "layout")
    for table, names in _preservation_names(source_document).items():
        for name in names:
            resources.add((table, name, name))
    for table, a, b in sorted(resources):
        source_table, target_table = getattr(source_document, table), getattr(target_document, table)
        if not source_table.has_entry(a) or not target_table.has_entry(b):
            failures.append({"check": "missing_resource", "table": table, "source": a, "target": b})
            continue
        ea, eb = source_table.get(a), target_table.get(b)
        ta, tb = [t for t in _semantic_tags(ea) if t[0] != 2], [t for t in _semantic_tags(eb) if t[0] != 2]
        if not _same_values(ta, tb):
            failures.append({"check": "resource_values", "table": table, "source": a, "target": b})
        if table == "dimstyles":
            for key in ("dimtxsty", "dimblk", "dimblk1", "dimblk2", "dimldrblk", "dimltype", "dimltex1", "dimltex2"):
                if str(ea.dxf.get(key, "") or "").casefold() != str(eb.dxf.get(key, "") or "").casefold():
                    failures.append({"check": "dimstyle_reference", "style": a, "attribute": key})
    headers = ("$INSUNITS", "$LTSCALE", "$CELTSCALE", "$PSLTSCALE", "$MEASUREMENT", "$DIMSTYLE",
               "$TEXTSTYLE", "$TEXTSIZE", "$LUNITS", "$LUPREC", "$AUNITS", "$AUPREC", "$FILLMODE")
    for key in headers:
        if key in source_document.header and not _same_values(source_document.header[key], target_document.header.get(key)):
            failures.append({"check": "presentation_header", "header": key})
    return {"ok": not failures, "native_entity_count": len(copies), "resource_pairs_checked": len(resources),
            "block_definition_pairs_checked": len(blocks), "image_count": len(image_ids),
            "source_image_definition_count": len(source_definition_ids),
            "failure_count": len(failures), "failures": failures[:20]}


def _verify_svg(path):
    from xml.etree import ElementTree
    try:
        root = ElementTree.parse(path).getroot()
        view = [float(v) for v in root.attrib.get("viewBox", "").replace(",", " ").split()]
    except (ElementTree.ParseError, ValueError) as exc:
        return {"ok": False, "reason": "invalid_svg_xml_or_viewbox", "detail": str(exc)[:240]}
    paths = [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "path" and e.attrib.get("d", "").strip()]
    external = []
    for element in root.iter():
        for key, value in element.attrib.items():
            if key.rsplit("}", 1)[-1] in {"href", "src"} and not value.startswith(("#", "data:")):
                external.append(value[:240])
            for match in re.findall(r"url\(\s*['\"]?([^)'\"]+)", value):
                if not match.startswith(("#", "data:")):
                    external.append(match[:240])
    raw = path.read_text("utf-8")
    external_css = bool(re.search(r"@import|@font-face|file://", raw, re.I))
    valid = len(view) == 4 and all(math.isfinite(v) for v in view) and view[2] > 0 and view[3] > 0
    return {"ok": valid and bool(paths) and not external and not external_css,
            "valid_viewbox": valid, "nonempty": bool(paths), "path_count": len(paths),
            "self_contained": not external and not external_css,
            "external_reference_count": len(external), "external_reference_samples": external[:4]}


def _verify_thumbnail(path, receipt):
    from PIL import Image
    try:
        with path.open("rb") as stream:
            signature = stream.read(8)
        with Image.open(path) as image:
            width, height = image.size
            valid = image.format == "PNG" and signature == b"\x89PNG\r\n\x1a\n"
            image.verify()
        digest = file_sha256(path)
        return {"ok": valid and width == receipt["width"] and height == receipt["height"]
                      and digest == receipt["sha256"] and path.stat().st_size == receipt["bytes"],
                "width": width, "height": height, "sha256": digest, "bytes": path.stat().st_size}
    except (OSError, ValueError, KeyError) as exc:
        return {"ok": False, "reason": "invalid_png_thumbnail", "detail": str(exc)[:240]}


def verify_split_artifacts(source_path, output_dir, manifest=None, source_document=None, source_sha256=None):
    """Read all serialized artifacts; fail on any actual preservation mismatch.

    Title validity checks evidence containment only; it is not an independent
    architectural interpretation of a drawing title. SVG checks are structural,
    not proof that every original DWG object or proprietary glyph was rendered.
    """
    started = time.perf_counter()
    directory = Path(output_dir).expanduser().resolve()
    if manifest is None:
        manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    if source_document is None:
        with readable_dxf(source_path) as normalized:
            source_document = read_document(normalized)
    native = list(source_document.layouts.get(manifest.get("layout", "Model")))
    inventory = {e.dxf.handle: e for e in native}
    verification = {"version": 1, "method": "actual serialized DXF readback and native geometric/resource comparison; SVG XML/path/reference checks",
        "scope": "relative to normalized DXF; no lossless DWG, application-object-graph, pixel-equivalence or title-meaning claim",
        "reopened_dxf_count": 0, "parsed_svg_count": 0, "nonempty_svg_count": 0,
        "verified_thumbnail_count": 0,
        "valid_title_evidence_count": 0, "empty_sheet_count": 0, "failures": [], "sheets": []}
    assigned = set()
    for sheet in manifest["sheets"]:
        handles = sheet.get("source_handles", [])
        if len(handles) != len(set(handles)) or not set(handles) <= inventory.keys():
            raise CadError("INVALID_MANIFEST", "Verification handles do not match unique source entities.")
        selected = set(handles)
        assigned.update(selected)
        originals = [e for e in native if e.dxf.handle in selected]
        path = _safe_artifact_path(directory, sheet.get("dxf"), ".dxf")
        target = read_document(path)
        checked = _verify_dxf_inventory(source_document, target, originals)
        checked["artifact_sha256"] = file_sha256(path)
        if sheet.get("dxf_sha256") and sheet["dxf_sha256"] != checked["artifact_sha256"]:
            checked["ok"] = False
            checked["failures"].append({"check": "artifact_sha256"})
        verification["reopened_dxf_count"] += 1
        verification["empty_sheet_count"] += not bool(originals)
        evidence = sheet.get("title_evidence", [])
        box = sheet.get("bbox")
        title_inside = bool(evidence) and bool(box) and all(box[0] <= t["x"] <= box[2] and box[1] <= t["y"] <= box[3] for t in evidence)
        verification["valid_title_evidence_count"] += title_inside
        item = {"id": sheet["id"], "dxf": checked, "title_evidence_inside_frame": title_inside,
                "primary_title_count": int(bool(sheet.get("primary_title", sheet.get("title"))))}
        if sheet.get("svg"):
            svg = _safe_artifact_path(directory, sheet["svg"], ".svg")
            item["svg"] = _verify_svg(svg)
            item["svg"]["artifact_sha256"] = file_sha256(svg)
            if sheet.get("svg_sha256") and sheet["svg_sha256"] != item["svg"]["artifact_sha256"]:
                item["svg"]["ok"] = False
                item["svg"]["reason"] = "artifact_sha256"
            verification["parsed_svg_count"] += 1
            verification["nonempty_svg_count"] += item["svg"].get("nonempty", False)
        if sheet.get("thumbnail"):
            thumbnail = sheet["thumbnail"]
            png = _safe_artifact_path(directory, thumbnail.get("output", thumbnail.get("path")), ".png")
            item["thumbnail"] = _verify_thumbnail(png, thumbnail)
            verification["verified_thumbnail_count"] += item["thumbnail"]["ok"]
        item["ok"] = (checked["ok"] and bool(originals) and item.get("svg", {"ok": True})["ok"]
                      and item.get("thumbnail", {"ok": True})["ok"])
        if not item["ok"]:
            verification["failures"].append({"id": sheet["id"], "dxf": checked["failures"],
                                              "svg": item.get("svg"), "thumbnail": item.get("thumbnail")})
        verification["sheets"].append(item)
    remainder = manifest.get("unassigned_artifact")
    remainder_handles = set()
    if remainder:
        path = _safe_artifact_path(directory, remainder["dxf"], ".dxf")
        wanted = set(remainder["source_handles"])
        remainder_handles = wanted
        if len(wanted) != len(remainder["source_handles"]) or wanted != set(inventory) - assigned:
            verification["failures"].append({"check": "remainder_inventory", "expected_count": len(set(inventory) - assigned), "actual_count": len(wanted)})
        verification["remainder"] = _verify_dxf_inventory(source_document, read_document(path), [e for e in native if e.dxf.handle in wanted])
        if not verification["remainder"]["ok"]:
            verification["failures"].append({"id": "unassigned", "dxf": verification["remainder"]["failures"]})
    verification["source_layout_entity_count"] = len(native)
    verification["preserved_native_entity_count"] = len(assigned | remainder_handles)
    verification["all_source_entities_preserved"] = assigned | remainder_handles == set(inventory)
    if not verification["all_source_entities_preserved"]:
        verification["failures"].append({"check": "source_coverage", "missing_count": len(set(inventory) - assigned - remainder_handles)})
    current_hash = file_sha256(source_path)
    identity = manifest.get("source", {})
    expected_hash = source_sha256 or (identity.get("normalized_dxf_sha256")
                                    if Path(source_path).suffix.lower() == ".dxf" else identity.get("sha256"))
    if expected_hash is None and Path(source_path).suffix.lower() == ".dxf":
        expected_hash = identity.get("sha256")
    verification["source_sha256"] = current_hash
    verification["source_unchanged"] = expected_hash is not None and current_hash == expected_hash
    if not verification["source_unchanged"]:
        verification["failures"].append({"check": "source_sha256", "expected": expected_hash, "actual": current_hash})
    original = identity.get("path", identity.get("input"))
    if original and identity.get("sha256") and Path(original).is_file():
        original_hash = file_sha256(original)
        verification["original_source_sha256"] = original_hash
        verification["original_source_unchanged"] = original_hash == identity["sha256"]
        if not verification["original_source_unchanged"]:
            verification["failures"].append({"check": "original_source_sha256", "expected": identity["sha256"], "actual": original_hash})
    verification["ok"] = not verification["failures"]
    verification["elapsed_seconds"] = round(time.perf_counter() - started, 6)
    manifest["verification"] = verification
    return verification


def _safe_artifact_path(directory, value, suffix):
    if (not isinstance(value, str) or not value or "/" in value or "\\" in value
            or Path(value).is_absolute() or Path(value).suffix.lower() != suffix):
        raise CadError("UNSAFE_PATH", "Artifact names must be plain local filenames with the expected extension.")
    path = directory / value
    if path.is_symlink() or path.resolve().parent != directory:
        raise CadError("UNSAFE_PATH", "Artifact path escapes the output directory.")
    return path


def export_unassigned(document, handles, output_path, layout="Model", overwrite=False):
    """Preserve all explicitly selected remainder entities, even invisible/empty ones."""
    target = Path(output_path).expanduser().resolve()
    if document.filename and target == Path(document.filename).expanduser().resolve():
        raise CadError("OUTPUT_CONFLICT", "Remainder output must not replace the source drawing.")
    selected = document.layouts.get(layout)
    requested = set(handles)
    entities = [e for e in selected if e.dxf.handle in requested]
    if len(entities) != len(requested):
        raise CadError("ENTITY_NOT_FOUND", "Remainder handles must identify native entities in the selected layout.")
    exported, images = _export_entities(document, selected, entities)
    with atomic_output(target, overwrite=overwrite) as temporary:
        exported.saveas(temporary)
        reopened = read_document(temporary)
        if len(reopened.modelspace()) != len(entities):
            raise CadError("SHEET_EXPORT_INCOMPLETE", "Remainder DXF inventory changed on serialization.")
        checked = repair_image_references(document, reopened, entities, reopened.modelspace())
        if checked["references_repaired"] or checked["definitions_created"]:
            raise CadError("IMAGE_REFERENCE_MISMATCH", "Remainder image links were not preserved by serialization.")
    return {"dxf": target.name, "entity_count": len(entities), "bytes": target.stat().st_size,
            "sha256": file_sha256(target), "image_preservation": images}


def preserve_unassigned_artifact(document, manifest, output_dir, overwrite=False):
    directory = Path(output_dir).expanduser().resolve()
    selected = document.layouts.get(manifest.get("layout", "Model"))
    assigned = {handle for sheet in manifest["sheets"] for handle in sheet["source_handles"]}
    remainder = [entity for entity in selected if entity.dxf.handle not in assigned]
    dxf_name, json_name = "__tcad_unassigned__.dxf", "unassigned.json"
    dxf_path = _safe_artifact_path(directory, dxf_name, ".dxf")
    json_path = _safe_artifact_path(directory, json_name, ".json")
    handles = [entity.dxf.handle for entity in remainder]
    receipt = export_unassigned(document, handles, dxf_path, selected.name, overwrite=overwrite)
    inventory = {"version": 1, "layout": selected.name,
                 "scope": "Remainder outside detected frames or without computable bounds; not an additional sheet.",
                 "entity_count": len(remainder), "source_handles": handles,
                 "entities": [{"handle": e.dxf.handle, "type": e.dxftype(),
                               "layer": e.dxf.get("layer", "0"), "invisible": e.dxf.get("invisible", 0),
                               **({"block_name": e.dxf.name} if e.dxftype() == "INSERT" else {})}
                              for e in remainder]}
    with atomic_output(json_path, overwrite=overwrite) as temporary:
        temporary.write_text(json.dumps(jsonable(inventory), ensure_ascii=False, indent=2), encoding="utf-8")
    receipt.update({"json": json_name, "json_bytes": json_path.stat().st_size,
                    "source_handles": handles})
    manifest["unassigned_artifact"] = receipt
    manifest["summary"]["preserved_native_entity_count"] = len(assigned | set(handles))
    manifest["summary"]["remainder_entity_count"] = len(remainder)
    return receipt


def repair_split_artifacts(source_path, output_dir, overwrite=False, preserve_unassigned=True):
    """Repair already generated DXFs atomically per file, without rendering SVGs.

    This is a maintenance helper for locally generated manifests. The manifest is
    published last; an error may leave completed per-file repairs, never a partial
    DXF. Every serialized artifact is checked before publishing the manifest.
    """
    if not overwrite:
        raise CadError("OVERWRITE_REQUIRED", "Repairing existing split artifacts requires overwrite=True.")
    repair_started = time.perf_counter()
    repair_input_hash = file_sha256(source_path)
    directory = Path(output_dir).expanduser().resolve()
    manifest_path = _safe_artifact_path(directory, "manifest.json", ".json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest.get("sheets"), list) or len(manifest["sheets"]) > 200:
        raise CadError("INVALID_MANIFEST", "Expected a bounded split manifest.")
    paths = [_safe_artifact_path(directory, sheet.get("dxf"), ".dxf") for sheet in manifest["sheets"]]
    if len({str(path).casefold() for path in paths}) != len(paths):
        raise CadError("INVALID_MANIFEST", "Each sheet must use a distinct DXF artifact path.")
    with destination_lock(directory), readable_dxf(source_path) as normalized:
        normalized_hash = file_sha256(normalized)
        document = read_document(normalized)
        identity = manifest.get("source", {})
        expected = identity.get("normalized_dxf_sha256") if isinstance(identity, dict) else None
        if expected is None and isinstance(identity, dict):
            expected = identity.get("sha256") if Path(source_path).suffix.lower() == ".dxf" else None
        if expected is not None and normalized_hash != expected:
            raise CadError("REVISION_CONFLICT", "Repair source differs from the original split source.")
        selected = document.layouts.get(manifest.get("layout", "Model"))
        native = list(selected)
        inventory = {entity.dxf.handle for entity in native}
        repaired = []
        for sheet in manifest["sheets"]:
            path = _safe_artifact_path(directory, sheet.get("dxf"), ".dxf")
            handles = sheet.get("source_handles")
            if (not isinstance(handles, list) or any(not isinstance(h, str) for h in handles)
                    or len(handles) != len(set(handles)) or not set(handles) <= inventory):
                raise CadError("INVALID_MANIFEST", "Sheet handles must be unique native source handles.")
            handle_set = set(handles)
            originals = [entity for entity in native if entity.dxf.handle in handle_set]
            target = read_document(path)
            resources = preserve_builtin_resources(document, target)
            images = repair_image_references(document, target, originals, target.modelspace())
            with atomic_output(path, overwrite=True) as temporary:
                target.saveas(temporary)
                reopened = read_document(temporary)
                if len(reopened.modelspace()) != len(originals):
                    raise CadError("SHEET_EXPORT_INCOMPLETE", "Repaired DXF inventory changed on serialization.")
                checked = repair_image_references(document, reopened, originals, reopened.modelspace())
                if checked["references_repaired"] or checked["definitions_created"]:
                    raise CadError("IMAGE_REFERENCE_MISMATCH", "Repaired image links were not preserved by serialization.")
            sheet["dxf_bytes"] = path.stat().st_size
            sheet["dxf_sha256"] = file_sha256(path)
            sheet["image_preservation"] = images
            sheet["resource_preservation"] = resources
            repaired.append({"id": sheet["id"], **images})
        if preserve_unassigned:
            preserve_unassigned_artifact(document, manifest, directory, overwrite=True)
        manifest["statistics"]["resource_repair"] = {"sheet_count": len(repaired),
            "image_count": sum(item["image_count"] for item in repaired),
            "elapsed_seconds": round(time.perf_counter() - repair_started, 6),
            "scope": "DXF-only resource repair; existing SVG bytes unchanged; per-file atomic, manifest last"}
        manifest["summary"]["artifact_bytes"] = sum(
            int(sheet.get("dxf_bytes", 0)) + int(sheet.get("svg_bytes", 0)) for sheet in manifest["sheets"])
        remainder = manifest.get("unassigned_artifact", {})
        manifest["summary"]["artifact_bytes"] += int(remainder.get("bytes", 0)) + int(remainder.get("json_bytes", 0))
        verification = verify_split_artifacts(source_path, directory, manifest=manifest,
                                               source_document=document, source_sha256=repair_input_hash)
        if not verification["ok"]:
            raise CadError("SPLIT_VERIFICATION_FAILED", "Repaired artifacts failed actual readback verification.",
                           {"failures": verification["failures"][:4]})
        with atomic_output(manifest_path, overwrite=True) as temporary:
            temporary.write_text(json.dumps(jsonable(manifest), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"manifest": str(manifest_path), "sheet_count": len(repaired),
            "image_count": sum(item["image_count"] for item in repaired),
            "unassigned_artifact": manifest.get("unassigned_artifact")}


def create_split_thumbnails(output_dir, max_side=1280, overwrite=False):
    """Upgrade a local split gallery using its SVGs, without reopening CAD.

    All PNGs are rendered and checked in a temporary directory first. Publication
    is atomic per file with the manifest last; interrupted upgrades may leave
    complete unreferenced PNGs, never a partially written image.
    """
    from .thumbnails import create_thumbnail
    directory = Path(output_dir).expanduser().resolve()
    manifest_path = _safe_artifact_path(directory, "manifest.json", ".json")
    started = time.perf_counter()
    with destination_lock(directory):
        manifest = json.loads(manifest_path.read_text("utf-8"))
        sheets = manifest.get("sheets")
        if not isinstance(sheets, list) or len(sheets) > 200:
            raise CadError("INVALID_MANIFEST", "Expected a bounded split manifest.")
        maximum = manifest.get("limits", {}).get("max_total_bytes", 512 * 1024 * 1024)
        if type(maximum) is not int or not 1024 <= maximum <= 2 * 1024**3:
            raise CadError("INVALID_MANIFEST", "Invalid aggregate split byte budget.")
        stage = Path(tempfile.mkdtemp(prefix=".tcad-thumbnails-", dir=directory))
        try:
            prepared, names = [], set()
            for sheet in sheets:
                if not sheet.get("svg"):
                    continue
                svg = _safe_artifact_path(directory, sheet["svg"], ".svg")
                name = Path(sheet["svg"]).with_suffix(".png").name
                target = _safe_artifact_path(directory, name, ".png")
                if name.casefold() in names:
                    raise CadError("INVALID_MANIFEST", "Thumbnail artifact names must be unique.")
                names.add(name.casefold())
                if target.exists() and not overwrite:
                    raise CadError("OUTPUT_EXISTS", "Thumbnail already exists; explicit overwrite required.", {"path": str(target)})
                try:
                    receipt = create_thumbnail(svg, stage / name, max_side=max_side)
                except CadError as exc:
                    raise CadError(exc.code, exc.message, {**(exc.details or {}),
                        "sheet_id": sheet.get("id"), "stage": "png_thumbnail"}) from exc
                receipt["output"] = name
                checked = _verify_thumbnail(stage / name, receipt)
                if not checked["ok"]:
                    raise CadError("THUMBNAIL_VERIFICATION_FAILED", "Serialized PNG readback failed.",
                                   {"sheet_id": sheet.get("id"), "verification": checked})
                prepared.append((sheet, target, receipt, checked))
            existing_bytes = sum(path.stat().st_size for path in directory.iterdir() if path.is_file())
            projected = existing_bytes + sum(receipt["bytes"] - (target.stat().st_size if target.exists() else 0)
                                             for _, target, receipt, _ in prepared)
            if projected > maximum:
                raise CadError("SPLIT_OUTPUT_TOO_LARGE", "PNG upgrade exceeds aggregate split byte budget.",
                               {"bytes": projected, "max_total_bytes": maximum})
            for sheet, target, receipt, checked in prepared:
                sheet["thumbnail"] = receipt
                for item in manifest.get("verification", {}).get("sheets", []):
                    if item["id"] == sheet["id"]:
                        item["thumbnail"] = checked
            manifest.setdefault("summary", {})["thumbnail_count"] = len(prepared)
            manifest["summary"]["artifact_bytes"] = sum(
                int(sheet.get("dxf_bytes", 0)) + int(sheet.get("svg_bytes", 0)) + int(sheet.get("thumbnail", {}).get("bytes", 0))
                for sheet in sheets) + sum(int(manifest.get("unassigned_artifact", {}).get(k, 0)) for k in ("bytes", "json_bytes"))
            manifest["thumbnail_options"] = {"enabled": True, "max_side": max_side, "renderer": "resvg_py"}
            manifest.setdefault("statistics", {})["thumbnail_generation"] = {
                "count": len(prepared), "elapsed_seconds": round(time.perf_counter() - started, 6),
                "scope": "existing SVG only; no CAD read or DXF/SVG mutation"}
            if "verification" in manifest:
                manifest["verification"]["verified_thumbnail_count"] = len(prepared)
            encoded = json.dumps(jsonable(manifest), ensure_ascii=False, indent=2)
            projected += len(encoded.encode("utf-8")) - manifest_path.stat().st_size
            if projected > maximum:
                raise CadError("SPLIT_OUTPUT_TOO_LARGE", "PNG upgrade including updated manifest exceeds aggregate byte budget.",
                               {"bytes": projected, "max_total_bytes": maximum})
            for sheet, target, receipt, checked in prepared:
                with atomic_output(target, overwrite=overwrite) as temporary:
                    shutil.copyfile(stage / receipt["output"], temporary)
            with atomic_output(manifest_path, overwrite=True) as temporary:
                temporary.write_text(encoded, "utf-8")
            return {"manifest": str(manifest_path), "thumbnail_count": len(prepared),
                    "thumbnail_bytes": sum(receipt["bytes"] for _, _, receipt, _ in prepared),
                    "elapsed_seconds": round(time.perf_counter() - started, 6)}
        finally:
            shutil.rmtree(stage)


def split_drawing(input_path, output_dir, layout="Model", frames=None, frame_layer=None,
                  min_frame_area=10000, max_sheets=200, preview=True,
                  max_entities=100000, max_svg_bytes=64 * 1024 * 1024,
                  max_total_bytes=512 * 1024 * 1024, source_info=None, *, frame_block_regex=None,
                  frame_size=None, title_layer=None, title_regex=None, number_layer=None,
                  shared_frame="largest", names=None, render_options=None,
                  thumbnail=True, thumbnail_width=1280):
    """Convert/read once, export all sheets, and atomically publish one new directory.

    Entities touching a frame are retained whole (including crossing entities).
    SVGs are clipped to frames. DXF copies preserve WCS and referenced resources;
    this operation is selection/export, not destructive geometry clipping.
    """
    started = time.perf_counter()
    if type(preview) is not bool:
        raise CadError("INVALID_ARGUMENT", "preview must be a boolean.")
    if type(thumbnail) is not bool:
        raise CadError("INVALID_ARGUMENT", "thumbnail must be a boolean.")
    if type(thumbnail_width) is not int or not 16 <= thumbnail_width <= 4096:
        raise CadError("INVALID_ARGUMENT", "thumbnail_width must be between 16 and 4096 pixels.")
    if render_options is not None and (not isinstance(render_options, dict) or set(render_options) - {
            "foreground", "background", "hatch_mode", "lineweight_scale", "font_family", "layers", "exclude_layers"}):
        raise CadError("INVALID_ARGUMENT", "render_options contains unsupported SVG preview options.")
    if type(max_entities) is not int or not 1 <= max_entities <= 100000:
        raise CadError("INVALID_ARGUMENT", "max_entities must be between 1 and 100000.")
    if type(max_total_bytes) is not int or not 1024 <= max_total_bytes <= 2 * 1024**3:
        raise CadError("INVALID_ARGUMENT", "max_total_bytes must be between 1024 and 2147483648.")
    source = Path(input_path).expanduser().resolve()
    unresolved_target = Path(output_dir).expanduser().absolute()
    if unresolved_target.is_symlink():
        raise CadError("UNSAFE_PATH", "Output directory must not be a symbolic link.")
    target = unresolved_target.resolve()
    if target == source or target in source.parents:
        raise CadError("UNSAFE_PATH", "Output directory must not contain the source drawing.")
    if not target.parent.is_dir():
        raise CadError("DIRECTORY_NOT_FOUND", "Output directory parent must already exist.")
    source_hash = file_sha256(source)
    with destination_lock(target):
        if target.exists():
            raise CadError("OUTPUT_EXISTS", "Split output directory already exists; choose a new directory.",
                           {"output_dir": str(target)})
        operation = re.sub(r"[^a-zA-Z0-9_-]", "", os.environ.get("TCAD_OPERATION_ID", "local"))[:80]
        stage = Path(tempfile.mkdtemp(prefix=f".tcad-{operation}-sheet-", dir=target.parent))
        try:
            with readable_dxf(source) as dxf_path:
                document = read_document(dxf_path)
                parsed_at = time.perf_counter()
                manifest = discover_sheets(document, layout, frames, frame_layer, min_frame_area, max_sheets,
                    frame_block_regex=frame_block_regex, frame_size=frame_size, title_layer=title_layer,
                    title_regex=title_regex, number_layer=number_layer, shared_frame=shared_frame, names=names)
                discovered_at = time.perf_counter()
                sheets = manifest["sheets"]
                if not sheets:
                    raise CadError("NO_SHEETS_FOUND", "No suitable sheets were found; supply explicit frame bboxes.")
                selected = document.layouts.get(layout)
                native = list(selected)
                bounded, unknown, bounds_statistics = _entity_bounds(native)
                bounded_at = time.perf_counter()
                manifest["source"] = source_info or {"input": str(source), "sha256": source_hash}
                manifest["version"] = 1
                manifest["render_options"] = render_options or {}
                manifest["thumbnail_options"] = {"enabled": thumbnail, "max_side": thumbnail_width,
                                                   "renderer": "resvg_py", "max_png_bytes": 4 * 1024 * 1024}
                manifest["limits"] = {"max_sheets": max_sheets, "max_entities": max_entities,
                                      "max_svg_bytes": max_svg_bytes, "max_total_bytes": max_total_bytes}
                manifest["statistics"] = {
                    "timing_scope": "split_drawing call; original DWG normalization performed by an outer dispatcher is recorded separately in CLI resource metrics",
                    "input_read_seconds": round(parsed_at - started, 6),
                    "discovery_seconds": round(discovered_at - parsed_at, 6),
                    "bounds_seconds": round(bounded_at - discovered_at, 6),
                    "entity_bounds": bounds_statistics,
                    "sheets": [],
                }
                manifest["selection"] = {"coordinate_system": "source WCS",
                    "rule": "native entity geometry bbox intersects frame; crossing entities retained whole",
                    "unknown_bounds_count": len(unknown), "unknown_bounds_handles": unknown[:100],
                    "unknown_bounds_handles_complete": len(unknown) <= 100,
                    "unknown_bounds_policy": "not assigned; explicitly reported",
                    "bounds_statistics": bounds_statistics}
                assigned_handles = set()
                total_bytes, duplicated = 0, defaultdict(int)
                for sheet in sheets:
                    sheet_started = time.perf_counter()
                    box = sheet["bbox"]
                    chosen = [(e, extent) for e, extent in bounded if _intersects(box, extent)]
                    if len(chosen) > max_entities:
                        raise CadError("SHEET_TOO_LARGE", "A sheet exceeds the selected native entity budget.",
                                       {"sheet": sheet["id"], "entity_count": len(chosen), "max_entities": max_entities})
                    handles = {e.dxf.handle for e, _ in chosen}
                    assigned_handles.update(handles)
                    for handle in handles:
                        duplicated[handle] += 1
                    crossing = [e.dxf.handle for e, extent in chosen if not _contains(box, extent, 1e-5)]
                    sheet["entity_count"] = len(chosen)
                    sheet["source_handles"] = sorted(handles)
                    sheet["crossing_entity_count"] = len(crossing)
                    sheet["crossing_handles"] = crossing[:100]
                    sheet["crossing_handles_complete"] = len(crossing) <= 100
                    if crossing:
                        sheet["warnings"].append("Entities crossing the frame are retained whole in DXF and clipped only in SVG.")
                    exported, image_preservation = _export_entities(document, selected, [e for e, _ in chosen])
                    sheet["image_preservation"] = image_preservation
                    sheet["dxf"] = sheet["name"] + ".dxf"
                    dxf_output = stage / sheet["dxf"]
                    exported.saveas(dxf_output)
                    export_finished = time.perf_counter()
                    sheet["dxf_bytes"] = dxf_output.stat().st_size
                    sheet["dxf_sha256"] = file_sha256(dxf_output)
                    total_bytes += sheet["dxf_bytes"]
                    sheet["svg"] = None
                    if preview:
                        from .render import render_document
                        sheet["svg"] = sheet["name"] + ".svg"
                        try:
                            rendered = render_document(document, stage / sheet["svg"], layout=layout,
                                                       entities=[e for e, _ in chosen], bbox=box,
                                                       max_entities=max_entities, max_svg_bytes=max_svg_bytes,
                                                       **(render_options or {}))
                        except CadError as exc:
                            counts = defaultdict(int)
                            for entity, _ in chosen:
                                counts[entity.dxftype()] += 1
                            details = dict(exc.details or {})
                            details.update({"sheet_id": sheet["id"], "sheet_name": sheet["name"],
                                            "sheet_bbox": box, "sheet_entity_count": len(chosen),
                                            "sheet_entity_types": dict(counts), "sheet_index": sheets.index(sheet) + 1,
                                            "total_sheets": len(sheets)})
                            raise CadError(exc.code, exc.message, details) from exc
                        sheet["preview"] = rendered
                        # Temporary absolute paths are implementation details,
                        # never durable references in the published manifest.
                        sheet["preview"]["output"] = sheet["svg"]
                        sheet["svg_bytes"] = (stage / sheet["svg"]).stat().st_size
                        sheet["svg_sha256"] = file_sha256(stage / sheet["svg"])
                        total_bytes += sheet["svg_bytes"]
                        if thumbnail:
                            from .thumbnails import create_thumbnail
                            png_name = sheet["name"] + ".png"
                            try:
                                receipt = create_thumbnail(stage / sheet["svg"], stage / png_name,
                                                           max_side=thumbnail_width)
                            except CadError as exc:
                                raise CadError(exc.code, exc.message, {**(exc.details or {}),
                                    "sheet_id": sheet["id"], "sheet_name": sheet["name"], "stage": "png_thumbnail"}) from exc
                            receipt["output"] = png_name
                            sheet["thumbnail"] = receipt
                            total_bytes += receipt["bytes"]
                    sheet_finished = time.perf_counter()
                    timing = {"sheet_id": sheet["id"],
                              "export_seconds": round(export_finished - sheet_started, 6),
                              "render_seconds": round(sheet_finished - export_finished, 6) if preview else 0.0,
                              "total_seconds": round(sheet_finished - sheet_started, 6)}
                    sheet["timing"] = timing
                    manifest["statistics"]["sheets"].append(timing)
                    if total_bytes > max_total_bytes:
                        raise CadError("SPLIT_OUTPUT_TOO_LARGE", "Split artifacts exceed their aggregate byte budget.",
                                       {"bytes": total_bytes, "max_total_bytes": max_total_bytes})
                unassigned = [e.dxf.handle for e in native if e.dxf.handle not in assigned_handles]
                manifest["summary"].update({"assigned_native_entities": len(assigned_handles),
                    "unassigned_entity_count": len(unassigned), "unassigned_handles": unassigned[:100],
                    "unassigned_handles_complete": len(unassigned) <= 100,
                    "duplicated_boundary_entities": sum(count > 1 for count in duplicated.values()),
                    "output_entity_count": sum(s["entity_count"] for s in sheets),
                    "preview_count": len(sheets) if preview else 0,
                    "thumbnail_count": sum(bool(s.get("thumbnail")) for s in sheets), "artifact_bytes": total_bytes})
                remainder_artifact = preserve_unassigned_artifact(document, manifest, stage)
                total_bytes += remainder_artifact["bytes"] + remainder_artifact["json_bytes"]
                manifest["summary"]["artifact_bytes"] = total_bytes
                if unknown:
                    manifest["warnings"].append("Some native entities have no computable bounds and were not assigned; inspect selection diagnostics.")
                if bounds_statistics["partial_bounds_count"]:
                    manifest["warnings"].append("Some INSERT bounds omit unsupported children; inspect partial_bounds_handles and render diagnostics.")
                if source.suffix.lower() == ".dwg":
                    manifest["warnings"].append("DWG conversion may lose unsupported properties or objects; split completeness is relative to the converted DXF.")
                manifest["report"] = "index.html"
                manifest["statistics"]["total_prepare_seconds"] = round(time.perf_counter() - started, 6)
                manifest["statistics"]["total_export_seconds"] = round(sum(t["export_seconds"] for t in manifest["statistics"]["sheets"]), 6)
                manifest["statistics"]["total_render_seconds"] = round(sum(t["render_seconds"] for t in manifest["statistics"]["sheets"]), 6)
                verification = verify_split_artifacts(source, stage, manifest=manifest,
                                                       source_document=document, source_sha256=source_hash)
                if not verification["ok"]:
                    raise CadError("SPLIT_VERIFICATION_FAILED", "Split artifacts failed actual readback verification.",
                                   {"failures": verification["failures"][:4]})
                (stage / "manifest.json").write_text(json.dumps(jsonable(manifest), ensure_ascii=False, indent=2), encoding="utf-8")
                from .sheet_report import write_sheet_report
                write_sheet_report(manifest, stage / "index.html")
                final_bytes = sum(p.stat().st_size for p in stage.iterdir() if p.is_file())
                if final_bytes > max_total_bytes:
                    raise CadError("SPLIT_OUTPUT_TOO_LARGE", "Split artifacts including manifest/HTML exceed their aggregate byte budget.",
                                   {"bytes": final_bytes, "max_total_bytes": max_total_bytes})
                if file_sha256(source) != source_hash:
                    raise CadError("REVISION_CONFLICT", "The source drawing changed during sheet splitting.")
                if target.exists():
                    raise CadError("OUTPUT_EXISTS", "Split destination appeared during export.")
                os.rename(stage, target)
            return {"source": manifest["source"], "output_dir": str(target),
                    "manifest": str(target / "manifest.json"), "report": str(target / "index.html"),
                    "summary": manifest["summary"], "bytes": final_bytes,
                    "examples": [{k: sheet[k] for k in ("id", "name", "title", "number", "entity_count", "dxf", "svg")}
                                 for sheet in sheets[:3]], "warnings": manifest["warnings"]}
        finally:
            if stage.exists():
                shutil.rmtree(stage)
