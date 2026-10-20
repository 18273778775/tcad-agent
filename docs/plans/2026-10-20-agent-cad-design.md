# Agent CAD CLI design

## Evidence and scope

The workspace initially contains a binary CAD viewer distribution, not a source repository.
Static analysis determines its architecture and possible automation affordances. The new
implementation is independent, with no proprietary executable or DLL dependency.

The first deliverable is a headless 2D CAD CLI: inspect, bounded entity queries, geometric
measurement, transactional edits, SVG preview, and optional DWG-to-DXF conversion.
DXF is the editable interchange format. 3D solids and a constraint solver are outside this version.

## Architecture choices

1. Use ezdxf as a maintained DXF library and its SVG rendering backend. This has the
   smallest headless deployment and preserves drawing structures through its native model.
2. Keep LibreDWG conversion in an external process, with explicit capability
   discovery, timeout, temporary output, and validation. Conversion is not lossless by assumption.
   ODA is detection-only in this release.
3. LibreCAD and CadQuery are comparison projects: desktop-first 2D editing and 3D
   parametric CAD respectively. Neither is needed for this initial 2D agent interface.

## Agent contract

All normal commands emit one JSON envelope, with stable error codes and process exit
codes. `schema` exposes JSON Schemas; `capabilities` and `doctor` expose supported
features and missing optional tools. `serve` reads JSON Lines, preserves request IDs,
and emits exactly one envelope per input line. `batch` executes a bounded array in order.
Each editing plan is atomic; a batch as a whole is not a transaction.

Edits are declarative JSON, validated before mutation. No arbitrary Python, shell,
or embedded scripting is accepted. Handles refer to native DXF entity handles.
Writes go through a temporary file in the destination directory and are published
only after serialization succeeds. Overwrites require an explicit flag. Dry runs
perform validation and in-memory operations without writing an output.
Existing-file DXF version changes are rejected. Before publication, serialized
block/layout entity inventories are compared to detect silently discarded entities.

## Verification

Use actual DXF round trips, known analytic measurements, XML parsing of SVG output,
CLI subprocesses, malformed protocol requests, and transactional failure tests.
External DWG conversion must report a missing backend honestly; real conversion is
only considered verified when an installed converter processes a real DWG fixture.
