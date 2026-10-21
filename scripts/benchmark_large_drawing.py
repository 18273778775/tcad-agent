# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Reproducible 100k-entity / >=20 MiB DXF query benchmark, without viewer automation.

Each measured workload runs in a fresh Python process. RSS covers Python, ezdxf,
SQLite, and native dependencies. Warm queries fail if source parsing or geometry
formatting is attempted. Run from an installed checkout or project environment.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time


def _peak_rss():
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024


def _generate(source, entities, minimum_bytes):
    import ezdxf

    text_count = max(entities // 5, 1)
    text_length = max(512, (minimum_bytes - entities * 120) // text_count + 256)
    doc = ezdxf.new("R2013", units=4)
    space = doc.modelspace()
    for index in range(20):
        doc.layers.add(f"L{index:02}")
    for index in range(entities):
        x, y = index % 1000, index // 1000
        attrs = {"layer": f"L{index % 20:02}"}
        if index % 5 == 0:
            text = (f"Asset {index}: room zone {y}; equipment annotation; " + "services; " * (text_length // 10))[:text_length]
            space.add_text(text, dxfattribs={**attrs, "insert": (x, y), "height": 0.2})
        elif index % 5 == 2:
            space.add_circle((x, y), 0.3, dxfattribs=attrs)
        else:
            space.add_line((x, y), (x + 0.5, y + 0.5), dxfattribs=attrs)
    doc.saveas(source)
    if source.stat().st_size < minimum_bytes:
        raise RuntimeError("Generated real entities did not meet minimum size; increase --entities.")


def _worker(args):
    from unittest.mock import patch
    from agentcad.engine import query_entities

    common = {"detail": "summary", "fields": ["handle", "type", "layer"], "limit": 100}
    options = dict(common)
    if args.worker == "filter":
        options.update(types=["LINE"], layer="L01")
    elif args.worker == "projected_geometry":
        options.update(types=["LINE"], layer="L01", fields=["handle", "start", "end"])
    elif args.worker == "bounded":
        options.update(max_output_bytes=4096)
    timings, sizes, counts = [], [], []
    cursor = args.cursor
    phases = args.pages if args.worker == "warm_pages" else 1
    parse_calls = 0
    format_calls = 0
    if args.worker == "warm_pages":
        with patch("agentcad.engine.read_document", side_effect=AssertionError("Warm page reparsed source")), \
                patch("agentcad.engine._record", side_effect=AssertionError("Warm page reformatted geometry")):
            for _ in range(phases):
                started = time.perf_counter()
                result = query_entities(args.source, cursor=cursor, **options)
                timings.append(time.perf_counter() - started)
                raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
                sizes.append(len(raw))
                counts.append(result["returned"])
                cursor = result["next_cursor"]
                if cursor is None:
                    break
    else:
        import agentcad.engine as engine
        original_read, original_record = engine.read_document, engine._record

        def read(*a, **kw):
            nonlocal parse_calls
            parse_calls += 1
            return original_read(*a, **kw)

        def record(*a, **kw):
            nonlocal format_calls
            format_calls += 1
            return original_record(*a, **kw)

        with patch("agentcad.engine.read_document", side_effect=read), patch("agentcad.engine._record", side_effect=record):
            started = time.perf_counter()
            result = query_entities(args.source, **options)
            timings.append(time.perf_counter() - started)
        sizes.append(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()))
        counts.append(result["returned"])
        cursor = result["next_cursor"]
    print(json.dumps({"scenario": args.worker, "seconds": timings, "p50_seconds": statistics.median(timings),
                      "max_seconds": max(timings), "response_bytes": sizes, "returned": counts,
                      "matched_total": result["total"], "peak_rss_bytes": _peak_rss(),
                      "read_document_calls": parse_calls, "record_calls": format_calls,
                      "next_cursor": cursor}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, default=Path("artifacts/large-drawing-benchmark"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--entities", type=int, default=100000)
    parser.add_argument("--min-mib", type=int, default=20)
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--cold-max-seconds", type=float, default=180)
    parser.add_argument("--warm-max-seconds", type=float, default=1)
    parser.add_argument("--rss-max-mib", type=float, default=1024)
    parser.add_argument("--worker", choices=["cold", "warm_pages", "filter", "projected_geometry", "bounded"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--cursor")
    args = parser.parse_args()
    if args.worker:
        _worker(args)
        return
    if args.entities < 100000 or args.min_mib < 20:
        parser.error("Acceptance benchmark requires at least 100000 entities and 20 MiB.")
    args.workdir.mkdir(parents=True, exist_ok=True)
    source = args.workdir.resolve() / "large-100k.dxf"
    if source.exists():
        parser.error("Use a new --workdir; benchmark preserves existing drawings and caches.")
    generated = time.perf_counter()
    _generate(source, args.entities, args.min_mib * 1024 * 1024)
    generation_seconds = time.perf_counter() - generated
    environment = os.environ.copy()
    environment["TCAD_CACHE_DIR"] = str(args.workdir.resolve() / "query-cache")
    results = []
    cursor = None
    for scenario in ("cold", "warm_pages", "filter", "projected_geometry", "bounded"):
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", scenario,
                   "--source", str(source), "--pages", str(args.pages)]
        if scenario == "warm_pages":
            command += ["--cursor", cursor]
        completed = subprocess.run(command, text=True, capture_output=True, env=environment,
                                   timeout=args.cold_max_seconds + 60, check=True)
        result = json.loads(completed.stdout)
        if scenario == "cold":
            cursor = result["next_cursor"]
        result.pop("next_cursor", None)
        results.append(result)
    checks = {
        "entities_at_least_100k": args.entities >= 100000,
        "source_at_least_20mib": source.stat().st_size >= 20 * 1024 * 1024,
        "cold_within_budget": results[0]["max_seconds"] <= args.cold_max_seconds,
        "warm_pages_within_budget": results[1]["max_seconds"] <= args.warm_max_seconds,
        "all_rss_measured": all(r["peak_rss_bytes"] > 0 for r in results),
        "all_rss_within_budget": max(r["peak_rss_bytes"] for r in results) <= args.rss_max_mib * 1024 * 1024,
        "warm_does_not_parse_or_format_source": results[1]["read_document_calls"] == results[1]["record_calls"] == 0,
        "filter_formats_only_matches": results[2]["record_calls"] == results[2]["matched_total"],
        "stdout_results_bounded": all(max(r["response_bytes"]) <= (4096 if r["scenario"] == "bounded" else 32768) for r in results),
    }
    report = {"schema_version": "large-drawing-benchmark-1", "entity_count": args.entities,
              "source_bytes": source.stat().st_size, "generation_seconds": generation_seconds,
              "environment": {"platform": platform.platform(), "python": platform.python_version(),
                              "cpu_count": os.cpu_count()}, "scenarios": results, "checks": checks,
              "thresholds": {"cold_max_seconds": args.cold_max_seconds,
                             "warm_page_max_seconds": args.warm_max_seconds, "peak_rss_max_mib": args.rss_max_mib},
              "passed": all(checks.values())}
    output = args.output or args.workdir / "benchmark.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output.resolve()), "passed": report["passed"], "checks": checks}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
