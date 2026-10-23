# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Shared command contract for CLI, ordered batches and JSON Lines services."""

import importlib.metadata
import logging
import math
from itertools import islice
import platform
import sys
from contextlib import redirect_stdout
from pathlib import Path

from jsonschema import Draft202012Validator

from . import __version__
from .errors import CadError
from .io import jsonable
from .output import DEFAULT_OUTPUT_BYTES, MAX_OUTPUT_BYTES, bounded_failure, finalize_response

from .transport import MAX_JSON_BYTES as MAX_JSON_BYTES, loads_json as loads_json, _finite_numbers

PROTOCOL_VERSION = "1.0"


def object_schema(properties=None, required=()):
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }


STRING = {"type": "string", "minLength": 1}
BOOL = {"type": "boolean"}
HANDLES = {"type": "array", "items": STRING, "minItems": 1, "maxItems": 10000}
LAYOUT = {"type": "string", "minLength": 1, "default": "Model"}
FILE_ARGS = {"input": STRING, "layout": LAYOUT}
CONVERTER = {"type": "string", "enum": ["auto", "libredwg", "oda"], "default": "auto"}
BBOX = {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}
STRINGS = {"type": "array", "items": STRING, "minItems": 1, "maxItems": 256}
SELECT = {"types": STRINGS, "layer": STRING, "layers": STRINGS, "exclude_layers": STRINGS,
          "handles": HANDLES, "bbox": BBOX, "sheet": STRING, "manifest": STRING,
          "expected_source_revision": STRING}
RENDER_OPTIONS = {"background": {"enum": ["white", "black", "layout"]},
                  "foreground": {"enum": ["cad", "monochrome"]},
                  "hatch_mode": {"enum": ["adaptive", "accurate"]},
                  "lineweight_scale": {"type": "number", "minimum": .05, "maximum": 10},
                  "font_family": STRING, "layers": STRINGS, "exclude_layers": STRINGS,
                  "resource_paths": STRINGS, "font_paths": STRINGS,
                  "font_map": {"type": "object", "additionalProperties": STRING},
                  "max_resource_bytes": {"type": "integer", "minimum": 1024, "maximum": 268435456},
                  "strict": BOOL}
SHEET_OPTIONS = {
    **FILE_ARGS,
    "frames": {"type": "array", "minItems": 1, "maxItems": 200, "items": {
        "oneOf": [STRING, object_schema({"bbox": BBOX, "name": STRING, "number": STRING}, ["bbox"])]}},
    "frame_layer": STRING,
    "min_frame_area": {"type": "number", "exclusiveMinimum": 0},
    "max_sheets": {"type": "integer", "minimum": 1, "maximum": 200},
    "frame_block_regex": STRING, "frame_size": {"oneOf": [STRING, {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0}, "minItems": 2, "maxItems": 2}]},
    "title_layer": STRING, "title_regex": STRING, "number_layer": STRING,
    "shared_frame": {"enum": ["largest", "split"]}, "names": {"type": "object"},
}
COMMAND_SCHEMAS = {
    "capabilities": object_schema(),
    "doctor": object_schema(),
    "schema": object_schema(
        {"target": {"enum": ["all", "request", "plan", "room-map", "room-spec"]}}
    ),
    "info": object_schema({**FILE_ARGS, "include_bounds": BOOL}, ["input"]),
    "query": object_schema(
        {
            **FILE_ARGS,
            "types": {"type": "array", "items": STRING},
            "layer": STRING,
            "text": {"type": "string"},
            "handles": HANDLES,
            "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
            "cursor": STRING,
            "vertex_offset": {"type": "integer", "minimum": 0},
            "vertex_limit": {"type": "integer", "minimum": 1, "maximum": 10000},
        },
        ["input"],
    ),
    "measure": object_schema({**FILE_ARGS, **SELECT,
        "max_entities": {"type": "integer", "minimum": 1, "maximum": 100000}}, ["input"]),
    "export": object_schema({**FILE_ARGS, "output": STRING, "overwrite": BOOL,
                             "types": {"type": "array", "items": STRING},
                             **SELECT,
                             "max_entities": {"type": "integer", "minimum": 1, "maximum": 1000000},
                             "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1073741824}},
                            ["input", "output"]),
    "new": object_schema(
        {
            "output": STRING,
            "units": {"enum": ["mm", "cm", "m", "in", "ft", "unitless"]},
            "overwrite": BOOL,
            "dry_run": BOOL,
        },
        ["output"],
    ),
    "apply": object_schema(
        {
            "input": STRING,
            "output": STRING,
            "plan": {"type": "object"},
            "overwrite": BOOL,
            "dry_run": BOOL,
            "expected_source_sha256": {"type": "string", "pattern": "^[a-fA-F0-9]{64}$"},
            "expected_output_sha256": {"type": "string", "pattern": "^([a-fA-F0-9]{64}|missing)$"},
        },
        ["output", "plan"],
    ),
    "render": object_schema({**FILE_ARGS, "output": STRING, "output_dir": STRING, "overwrite": BOOL,
                             "sheet": STRING, "manifest": STRING, **RENDER_OPTIONS,
                             "annotate": {"enum": ["labels", "areas", "axes"]}, "legend": BOOL,
                             "max_annotations": {"type": "integer", "minimum": 1, "maximum": 2000},
                             "width": {"type": "integer", "minimum": 16, "maximum": 4096},
                             "types": {"type": "array", "items": STRING}, "layer": STRING,
                             "handles": HANDLES,
                             "bbox": BBOX,
                             "background": {"enum": ["white", "black", "layout"]},
                             "foreground": {"enum": ["cad", "monochrome"]},
                             "hatch_mode": {"enum": ["adaptive", "accurate"]},
                             "lineweight_scale": {"type": "number", "minimum": 0.05,
                                                  "maximum": 10},
                             "font_family": {"type": "string", "minLength": 1, "maxLength": 128},
                             "max_entities": {"type": "integer", "minimum": 1, "maximum": 100000},
                             "max_svg_bytes": {"type": "integer", "minimum": 1024,
                                               "maximum": 64 * 1024 * 1024}}, ["input"]),
    "sheets": object_schema({**SHEET_OPTIONS, "output": STRING, "overwrite": BOOL}, ["input"]),
    "split": object_schema({**SHEET_OPTIONS, "output_dir": STRING, "preview": BOOL,
                            "render_options": object_schema(RENDER_OPTIONS),
                            "thumbnail": BOOL, "thumbnail_width": {"type": "integer", "minimum": 16, "maximum": 4096},
                            "max_entities": {"type": "integer", "minimum": 1, "maximum": 100000},
                            "max_svg_bytes": {"type": "integer", "minimum": 1024,
                                              "maximum": 64 * 1024 * 1024},
                            "max_total_bytes": {"type": "integer", "minimum": 1024,
                                                "maximum": 2 * 1024 * 1024 * 1024}},
                           ["input", "output_dir"]),
    "convert": object_schema(
        {
            "input": STRING,
            "output": STRING,
            "backend": CONVERTER,
            "overwrite": BOOL,
            "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 600},
        },
        ["input", "output"],
    ),
    "room-extract": object_schema(
        {"input": STRING, "mapping": {"type": "object"}, "output": STRING, "overwrite": BOOL},
        ["input", "mapping"],
    ),
    "room-model": object_schema(
        {"spec": {"type": "object"}, "output": STRING, "overwrite": BOOL}, ["spec", "output"]
    ),
    "room-verify": object_schema(
        {
            "spec": {"type": "object"},
            "model": STRING,
            "tolerance_mm": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
            "source": STRING,
            "mapping": {"type": "object"},
        },
        ["spec", "model"],
    ),
    "setup-dwg": object_schema({"data_dir": STRING}),
}
COMMAND_SCHEMAS["query"]["properties"].update(SELECT)
COMMAND_SCHEMAS["query"]["properties"]["text_scope"] = {"enum": ["layout", "block", "all"]}
COMMAND_SCHEMAS.update({
    "index": object_schema({**FILE_ARGS, "text_scope": {"enum": ["layout", "block", "all"]}}, ["input"]),
    "area": object_schema({**FILE_ARGS, "handles": HANDLES, "layers": STRINGS,
        "entity_types": STRINGS, "bbox": BBOX, "sheet": STRING, "manifest": STRING,
        "mode": {"enum": ["sum", "union", "overlap"]}, "by": {"enum": ["layer", "block", "sheet"]},
        "tolerance": {"type": "number", "minimum": 0},
        "flatten_tolerance": {"type": "number", "exclusiveMinimum": 0},
        "linear_unit": {"enum": ["mm", "cm", "m", "in", "ft"]},
        "make_valid": BOOL, "output": STRING, "overwrite": BOOL,
        "evidence_limit": {"type": "integer", "minimum": 1, "maximum": 10000},
        "max_relations": {"type": "integer", "minimum": 1, "maximum": 1000000}}, ["input"]),
    "associate": object_schema({**FILE_ARGS, "sheet": STRING, "manifest": STRING,
        "label_layers": STRINGS, "target_layers": STRINGS, "label_types": STRINGS,
        "target_types": STRINGS, "label_regex": STRING,
        "method": {"enum": ["inside", "nearest", "leader"]},
        "max_distance": {"type": "number", "exclusiveMinimum": 0}, "one_to_one": BOOL,
        "leader_layers": STRINGS, "ambiguity_tolerance": {"type": "number", "minimum": 0},
        "bbox": BBOX, "flatten_tolerance": {"type": "number", "exclusiveMinimum": 0},
        "output": STRING, "unmatched_output": STRING, "overwrite": BOOL}, ["input"]),
    "axes": object_schema({**FILE_ARGS, "sheet": STRING, "manifest": STRING,
        "line_layers": STRINGS, "label_layers": STRINGS, "label_regex": STRING,
        "bubble_block_regex": STRING, "tolerance": {"type": "number", "exclusiveMinimum": 0},
        "max_distance": {"type": "number", "exclusiveMinimum": 0}, "bbox": BBOX,
        "point": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
        "axes": STRING, "output": STRING, "overwrite": BOOL,
        "max_axes": {"type": "integer", "minimum": 1, "maximum": 1000},
        "max_intersections": {"type": "integer", "minimum": 1, "maximum": 100000}}, ["input"]),
})
COMMAND_SCHEMAS["workset"] = object_schema({
    **FILE_ARGS, "output_dir": STRING, "manifest": STRING, "sheets": STRINGS,
    "bboxes": {"type": "array", "items": BBOX, "minItems": 1, "maxItems": 200},
    "building": STRING, "kind": STRING, "title_regex": STRING,
    "handles": HANDLES, "layers": STRINGS, "types": STRINGS,
    "max_entities": {"type": "integer", "minimum": 1, "maximum": 1000000},
    "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1073741824},
}, ["input", "output_dir"])
WORKSET_COMMANDS = {"info", "query", "index", "measure", "export", "render", "area", "associate", "axes"}
for _name in WORKSET_COMMANDS:
    _schema = COMMAND_SCHEMAS[_name]
    _schema["properties"]["workset"] = STRING
    _schema["required"] = [key for key in _schema["required"] if key != "input"]
    _schema["anyOf"] = [{"required": ["input"]}, {"required": ["workset"]}]
OUTPUT_ARGUMENTS = {
    "detail": {"enum": ["summary", "full"], "default": "summary"},
    "fields": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 128},
               "minItems": 1, "maxItems": 64},
    "no_provenance": BOOL,
    "max_output_bytes": {"type": "integer", "minimum": 4096, "maximum": MAX_OUTPUT_BYTES},
    "result_file": STRING,
    "overwrite_result_file": BOOL,
    "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 1800},
    "cache_dir": STRING,
    "max_memory_mb": {"type": "integer", "minimum": 64, "maximum": 16384},
}
for _command_schema in COMMAND_SCHEMAS.values():
    _command_schema["properties"].update(OUTPUT_ARGUMENTS)
REQUEST_ID = {"type": ["string", "integer", "null"], "maxLength": 256,
              "minimum": -(2 ** 53 - 1), "maximum": 2 ** 53 - 1}


def _discovery_args_schema(command_schema):
    # Share presentation options instead of repeating them for every command.
    # unevaluatedProperties preserves the strict expanded runtime contract.
    local = {key: value for key, value in command_schema.items()
             if key not in {"properties", "additionalProperties"}}
    local["properties"] = {key: value for key, value in command_schema["properties"].items()
                           if key not in OUTPUT_ARGUMENTS}
    return {"allOf": [{"$ref": "#/$defs/output_arguments"}, local],
            "unevaluatedProperties": False}


REQUEST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "TCAD agent request",
    "$defs": {"output_arguments": {"type": "object", "properties": OUTPUT_ARGUMENTS}},
    "oneOf": [
        object_schema(
            {
                "id": REQUEST_ID,
                "command": {"const": name},
                "args": _discovery_args_schema(schema),
            },
            ["command", "args"],
        )
        for name, schema in COMMAND_SCHEMAS.items()
    ],
}


def capabilities():
    from .converters import converter_status
    from .query import _QUERY_FIELDS

    return {
        "name": "tcad-agent",
        "role": "CAD evidence and geometry primitives for agents; downstream quantity rules, spreadsheets and general modelling belong to the calling workflow.",
        "version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "commands": list(COMMAND_SCHEMAS) + ["batch", "serve"],
        "operation_commands": list(COMMAND_SCHEMAS),
        "transport_commands": {"batch": "CLI ordered requests; not atomic", "serve": "CLI JSONL stdio"},
        "transport": ["cli", "jsonl-stdio", "ordered-batch"],
        "formats": {
            "read": ["dxf", "dwg (external converter required)"],
            "write": ["dxf"],
            "preview": ["svg", "png"],
        },
        "editing": ["layer", "add", "update", "delete", "transform"],
        "selection_workflow": {
            "prepare": "tcad workset drawing.dxf --manifest sheets/manifest.json --building 1#楼 --title-regex 基础 --output-dir foundation",
            "reuse": "tcad measure --workset foundation --handle <handle-from-query-of-subset>",
            "commands": sorted(WORKSET_COMMANDS),
            "regions": "Repeated sheet selectors and bboxes are a union; native root filters intersect it. A sheet plus bbox on an analysis command refines that sheet.",
            "preservation": "Source WCS and units remain unchanged. Crossing native entities stay whole; bbox selection is not geometric clipping.",
            "freshness": "Workset and original source SHA256 are verified without parsing the original. Source/subset changes return STALE_WORKSET.",
            "performance": "Preparation decodes the source once; subsequent commands decode only the smaller subset. One read-only request reuses its first unchanged source document.",
            "identity": "Query the subset's handles; the original-to-exported mapping is stored in workset.json.",
        },
        "preview_quality": {
            "resources": "Local raster resources can be embedded using explicit resource_paths; no automatic network fetches.",
            "fonts": "Explicit font_paths and style/font file mapping supplement installed fonts; output uses glyph paths.",
            "report": "preview_quality distinguishes content/fidelity completeness and records missing resources or simplification.",
            "strict": "strict refuses publication when selected preview content/fidelity is incomplete.",
        },
        "sheet_workflow": {
            "single_sheet_layers": "tcad render sheets/column-plan.dxf --layers AXES AXIS_LABELS COLUMNS COLUMN_LABELS --output columns.svg",
            "single_sheet_frame": "An adjacent split manifest must identify the exported DXF and its SHA256 before its frame can be reused. Explicit bbox takes priority; a copied DXF without verified frame evidence uses ordinary drawing extents.",
            "discover": "tcad sheets drawing.dwg --output sheets.json",
            "split_preview_report": "tcad split drawing.dwg --output-dir sheets",
            "outputs": ["manifest.json", "index.html", "independent DXF per frame", "SVG per frame",
                        "__tcad_unassigned__.dxf and unassigned.json when entities do not belong to any frame"],
            "detection": "Axis-aligned rectangular geometry, nested block frames and title text rules; manual frames are supported.",
            "naming": "Frame-local title and drawing-number evidence, safe unique filenames.",
            "output_safety": "A new output directory is staged and published only after all requested sheets succeed; existing destinations are refused.",
            "limits": "At most 200 sheets; entity, per-SVG, total-file, worker time and memory budgets apply.",
            "boundary": "DXF retains intersecting native entities; SVG is clipped to the frame. See manifest for crossings and unknown extents.",
            "limitations": ["Detection and naming are geometric/text heuristics; inspect evidence and warnings.",
                            "Missing xrefs, linked images, proxy geometry and converter omissions cannot be reconstructed."],
        },
        "entity_inspection": {
            "query_export_fields": sorted(_QUERY_FIELDS),
            "field_discovery_command": "tcad capabilities --detail full --fields entity_inspection",
            "field_scope": "query/export fields select native entity data; unsupported fields for an entity are absent.",
            "dimension": {
                "measurement": "Computed definition geometry, without dimlfac scaling; use measure_kind to distinguish drawing-unit lengths, angular degrees and ordinate WCS vectors.",
                "actual_measurement": "Optional stored DXF group 42; may be absent or stale.",
                "text_override": "User dimension text override; rendered display text is not evaluated.",
                "dimlfac": "Effective dimension-style linear factor including entity overrides; does not establish physical drawing units.",
                "evidence": ["dimension_type", "dimension_type_raw", "defpoint", "defpoint2", "defpoint3", "defpoint4", "defpoint5", "dimstyle", "geometry_block"],
            },
            "attributes": {
                "native_types": ["ATTRIB", "ATTDEF"],
                "attached_insert_field": "attributes",
                "maximum_attached_attributes": 128,
                "maximum_attached_string_characters": 1024,
                "completeness_fields": ["attribute_count", "attribute_complete", "truncation"],
                "text_filter": "Indexes directly placed layout text/attributes; attached INSERT attributes are not automatically expanded.",
            },
            "blocks": {
                "trigger": "Explicitly include block_entities in fields; detail=full alone does not inspect children.",
                "maximum_children": 128,
                "maximum_child_vertices": 128,
                "coordinate_system": "Child handles and coordinates belong to the native block definition. Convert child OCS to definition Cartesian coordinates before transforming.",
                "matrix44_convention": "row_vector: [x,y,z,1] @ matrix44 maps definition Cartesian coordinates to drawing WCS, including block base point, scale, rotation, OCS and translation.",
                "completeness_fields": ["block_entity_count", "block_complete", "block_omissions", "block_omission_count", "block_omissions_complete", "truncation"],
                "limitations": ["First native level only; nested INSERTs and external references are not resolved.",
                                "MINSERT matrix44 describes the first grid cell only.",
                                "Child/attribute index pagination is unavailable; inspect omissions instead of assuming completeness."],
            },
            "text_encoding": {
                "metadata_field": "text_encoding",
                "representation": "Undecodable CAD text is exposed as literal backslash-u escapes with encoding_complete=false; normal Unicode is unchanged and source bytes are preserved.",
                "maximum_diagnostic_issues": 4,
                "evidence": "Field paths, character indices, original codepoints and surrogateescape byte escapes; issue_count/issue completeness identify bounded diagnostics.",
            },
            "export": {
                "fields_scope": "Selects entity fields inside JSONL only; the output receipt is not projected by entity field names.",
                "receipt_fields": ["output", "entity_count", "bytes", "sha256", "format", "layout", "warnings"],
                "annotation_caps_apply": True,
                "scope": "Supported native fields; explicit block inspection remains bounded and is not whole-block or lossless DXF export.",
            },
        },
        "room_model": {
            "role": "Controlled acceptance adapter, not a general building modelling or BIM capability.",
            "formats": ["glb", "obj", "dxf"],
            "source": "explicit mapped plan and elevation geometry",
            "verification": "reopened wall faces, floor, ceiling, and opening voids",
            "scope": "axis-aligned rectangular rooms with explicit wall and slab thicknesses",
        },
        "transactions": "One apply plan is atomic; batches are ordered and not atomic.",
        "spatial": {"index": "SQLite R-tree; native INSERT world bounds and instance-specific block text",
                    "selectors": ["bbox", "sheet with source-bound manifest", "workset", "layers", "exclude_layers"],
                    "text_scope": ["layout", "block", "all"],
                    "warm_scope": "Native base-field query can avoid parsing. Prepared worksets avoid parsing the original source; geometry commands still decode the selected subset."},
        "area": {"modes": ["sum", "union", "overlap"], "backend": "Shapely/GEOS",
                 "units": "drawing-unit squared; explicit linear_unit is required for m2 conversion",
                 "invalid": "reject by default; explicit make_valid records area changes",
                 "curves": "flatten_tolerance in drawing units; approximation is reported"},
        "association": {"methods": ["inside", "nearest", "leader"],
                        "ties": "ambiguous; no silent choice", "axes": "finite straight axis grids with source label evidence; inspect unsupported curve diagnostics"},
        "limits": {"default_response_bytes": DEFAULT_OUTPUT_BYTES,
                   "maximum_response_bytes": MAX_OUTPUT_BYTES,
                   "default_input_file_mib": 128, "default_result_file_mib": 64,
                   "default_timeout_seconds": 60, "default_memory_mib": 1024,
                   "supervision": "CLI/JSONL workers; direct Python API is not supervised",
                   "query": "revision-bound SQLite pages; native entities, no automatic INSERT expansion",
                   "output": "Explicit omissions; use fields, vertex pages or result_file for details"},
        "converters": converter_status(),
        "limitations": [
            "General editing is 2D; mapped rectangular-room mesh modelling is available; no constraint solver.",
            "DWG conversion may omit unsupported objects; inspect conversion warnings.",
            "SVG appearance depends on fonts, external references and supported entity types.",
            "Measurements use drawing units; no inference of physical scale.",
        ],
    }


def doctor():
    from .converters import converter_status

    return {
        "python": platform.python_version(),
        "executable": sys.executable,
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("tcad-agent", "ezdxf", "jsonschema", "Pillow", "trimesh", "shapely", "resvg_py")
        },
        "converters": converter_status(),
        "dxf_backend": "ready",
        "svg_backend": "ready",
        "png_backend": "resvg_py; static glyph-path SVG, no browser",
    }


def schema(target="all"):
    from .room import ROOM_MAPPING_SCHEMA, ROOM_SPEC_SCHEMA
    from .schema import PLAN_SCHEMA

    schemas = {
        "request": REQUEST_SCHEMA,
        "plan": PLAN_SCHEMA,
        "room-map": ROOM_MAPPING_SCHEMA,
        "room-spec": ROOM_SPEC_SCHEMA,
    }
    return schemas if target == "all" else schemas[target]


def dispatch(command, args):
    from .converters import convert_file

    presentation = {key: args.pop(key) for key in list(args) if key in OUTPUT_ARGUMENTS}
    if presentation.get("cache_dir"):
        import os
        os.environ["TCAD_CACHE_DIR"] = str(Path(presentation["cache_dir"]).expanduser().resolve())
    if command == "query":
        args.update({key: presentation[key] for key in ("fields", "detail", "max_output_bytes")
                     if key in presentation})
        args.setdefault("detail", "summary")
        args.setdefault("max_output_bytes", DEFAULT_OUTPUT_BYTES)
    if command == "export" and "fields" in presentation:
        args["fields"] = presentation["fields"]
    if command == "info":
        args.setdefault("include_bounds", False)
    if command == "setup-dwg":
        from .install import install_dwg
        return install_dwg(**args)

    if command == "capabilities":
        return capabilities()
    if command == "doctor":
        return doctor()
    if command == "schema":
        return schema(**args)
    if command == "convert":
        return convert_file(
            args["input"],
            args["output"],
            **{key: value for key, value in args.items() if key not in ("input", "output")},
        )
    if command in {"room-model", "room-verify"}:
        from .room_model import build_room_model, verify_room_model, verify_room_model_against_source

        if command == "room-model":
            return build_room_model(
                args["spec"], args["output"], overwrite=args.get("overwrite", False)
            )
        if args.get("source") is not None or args.get("mapping") is not None:
            if not args.get("source") or not args.get("mapping"):
                raise CadError("INVALID_REQUEST", "Strict verification requires both source and mapping.")
            return verify_room_model_against_source(
                args["spec"], args["model"], args["source"], args["mapping"],
                tolerance_mm=args.get("tolerance_mm", 0.01))
        return verify_room_model(
            args["spec"], args["model"], tolerance_mm=args.get("tolerance_mm", 0.01)
        )
    if command == "new":
        from . import engine
        return engine.apply_plan(
            {"version": 1, "units": args.get("units", "mm"), "operations": []},
            args["output"],
            overwrite=args.get("overwrite", False),
            dry_run=args.get("dry_run", False),
        )
    workset_context = None
    if args.get("workset"):
        from .worksets import resolve_workset
        workset_input = args.pop("workset")
        subset, workset_context = resolve_workset(workset_input, input_path=args.get("input"))
        protected = {Path(subset).resolve(), Path(workset_input).expanduser().resolve()}
        if Path(workset_input).expanduser().is_dir():
            protected.add(Path(workset_input).expanduser().resolve() / "workset.json")
        for key in ("source_input", "normalized_input"):
            if workset_context.get(key):
                protected.add(Path(workset_context[key]).resolve())
        for key in ("output", "unmatched_output", "result_file"):
            value = args.get(key) or presentation.get(key)
            if value and Path(value).expanduser().resolve() in protected:
                raise CadError("OUTPUT_CONFLICT", "Output must not replace the active workset drawing or manifest.")
        args["input"] = str(subset)
        args.setdefault("layout", workset_context.get("layout", "Model"))
        selected_sheets = workset_context.pop("selected_sheets", [])
        regions = workset_context.pop("regions", [])
        workset_context.get("selection", {}).pop("preview_regions", None)
        if args.get("sheet"):
            if args.get("manifest"):
                raise CadError("INVALID_ARGUMENT", "Workset sheet selection uses its verified internal metadata; omit manifest.")
            if not selected_sheets:
                selected_sheets = [{"id": f"region-{i:03d}", "title": f"Explicit region {i}", "bbox": box}
                                   for i, box in enumerate(regions, 1)]
            if not selected_sheets:
                raise CadError("SHEET_NOT_FOUND", "This full-layout workset has no saved sheet or region selectors.")
            args["manifest"] = {"source": {"sha256": workset_context["artifact_sha256"]},
                                "layout": workset_context["layout"], "sheets": selected_sheets}
        elif command == "render" and args.get("bbox") is None and len(regions) == 1:
            args["bbox"] = regions[0]
    input_path = args.get("input")
    conversion_info = {}
    if command == "apply":
        from . import engine
        from .io import revision_guard
        expected_source = args.pop("expected_source_sha256", None)
        expected_output = args.pop("expected_output_sha256", None)
        with revision_guard(input_path, args["output"],
                            expected_source.lower() if expected_source else None,
                            expected_output.lower() if expected_output else None):
            if input_path is None:
                return engine.apply_plan(args["plan"], args["output"],
                                         overwrite=args.get("overwrite", False),
                                         dry_run=args.get("dry_run", False))
            return _drawing_dispatch(command, args, input_path, conversion_info)
    result = _drawing_dispatch(command, args, input_path, conversion_info)
    if workset_context is not None and isinstance(result, dict):
        result["workset"] = workset_context
    return result


def _drawing_dispatch(command, args, input_path, conversion_info):
    if command == "apply":
        return _drawing_dispatch_impl(command, args, input_path, conversion_info)
    from .io import document_read_scope
    with document_read_scope() as reads:
        result = _drawing_dispatch_impl(command, args, input_path, conversion_info)
        if isinstance(result, dict):
            result["document_io"] = {"document_loads": reads["document_loads"],
                                     "source_cache_hits": reads["source_cache_hits"],
                                     "scope": "one_request_first_source_only"}
        return result


def _drawing_dispatch_impl(command, args, input_path, conversion_info):
    from .query import query_entities
    if command != "query":
        from . import engine
    from .converters import readable_dxf
    from .render import render_drawing
    with readable_dxf(input_path, conversion_info=conversion_info) as path:
        options = {key: value for key, value in args.items() if key != "input"}
        from .io import file_sha256
        identity = {"path": str(Path(input_path).expanduser().resolve()), "sha256": file_sha256(input_path)}
        if conversion_info:
            identity.update(conversion=conversion_info, normalized_dxf_sha256=file_sha256(path))
        if command in {"render", "area", "associate", "axes", "sheets", "split", "workset"}:
            options["source_info"] = identity
        if options.get("sheet") is not None and command != "render":
            from .render_jobs import resolve_sheets
            from .io import read_document
            selected = resolve_sheets(None if options.get("manifest") else read_document(path), path, options.pop("sheet"),
                options.pop("manifest", None), options.get("layout", "Model"), identity)
            if len(selected) != 1:
                raise CadError("INVALID_ARGUMENT", "This analysis requires one sheet id or title.")
            from .spatial import validated_bbox
            sheet_box = validated_bbox(selected[0]["bbox"])
            if options.get("bbox") is not None:
                region = validated_bbox(options["bbox"])
                sheet_box = [max(sheet_box[0], region[0]), max(sheet_box[1], region[1]),
                             min(sheet_box[2], region[2]), min(sheet_box[3], region[3])]
                if sheet_box[0] > sheet_box[2] or sheet_box[1] > sheet_box[3]:
                    raise CadError("EMPTY_SELECTION", "bbox does not intersect the selected sheet.")
            options["bbox"] = sheet_box
            if command == "area":
                options["sheet_id"] = selected[0]["id"]
        elif command not in {"render", "workset"} and options.pop("manifest", None):
            raise CadError("INVALID_ARGUMENT", "manifest requires sheet.")
        if command == "info":
            result = engine.inspect_drawing(path, **options)
        elif command == "query":
            result = query_entities(path, **options)
        elif command == "measure":
            result = engine.measure_entities(path, **options)
        elif command == "export":
            result = engine.export_entities(path, options.pop("output"), **options)
        elif command == "render":
            result = render_drawing(path, options.pop("output", None), **options)
        elif command == "index":
            from .spatial import index_drawing
            result = index_drawing(path, **options)
        elif command == "area":
            from .area import area_entities
            result = area_entities(path, **options)
        elif command in {"associate", "axes"}:
            from .semantics import associate_entities, axes_entities
            result = (associate_entities if command == "associate" else axes_entities)(path, **options)
        elif command in {"sheets", "split"}:
            from .sheets import discover_drawing, split_drawing
            from .io import file_sha256

            identity = {"path": str(Path(input_path).expanduser().resolve()),
                        "sha256": file_sha256(input_path)}
            if conversion_info:
                identity["conversion"] = conversion_info
                identity["normalized_dxf_sha256"] = file_sha256(path)
            if command == "sheets":
                result = discover_drawing(path, **options)
            else:
                result = split_drawing(path, options.pop("output_dir"), **options)
        elif command == "workset":
            from .worksets import create_workset
            result = create_workset(path, options.pop("output_dir"), **options)
        elif command == "apply":
            result = engine.apply_plan(
                options.pop("plan"), options.pop("output"), input_path=path, **options
            )
        elif command == "room-extract":
            from .room import extract_room, source_identity

            identity = source_identity(input_path)
            identity["normalized_dxf_sha256"] = source_identity(path)["sha256"]
            if conversion_info:
                identity["conversion"] = conversion_info
            result = extract_room(path, options.pop("mapping"), source_info=identity, **options)
        else:
            raise CadError("UNKNOWN_COMMAND", f"Unknown command: {command}")
    if isinstance(result, dict):
        result["source"] = str(Path(input_path).expanduser().resolve())
        for key in ("path", "input"):
            if key in result:
                result[key] = result["source"]
        if conversion_info:
            if conversion_info.get("source_sha256"):
                result["source_sha256"] = conversion_info["source_sha256"]
            result["conversion"] = conversion_info
            warnings = result.setdefault("warnings", [])
            warnings.extend(
                item for item in conversion_info.get("warnings", []) if item not in warnings
            )
    return result


def failure(error, request_id=None):
    return bounded_failure(error, request_id)


def execute_request(request):
    request_id = request.get("id") if isinstance(request, dict) else None
    if isinstance(request_id, float) and math.isfinite(request_id) and request_id.is_integer():
        request_id = int(request_id)
    if request_id is not None and (isinstance(request_id, bool) or
            not isinstance(request_id, (str, int)) or
            (isinstance(request_id, str) and len(request_id) > 256) or
            (isinstance(request_id, int) and abs(request_id) > 2 ** 53 - 1)):
        request_id = None
    if isinstance(request_id, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in request_id):
        request_id = None
    original_args = request.get("args", {}) if isinstance(request, dict) else {}
    requested_budget = original_args.get("max_output_bytes", DEFAULT_OUTPUT_BYTES) if isinstance(original_args, dict) else DEFAULT_OUTPUT_BYTES
    if not isinstance(requested_budget, (int, float)) or isinstance(requested_budget, bool) or not 4096 <= requested_budget <= MAX_OUTPUT_BYTES:
        requested_budget = DEFAULT_OUTPUT_BYTES
    args, command = {"max_output_bytes": int(requested_budget)}, None
    try:
        if not isinstance(request, dict):
            raise CadError("INVALID_REQUEST", "Request must be an object.")
        command = request.get("command")
        if not isinstance(command, str) or command not in COMMAND_SCHEMAS:
            raise CadError(
                "UNKNOWN_COMMAND",
                "Unknown or missing command.",
                {"available": list(COMMAND_SCHEMAS)},
            )
        _finite_numbers(request)
        # Validate envelope separately for actionable errors instead of a giant oneOf message.
        envelope = object_schema(
            {
                "id": REQUEST_ID,
                "command": {"const": command},
                "args": COMMAND_SCHEMAS[command],
            },
            ["command", "args"],
        )
        errors = list(islice(Draft202012Validator(envelope).iter_errors(request), 10))
        if errors:
            raise CadError(
                "INVALID_REQUEST",
                "Request does not match the command schema.",
                {
                    "errors": [
                        {"path": list(error.absolute_path)[:12], "message": error.message[:512]}
                        for error in errors[:10]
                    ]
                },
            )
        args = request["args"].copy()
        for key, value in list(args.items()):
            if COMMAND_SCHEMAS[command]["properties"].get(key, {}).get("type") == "integer":
                args[key] = int(value)
        if args.get("result_file"):
            artifact = Path(args["result_file"]).expanduser().resolve()
            for key in ("input", "output", "output_dir", "model", "source", "manifest", "axes", "unmatched_output", "workset"):
                value = args.get(key)
                if isinstance(value, str) and Path(value).expanduser().resolve() == artifact:
                    raise CadError("OUTPUT_CONFLICT", "Result JSON must not replace a drawing or model.")
            spec_source = args.get("spec", {}).get("source", {}) if isinstance(args.get("spec"), dict) else {}
            if isinstance(spec_source, dict) and isinstance(spec_source.get("path"), str):
                if Path(spec_source["path"]).expanduser().resolve() == artifact:
                    raise CadError("OUTPUT_CONFLICT", "Result JSON must not replace the source drawing.")
            from .output import validate_result_destination
            validate_result_destination(artifact, args.get("overwrite_result_file", False))
        # Third-party diagnostics must not corrupt the JSON transport.
        import os
        previous_cache_dir = os.environ.get("TCAD_CACHE_DIR")
        try:
            with redirect_stdout(sys.stderr):
                result = dispatch(command, args.copy())
        finally:
            if previous_cache_dir is None:
                os.environ.pop("TCAD_CACHE_DIR", None)
            else:
                os.environ["TCAD_CACHE_DIR"] = previous_cache_dir
        response = {
            "schema_version": PROTOCOL_VERSION,
            "id": request_id,
            "ok": True,
            "result": jsonable(result),
        }
        return finalize_response(response, args, command)
    except CadError as exc:
        budget = args.get("max_output_bytes", DEFAULT_OUTPUT_BYTES)
        if type(budget) is not int or not 4096 <= budget <= MAX_OUTPUT_BYTES:
            budget = DEFAULT_OUTPUT_BYTES
        return bounded_failure(exc, request_id, budget)
    except OSError as exc:
        return failure(CadError("IO_ERROR", str(exc)), request_id)
    except Exception as exc:
        logging.getLogger(__name__).exception("Unexpected CAD operation failure")
        return failure(
            CadError(
                "INTERNAL_ERROR",
                "Unexpected operation failure.",
                {"type": type(exc).__name__, "reason": str(exc)},
            ),
            request_id,
        )
