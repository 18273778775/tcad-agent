# Large drawing query acceptance

This benchmark checks persistent query pagination and bounded response size. It
is a controlled workload, not evidence that arbitrary architectural drawings,
deep block hierarchies, fonts, rendering, or editing all meet these timings.

## Run

```powershell
python scripts/benchmark_large_drawing.py --workdir artifacts/large-drawing-benchmark --entities 100000 --min-mib 20 --output artifacts/large-drawing-benchmark/benchmark.json
```

Use a new `--workdir` for each run. The script preserves existing source drawings
and fails if `large-100k.dxf` already exists. The 100,000 native entities include
60,000 LINEs, 20,000 CIRCLEs, and 20,000 actual TEXT annotations over 20 layers.
The file-size requirement is met by valid entity data, with no trailing padding.

The script launches fresh Python workers for cold query, warm pagination,
layer/type filtering, geometry projection, and a 4 KiB response budget. Source
parsing and record formatting are instrumented. Warm pages raise immediately if
`read_document` or `_record` is called. Each workload records peak process RSS,
core query latency, response bytes, and selected/returned counts.

Default acceptance gates are: at least 100,000 entities; at least 20 MiB DXF;
cold query <=180 seconds; each warm query <=1 second; each worker peak RSS
<=1024 MiB; measured RSS >0; warm parsing/formatting calls =0; filtering formats
only matching entities; response <=32 KiB (<=4 KiB for the bounded workload).
Thresholds are explicit script options. Timing starts after worker imports, so
it excludes Python process startup and dependency import time. A new projection
or filter can require one source parse; all following pages of that prepared
query, including a fresh process, use SQLite only.

The GitHub benchmark workflow runs these gates on Windows and Linux for source,
dependency or benchmark changes in pushes and pull requests. It can also be run
manually. Its uploaded report is separate from the recorded local baseline below.

## Local baseline: 2026-10-21

[Machine-readable report](benchmarks/large-drawing-windows.json). Windows 10,
Python 3.12.13, 16 logical CPUs. Input: 100,000 entities / 27,598,154 bytes
(26.32 MiB). Fixture generation took 11.75 seconds, reported separately.

| Workload | Core query latency | Peak RSS | Result bytes | Source parses / formatted records |
| --- | ---: | ---: | ---: | ---: |
| Cold compact query | 20.08 s | 289.64 MiB | 5,062 | 1 / 100,000 |
| Warm pages (10 pages) | median 17.48 ms; max 37.23 ms | 59.91 MiB | 5,072–5,163/page | 0 / 0 |
| Layer + type filter (5,000 matches) | 7.67 s | 211.43 MiB | 5,112 | 1 / 5,000 |
| Projected LINE geometry | 7.37 s | 211.50 MiB | 6,888 | 1 / 5,000 |
| 4 KiB budget | 36.05 ms | 59.70 MiB | 2,661 | 0 / 0 |

Real fresh CLI calls (three warm pages, including Python startup, imports, validation, and resource supervision) had a median of **1.248 seconds**, with 3,006–3,009 stdout bytes under a 4 KiB budget. These are end-to-end latencies; the table's core-query milliseconds must not be advertised as complete CLI latency. The one-second warm-page gate applies to the instrumented core query, not the complete CLI call.

The baseline passes all gates. These are measured results for this input on this
machine, not universal performance guarantees. The parser still materializes a
DXF document on first read. Highly complex single entities, unsupported bounds,
new filters/projections, and larger inputs have separate costs. Rendering and
DXF edits are not covered by this query benchmark.

## Query cache and output contract

- Native entity metadata has SQLite indexes for layout/type, layout/layer,
  layout/layer/type, and handles. Selection happens before bounds and geometry
  formatting. Query returns native INSERT records; it never expands block
  definitions. INSERT bounds are unavailable in native query, including bbox
  filtering, rather than expanding the block silently.
- Each source version and filter/projection prepares one bounded SQLite result
  snapshot. Keyset `next_cursor` pages read only their selected records. Legacy
  `offset` uses SQL OFFSET, which may scan index entries up to that offset; use
  cursors for deep pagination. Substring text searches require a metadata scan.
- Cursors bind source version, filters, layout, detail, fields, and vertex paging.
  Source changes return `STALE_CURSOR`; filter changes return `CURSOR_MISMATCH`;
  evicted snapshots return `CURSOR_EXPIRED`. Restart without a cursor in these
  cases. The local source version uses filesystem path/inode/size/mtime/ctime;
  it is an invalidation token, not a cryptographic content attestation.
- `detail=summary` avoids vertex arrays, text content, and expensive bounds.
  `fields` is applied before those computations. `detail=full` includes a bounded
  vertex page: default 128, maximum explicitly requested 10,000. Use exactly one
  `handles` selector with `vertex_offset` and `vertex_limit` to retrieve further
  vertex pages. `geometry_page` reports total/returned/next_offset; budget
  clipping can reduce the returned vertex count further. Text is capped at
  1,024 characters with explicit `truncation` metadata. There is no text-content
  pagination in this release; use `export --output selected.jsonl` for complete supported annotation/vertex fields without injecting them into stdout.
- Results reserve 2,048 bytes for protocol metadata. Byte clipping stops before
  an unreturned entity and advances the cursor only through returned entities.
  A single oversized record is clipped with explicit geometry/truncation data;
  an unrepresentable projection produces `OUTPUT_BUDGET_TOO_SMALL`.
- Default cache location is the user's local cache directory under
  `tcad-agent/queries`. Set `TCAD_CACHE_DIR` for a task-specific location. SQLite
  stores query text/geometry in plaintext local files, never pickle or code.
  Cache files use least-recently-used cleanup: at most 16 snapshots, 512 MiB
  total, 128 MiB per snapshot, and 32 query views per snapshot. A full SQLite
  snapshot returns an actionable `QUERY_CACHE_ERROR` instead of growing without
  limit. Cleanup of files held open by another process is best effort.
- `info` can skip approximate bounds (`include_bounds=false`, CLI default);
  requesting bounds explicitly can still be expensive. Exact measurements use
  entity database handle lookup rather than constructing a full-layout handle
  dictionary. The initial DXF parse is still required.

The implementation uses the Python standard library's SQLite engine and
parameterized SQL. The index approach follows SQLite's documented
[query planner](https://www.sqlite.org/queryplanner.html). No external database
server or executable is required.

## DWG and resource limits

DWG queries reuse a completed conversion cache bound to the original DWG SHA-256
and converter identity. Cache hits use a stable DXF path so query cursors survive
independent CLI invocations. Every DWG hit still hashes source and cached DXF
bytes sequentially: O(source bytes + DXF bytes) IO, with no repeated conversion
or DXF parsing. The DXF benchmark above is not a DWG timing benchmark.

There is no Excel command in this release. The bounded JSONL export is the
supported route for complete selected native fields outside the agent context;
the query benchmark does not certify export throughput or spreadsheet limits.

The conversion cache retains at most 16 completed entries / 512 MiB, with
best-effort eviction of active files. Explicit `TCAD_CACHE_DIR` uses its `dwg/`
subdirectory. Source and generated DXF file budgets default to 128 MiB. Complete
native-field JSONL export and filtered SVG preview each have explicit file and
entity limits. Worker deadlines and sampled RSS monitoring provide cancellation
for work that exceeds configured budgets; they do not guarantee arbitrary CAD
input can be processed within those budgets. See [agent usage](agent-usage.md).
