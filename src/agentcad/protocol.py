# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Shared command contract for CLI, ordered batches and JSON Lines services."""

import importlib.metadata
import json
import logging
import platform
import sys
from contextlib import redirect_stdout
from pathlib import Path

from jsonschema import Draft202012Validator

from . import __version__
from .errors import CadError
from .io import jsonable

PROTOCOL_VERSION = "1.0"
MAX_JSON_BYTES = 8 * 1024 * 1024


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
    "info": object_schema(FILE_ARGS, ["input"]),
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
        },
        ["input"],
    ),
    "measure": object_schema({**FILE_ARGS, "handles": HANDLES}, ["input", "handles"]),
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
        },
        ["output", "plan"],
    ),
    "render": object_schema(
        {**FILE_ARGS, "output": STRING, "overwrite": BOOL}, ["input", "output"]
    ),
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
        },
        ["spec", "model"],
    ),
}
REQUEST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "TCAD agent request",
    "oneOf": [
        object_schema(
            {
                "id": {"type": ["string", "integer", "null"]},
                "command": {"const": name},
                "args": schema,
            },
            ["command", "args"],
        )
        for name, schema in COMMAND_SCHEMAS.items()
    ],
}


def loads_json(raw):
    if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise CadError("REQUEST_TOO_LARGE", "JSON input exceeds 8 MiB.")

    def reject_constant(value):
        raise CadError("INVALID_JSON", f"Non-finite JSON number is not allowed: {value}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CadError("INVALID_JSON", f"Duplicate object key: {key}")
            result[key] = value
        return result

    try:
        result = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except (ValueError, RecursionError) as exc:
        raise CadError("INVALID_JSON", "Input is not valid JSON.", {"reason": str(exc)}) from exc
    _finite_numbers(result)
    return result


def _finite_numbers(value, depth=0):
    import math

    if depth > 64:
        raise CadError("INVALID_JSON", "JSON nesting exceeds 64 levels.")
    if isinstance(value, str) and any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise CadError("INVALID_JSON", "JSON strings must not contain unpaired Unicode surrogates.")
    if isinstance(value, float) and not math.isfinite(value):
        raise CadError("INVALID_JSON", "JSON numbers must be finite.")
    if isinstance(value, dict):
        for key, item in value.items():
            _finite_numbers(key, depth + 1)
            _finite_numbers(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _finite_numbers(item, depth + 1)


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
    from . import engine
    from .converters import convert_file, readable_dxf
    from .render import render_drawing

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
        from .room_model import build_room_model, verify_room_model

        if command == "room-model":
            return build_room_model(
                args["spec"], args["output"], overwrite=args.get("overwrite", False)
            )
        return verify_room_model(
            args["spec"], args["model"], tolerance_mm=args.get("tolerance_mm", 0.01)
        )
    if command == "new":
        return engine.apply_plan(
            {"version": 1, "units": args.get("units", "mm"), "operations": []},
            args["output"],
            overwrite=args.get("overwrite", False),
            dry_run=args.get("dry_run", False),
        )
    input_path = args.get("input")
    if command == "apply" and input_path is None:
        return engine.apply_plan(
            args["plan"],
            args["output"],
            overwrite=args.get("overwrite", False),
            dry_run=args.get("dry_run", False),
        )
    conversion_info = {}
    with readable_dxf(input_path, conversion_info=conversion_info) as path:
        options = {key: value for key, value in args.items() if key != "input"}
        if command == "info":
            result = engine.inspect_drawing(path, **options)
        elif command == "query":
            result = engine.query_entities(path, **options)
        elif command == "measure":
            result = engine.measure_entities(path, **options)
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
            result["conversion"] = conversion_info
            warnings = result.setdefault("warnings", [])
            warnings.extend(
                item for item in conversion_info.get("warnings", []) if item not in warnings
            )
    return result


def failure(error, request_id=None):
    return {
        "schema_version": PROTOCOL_VERSION,
        "id": request_id,
        "ok": False,
        "error": error.as_dict(),
    }


def execute_request(request):
    request_id = request.get("id") if isinstance(request, dict) else None
    if request_id is not None and (
        isinstance(request_id, bool) or not isinstance(request_id, (str, int))
    ):
        request_id = None
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
                "id": {"type": ["string", "integer", "null"]},
                "command": {"const": command},
                "args": COMMAND_SCHEMAS[command],
            },
            ["command", "args"],
        )
        errors = list(Draft202012Validator(envelope).iter_errors(request))
        if errors:
            raise CadError(
                "INVALID_REQUEST",
                "Request does not match the command schema.",
                {
                    "errors": [
                        {"path": list(error.absolute_path), "message": error.message}
                        for error in errors[:10]
                    ]
                },
            )
        # Third-party diagnostics must not corrupt the JSON transport.
        with redirect_stdout(sys.stderr):
            result = dispatch(command, request["args"].copy())
        return {
            "schema_version": PROTOCOL_VERSION,
            "id": request_id,
            "ok": True,
            "result": jsonable(result),
        }
    except CadError as exc:
        return failure(exc, request_id)
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
