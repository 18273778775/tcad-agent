# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Ordered, non-atomic batches with streaming outcome reports and bounded stdout."""

import os
from pathlib import Path
import time
import uuid

from .errors import CadError
from .io import atomic_output, file_sha256
from .output import DEFAULT_OUTPUT_BYTES, ENCODER, finalize_response, validate_result_destination
from .resources import run_request


def _report_path(args):
    if args.get("result_file"):
        return Path(args["result_file"]).expanduser().resolve(), False
    base = Path(os.environ.get("TCAD_CACHE_DIR") or
                (Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) /
                 "tcad-agent" / "cache")) / "batch-reports"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base / ("batch-" + uuid.uuid4().hex + ".json"), True


def _prune_reports(directory, keep):
    total = 0
    for index, path in enumerate(sorted(directory.glob("batch-*.json"),
                                         key=lambda p: p.stat().st_mtime_ns, reverse=True)):
        if path.is_symlink():
            continue
        total += path.stat().st_size
        if path != keep and (index >= 16 or total > 128 * 1024 * 1024):
            try:
                path.unlink()
            except OSError:
                pass


def execute_batch(requests, args):
    if not isinstance(requests, list) or len(requests) > 1000:
        raise CadError("INVALID_REQUEST", "Batch must be an array of at most 1000 requests.")
    seconds = args.get("timeout_seconds")
    if seconds is None:
        seconds = 60
    if not 0.1 <= seconds <= 600 or not 64 <= args["max_memory_mb"] <= 16384:
        raise CadError("INVALID_ARGUMENT", "Batch resource budgets are outside supported limits.")
    budget = args.get("max_output_bytes", DEFAULT_OUTPUT_BYTES)
    if not 4096 <= budget <= 1048576:
        raise CadError("INVALID_ARGUMENT", "Batch output budget must be between 4096 and 1048576.")
    path, automatic = _report_path(args)
    if len(str(path).encode("utf-8")) > budget // 2:
        raise CadError("OUTPUT_BUDGET_TOO_SMALL", "Use a shorter batch result path or raise the output budget.")
    validate_result_destination(path, args.get("overwrite_result_file", False))
    # Validate all known child file boundaries before executing any mutating work.
    for request in requests:
        child_args = request.get("args", {}) if isinstance(request, dict) else {}
        if not isinstance(child_args, dict):
            continue
        for key in ("input", "output", "model", "source", "result_file"):
            value = child_args.get(key)
            if isinstance(value, str) and Path(value).expanduser().resolve() == path:
                raise CadError("OUTPUT_CONFLICT", "Batch report must not replace a child drawing or result.")
        spec = child_args.get("spec")
        spec_source = spec.get("source", {}) if isinstance(spec, dict) else {}
        if isinstance(spec_source, dict) and isinstance(spec_source.get("path"), str):
            if Path(spec_source["path"]).expanduser().resolve() == path:
                raise CadError("OUTPUT_CONFLICT", "Batch report must not replace a source CAD drawing.")
    try:
        maximum = int(os.environ.get("TCAD_MAX_RESULT_MB", "64")) * 1024 * 1024
    except ValueError as exc:
        raise CadError("INVALID_CONFIGURATION", "TCAD_MAX_RESULT_MB must be an integer.") from exc
    if maximum < 4096:
        raise CadError("INVALID_CONFIGURATION", "Batch result file budget must be at least 4096 bytes.")
    started, inline, inline_bytes, executed, failed = time.monotonic(), [], 0, 0, []
    inline_open, stop_reason = True, None
    with atomic_output(path, overwrite=args.get("overwrite_result_file", False)) as temporary:
        with temporary.open("wb") as stream:
            stream.write(b'{"schema_version":"1.0","id":null,"results":[')
            checkpoint = (stream.tell(), 0, 0, 0, 0)
            unconfirmed = None
            try:
                for request in requests:
                    remaining = seconds - (time.monotonic() - started)
                    if remaining < 0.1:
                        stop_reason = "BATCH_TIMEOUT"
                        break
                    # Reserve one whole child envelope and footer before executing;
                    # never execute a write whose outcome cannot be recorded.
                    if stream.tell() + budget + 8192 > maximum:
                        stop_reason = "BATCH_REPORT_LIMIT"
                        break
                    if isinstance(request, dict) and isinstance(request.get("args"), dict):
                        inherited = request["args"].copy()
                        for key, maximum_value in (("timeout_seconds", remaining),
                                                   ("max_memory_mb", args["max_memory_mb"]),
                                                   ("max_output_bytes", budget)):
                            value = inherited.get(key, maximum_value)
                            if isinstance(value, (int, float)) and not isinstance(value, bool):
                                inherited[key] = min(value, maximum_value)
                        if args.get("no_provenance"):
                            inherited["no_provenance"] = True
                        if args.get("fields"):
                            inherited.setdefault("fields", args["fields"])
                        inherited.setdefault("detail", args.get("detail", "summary"))
                        request = {**request, "args": inherited}
                    result = run_request(request)
                    raw = ENCODER.encode(result).encode("utf-8")
                    if executed:
                        stream.write(b",")
                    stream.write(raw)
                    if not result["ok"]:
                        failed.append(executed)
                    executed += 1
                    if inline_open and inline_bytes + len(raw) <= max(0, budget - 6144):
                        inline.append(result)
                        inline_bytes += len(raw)
                    else:
                        inline_open = False
                    checkpoint = (stream.tell(), executed, len(failed), len(inline), inline_bytes)
                    if result.get("error", {}).get("code") == "INTERRUPTED":
                        stop_reason = "BATCH_INTERRUPTED"
                        break
            except KeyboardInterrupt:
                position, executed, failure_count, inline_count, inline_bytes = checkpoint
                stream.seek(position)
                stream.truncate()
                failed = failed[:failure_count]
                inline = inline[:inline_count]
                unconfirmed = executed
                stop_reason = "BATCH_INTERRUPTED"
            summary = {"ok": not failed and executed == len(requests), "executed": executed,
                       "not_executed": len(requests) - executed, "failed_count": len(failed),
                       "failed_indices": failed, "stop_reason": stop_reason,
                       "interrupted_unconfirmed_index": unconfirmed}
            stream.write(b"]," + ENCODER.encode(summary)[1:].encode("utf-8") + b"\n")
        artifact = {"path": str(path), "bytes": temporary.stat().st_size,
                    "sha256": file_sha256(temporary), "complete_executed_responses": True,
                    "automatic_cache": automatic}
    if automatic:
        _prune_reports(path.parent, path)
    response = {"schema_version": "1.0", "id": None, **summary, "results": inline,
                "inline_results": len(inline), "omitted_results": executed - len(inline),
                "artifact": artifact}
    # Every inline child response has already been budgeted by its own worker.
    # Only whole child outcomes may be omitted; never alter their entity/vertex
    # arrays or cursor accounting for a second time.
    outer_args = {key: value for key, value in args.items()
                  if key not in {"result_file", "overwrite_result_file", "fields"}}
    wrapped = {"schema_version": "1.0", "id": None, "ok": True, "result": response}
    bounded = finalize_response(wrapped, outer_args, "batch")
    emitted = bounded["result"]
    emitted.update(schema_version="1.0", id=None, ok=summary["ok"], executed=executed,
                   not_executed=summary["not_executed"], failed_count=len(failed),
                   artifact=artifact)
    emitted["output"] = bounded.get("output", {})
    emitted["inline_results"] = len(emitted.get("results", []))
    emitted["omitted_results"] = executed - emitted["inline_results"]
    if emitted["omitted_results"]:
        emitted["output"]["truncated"] = True
        emitted["output"].setdefault("omissions", []).append(
            {"path": "/results", "kind": "whole_responses_in_artifact",
             "total": executed, "returned": emitted["inline_results"]})
        emitted["output"]["omitted_sections"] = emitted["output"].get("omitted_sections", 0) + 1
    return emitted
