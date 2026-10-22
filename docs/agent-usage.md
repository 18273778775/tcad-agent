# Agent-safe CAD operation

TCAD 0.3.0 separates CAD work, complete result files and bounded agent-visible
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
Query defaults to native layout entities; explicit block text scopes are
instance-aware annotation records, not a recursive geometry export. See the
spatial workflow below.

Discover the supported entity fields before constructing a query or export:

```sh
tcad capabilities --detail full --fields entity_inspection
```

`entity_inspection.query_export_fields` is the sorted field inventory shared by
`query` and `export`. Full detail preserves this inventory within the response
budget; inspect `output.truncated` if requesting a smaller budget. Fields that do
not apply to an entity are absent. Optional unavailable annotation values can be
null with explicit warnings.

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

## Read annotation evidence without confusing it with geometry

```sh
tcad query building.dxf --type DIMENSION --fields handle,dimension_type,measurement,measure_kind,actual_measurement,text_override,dimlfac,defpoint2,defpoint3 --limit 20 --max-output-bytes 16384
tcad query building.dxf --type ATTRIB --fields handle,tag,text,plain_text,insert,height
tcad query building.dxf --type INSERT --fields handle,name,attributes,attribute_count,attribute_complete --limit 5
```

For a DIMENSION, `measurement` comes from its definition geometry without
`dimlfac` scaling. `measure_kind` identifies a drawing-unit length, an angle in
degrees, or an ordinate displacement vector in WCS. `actual_measurement` is the
optional stored group 42 value and can be stale. `text_override` is the user's
dimension text, which can contradict the geometry. `dimlfac` is the effective
linear style factor including entity overrides; it does not establish drawing
units. TCAD does not evaluate the final rendered dimension text. Keep these
fields separate when recording measurement evidence.

ATTRIB/ATTDEF expose `tag`, `text`, `plain_text`, `insert` and `height`. Text
filtering in the default layout scope indexes attributes placed directly in a
layout. Use `--text-scope block|all` for placed block text and attached attributes;
instance keys distinguish repeated definitions. Explicit `attributes` inspection returns at most
128 attached attributes with 1,024 characters per string. Check
`attribute_count`, `attribute_complete` and `truncation`. These attached-attribute
caps also apply to export; attribute index pagination is unavailable.

Legacy CAD encodings can contain undecodable bytes. TCAD exposes an affected
character as literal escape text, for example `\udca2`, instead of dropping it
or substituting a question mark. `text_encoding.encoding_complete=false` marks
this evidence. Diagnostics record affected field paths, original character
indices, escaped character indices, original codepoints and, where applicable,
byte escapes such as `\xa2`. At most four issues are listed; `issue_count`,
`issues_returned` and `issues_complete` describe the diagnostic coverage. An
original literal `\udca2` has no encoding issue by itself. Normal Chinese text
and source bytes remain unchanged. Treat escaped text as incomplete decoding,
not authoritative decoded annotation. User request JSON still rejects isolated
surrogates.

## Inspect an INSERT definition and its instance transform explicitly

```sh
tcad query building.dxf --type INSERT --fields handle,name,block_base_point,block_entity_count,matrix44,matrix44_convention,block_entities,block_complete --limit 1 --max-output-bytes 65536
```

`block_entities` requires explicit field selection; `--detail full` alone does
not inspect the definition's children. Records preserve native definition
handles and local coordinates. A child's WCS/OCS label refers to the block
definition's coordinate frame. Convert child OCS coordinates to definition
Cartesian coordinates before applying the matrix:

```text
[drawing_x, drawing_y, drawing_z, 1] = [definition_x, definition_y, definition_z, 1] @ matrix44
```

The row-vector matrix includes the block base point, scale, rotation, INSERT OCS
and translation. `insert` alone can therefore be insufficient for locating the
drawn geometry. For MINSERT, the matrix describes only the first grid cell.

Inspection returns the first 128 native children with at most 128 vertices per
child. It does not recursively inspect nested INSERTs or resolve external
references. `block_complete=false`, `block_omissions`, `block_omission_count`,
`block_omissions_complete`, child `geometry_page` and `truncation` identify
omissions. A response byte budget can further reduce `block_entities`; child
index pagination is unavailable. A complete returned native level does not
establish a complete building component or a Boolean union of its geometry.

## Keep complete data out of the model context

Default stdout budget is **32 KiB**, including UTF-8 and the final LF. Supported
explicit budgets are 4 KiB through 1 MiB. Summary mode bounds arrays, strings and
large mappings and reports `output.truncated`, omission paths and counts.
Entity/vertex continuation bookkeeping is never changed by a second truncation.

`--fields` chooses entity fields for query/export and dotted result fields for
other commands. `--no-provenance` removes source/evidence/conversion details.
These selections apply to explicit result files too. For `export`, entity field
selection applies inside JSONL; it is not reapplied to the receipt, which retains
the output path, entity count, bytes and SHA-256. Schemas are executable
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
blocks remain explicit limitations; attached-attribute and explicit block-child
caps still apply. Check their completeness metadata in the file. This is not
lossless DXF archival.

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
defaults to 600 seconds for downloads, and `split` to 1800 seconds for complete
export/readback. Override with `--timeout-seconds` and
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

## Split sheets without filling the agent context

```sh
tcad capabilities --detail full --fields sheet_workflow
tcad sheets building.dwg --output frames.json
tcad split building.dwg --output-dir sheets
```

The split operation creates named DXFs, path SVGs, PNG thumbnails, a complete
manifest and an offline HTML gallery in one new directory. Preparation reads
once and reuses frame/text/entity information across sheets; verification then
reopens each exported DXF and checks SVG/PNG artifacts. The stdout receipt is compact;
consult the manifest instead of asking for every entity in chat. Inspect naming
evidence, unassigned/unknown extents, cross-frame entities and render diagnostics.
SVGs clip to WCS frame regions; DXFs retain whole intersecting native entities
and referenced resources. Handles can change on export. Existing directories
are refused; choose a new destination. See [sheet workflow](sheet-workflow.md)
for overrides, limits and supported frame geometry.

## Reuse spatial selection with explicit instance identity

```sh
tcad index building.dxf --text-scope all --cache-dir .tcad-cache
tcad query building.dxf --bbox 0 0 1000 700 --layers AXIS COLUMN --fields handle,type,layer,bounds --limit 100 --cache-dir .tcad-cache
tcad query building.dxf --text-scope all --text KZ --limit 20 --cache-dir .tcad-cache
tcad export building.dxf --sheet sheet-001 --manifest sheets/manifest.json --output selected.jsonl
tcad measure building.dxf --sheet sheet-001 --manifest sheets/manifest.json --layers COLUMN --fields totals,units
```

`index` builds native bounds and a SQLite R-tree; block annotation indexing is
controlled by `--text-scope layout|block|all`. INSERT bounds use transformed
definition geometry rather than only insertion anchors. Unknown/partial bounds
remain diagnostics, not proof that an unbounded object lies outside a region.
Geometry selection is by conservative WCS bbox intersection, not destructive
clipping. An intersecting INSERT remains a native root handle for query/export.

The default layout scope returns native entities. The block scope returns placed
block annotations; all combines both. `entity_key`, `instance_path`,
`root_handle`, `world_bbox` and scope metadata distinguish repeated definition
handles. Treat annotation world positions as evidence; do not substitute a
definition handle for an editable layout instance.

`query`, `measure`, `export` and `render` accept `--sheet`/`--manifest`, `--bbox`,
`--layers` and `--exclude-layers`. Prefer a source-bound manifest and stable sheet
ID. Duplicate titles are ambiguous. A changed source/normalized DXF invalidates
the manifest. `render` also supports `--sheet all` and refuses simultaneous sheet
and bbox. Native query/measure/export layer filters select root entities;
render and spatial semantic commands can inspect effective block leaf layers.
Do not equate those different scopes without inspecting returned selection data.

SQLite selection and warm query pages avoid repeating the full native scan.
Geometry-producing commands still may parse DXF after selecting handles;
`render --sheet all` and `split` reuse one source document within their batch.
There is no promise that all commands share one resident parsed document or run
under one second. Caches expose their limits and source revision in receipts.
Pass `--expected-source-revision` to query/measure/export when a stale selection
must fail. This snapshot precondition is distinct from edit SHA preconditions.

## Render the needed layers once

```sh
tcad render building.dxf --sheet sheet-001 --manifest sheets/manifest.json --layers AXIS AXIS_LABEL COLUMN COLUMN_LABEL --foreground monochrome --output columns.svg
tcad render building.dxf --sheet sheet-001 --manifest sheets/manifest.json --layers AXIS AXIS_LABEL COLUMN COLUMN_LABEL --width 1600 --output columns.png
tcad render building.dxf --sheet all --manifest sheets/manifest.json --layers AXIS COLUMN --output-dir selected-previews
tcad render building.dxf --bbox 0 0 1000 700 --annotate areas --legend --max-annotations 100 --output reviewed.svg
```

Layer names are examples; inspect actual source layers first. Render whitelist
and exclusions apply to leaf entities, resolving nested layer-0 inheritance.
Exclusions take priority. A mixed INSERT does not force unrelated leaf layers
into the preview. Check drawn/excluded layer statistics and diagnostics,
especially dimension graphics, external content, missing glyphs and encoding.

The output suffix selects SVG or PNG. `--width` means PNG longest side, 16–4096
pixels, default 1600. A batch redraw creates SVG/PNG and `render-manifest.json`
in a new directory with one source document read. Rasterization normalizes
intrinsic SVG sizes before rendering and verifies actual PNG dimensions/hash.
It uses code-only resvg, not a browser. There is no PDF output.

`--annotate labels|areas|axes` and `--legend` add bounded SVG overlays without
editing CAD. Area labels are individual boundary areas, not union totals;
axis overlays depend on geometric grid evidence. Inspect annotation counts,
omissions and warnings. Original CAD labels and chosen overlays can coexist.

## Separate raw sums from planar union

```sh
tcad area building.dxf --layers COLUMN COLUMN_OUTLINE --mode union --by layer --flatten-tolerance 0.1 --output union-evidence.json
tcad area building.dxf --sheet sheet-001 --manifest sheets/manifest.json --mode overlap --linear-unit mm --output overlap-evidence.json
tcad area building.dxf --layers COLUMN --tolerance 0.5 --make-valid --output repaired-evidence.json
```

`area` accepts repeated `--handle`/`--type`, multiple `--layers`, a bbox or sheet,
`--mode union|sum|overlap`, and grouping `--by layer|block|sheet`. Block grouping
means root block instance, not collapsing every insertion of one definition.
WCS XY planar geometry is evaluated, with a local origin for large coordinates.
Closed supported polyline/circle/ellipse/spline boundaries and HATCH islands are
handled; open, invalid, nonplanar or unsupported entities are reported as rejected.
Check `area_complete`, traversal diagnostics and the rejected list before treating
the result as the complete requested selection.

`sum_area` counts repeated/overlapping boundaries separately. `union_area`
deduplicates accepted coverage. `overlap_area` is multiplicity excess
(`sum_area` minus unsnapped union), so triple coverage contributes twice its area;
it is not the unique region covered at least twice. Precision-grid changes are
reported separately, including `precision_area_delta`.

`--tolerance` is a nonnegative precision-grid size (default zero), not a drawing
scale or a certified error. Curves are deterministically flattened using
`--flatten-tolerance` in drawing units (default 0.1). Read `approximate`,
`flatten_rule_version`, `area_error_bound=null` and `area_error_bound_status`;
this release does not certify a global area-error bound. Invalid boundaries are
excluded by default. `--make-valid` explicitly enables GEOS repair and records
changes; it does not establish which repaired shape the designer intended.

Areas remain drawing-unit squared. Only explicit `--linear-unit mm|cm|m|in|ft`
adds m² conversions; INSUNITS or annotation guesses never silently rescale them.
`measure` remains a per-entity sum, now rejecting invalid self-intersections and
supporting additional planar HATCH/ELLIPSE/SPLINE boundaries with approximation
metadata. Inspect `length_complete`, `area_complete`, `invalid_area_handles` and
`incomplete_area_handles`; `ok=true` is not a completeness guarantee.

`--output` stores full supported evidence and union geometry as JSON. Inline
lists default to 100 items (`--evidence-limit`); pair evidence is bounded by
`--max-relations`. `relations_complete=false` means pair diagnostics are partial,
even when union totals cover all accepted boundaries. Artifacts have a 64 MiB
cap. This is geometric analysis, not automatic structural quantity certification.

## Associate labels and inspect orthogonal grids

```sh
tcad associate building.dxf --sheet sheet-001 --manifest sheets/manifest.json --label-layers COLUMN_LABEL --target-layers COLUMN --method inside --one-to-one --output associations.json --unmatched-output unresolved.json
tcad associate building.dxf --label-layers ROOM_NAME --target-layers ROOM_OUTLINE --method nearest --max-distance 500 --output nearest.json
tcad axes building.dxf --sheet sheet-001 --manifest sheets/manifest.json --line-layers AXIS --label-layers AXIS_LABEL --tolerance 1 --output axes.json
tcad axes building.dxf --axes axes.json --point 200 300
tcad axes building.dxf --axes axes.json --bbox 0 0 1000 700
```

`associate` uses actual label anchors and valid boundary geometry, with methods
`inside`, `nearest` or native `leader`. Configure `--label-types`,
`--target-types`, `--label-regex`, `--flatten-tolerance` and a positive maximum
distance when needed. Ties and one-to-one conflicts go to `ambiguous`; unmatched
labels and invalid targets remain explicit. Confidence describes geometric
uniqueness, not calibrated probability, OCR interpretation or construction intent.

`axes` supports orthogonal LINE axes with geometric endpoint/label evidence,
including supported block annotations. `--bubble-block-regex`, `--label-regex`,
`--max-distance`, `--max-axes` and `--max-intersections` bound extraction. Check
unmatched/ambiguous labels, warnings, `selection_complete` and
`intersections_complete`. No number is invented for an unresolved axis.
Saved `--axes` JSON must match the current source SHA and layout. Point queries
report nearest evidence and whether it lies on actual axis extents; proximity
alone does not mean a point is exactly at an engineering grid intersection.
Curved/skew axes and general structural semantics are outside this release.

## Why these dependencies

TCAD pins Shapely 2.1.2 for planar validation/Boolean operations and spatial
relations. Shapely delegates these operations to GEOS; this avoids introducing
a separate hand-written polygon Boolean engine. See the upstream
[Shapely manual](https://shapely.readthedocs.io/en/2.1.2/manual.html),
[union API](https://shapely.readthedocs.io/en/2.1.2/reference/shapely.unary_union.html)
and [repair API](https://shapely.readthedocs.io/en/2.1.2/reference/shapely.make_valid.html).

TCAD pins resvg_py 0.5.0 for deterministic static SVG-to-PNG conversion, with
system-font loading disabled for glyph-path previews. See upstream
[resvg-py](https://github.com/baseplate-admin/resvg-py) and
[resvg](https://github.com/linebender/resvg). TCAD validates inputs and verifies
PNG dimensions/hash around the renderer; upstream capability alone is not an
end-to-end drawing correctness guarantee. Dependency and bundled component
licenses are recorded in [third-party notices](../THIRD_PARTY_NOTICES.md).
