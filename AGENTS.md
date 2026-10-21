# Working on TCAD Agent CLI

The executable interface is `.\tcad.ps1` on Windows, or `tcad` from the project's
Python environment. Run `capabilities` and `schema` before constructing requests.
Do not launch the original third-party viewer distribution to perform CAD work.

## Using the CAD tool

- `info <drawing.dxf|drawing.dwg>`: units, layouts, layers, counts and approximate bounds.
- `query <drawing> --type CIRCLE --layer HOLES --limit 100`: paginated native handles.
- `measure <drawing> --handle <handle>`: supported exact geometry with explicit limitations.
- `apply --input <drawing> --plan <json> --output <new.dxf>`: atomic declarative edit.
- `render <drawing> --output <preview.svg>`: headless preview for visual inspection.
- `serve`: one `{id,command,args}` JSON request and response per line.

Inspect `ok` and `error.code`, not just process stdout. Handles are specific to the
current file. Query after importing DWG; conversion can change native handles.
Never infer a measurement from an approximate text extent or assume drawing units.
When `length_complete` is false, report the incomplete selection. Entity areas are
not a Boolean union. `--dry-run` validates changes before publication.

Default stdout is bounded to 32 KiB. Check `output.truncated`, record `truncation`
and `geometry_page` before treating detail as complete. Prefer `--fields` and
`--no-provenance`, follow `next_cursor` with unchanged filters/projection, and use
`export --output selected.jsonl` for complete supported native fields outside the
agent context. `--result-file` preserves the selected result/current page, not the
whole drawing. Batch reports preserve all executed child responses; inspect their
artifact instead of replaying mutations to recover omitted inline outcomes.

Use `source_sha256` for `--expected-source-sha256`; `source_revision` is a snapshot
token, not a cryptographic edit condition. Cooperating writers serialize commits.
CLI requests run in supervised workers (60 s / 1024 MiB defaults); budget failures
are structured, and a completed atomic commit may exist after cancellation.

Existing output requires explicit overwrite. Preserve the source unless the user
asks to replace it. Batch requests continue after an individual failure; a batch
is not atomic. DWG editing produces DXF output in this version.

## Mapped room modelling

Use `room-extract <drawing> --mapping <map.json> --output <spec.json>`, then
`room-model --spec <spec.json> --output <model.glb|obj|dxf>` and
`room-verify --spec <spec.json> --model <model> --tolerance-mm 0.01`.
Read `schema room-map` / `schema room-spec` before constructing inputs.
Height requires explicit elevation evidence; missing height is an error.
Room dimensions mean clear interior dimensions, not total assembly bounds.
This models axis-aligned rectangular rooms with explicit plan/elevation roles;
do not claim automatic recognition of arbitrary plans. See docs/room-workflow.md.

Add `--source drawing.dwg --mapping map.json` to `room-verify` for strict current
source re-extraction and model comparison. Without these flags, model/spec
consistency alone is verified. GLB uses the versioned standard Y-up convention;
legacy unmarked room GLBs require regeneration.

## Development

Source lives in `src/agentcad`. Install with `scripts/bootstrap.ps1`; use
`-WithDwg` for pinned workspace-local LibreDWG. It does not modify global PATH.
`scripts/install-libredwg.ps1 -IncludeTestFixture` also downloads a pinned upstream
fixture for the optional real-converter integration test.

Run `.venv\Scripts\python.exe -m pytest -q` and
`.venv\Scripts\ruff.exe check --select E4,E7,E9,F src tests`.
Stdout must contain only protocol JSON for normal operation commands. Keep
third-party diagnostics on stderr or in bounded response diagnostic fields.
Do not introduce eval, arbitrary script plans, shell=True, or GUI automation.
