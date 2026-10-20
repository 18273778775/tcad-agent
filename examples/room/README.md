# Controlled complete-room acceptance drawing

`controlled-complete-room.dxf` is an independently constructed acceptance fixture,
not an external building project. It contains a floor plan, south and north
elevations with section datums, door swing, window details, wall outlines,
dimension lines and millimetre labels, room name and sheet title.

The clear internal dimensions are **6000 × 4500 × 3000 mm**. Height means finished
floor to ceiling underside and comes from explicit elevation geometry. The walls
are 200 mm thick; the floor and ceiling slabs are 150 mm thick. Door D1 is on the
south wall, offset 1000 mm, width 900 mm, height 2100 mm and sill 0. Window W1 is
on the north wall, offset 2200 mm, width 1600 mm, height 1200 mm and sill 900 mm.

`room-map.json` explicitly supplies the meaning of the drawing layers. Exactly
one native geometric entity represents each footprint, height, construction and
opening role. `ROOM_CLEAR` is a closed rectangular footprint. Elevation and plan
association is supplied by the map; this workflow does not perform arbitrary
architectural recognition. `expected-oracle.json` records the independently
known fixture inputs and is never consumed by the extractor or model builder.

From the repository root in PowerShell:

```powershell
.venv\Scripts\python.exe -m agentcad capabilities
.venv\Scripts\python.exe -m agentcad schema
.venv\Scripts\python.exe -m agentcad room-extract examples/room/controlled-complete-room.dxf --mapping examples/room/room-map.json --output artifacts/room-spec.json
.venv\Scripts\python.exe -m agentcad room-model --spec artifacts/room-spec.json --output artifacts/room.glb
.venv\Scripts\python.exe -m agentcad room-verify --spec artifacts/room-spec.json --model artifacts/room.glb --tolerance-mm 0.01
```

To run the complete reproducible acceptance workflow, provision the pinned
workspace-local LibreDWG tools with `scripts/install-libredwg.ps1` and choose a
new evidence directory:

```powershell
.venv\Scripts\python.exe scripts/validate_room_workflow.py --output artifacts/room-validation/run-001
```

The workflow creates a genuine controlled DWG using the official `dwgadd` native
declaration language and checks its DWG-to-DXF roundtrip before using it. The
finite fixture helper is not a general DWG writer. `--dwg` can instead select an
existing controlled complete-room DWG conforming to the map. The source drawing
is preserved; DWG editing publishes a new DXF.

The acceptance run saves every public CLI JSON response, stderr, actual process
exit code and command argument vector. It compares DXF and DWG extraction,
measures 27 m² area and 21000 mm perimeter, verifies GLB/OBJ/3D DXF after reloading
their serialized geometry, and atomically revises the complete drawing to
6400 × 4800 × 3200 mm before repeating verification. Negative cases exercise
missing height, misleading height text, overwrite refusal, dry-run publication
and rollback after a valid operation followed by an invalid handle. The final
`summary.json` links to previews, models and the command evidence manifest.

The 3D output comprises separate closed rectangular components with actual
door/window voids. It does not claim a Boolean-fused building solid or general
DWG-to-BIM conversion.
