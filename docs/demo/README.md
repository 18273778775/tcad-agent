<!-- SPDX-FileCopyrightText: 2026 LiTao -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Controlled room acceptance demo

Download this directory and open [viewer.html](viewer.html) in a browser. The
viewer embeds the actual GLB triangles, CAD SVG previews, and saved verification
results. All rendering runs locally; there are no remote assets or dependencies.
GitHub displays HTML source instead of running the interactive viewer.

This is user-authored, controlled test data for one mapped, axis-aligned
rectangular room. It is not an independently sourced building project, an
arbitrary floor-plan recognition benchmark, or a construction compliance check.
Clear interior dimensions change from **6000 × 4500 × 3000 mm** to
**6400 × 4800 × 3200 mm**. Wall/slab geometry and through-wall door/window voids
are checked by reopening GLB, OBJ, and 3D DXF with a **0.01 mm** tolerance.
Individual components are closed solids; the assembly is not claimed to be a
single Boolean union.

## Published evidence

- [Acceptance summary](summary.json) and [55 CLI requests](commands.json)
- [Original DXF](fixtures/controlled-complete-room.dxf),
  [original DWG](fixtures/controlled-complete-room.dwg), and
  [revised DXF](revised-complete-room.dxf)
- [Semantic mapping](fixtures/room-map.json), [expected-value oracle](fixtures/expected-oracle.json),
  and [DWG generation provenance](fixtures/dwg-provenance.json)
- [Before spec](before-dwg.room.json), [after spec](after.room.json),
  [revision plan](plans/revise-length-width-height.json), and
  [byte/hash publication manifest](publication-manifest.json)
- Models: [before GLB](models/before.glb), [before OBJ](models/before.obj),
  [before 3D DXF](models/before.dxf), [after GLB](models/after.glb),
  [after OBJ](models/after.obj), [after 3D DXF](models/after.dxf)
- Raw previews: [before DWG](previews/before-dwg.svg),
  [before DXF](previews/before-dxf.svg), [after](previews/after.svg)

## Public-copy normalization

This is a sanitized public copy of a saved acceptance run. JSON keys and string
values, including converter diagnostics, use paths relative to this directory.
`<workspace>`, `<python>`, `<libredwg>`, `<temporary-file>`, and
`<temporary-dwg>`, and `<cad-cache-file>` replace local execution-environment paths. Command arguments
are historical records; substitute installed tools and a writable output
directory before replaying them. Numeric results, entity handles, status/error
codes, and all recorded original SHA-256 identities are retained.

The two source drawings, revised drawing, all six models, and three SVG previews
are copied byte for byte. JSON files change only by path normalization and the
explicit publication annotations in summary/command/provenance records. The
viewer is rebuilt against this public copy and validates GLB hashes, component
counts, and specification dimensions against the saved verification. The
manifest records both original and public-copy hashes for each copied file.
It excludes itself from its file hash list.

All 55 requests and their 110 response files are included. Negative-test fixture
paths and deliberately absent outputs remain historical references, not download
links. The missing-height and misleading-text drawings can be reconstructed
from the main fixture using the published negative-test plans. Temporary DWG
generation intermediates, converter/tool binaries, font binaries, and external
sample drawings are omitted. Their hashes and historical provenance remain in
the records where originally present. SVGs contain exported glyph outlines.

Regenerate from an existing private acceptance directory, at repository root:

```powershell
.venv\Scripts\python.exe scripts/prepare_public_demo.py --input artifacts/room-validation/acceptance-004 --output docs/demo
```

The output must be empty unless `--overwrite` is supplied. Existing unrecognized
files are rejected even with `--overwrite`; the helper never deletes files and
never modifies the source run. The original run is required for regeneration;
the public demo alone does not reproduce the private converter installation.

Copyright 2026 LiTao. The original TCAD code and controlled demo data are licensed
under [Apache-2.0](../../LICENSE). Third-party tools are not distributed here.
