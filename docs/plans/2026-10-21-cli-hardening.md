# Agent CLI hardening implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make TCAD installable in one command, correct the reviewed defects, and bound large-drawing work and agent-visible output with reproducible evidence.

**Architecture:** Keep ezdxf as the editable CAD model. Use revision-bound, bounded SQLite read snapshots to amortize cold parsing across independent CLI pages. Separate selected complete result artifacts from compact JSON stdout; run CLI operations in supervised workers with deadlines and aggregate memory limits. Preserve atomic writes and add optimistic version checks under destination locks. Apply coordinate adapters at format boundaries and verify source/model identity independently.

**Tech Stack:** Python 3.11+, ezdxf, SQLite, jsonschema, trimesh, psutil, uv/pipx packaging, GitHub Actions. Apache-2.0; third-party licenses remain unchanged.

## Decisions and alternatives

- Re-reading DXF for every offset page is simple but retains O(pages × file parse/scan). A process-only document cache helps `serve` but not independent CLI calls. Choose persistent indexed read snapshots, bounded on disk and invalidated when the source revision changes.
- Pure streaming reads save memory but cannot preserve every DXF structure for edits; retain the document model for edits and add explicit process/file/resource budgets. Do not claim unbounded or arbitrary-input constant memory.
- Do not print arbitrarily large JSON and merely warn. Default to 32 KiB response budgets, compact query records, explicit field selection and truncation metadata. Preserve full selected results in an explicitly requested artifact, with its own size bound and hash.
- Use actual GitHub release sources with uv/pipx rather than documenting an unpublished PyPI package. Add user-level DWG setup independent of a repository checkout.

## Task 1: Query scaling and basic geometry correctness

**Owner/files:** engine worker: `src/agentcad/engine.py`, query cache module, `tests/test_query_scaling.py`, `scripts/benchmark_large_drawing.py`.

1. Reproduce repeated parsing, elevated POLYLINE regression and hidden layer color behavior.
2. Build bounded indexed snapshots with version/filter-bound cursors; move projection ahead of expensive geometry and bounds work.
3. Ensure page byte budgets advance cursors by returned entities, with explicit geometry omissions and no skipped records.
4. Add targeted tests for cold/warm parsing counts, filters, stale cursors, bounded geometry and original regressions.
5. Generate >=100k entities and >=20 MiB actual DXF data; measure cold/warm latency, total traversal, peak RSS and response bytes in isolated processes.

## Task 2: Model interoperability and source verification

**Owner/files:** room worker: `room.py`, `room_model.py`, room tests, `scripts/build_room_viewer.py`.

1. Add independent glTF axis assertions; export Y-up with a versioned coordinate convention and normalize during verification/viewing.
2. Reject inward solid orientation and cover negative/mirror transforms.
3. Add strict source+mapping verification distinct from ordinary spec/model consistency.
4. Implement local mapped room resizing only if unrelated entities can be preserved and verified; never use fixture-wide reconstruction as its proof.

## Task 3: Installation and release engineering

**Owner/files:** install worker: packaging metadata/lock, install/bootstrap scripts, package DWG installer, README/installation docs, distribution tests, `.github/workflows/`.

1. Provide uv/pipx one-line installation without a checkout and user-level shims/install scripts.
2. Package an explicit pinned/hash-verified DWG setup module; retain upstream COPYING and user-level discovery.
3. Bump to 0.2.0, validate a clean wheel/tool installation and add platform/Python/build CI; distinguish required DWG tests from skipped optional runs.

## Task 4: Bounded agent interface and write integrity

**Owner/files:** parent: `protocol.py`, `cli.py`, new response/resource modules, `io.py`, `converters.py`, `render.py`, corresponding new tests.

1. Fix integer-valued numeric request IDs and bound schema diagnostics.
2. Add `--fields`, `--detail`, `--no-provenance`, `--max-output-bytes` and `--result-file`; enforce the final JSON byte limit and preserve explicit omission metadata.
3. Supervise CLI/JSONL work in owned subprocesses with default time/memory budgets; bound external diagnostics and large input/artifact handling.
4. Add version preconditions under per-target write locks, returning explicit revision conflicts.
5. Add render type/layer/entity selection, maximum selected entities, output size limits and stroke-safe viewport margins.
6. Connect cache, installer and strict room APIs; update CLI examples and acceptance scripts for compact defaults.

## Task 5: Integration and acceptance

1. Run focused regression suites, then full pytest/Ruff/build once integration stabilizes.
2. Run 100k/20 MiB baseline and publish actual results/limits, not an unqualified scalability claim.
3. Re-run complete DWG/DXF room acceptance in a new evidence directory, regenerate a public sanitized demo and check model/preview hashes and links.
4. Test the final fixed-release one-line install in an isolated tool directory and check installed `tcad` from outside the checkout.
5. Commit, publish the tested source/release tag, verify remote installation and report material remaining scope limits. PyPI publication requires actual registry setup; do not claim it occurred.

## Acceptance requirements

- Warm cursor pages do not reparse the DXF or regenerate all entity records.
- Source revisions or cursor/filter mismatches cannot silently mix pages.
- Normal JSON stdout never exceeds the requested byte budget; omissions are explicit and pagination does not skip omitted entities.
- Timeout/memory/output limit failures are structured; sources are preserved and partial destination files are never published. Cancellation can race a completed atomic commit, so callers inspect the output revision before retrying. JSONL continues after a failed operation.
- Concurrent guarded overwrites cannot both succeed from the same stale revision.
- Standard glTF height/up axes, model normals and strict source/model checks pass targeted negative tests.
- The advertised install command resolves a real published version and works without a repository-local virtual environment.

## Verified checkpoint

- Full regression: 204 passed, one optional distribution test skipped, nine subtests passed; real LibreDWG integration executed. Ruff passed for source, tests and scripts.
- Distribution checks separately enabled: 11 passed, including building and installing the current wheel outside the checkout; final wheel/source module hashes checked.
- The recorded 100k / 26.32 MiB benchmark passes its gates. Real fresh-CLI warm pagination is 1.248 s median; core-query median is 17.48 ms.
- `acceptance-004` completed 55 CLI calls and six strict current-source model verifications. Public-copy validation checked 136 artifact hashes and retained 137 files without private execution paths.
- Universal architectural resize dependency solving is not implemented. Explicit local supported transforms preserve unrelated drawing entities; the controlled fixture resize is not proof of general renovation editing.
