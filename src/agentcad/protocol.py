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
    "measure": object_schema({**FILE_ARGS, "handles": HANDLES}, ["input", "handles"]),
    "export": object_schema({**FILE_ARGS, "output": STRING, "overwrite": BOOL,
                             "types": {"type": "array", "items": STRING},
                             "layer": STRING, "handles": HANDLES,
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
    "render": object_schema({**FILE_ARGS, "output": STRING, "overwrite": BOOL,
                             "types": {"type": "array", "items": STRING}, "layer": STRING,
                             "handles": HANDLES,
                             "max_entities": {"type": "integer", "minimum": 1, "maximum": 100000},
                             "max_svg_bytes": {"type": "integer", "minimum": 1024,
                                               "maximum": 64 * 1024 * 1024}}, ["input", "output"]),
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
OUTPUT_ARGUMENTS = {
    "detail": {"enum": ["summary", "full"], "default": "summary"},
    "fields": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 128},
               "minItems": 1, "maxItems": 64},
    "no_provenance": BOOL,
    "max_output_bytes": {"type": "integer", "minimum": 4096, "maximum": MAX_OUTPUT_BYTES},
    "result_file": STRING,
    "overwrite_result_file": BOOL,
    "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 600},
    "max_memory_mb": {"type": "integer", "minimum": 64, "maximum": 16384},
}
for _command_schema in COMMAND_SCHEMAS.values():
    _command_schema["properties"].update(OUTPUT_ARGUMENTS)
REQUEST_ID = {"type": ["string", "integer", "null"], "maxLength": 256,
              "minimum": -(2 ** 53 - 1), "maximum": 2 ** 53 - 1}
REQUEST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "TCAD agent request",
    "oneOf": [
        object_schema(
            {
                "id": REQUEST_ID,
                "command": {"const": name},
                "args": schema,
            },
            ["command", "args"],
        )
        for name, schema in COMMAND_SCHEMAS.items()
    ],
}


def capabilities():
    from .converters import converter_status

    return {
        "name": "tcad-agent",
        "version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "commands": list(COMMAND_SCHEMAS),
        "transport": ["cli", "jsonl-stdio", "ordered-batch"],
        "formats": {
            "read": ["dxf", "dwg (external converter required)"],
            "write": ["dxf"],
            "preview": ["svg"],
        },
        "editing": ["layer", "add", "update", "delete", "transform"],
        "room_model": {
            "formats": ["glb", "obj", "dxf"],
            "source": "explicit mapped plan and elevation geometry",
            "verification": "reopened wall faces, floor, ceiling, and opening voids",
            "scope": "axis-aligned rectangular rooms with explicit wall and slab thicknesses",
        },
        "transactions": "One apply plan is atomic; batches are ordered and not atomic.",
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
            for name in ("tcad-agent", "ezdxf", "jsonschema", "Pillow", "trimesh")
        },
        "converters": converter_status(),
        "dxf_backend": "ready",
        "svg_backend": "ready",
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
    return _drawing_dispatch(command, args, input_path, conversion_info)


def _drawing_dispatch(command, args, input_path, conversion_info):
    from .query import query_entities
    if command != "query":
        from . import engine
    from .converters import readable_dxf
    from .render import render_drawing
    with readable_dxf(input_path, conversion_info=conversion_info) as path:
        options = {key: value for key, value in args.items() if key != "input"}
        if command == "info":
            result = engine.inspect_drawing(path, **options)
        elif command == "query":
            result = query_entities(path, **options)
        elif command == "measure":
            result = engine.measure_entities(path, **options)
        elif command == "export":
            result = engine.export_entities(path, options.pop("output"), **options)
        elif command == "render":
            result = render_drawing(path, options.pop("output"), **options)
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
            for key in ("input", "output", "model", "source"):
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
        with redirect_stdout(sys.stderr):
            result = dispatch(command, args.copy())
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
