# Changelog

## 0.2.0 — 2026-10-21

- Install the pinned GitHub version with `uv tool install` or pipx; user installers
  support Python/pip and optional verified LibreDWG provisioning. PyPI publication
  remains disabled until Trusted Publishing is configured.
- Cache native query selections in bounded SQLite snapshots. Cursor pages reuse
  prepared records across CLI processes, with native layer/type filtering before
  geometry formatting. Source changes invalidate cursors.
- Default JSON responses to a 32 KiB budget, with field projection, provenance
  suppression, vertex pagination, explicit omission metadata and file artifacts.
  Stream complete selected entity records to bounded JSONL files with `export`.
- Supervise CLI requests for time, sampled process-tree RSS and diagnostic/output
  limits. Bound preview work and ordered batch reports; preserve completed batch
  outcomes on interruption.
- Protect cooperating writers with per-target locks and optional SHA-256
  preconditions. Preserve hidden layers, elevated polylines and unrelated room
  entities during supported edits.
- Export standard Y-up glTF models with outward component faces, and optionally
  verify model/specification evidence against the current source drawing and map.
  Regenerate legacy unmarked GLB models before using the new verifier.
- Publish a 100,000-entity / 26.32 MiB benchmark and a controlled room acceptance
  run covering 55 CLI calls, six reopened models and explicit source verification.

First-time DXF parsing and preparation of a new query projection still require a
full document read. Room modelling requires explicit mapping and rectangular,
axis-aligned geometry. DWG editing continues to produce DXF output.
