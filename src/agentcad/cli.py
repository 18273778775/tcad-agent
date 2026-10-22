# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Small argparse frontend; all operation results share one JSON contract."""

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .errors import CadError
from .transport import MAX_JSON_BYTES, loads_json
from .output import bounded_failure as failure
from .output import DEFAULT_OUTPUT_BYTES, validate_result_destination
from .resources import run_request


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CadError("INVALID_ARGUMENT", message)


def parser():
    root = Parser(prog="tcad", description="Headless 2D CAD for agents. Operation output is JSON.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="Machine-readable features and limits")
    commands.add_parser("doctor", help="Runtime and optional converter availability")
    schema_cmd = commands.add_parser("schema", help="Request and editing-plan JSON Schemas")
    schema_cmd.add_argument(
        "target",
        choices=["all", "request", "plan", "room-map", "room-spec"],
        nargs="?",
        default="all",
    )
    for name in ("info", "query", "measure", "render", "export"):
        sub = commands.add_parser(name)
        sub.add_argument("input")
        sub.add_argument("--layout", default="Model")
        if name == "query":
            sub.add_argument("--type", dest="types", action="append")
            sub.add_argument("--layer")
            sub.add_argument("--text")
            sub.add_argument("--handle", dest="handles", action="append")
            sub.add_argument("--bbox", nargs=4, type=float)
            sub.add_argument("--offset", type=int, default=0)
            sub.add_argument("--limit", type=int, default=100)
            sub.add_argument("--cursor")
            sub.add_argument("--vertex-offset", type=int)
            sub.add_argument("--vertex-limit", type=int)
        if name == "info":
            sub.add_argument("--bounds", dest="include_bounds", action="store_true")
        if name == "measure":
            sub.add_argument("--handle", dest="handles", action="append")
            sub.add_argument("--type", dest="types", action="append")
            sub.add_argument("--layer")
            sub.add_argument("--bbox", nargs=4, type=float)
            sub.add_argument("--max-entities", type=int, default=100000)
        if name == "render":
            sub.add_argument("--output")
            sub.add_argument("--output-dir")
            sub.add_argument("--overwrite", action="store_true")
            sub.add_argument("--type", dest="types", action="append")
            sub.add_argument("--layer")
            sub.add_argument("--handle", dest="handles", action="append")
            sub.add_argument("--max-entities", type=int, default=20000)
            sub.add_argument("--max-svg-bytes", type=int, default=8 * 1024 * 1024)
            sub.add_argument("--bbox", nargs=4, type=float, help="WCS rectangle to clip and fit")
            sub.add_argument("--background", choices=["white", "black", "layout"], default="white")
            sub.add_argument("--foreground", choices=["cad", "monochrome"], default="cad")
            sub.add_argument("--hatch-mode", choices=["adaptive", "accurate"], default="adaptive")
            sub.add_argument("--lineweight-scale", type=float, default=1.0)
            sub.add_argument("--font-family", help="Installed font family for outline text")
            sub.add_argument("--annotate", choices=["labels", "areas", "axes"])
            sub.add_argument("--legend", action="store_true")
            sub.add_argument("--max-annotations", type=int, default=200)
            sub.add_argument("--width", type=int, default=1600, help="PNG longest side, 16..4096 pixels")
        if name == "export":
            sub.add_argument("--output", required=True)
            sub.add_argument("--overwrite", action="store_true")
            sub.add_argument("--type", dest="types", action="append")
            sub.add_argument("--layer")
            sub.add_argument("--handle", dest="handles", action="append")
            sub.add_argument("--max-entities", type=int, default=100000)
            sub.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
            sub.add_argument("--bbox", nargs=4, type=float)
        if name in {"query", "measure", "render", "export"}:
            sub.add_argument("--layers", nargs="+", help="Layer whitelist; render resolves nested layer-0 inheritance")
            sub.add_argument("--exclude-layers", nargs="+")
            sub.add_argument("--sheet", help="Sheet id or exact title; render also accepts all")
            sub.add_argument("--manifest", help="Reuse a source-SHA-validated sheet manifest")
        if name in {"query", "measure", "export"}:
            sub.add_argument("--expected-source-revision")
        if name == "query":
            sub.add_argument("--text-scope", choices=["layout", "block", "all"], default="layout")
    for name in ("sheets", "split"):
        sub = commands.add_parser(name, help="Discover drawing frames" if name == "sheets"
                                  else "Split frames to named DXF, SVG and offline HTML")
        sub.add_argument("input")
        sub.add_argument("--layout", default="Model")
        sub.add_argument("--frames", help="JSON array of frame handles or explicit bbox/name/number objects")
        sub.add_argument("--frame-layer", help="Restrict frame geometry to a specific layer")
        sub.add_argument("--min-frame-area", type=float, default=10000)
        sub.add_argument("--max-sheets", type=int, default=200)
        sub.add_argument("--frame-block-regex")
        sub.add_argument("--frame-size", help="Frame aspect-ratio hint, e.g. 841x594")
        sub.add_argument("--title-layer")
        sub.add_argument("--title-regex", help="May capture title, number, building, elevation_range, kind")
        sub.add_argument("--number-layer")
        sub.add_argument("--shared-frame", choices=["largest", "split"], default="largest")
        sub.add_argument("--names", help="JSON mapping of sheet/frame ids to name overrides")
        if name == "sheets":
            sub.add_argument("--output", help="Write full discovery JSON and return a compact receipt")
            sub.add_argument("--overwrite", action="store_true")
        else:
            sub.add_argument("--output-dir", required=True, help="New directory; existing destinations are refused")
            sub.add_argument("--no-preview", dest="preview", action="store_false", default=True)
            sub.add_argument("--no-thumbnail", dest="thumbnail", action="store_false", default=True)
            sub.add_argument("--thumbnail-width", type=int, default=1280)
            sub.add_argument("--max-entities", type=int, default=100000)
            sub.add_argument("--max-svg-bytes", type=int, default=64 * 1024 * 1024)
            sub.add_argument("--max-total-bytes", type=int, default=512 * 1024 * 1024)
            sub.add_argument("--foreground", choices=["cad", "monochrome"])
            sub.add_argument("--background", choices=["white", "black", "layout"])
            sub.add_argument("--hatch-mode", choices=["adaptive", "accurate"])
            sub.add_argument("--lineweight-scale", type=float)
            sub.add_argument("--font-family")
            sub.add_argument("--layers", nargs="+")
            sub.add_argument("--exclude-layers", nargs="+")
    for name in ("index", "area", "associate", "axes"):
        sub = commands.add_parser(name)
        sub.add_argument("input")
        sub.add_argument("--layout", default="Model")
        if name == "index":
            sub.add_argument("--text-scope", choices=["layout", "block", "all"], default="all")
            continue
        sub.add_argument("--sheet")
        sub.add_argument("--manifest")
        sub.add_argument("--bbox", nargs=4, type=float)
        sub.add_argument("--output")
        sub.add_argument("--overwrite", action="store_true")
        if name == "area":
            sub.add_argument("--handle", dest="handles", action="append")
            sub.add_argument("--layers", nargs="+")
            sub.add_argument("--type", dest="entity_types", action="append")
            sub.add_argument("--mode", choices=["union", "sum", "overlap"], default="union")
            sub.add_argument("--by", choices=["layer", "block", "sheet"])
            sub.add_argument("--tolerance", type=float, default=0)
            sub.add_argument("--flatten-tolerance", type=float, default=.1)
            sub.add_argument("--linear-unit", choices=["mm", "cm", "m", "in", "ft"])
            sub.add_argument("--make-valid", action="store_true")
            sub.add_argument("--evidence-limit", type=int, default=100)
            sub.add_argument("--max-relations", type=int, default=100000)
        else:
            sub.add_argument("--label-layers", nargs="+")
            sub.add_argument("--label-regex")
            sub.add_argument("--max-distance", type=float, default=500 if name == "associate" else 1000)
            if name == "associate":
                sub.add_argument("--target-layers", nargs="+")
                sub.add_argument("--label-types", nargs="+")
                sub.add_argument("--target-types", nargs="+")
                sub.add_argument("--method", choices=["inside", "nearest", "leader"], default="inside")
                sub.add_argument("--one-to-one", action="store_true")
                sub.add_argument("--flatten-tolerance", type=float, default=.1)
                sub.add_argument("--unmatched-output")
            else:
                sub.add_argument("--line-layers", nargs="+")
                sub.add_argument("--bubble-block-regex")
                sub.add_argument("--tolerance", type=float, default=1)
                sub.add_argument("--point", nargs=2, type=float)
                sub.add_argument("--axes", help="Previously saved source-bound axes JSON")
                sub.add_argument("--max-axes", type=int, default=1000)
                sub.add_argument("--max-intersections", type=int, default=100000)
    for name in ("new", "apply"):
        sub = commands.add_parser(name)
        sub.add_argument("--output", required=True)
        sub.add_argument("--overwrite", action="store_true")
        sub.add_argument("--dry-run", action="store_true")
        if name == "apply":
            sub.add_argument("--input")
            sub.add_argument("--plan", required=True, help="JSON file, or - for stdin")
            sub.add_argument("--expected-source-sha256")
            sub.add_argument("--expected-output-sha256")
        else:
            sub.add_argument(
                "--units", choices=["mm", "cm", "m", "in", "ft", "unitless"], default="mm"
            )
    sub = commands.add_parser("convert")
    sub.add_argument("input")
    sub.add_argument("--output", required=True)
    sub.add_argument("--backend", choices=["auto", "libredwg", "oda"], default="auto")
    sub.add_argument("--timeout", type=float, default=60)
    sub.add_argument("--overwrite", action="store_true")
    sub = commands.add_parser("room-extract", help="Extract evidence-bearing clear room dimensions")
    sub.add_argument("input")
    sub.add_argument("--mapping", required=True, help="Explicit room role mapping JSON")
    sub.add_argument("--output", help="Write a plain room specification JSON")
    sub.add_argument("--overwrite", action="store_true")
    sub = commands.add_parser("room-model", help="Build and verify a mapped rectangular-room model")
    sub.add_argument("--spec", required=True)
    sub.add_argument("--output", required=True)
    sub.add_argument("--overwrite", action="store_true")
    sub = commands.add_parser(
        "room-verify", help="Compare reopened model geometry to extracted dimensions"
    )
    sub.add_argument("--spec", required=True)
    sub.add_argument("--model", required=True)
    sub.add_argument("--tolerance-mm", dest="tolerance_mm", type=float, default=0.01)
    sub.add_argument("--source", help="Original CAD for strict source/model verification")
    sub.add_argument("--mapping", help="Role mapping for strict verification")
    sub = commands.add_parser("setup-dwg", help="Install the pinned optional DWG backend for this user")
    sub.add_argument("--data-dir")
    sub = commands.add_parser("batch", help="Ordered JSON array of requests; each plan is atomic")
    sub.add_argument("--file", default="-", help="JSON file, or - for stdin")
    commands.add_parser("serve", help="One JSON request and response per line over stdio")
    for name, sub in commands.choices.items():
        if name == "serve":
            continue
        sub.add_argument("--detail", choices=["summary", "full"], default="summary")
        sub.add_argument("--fields", type=lambda value: [item.strip() for item in value.split(",") if item.strip()],
                         help="Comma-separated entity fields (query) or dotted result fields")
        sub.add_argument("--no-provenance", action="store_true")
        sub.add_argument("--max-output-bytes", type=int, default=DEFAULT_OUTPUT_BYTES)
        sub.add_argument("--result-file", help="Write complete selected result JSON; return its path/hash")
        sub.add_argument("--overwrite-result-file", action="store_true")
        sub.add_argument("--timeout-seconds", type=float)
        sub.add_argument("--max-memory-mb", type=int, default=1024)
        sub.add_argument("--cache-dir", help="Isolated persistent drawing/index cache")
    return root


def emit(result):
    print(
        json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")), flush=True
    )


def read_json_file(path):
    try:
        if path == "-":
            raw = sys.stdin.read(MAX_JSON_BYTES + 1)
        else:
            source = Path(path)
            if source.stat().st_size > MAX_JSON_BYTES:
                raise CadError("REQUEST_TOO_LARGE", "JSON file exceeds 8 MiB.")
            raw = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise CadError("INPUT_READ_ERROR", "Cannot read JSON input.", {"reason": str(exc)}) from exc
    return loads_json(raw)


def serve():
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    while True:
        raw = stream.readline(MAX_JSON_BYTES + 2)
        if not raw:
            return 0
        newline = b"\n" if isinstance(raw, bytes) else "\n"
        if len(raw if isinstance(raw, bytes) else raw.encode("utf-8")) > MAX_JSON_BYTES:
            while raw and not raw.endswith(newline):
                raw = stream.readline(MAX_JSON_BYTES + 2)
            emit(failure(CadError("REQUEST_TOO_LARGE", "JSON line exceeds 8 MiB.")))
            continue
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            result = run_request(loads_json(raw))
            emit(result)
            if result.get("error", {}).get("code") == "INTERRUPTED":
                return 130
        except UnicodeDecodeError:
            emit(failure(CadError("INVALID_JSON", "JSON input must be valid UTF-8.")))
        except CadError as exc:
            emit(failure(exc))


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace", newline="\n")
    try:
        args = vars(parser().parse_args(argv))
        command = args.pop("command")
        if command == "serve":
            return serve()
        if args.get("result_file"):
            target = Path(args["result_file"]).expanduser().resolve()
            for key in ("file", "plan", "spec", "mapping", "frames", "input", "output", "output_dir", "model", "source"):
                source = args.get(key)
                if isinstance(source, str) and source != "-" and Path(source).expanduser().resolve() == target:
                    raise CadError("OUTPUT_CONFLICT", "Result JSON must not replace an input, plan, spec or model.")
            validate_result_destination(target, args.get("overwrite_result_file", False))
        if command == "batch":
            from .batch import execute_batch
            response = execute_batch(read_json_file(args["file"]), args)
            emit(response)
            return 0 if response["ok"] else 1
        if command == "apply":
            args["plan"] = read_json_file(args["plan"])
        if command in {"sheets", "split"} and args.get("frames"):
            args["frames"] = read_json_file(args["frames"])
        if command in {"sheets", "split"} and args.get("names"):
            args["names"] = read_json_file(args["names"])
        if command == "split":
            render_keys = {"foreground", "background", "hatch_mode", "lineweight_scale", "font_family", "layers", "exclude_layers"}
            render_options = {key: args.pop(key) for key in render_keys if args.get(key) is not None}
            if render_options:
                args["render_options"] = render_options
        if command == "room-extract":
            args["mapping"] = read_json_file(args["mapping"])
        if command in {"room-model", "room-verify"}:
            args["spec"] = read_json_file(args["spec"])
        if command == "room-verify" and args.get("mapping"):
            args["mapping"] = read_json_file(args["mapping"])
        args = {key: value for key, value in args.items() if value is not None}
        result = run_request({"command": command, "args": args})
        emit(result)
        if result.get("error", {}).get("code") == "INTERRUPTED":
            return 130
        return 0 if result["ok"] else 1
    except CadError as exc:
        emit(failure(exc))
        return 2
    except OSError as exc:
        emit(failure(CadError("IO_ERROR", "Cannot read or write CLI files.", {"reason": str(exc)[:512]})))
        return 1
    except KeyboardInterrupt:
        emit(failure(CadError("INTERRUPTED", "Operation interrupted.")))
        return 130
