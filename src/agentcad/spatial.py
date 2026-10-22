# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Shared conservative spatial selection and explicit block instance traversal.

Definition handles identify a definition entity, never its drawing instance.
``entity_key`` combines those handles with the ordered INSERT instance path.
"""
from __future__ import annotations

import math

from .errors import CadError


def validated_bbox(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)
            or value[0] > value[2] or value[1] > value[3]):
        raise CadError("INVALID_ARGUMENT", "bbox must contain four finite ordered coordinates.")
    return [float(v) for v in value]


def intersects(a, b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def _layer_allowed(layer, layers, exclude_layers):
    return (layers is None or layer in layers) and (exclude_layers is None or layer not in exclude_layers)


def entity_visible(entity, layer=None):
    """Cheap DXF visibility, including effective layer and ATTRIB flags."""
    from ezdxf.lldxf.const import DXFTableEntryError
    if bool(entity.dxf.get("invisible", 0)):
        return False
    if entity.dxftype() in {"ATTRIB", "ATTDEF"} and entity.dxf.get("flags", 0) & 1:
        return False
    layer_name = layer if layer is not None else entity.dxf.get("layer", "0")
    try:
        entry = entity.doc.layers.get(layer_name)
        return not entry.is_off() and not entry.is_frozen()
    except (AttributeError, KeyError, DXFTableEntryError):
        return True


def entity_bounds(entities):
    """Compute every native bound once, reusing definition-local block bounds.

    Transforming a cached definition AABB can enlarge a rotated/curved block's
    bound. It is conservative: no geometry is excluded because of this shortcut.
    Attached attributes are added separately in their containing coordinates.
    """
    from ezdxf import bbox
    from ezdxf.math import BoundingBox
    entities = list(entities)
    cache = bbox.Cache()
    definitions, definition_issues, active = {}, {}, set()
    result, missing, partial = [], [], []
    statistics = {"definition_cache_hits": 0, "definition_cache_entries": 0,
                  "method": "cached conservative definition AABB transformed to WCS; native non-INSERT bounds computed once"}

    def box_for(entity):
        if entity.dxftype() != "INSERT":
            extent = bbox.extents([entity], fast=True, cache=cache)
            return extent, extent.has_data
        name = entity.dxf.name
        if name in active or len(active) >= 32:
            return BoundingBox(), False
        if name in definitions:
            local, complete = definitions[name]
            statistics["definition_cache_hits"] += 1
        else:
            block = entity.doc.blocks.get(name)
            if block is None or block.block.is_xref:
                definitions[name] = (BoundingBox(), False)
                definition_issues[name] = [{"block_name": name, "reason": "missing_or_external_definition"}]
            else:
                active.add(name)
                local, complete = BoundingBox(), True
                issues = []
                try:
                    for child in block:
                        if child.dxftype() == "ATTDEF":
                            continue
                        child_box, child_complete = box_for(child)
                        complete = complete and child_complete
                        if not child_complete and len(issues) < 4:
                            issues.append({"handle": child.dxf.handle, "type": child.dxftype(),
                                           "reason": "no_computable_bounds" if not child_box.has_data else "incomplete_child_bounds",
                                           **({"block_name": child.dxf.name} if child.dxftype() == "INSERT" else {})})
                        if child_box.has_data:
                            local.extend([child_box.extmin, child_box.extmax])
                finally:
                    active.remove(name)
                definitions[name] = (local, complete)
                definition_issues[name] = issues
            local, complete = definitions[name]
        extent = BoundingBox()
        instances = entity.multi_insert() if entity.mcount > 1 else [entity]
        for instance in instances:
            if local.has_data:
                extent.extend(instance.matrix44().transform_vertices(local.cube_vertices()))
            for attribute in instance.attribs:
                attribute_box, attribute_complete = box_for(attribute)
                complete = complete and attribute_complete
                if attribute_box.has_data:
                    extent.extend([attribute_box.extmin, attribute_box.extmax])
        return extent, complete and extent.has_data

    for entity in entities:
        try:
            extent, complete = box_for(entity)
            if not extent.has_data:
                missing.append(entity.dxf.handle)
                continue
            if not complete:
                partial.append(entity.dxf.handle)
            lo, hi = extent.extmin, extent.extmax
            result.append((entity, [lo.x, lo.y, hi.x, hi.y]))
        except (ValueError, TypeError, AttributeError, ArithmeticError):
            missing.append(entity.dxf.handle)
    by_handle = {entity.dxf.handle: entity for entity in entities}

    def diagnostic(handle):
        entity = by_handle[handle]
        return {"handle": handle, "type": entity.dxftype(), "layer": entity.dxf.get("layer", "0"),
                **({"block_name": entity.dxf.name,
                    "definition_issues": definition_issues.get(entity.dxf.name, [])}
                   if entity.dxftype() == "INSERT" else {})}

    statistics.update({"definition_cache_entries": len(definitions),
                       "partial_bounds_count": len(partial), "partial_bounds_handles": partial[:100],
                       "partial_bounds_handles_complete": len(partial) <= 100,
                       "unknown_bounds_samples": [diagnostic(h) for h in missing[:4]],
                       "partial_bounds_samples": [diagnostic(h) for h in partial[:4]],
                       "diagnostic_sample_limit": 4})
    return result, missing, statistics


def iter_world_entities(document, layout="Model", roots=None, layers=None, exclude_layers=None,
                        bbox=None, max_entities=250000, max_depth=16, diagnostics=None, types=None):
    """Yield independent leaf instances without changing source entities.

    ``entity`` is a transformed copy; DXF OCS fields retain their usual meaning.
    ``world_insert`` and ``bounds`` are drawing WCS. Layer 0 inherits its INSERT
    layer. Attached attributes use the *parent* matrix, since their positions
    already include their owning INSERT transformation. Nonuniform circles are
    converted to ellipses by ezdxf, never assigned an incorrect circular radius.
    ``diagnostics`` receives every omission count and at most 32 evidence rows.
    """
    from ezdxf.transform import copies

    if type(max_entities) is not int or max_entities < 1 or type(max_depth) is not int or max_depth < 0:
        raise CadError("INVALID_ARGUMENT", "Traversal budgets must be nonnegative, with max_entities positive.")
    if bbox is not None:
        bbox = validated_bbox(bbox)
    report = diagnostics if diagnostics is not None else {}
    report.setdefault("omitted_count", 0)
    report.setdefault("omissions", [])
    report.setdefault("visited_count", 0)
    report.setdefault("returned_count", 0)
    report.setdefault("leaf_visit_count", 0)
    report.setdefault("complete", True)
    layers = None if layers is None else set(layers)
    exclude_layers = None if exclude_layers is None else set(exclude_layers)
    types = None if types is None else set(types)
    definition_type_cache = {}

    def definition_matches(name, stack=()):
        if types is None:
            return True
        if name in definition_type_cache:
            return definition_type_cache[name]
        if name in stack or len(stack) >= max_depth:
            return True  # Keep cycles/depth violations visible to diagnostics.
        block = document.blocks.get(name)
        if block is None or block.block.is_xref:
            return True
        for child in block:
            kind = child.dxftype()
            if kind != "INSERT" and kind in types and kind != "ATTDEF":
                definition_type_cache[name] = True
                return True
            if kind == "INSERT" and (any(a.dxftype() in types for a in child.attribs)
                                      or definition_matches(child.dxf.name, (*stack, name))):
                definition_type_cache[name] = True
                return True
        definition_type_cache[name] = False
        return False
    space = document.layouts.get(layout)
    selected = list(space) if roots is None else list(roots)
    if selected and isinstance(selected[0], str):
        native = {e.dxf.handle: e for e in space}
        absent = [handle for handle in selected if handle not in native]
        if absent:
            raise CadError("ENTITY_NOT_FOUND", "Traversal roots must be native layout handles.",
                           {"handles": absent[:32]})
        selected = [native[handle] for handle in selected]

    def omit(entity, path, reason):
        report["omitted_count"] += 1
        report["complete"] = False
        if len(report["omissions"]) < 32:
            report["omissions"].append({"handle": entity.dxf.handle,
                                        "instance_path": list(path), "reason": reason})
        report["omissions_complete"] = report["omitted_count"] == len(report["omissions"])

    def leaf(entity, matrix, path, root, inherited, parent_visible=True, source_handle=None):
        report["leaf_visit_count"] += 1
        if report["leaf_visit_count"] > max_entities:
            raise CadError("SPATIAL_TRAVERSAL_LIMIT", "Block leaves exceed their entity budget.",
                           {"max_entities": max_entities, "leaf_visit_count": report["leaf_visit_count"]})
        if types is not None and entity.dxftype() not in types:
            return
        own_layer = entity.dxf.get("layer", "0")
        layer = inherited if own_layer == "0" and inherited is not None else own_layer
        if not _layer_allowed(layer, layers, exclude_layers):
            return
        if matrix is None:
            result = entity.copy()
        else:
            try:
                log, transformed = copies([entity], matrix)
                if len(transformed) != 1 or len(log):
                    omit(entity, path, "unsupported_or_inexact_transform")
                    return
                result = transformed[0]
            except (TypeError, ValueError, ArithmeticError, AttributeError):
                omit(entity, path, "unsupported_transform")
                return
        result.dxf.layer = layer
        bounded, missing, statistics = entity_bounds([result])
        extent = bounded[0][1] if bounded else None
        if missing or statistics["partial_bounds_count"]:
            omit(entity, path, "no_computable_bounds" if missing else "incomplete_bounds")
        if bbox is not None and (extent is None or not intersects(extent, bbox)):
            return
        source_handle = source_handle or entity.dxf.handle
        item = {"entity": result, "handle": source_handle, "instance_path": list(path),
                "root_handle": root, "entity_key": "/".join([*path, source_handle]),
                "layer": layer, "coordinate_system": "WCS", "bounds": extent,
                "visible": parent_visible and entity_visible(entity, layer)}
        if entity.dxftype() in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            point = entity.dxf.insert
            if entity.dxftype() != "MTEXT":
                point = entity.ocs().to_wcs(point)
            if matrix is not None:
                point = matrix.transform(point)
            item["world_insert"] = list(point)
        report["returned_count"] += 1
        yield item

    def visit(entity, matrix=None, path=(), root=None, inherited=None, stack=(), parent_visible=True):
        if types is not None:
            if entity.dxftype() != "INSERT" and entity.dxftype() not in types:
                return
            if (entity.dxftype() == "INSERT" and not any(a.dxftype() in types for a in entity.attribs)
                    and not definition_matches(entity.dxf.name)):
                return
        report["visited_count"] += 1
        if report["visited_count"] > max_entities:
            raise CadError("SPATIAL_TRAVERSAL_LIMIT", "Block traversal exceeds its entity budget.",
                           {"max_entities": max_entities, "visited_count": report["visited_count"]})
        root = root or entity.dxf.handle
        if entity.dxftype() != "INSERT":
            if entity.dxftype() != "ATTDEF" or not path:
                yield from leaf(entity, matrix, path, root, inherited, parent_visible)
            return
        name = entity.dxf.name
        if len(stack) >= max_depth or name in stack:
            omit(entity, path, "depth_limit" if len(stack) >= max_depth else "cyclic_block")
            return
        block = document.blocks.get(name)
        if block is None or block.block.is_xref:
            omit(entity, path, "missing_or_external_definition")
            return
        own_layer = entity.dxf.get("layer", "0")
        effective = inherited if own_layer == "0" and inherited is not None else own_layer
        visible = parent_visible and entity_visible(entity, effective)
        for cell, instance in enumerate(entity.multi_insert() if entity.mcount > 1 else [entity]):
            token = entity.dxf.handle + (f"[{cell}]" if entity.mcount > 1 else "")
            child_path = (*path, token)
            for attribute_index, attribute in enumerate(instance.attribs):
                source_handle = entity.attribs[attribute_index].dxf.handle
                yield from leaf(attribute, matrix, child_path, root, effective, visible, source_handle)
            child_matrix = instance.matrix44()
            if matrix is not None:
                child_matrix = child_matrix @ matrix
            if definition_matches(name):
                for child in block:
                    yield from visit(child, child_matrix, child_path, root, effective, (*stack, name), visible)

    # Keep this import local: a warm SQLite selection needs no CAD library.
    if bbox is not None:
        bounded_roots, unknown_roots, root_statistics = entity_bounds(selected)
        by_handle = {entity.dxf.handle: entity for entity in selected}
        for handle in unknown_roots:
            omit(by_handle[handle], (), "native_root_bounds_unavailable")
        report["root_selection"] = {
            "rule": "conservative native WCS bounds intersect bbox before block traversal",
            "candidate_count": len(selected), "unknown_count": len(unknown_roots),
            "partial_bounds_count": root_statistics["partial_bounds_count"],
        }
        selected = [entity for entity, extent in bounded_roots if intersects(extent, bbox)]
        report["root_selection"]["selected_count"] = len(selected)
    for entity in selected:
        yield from visit(entity)


def select_handles(path, layout="Model", types=None, layer=None, layers=None, exclude_layers=None,
                   text=None, handles=None, bbox=None, text_scope="layout", expected_source_revision=None):
    """Persistent R-tree selection used by every native read command.

    Block annotation scopes return ``entities`` with instance keys in addition
    to native ``handles``; callers must not treat definition handles as native.
    """
    from .query_cache import select_indexed
    from .query import _validate_handles
    if not isinstance(layout, str) or not layout:
        raise CadError("INVALID_ARGUMENT", "layout must be a nonempty name.")
    if types is not None:
        if not isinstance(types, list) or any(not isinstance(kind, str) or not kind for kind in types):
            raise CadError("INVALID_ARGUMENT", "types must be a list of entity names.")
        types = sorted({kind.upper() for kind in types})
    if layer is not None and not isinstance(layer, str):
        raise CadError("INVALID_ARGUMENT", "layer must be a string.")
    for value in (layers, exclude_layers):
        if value is not None and (not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value)):
            raise CadError("INVALID_ARGUMENT", "layers must be lists of nonempty names.")
    if text is not None and not isinstance(text, str):
        raise CadError("INVALID_ARGUMENT", "text must be a string.")
    if handles is not None:
        handles = _validate_handles(handles, allow_empty=True)
    if expected_source_revision is not None and not isinstance(expected_source_revision, str):
        raise CadError("INVALID_ARGUMENT", "expected_source_revision must be a string.")
    return select_indexed(path, layout=layout, types=types, layer=layer, layers=layers,
                          exclude_layers=exclude_layers, text=text, handles=handles, bbox=bbox,
                          text_scope=text_scope, expected_source_revision=expected_source_revision)


def index_drawing(path, layout="Model", text_scope="all"):
    from .query_cache import index_source
    return index_source(path, layout=layout, text_scope=text_scope)
