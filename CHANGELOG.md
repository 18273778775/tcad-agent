# Changelog

## 0.4.0 — 2026-10-23

- Prepare SHA-verified worksets from related sheets or disjoint regions. Reuse
  them across analysis commands without decoding the original full drawing;
  retain WCS, units, root-handle mapping and selection limitations.
- Reuse the first unchanged document inside one read-only request. Refine a
  selected sheet with bbox and redraw all workset pages as SVG/PNG/HTML.
- Actually embed local raster images in SVG, including partial clipping and
  PNG/split thumbnails. Add bounded resource/font directories, font maps,
  per-selection quality reports and strict publication checks.
- Extend annotation evidence to MLEADER and explicitly selected polyline
  leaders; retain competitors and ambiguity margins. Support rotated straight
  axes and collinear open polylines, including saved spatial queries.
- Treat room models and Excel takeoff as agent acceptance workflows using CAD
  primitives, rather than missing general business engines.

See [0.4.0 release notes](docs/releases/0.4.0.md).

## 0.3.1 — 2026-10-23

- Fix the Windows pip installer selecting an unrelated global Python launcher
  instead of the current PATH interpreter. Resolve one absolute interpreter
  before creating the environment; allow an explicit `-Python` selection.
- Exercise the pip installer on every Windows CI Python version and verify the
  installed environment uses the matrix interpreter.
- Reuse a split drawing's own verified frame for single-file layer previews.
  Validate the neighboring manifest's DXF identity instead of applying original
  drawing handles to an exported file. Explicit bbox selections take priority.

See [0.3.1 release notes](docs/releases/0.3.1.md).

## 0.3.0 — 2026-10-22

- Discover/configure rectangular sheets and title evidence; split to independent
  DXF, clipped SVG, code-rendered PNG and an offline searchable HTML gallery.
  Read back geometry, referenced resources, headers and image identities before
  publishing. Preserve entities outside frames in a separate remainder artifact.
- Filter SVG by resolved nested leaf layers, preserve dimension layer context,
  redraw one/all manifest sheets and add bounded preview annotations/legends.
- Index conservative native world bounds using SQLite R-tree, including INSERTs;
  query block text/attributes with stable instance paths and world anchors.
- Add Shapely-based sum/union/overlap, explicit curve approximation and units,
  invalid-boundary diagnostics and optional recorded repairs. Extend measurement
  to HATCH islands, ellipses and closed splines; reject self-intersecting areas.
- Associate labels by containment, distance or LEADER evidence; report ties and
  unmatched labels. Extract finite orthogonal grids from explicit axis layers,
  bubbles and labels, with source-bound saved point/region queries.
- Retain bounded JSON responses, source revision checks and atomic outputs.
  Strict split readback defaults to an 1800-second worker budget; ordinary
  commands retain 60 seconds. All workers retain explicit memory limits.

See [release notes](docs/releases/0.3.0.md) for validation scope and remaining
font, external-content, converter and geometric limitations.

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
