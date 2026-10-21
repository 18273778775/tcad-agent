# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Byte-bounded agent responses with explicit omissions and full-result artifacts."""

import hashlib
import json
import os
from pathlib import Path

from .errors import CadError
from .io import atomic_output

DEFAULT_OUTPUT_BYTES = 32768
MAX_OUTPUT_BYTES = 1024 * 1024
ENCODER = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def validate_result_destination(path, overwrite=False):
    target = Path(path).expanduser().resolve()
    if target.exists() and not target.is_file():
        raise CadError("INVALID_OUTPUT", "Result destination must be an ordinary file.")
    if target.exists() and not overwrite:
        raise CadError("OUTPUT_EXISTS", "Result file exists; use overwrite_result_file explicitly.")
    if not target.parent.is_dir():
        raise CadError("DIRECTORY_NOT_FOUND", "Result file parent directory does not exist.")


def encoded_size(value, stop_after=None):
    total = 0
    for part in ENCODER.iterencode(value):
        total += len(part.encode("utf-8"))
        if stop_after is not None and total > stop_after:
            break
    return total


def project_fields(value, fields):
    """Project result fields, including dotted object paths; never evaluate expressions."""
    if not fields:
        return value
    result = {}
    for field in fields:
        parts = field.split(".")
        source, target = value, result
        for index, key in enumerate(parts):
            if not isinstance(source, dict) or key not in source:
                break
            if index == len(parts) - 1:
                target[key] = source[key]
            else:
                source = source[key]
                target = target.setdefault(key, {})
    return result


def _artifact(result, path, overwrite=False):
    try:
        maximum = int(os.environ.get("TCAD_MAX_RESULT_MB", "64")) * 1024 * 1024
    except ValueError as exc:
        raise CadError("INVALID_CONFIGURATION", "TCAD_MAX_RESULT_MB must be an integer.") from exc
    if maximum <= 0:
        raise CadError("INVALID_CONFIGURATION", "TCAD_MAX_RESULT_MB must be positive.")
    digest, size = hashlib.sha256(), 0
    with atomic_output(path, overwrite=overwrite) as temporary:
        with temporary.open("wb") as stream:
            for part in ENCODER.iterencode(result):
                block = part.encode("utf-8")
                size += len(block)
                if size + 1 > maximum:
                    raise CadError("RESULT_FILE_TOO_LARGE", "Selected result exceeds its file budget.",
                                   {"max_bytes": maximum})
                digest.update(block)
                stream.write(block)
            stream.write(b"\n")
            digest.update(b"\n")
            size += 1
    return {"path": str(Path(path).expanduser().resolve()), "bytes": size,
            "sha256": digest.hexdigest(), "complete_selected_result": True}


def _without_provenance(value, removed, path="/result"):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            item_path = path + "/" + key
            if key in {"provenance", "evidence", "source", "conversion"}:
                removed.append({"path": item_path[:256], "kind": "provenance_removed"})
            else:
                result[key] = _without_provenance(item, removed, item_path)
        return result
    if isinstance(value, (tuple, list)):
        return [_without_provenance(item, removed, path + "/" + str(index))
                for index, item in enumerate(value)]
    return value


def _compact(value, omissions, *, array_limit, string_limit, mapping_limit,
             no_provenance=False, path="", preserve_entities=False):
    # Query-cache owns both entity and vertex continuation offsets. Its bounded
    # records must stay intact or a second truncation would skip unseen vertices.
    if preserve_entities and path.startswith("/result/entities/"):
        return value
    if preserve_entities == "batch" and path.startswith("/result/results/"):
        return value
    def omitted(kind, original, returned):
        omissions.append({"path": path or "/", "kind": kind,
                          "total": original, "returned": returned})

    if isinstance(value, dict):
        result = {}
        keys = list(value)
        selected = keys[:mapping_limit]
        if len(selected) < len(keys):
            omitted("object_keys", len(keys), len(selected))
        for key in selected:
            if no_provenance and key in {"provenance", "evidence", "source", "conversion"}:
                omissions.append({"path": path + "/" + key, "kind": "provenance_removed"})
                continue
            result[key] = _compact(value[key], omissions, array_limit=array_limit,
                                   string_limit=string_limit, mapping_limit=mapping_limit,
                                   no_provenance=no_provenance, path=path + "/" + key,
                                   preserve_entities=preserve_entities)
        return result
    if isinstance(value, (tuple, list)):
        # The engine's cursor advances by returned entities. Never silently drop
        # entities here, or a caller following next_cursor would skip records.
        vector = len(value) <= 5 and all(isinstance(x, (int, float)) for x in value)
        protected = ((preserve_entities and path == "/result/entities") or
                     (preserve_entities == "batch" and path == "/result/results"))
        count = len(value) if vector or protected else array_limit
        selected = value[:count]
        if len(selected) < len(value):
            omitted("array_items", len(value), len(selected))
        return [_compact(item, omissions, array_limit=array_limit, string_limit=string_limit,
                         mapping_limit=mapping_limit, no_provenance=no_provenance,
                         path=path + "/" + str(index), preserve_entities=preserve_entities)
                for index, item in enumerate(selected)]
    path_value = path.rsplit("/", 1)[-1] in {"path", "input", "output", "source", "model", "next_cursor"}
    if isinstance(value, str) and len(value) > string_limit and not path_value:
        omitted("string_characters", len(value), string_limit)
        return value[:string_limit]
    return value


def finalize_response(response, args=None, command=None):
    args = args or {}
    budget = int(args.get("max_output_bytes", DEFAULT_OUTPUT_BYTES))
    if not 4096 <= budget <= MAX_OUTPUT_BYTES:
        raise CadError("INVALID_ARGUMENT", "Output budget must be between 4096 and 1048576 bytes.")
    mode = args.get("detail", "summary")
    response = dict(response)
    selected_omissions = []
    if command == "schema" and (args.get("fields") or args.get("no_provenance")):
        raise CadError("INVALID_ARGUMENT", "Schema contracts cannot be projected or stripped; select a target schema.")
    if response.get("ok") and args.get("fields") and command != "query":
        response["result"] = project_fields(response["result"], args["fields"])
    if response.get("ok") and args.get("no_provenance"):
        response["result"] = _without_provenance(response["result"], selected_omissions)
    if response.get("ok") and args.get("result_file"):
        artifact = _artifact(response["result"], args["result_file"],
                             args.get("overwrite_result_file", False))
        original = response["result"]
        summary = {key: original[key] for key in
                   ("total", "returned", "has_more", "next_cursor", "source_revision", "counts",
                    "units", "totals", "output", "passed", "verified", "source_verified",
                    "model_verified") if isinstance(original, dict) and key in original}
        response["result"] = {**summary, "artifact": artifact}
    # JSON schemas are executable contracts. Return them whole or an actionable
    # error, never a truncated schema that silently validates different inputs.
    if command == "schema" and response.get("ok"):
        if encoded_size(response, budget) + 1 <= budget:
            return response
        raise CadError("RESPONSE_TOO_LARGE", "Schema exceeds the response budget; select a schema, "
                       "raise max_output_bytes, or use result_file.", {"max_bytes": budget})
    starts = [(32, 1024, 64), (16, 512, 32), (8, 256, 24), (4, 128, 16), (1, 64, 8)]
    if mode == "full":
        starts.insert(0, (100000, 1000000, 100000))
    for array_limit, string_limit, mapping_limit in starts:
        omissions = list(selected_omissions)
        candidate = _compact(response, omissions, array_limit=array_limit,
                             string_limit=string_limit, mapping_limit=mapping_limit,
                             no_provenance=args.get("no_provenance", False),
                             preserve_entities="batch" if command == "batch" else command == "query")
        # Always retain request association, even at the most compact setting.
        candidate["id"] = response.get("id")
        candidate["output"] = {"detail": mode, "max_bytes": budget,
                               "truncated": bool(omissions), "omitted_sections": len(omissions),
                               "omissions": omissions[:12]}
        if encoded_size(candidate, budget) + 1 <= budget:
            return candidate
    raise CadError("RESPONSE_TOO_LARGE", "Result cannot fit the output budget without losing "
                   "pagination or essential status. Use result_file or a smaller selection.",
                   {"max_bytes": budget})


def bounded_failure(error, request_id=None, budget=DEFAULT_OUTPUT_BYTES):
    """Final fallback must itself fit, including errors from output formatting."""
    response = {"schema_version": "1.0", "id": request_id, "ok": False,
                "error": error.as_dict()}
    return finalize_response(response, {"max_output_bytes": budget}, None)
