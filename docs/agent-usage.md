# Agent-safe CAD operation

TCAD 0.2.0 separates CAD work, complete result files and bounded agent-visible
JSON. Start with `tcad capabilities` and `tcad schema request`. Normal command
responses use UTF-8 JSON with one LF, including structured failures.

## Read only the data needed for the next decision

```sh
tcad info building.dxf --fields entity_count,entity_types,units,source_sha256 --max-output-bytes 4096
tcad query building.dxf --type LINE --layer WALLS --fields handle,type,layer --limit 100 --max-output-bytes 8192
tcad measure building.dxf --handle ABC --fields totals,units --max-output-bytes 4096
```

`info` skips expensive approximate bounds by default; request `--bounds` when
needed. Type/layer filters and field projection precede geometry formatting.
Query returns native entities; INSERTs are not recursively expanded.

Follow `result.next_cursor` with the same source, filters, fields, detail and
vertex selection. Changing a query invalidates its cursor. Never calculate the
next offset from the requested limit: the byte budget can return fewer entities.
`returned` and `has_more` describe the actual page. Snapshot eviction/revision
changes require restarting, not silently mixing results from two drawings.

For a single polyline, explicitly retrieve bounded vertex pages:

```sh
tcad query building.dxf --handle ABC --detail full --fields handle,points --vertex-offset 0 --vertex-limit 128
```

Continue from `geometry_page.next_offset` when `has_more` is true. Inspect
`truncation` and `geometry_page` before treating a record as complete. Query text
is capped at 1,024 characters; full export below retains complete supported text
and vertex fields. New projections/vertex selections may require a new cold
parse. Approximate bounds and text extents are not exact measurement evidence.

## Keep complete data out of the model context

Default stdout budget is **32 KiB**, including UTF-8 and the final LF. Supported
explicit budgets are 4 KiB through 1 MiB. Summary mode bounds arrays, strings and
large mappings and reports `output.truncated`, omission paths and counts.
Entity/vertex continuation bookkeeping is never changed by a second truncation.

`--fields` chooses entity fields for query/export and dotted result fields for
other commands. `--no-provenance` removes source/evidence/conversion details.
These selections apply to explicit result files too. Schemas are executable
contracts: choose a schema target; field projection/provenance stripping is
rejected, and an oversized schema is returned whole via a file or rejected.

```sh
tcad info building.dxf --bounds --result-file inspection.json
tcad query building.dxf --handle ABC --detail full --result-file current-page.json
tcad export building.dxf --type LINE --layer WALLS --fields handle,layer,start,end --output walls.jsonl
```

`--result-file` preserves the complete **selected command result/current page**
before generic stdout compaction; query page/vertex/text limits still apply.
It does not export the whole drawing. Its default file budget is 64 MiB,
configurable with `TCAD_MAX_RESULT_MB`. Existing files require the independent
`--overwrite-result-file` flag. A result file cannot replace a drawing, model,
input plan or room spec. The response gives path, bytes and SHA-256.

`export` streams complete supported native entity fields as JSONL without
accumulating every output record. Default limits are 100,000 entities / 64 MiB;
use explicit `--max-entities` / `--max-bytes` for another budget. Exceeding either
limit publishes no partial export. Unsupported native fields and unexpanded
blocks remain explicit limitations; this is not lossless DXF archival.

Batch execution is ordered and is not a transaction. It streams every executed
child response to an explicit `--result-file` or an automatic user-cache report.
Stdout includes only a prefix of complete child responses that fits the budget,
plus `executed`, `not_executed`, `failed_count`, `omitted_results` and the report
path/hash. Read that report to inspect omitted outcomes; **do not repeat mutating
requests just to recover their responses**. A deadline, report budget or
interruption stops later requests and preserves completed outcomes. Cancellation
can leave the current operation's final commit uncertain; inspect its output.
Automatic reports retain at most 16 files / 128 MiB (active cleanup is best
effort); use an explicit result path for durable records.

## Budget the actual computation

CLI and JSONL operations run in owned supervised workers. Defaults are a
60-second deadline and 1,024 MiB aggregate worker/descendant RSS; `setup-dwg`
defaults to 600 seconds for downloads. Override with `--timeout-seconds` and
`--max-memory-mb`. RSS is sampled every 25 ms; this is monitored cancellation,
not an OS memory reservation or proof that instantaneous allocation cannot
exceed the threshold. Direct Python API calls are not supervised.

Drawing and generated conversion input defaults to 128 MiB (`TCAD_MAX_INPUT_MB`).
External converter diagnostics keep a 4 KiB tail with an 8 MiB flood limit.
Timeout, memory, log and output limits return structured errors; JSONL can
continue after failed requests. Atomic publication prevents partial destination
files. A process can complete a valid commit just before cancellation, so inspect
the output revision before retrying a timed-out edit.

```sh
tcad render building.dxf --layer WALLS --type LINE --max-entities 5000 --max-svg-bytes 8388608 --output walls.svg
```

Previews default to 20,000 selected native entities / 8 MiB SVG. A small number
of complex blocks can still be expensive; worker budgets cover that expansion.
Filtering and entity budgets run before drawing. SVG is a fitted preview, not a
1:1 printout. The performance benchmark does not certify render/edit workloads.

## Edit against a known revision

`source_sha256` is the cryptographic source identity for optimistic editing.
`source_revision` is a local snapshot token for pagination and is not a SHA-256
edit precondition. DWG results expose the original DWG SHA, not the cached DXF.

```sh
tcad apply --input building.dxf --plan edits.json --output revised.dxf --expected-source-sha256 SOURCE_SHA
tcad apply --input revised.dxf --plan edits.json --output revised.dxf --overwrite --expected-source-sha256 SOURCE_SHA --expected-output-sha256 TARGET_SHA
```

Cooperating TCAD writers lock the destination from read through commit. Stale
preconditions or detected external changes return `REVISION_CONFLICT`. Use
`expected_output_sha256=missing` when the target must not exist. This does not
give arbitrary external CAD applications a shared locking protocol.

## Distinguish source and model verification

```sh
tcad room-verify --spec room.json --model room.glb --source room.dwg --mapping room-map.json
```

Strict verification checks current source SHA, re-extracts mapped CAD dimensions,
construction and openings, then reopens the model. `source_verified` and
`model_verified` report separate conclusions. Without source/mapping, only the
model/spec comparison is made. GLB uses versioned right-handed Y-up coordinates;
old unmarked Z-up room GLBs require regeneration. Missing height evidence remains
an error. Full architectural resize dependency solving is outside this release.

See [large-drawing measurements](large-drawing-performance.md) and
[installation](installation.md). The implementation follows the official
[uv tool workflow](https://docs.astral.sh/uv/guides/tools/) and uses existing
[ezdxf bounding APIs](https://ezdxf.readthedocs.io/en/stable/bbox.html) without
requiring an interactive CAD runtime.
