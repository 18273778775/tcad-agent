# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Reproduce controlled full-room CLI acceptance and save every JSON response.

Run with the project's Python, e.g. .venv/Scripts/python.exe
scripts/validate_room_workflow.py --output artifacts/room-validation/run-001.
Only fixture construction uses ezdxf; all CAD inspection, edits, extraction,
rendering, modelling and serialized-model verification use public CLI calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from build_room_fixture import create_room_drawing, geometry_specs, room_mapping


PROJECT = Path(__file__).resolve().parents[1]


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class EvidenceCLI:
    """Fail closed on return codes, malformed/extra stdout, or protocol errors."""

    def __init__(self, output, converter=None):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.records = []
        self.environment = os.environ.copy()
        self.environment["PYTHONPATH"] = str(PROJECT / "src")
        self.environment["PYTHONIOENCODING"] = "utf-8"
        if converter:
            self.environment["TCAD_DWG2DXF"] = str(Path(converter).resolve())

    def call(self, name, *arguments, error=None):
        # Acceptance saves complete evidence to disk. Agent-facing defaults stay
        # compact; this reproducibility harness explicitly opts into more bytes.
        command = [sys.executable, "-m", "agentcad", *map(str, arguments),
                   "--detail", "full", "--max-output-bytes", "1048576"]
        completed = subprocess.run(
            command, cwd=PROJECT, env=self.environment, capture_output=True, text=True,
            encoding="utf-8", timeout=120, stdin=subprocess.DEVNULL, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        prefix = f"{len(self.records) + 1:03d}-{name}"
        response_file = self.output / f"{prefix}.stdout.json"
        response_file.write_text(completed.stdout, encoding="utf-8")
        stderr_file = self.output / f"{prefix}.stderr.txt"
        stderr_file.write_text(completed.stderr, encoding="utf-8")
        record = {"name": name, "command": command, "cwd": str(PROJECT),
                  "returncode": completed.returncode, "expected_error_code": error,
                  "stdout": str(response_file.resolve()), "stderr": str(stderr_file.resolve())}
        self.records.append(record)
        save_json(self.output.parent / "commands.json", {
            "python": sys.executable,
            "converter": self.environment.get("TCAD_DWG2DXF"), "commands": self.records,
        })
        response = json.loads(completed.stdout)
        assert isinstance(response, dict) and type(response.get("ok")) is bool, record
        if error:
            assert completed.returncode != 0 and not response["ok"], (record, response)
            assert response["error"]["code"] == error, (record, response)
            return response["error"]
        assert completed.returncode == 0 and response["ok"], (record, response)
        return response["result"]

    def query_all(self, name, drawing):
        entities, offset = [], 0
        while True:
            page = self.call(f"{name}-page-{offset}", "query", drawing,
                             "--offset", offset, "--limit", 25)
            entities.extend(page["entities"])
            offset += len(page["entities"])
            if offset >= page["total"]:
                return entities
            assert page["entities"], "Pagination made no progress."


def require_close(actual, expected, tolerance=0.01):
    assert abs(float(actual) - float(expected)) <= tolerance, (actual, expected)


def compare_spec(spec, oracle):
    for axis, expected in oracle["dimensions"].items():
        require_close(spec["dimensions"][axis], expected)
    for key, expected in oracle["construction"].items():
        require_close(spec["construction"][key], expected)
    actual_openings = {item["id"]: item for item in spec["openings"]}
    assert set(actual_openings) == {"D1", "W1"}
    for expected in oracle["openings"]:
        actual = actual_openings[expected["id"]]
        for key in ("kind", "wall"):
            assert actual[key] == expected[key], (actual, expected)
        for key in ("offset", "width", "height", "sill"):
            require_close(actual[key], expected[key])


def measured_footprint(cli, name, drawing, oracle, entities):
    query = cli.call(f"{name}-footprint-query", "query", drawing,
                     "--layer", "ROOM_CLEAR", "--limit", 10)
    assert query["total"] == 1
    measure = cli.call(f"{name}-footprint-measure", "measure", drawing,
                       "--handle", query["entities"][0]["handle"])
    assert measure["totals"]["length_complete"] is True
    require_close(measure["totals"]["length"], oracle["clear_perimeter_mm"])
    require_close(measure["totals"]["area"], oracle["clear_area_mm2"])
    points = query["entities"][0]["points"]
    native_dimensions = {
        "length": max(point[0] for point in points) - min(point[0] for point in points),
        "width": max(point[1] for point in points) - min(point[1] for point in points),
    }
    roles = {"ROOM_HEIGHT": "height", "WALL_THICKNESS": "wall_thickness",
             "FLOOR_THICKNESS": "floor_thickness", "CEILING_THICKNESS": "ceiling_thickness",
             "DOOR_PLAN": "door_plan_width", "WINDOW_PLAN": "window_plan_width"}
    line_entities = [item for item in entities if item["layer"] in roles]
    assert len(line_entities) == len(roles)
    arguments = ["measure", drawing]
    for entity in line_entities:
        arguments.extend(["--handle", entity["handle"]])
    lines = cli.call(f"{name}-geometric-dimensions-measure", *arguments)
    assert lines["totals"]["length_complete"] is True
    line_dimensions = {roles[item["layer"]]: item["length"] for item in lines["measurements"]}
    native_dimensions["height"] = line_dimensions["height"]
    for axis, value in native_dimensions.items():
        require_close(value, oracle["dimensions"][axis])
    for key, expected in oracle["construction"].items():
        require_close(line_dimensions[key], expected)
    require_close(line_dimensions["door_plan_width"], oracle["openings"][0]["width"])
    require_close(line_dimensions["window_plan_width"], oracle["openings"][1]["width"])
    return {"area_mm2": measure["totals"]["area"],
            "area_m2": measure["totals"]["area"] / 1_000_000,
            "perimeter_mm": measure["totals"]["length"], "complete": True,
            "native_dimensions_mm": native_dimensions,
            "supporting_line_measurements_mm": line_dimensions}


def build_and_verify_models(cli, name, spec_file, models, source_drawing, mapping):
    models.mkdir(parents=True, exist_ok=True)
    result = {}
    for extension in ("glb", "obj", "dxf"):
        target = models / f"{name}.{extension}"
        cli.call(f"{name}-model-{extension}", "room-model", "--spec", spec_file,
                 "--output", target)
        verification = cli.call(f"{name}-verify-{extension}", "room-verify", "--spec",
                                spec_file, "--model", target, "--tolerance-mm", "0.01",
                                "--source", source_drawing, "--mapping", mapping)
        assert verification["passed"] and verification["verified"], verification
        assert verification["source_verified"] and verification["model_verified"]
        for axis in ("length", "width", "height"):
            assert verification["dimensions"][axis]["absolute_error_mm"] <= 0.01
        result[extension] = {"output": str(target.resolve()), "sha256": sha256(target),
                             "verification": verification}
    return result


def run_workflow(output, dwg=None, converter=None):
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Use a new empty evidence directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    fixtures = output / "fixtures"
    fixtures.mkdir()
    plans = output / "plans"
    plans.mkdir()
    previews = output / "previews"
    previews.mkdir()
    converter = converter or os.environ.get("TCAD_DWG2DXF")
    local_converter = PROJECT / ".tools" / "libredwg-0.14" / "dwg2dxf.exe"
    if not converter and local_converter.is_file():
        converter = local_converter
    cli = EvidenceCLI(output / "responses", converter)
    # Discover and save the exact running interface before constructing requests.
    cli.call("capabilities", "capabilities")
    cli.call("schema", "schema")
    cli.call("doctor", "doctor")
    source = fixtures / "controlled-complete-room.dxf"
    factory_result = create_room_drawing(source)
    oracle = factory_result["oracle"]
    save_json(fixtures / "expected-oracle.json", oracle)
    mapping = fixtures / "room-map.json"
    save_json(mapping, room_mapping())
    if dwg:
        dwg = Path(dwg).resolve()
        provenance = {"kind": "caller-supplied controlled complete-room fixture", "path": str(dwg)}
    else:
        from room_dwg_fixture import create_dwg_from_specs
        dwg = fixtures / "controlled-complete-room.dwg"
        tool_directory = Path(converter).parent if converter else None
        provenance = create_dwg_from_specs(geometry_specs(), dwg, tool_directory=tool_directory)
    save_json(fixtures / "dwg-provenance.json", provenance)
    source_hashes = {str(path): sha256(path) for path in (source, dwg)}
    before_specs = {}
    before_measurements = {}
    for name, drawing in (("dxf", source), ("dwg", dwg)):
        cli.call(f"{name}-info", "info", drawing)
        entities = cli.query_all(f"{name}-all", drawing)
        assert entities and {e["layer"] for e in entities} >= set(
            item["layer"] for item in room_mapping()["roles"].values())
        before_measurements[name] = measured_footprint(cli, name, drawing, oracle, entities)
        spec_file = output / f"before-{name}.room.json"
        extracted = cli.call(f"{name}-extract", "room-extract", drawing, "--mapping", mapping,
                             "--output", spec_file)
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        assert extracted["spec"] == spec
        compare_spec(spec, oracle)
        before_specs[name] = spec
        cli.call(f"{name}-render", "render", drawing,
                 "--output", previews / f"before-{name}.svg")
    # Compare extracted semantics; native handles and provenance legitimately differ.
    for key in ("dimensions", "construction"):
        assert before_specs["dxf"][key] == before_specs["dwg"][key]
    for left, right in zip(before_specs["dxf"]["openings"], before_specs["dwg"]["openings"]):
        for key in ("id", "kind", "wall", "offset", "width", "height", "sill"):
            assert left[key] == right[key]
    before_models = build_and_verify_models(cli, "before", output / "before-dwg.room.json",
                                            output / "models", dwg, mapping)

    # Revise a DWG through public CLI import/apply, publishing a new DXF atomically.
    entities = cli.query_all("revision-source", dwg)
    handles = [item["handle"] for item in entities]
    dimensions = before_specs["dwg"]["dimensions"]
    revised_dimensions = {axis: dimensions[axis] + delta
                          for axis, delta in (("length", 400), ("width", 300), ("height", 200))}
    new_geometry = geometry_specs(**revised_dimensions)
    plan = {"version": 1, "units": "mm", "operations": [
        {"op": "delete", "handles": handles},
        *({"op": "add", "entity": item} for item in new_geometry),
    ]}
    revision_plan = plans / "revise-length-width-height.json"
    save_json(revision_plan, plan)
    revised = output / "revised-complete-room.dxf"
    dry_run = cli.call("revision-dry-run", "apply", "--input", dwg, "--plan", revision_plan,
                       "--output", revised, "--dry-run")
    assert dry_run["dry_run"] and not revised.exists()
    cli.call("revision-apply", "apply", "--input", dwg, "--plan", revision_plan, "--output", revised)
    revised_hash = sha256(revised)
    cli.call("revision-no-overwrite", "apply", "--input", dwg, "--plan", revision_plan,
             "--output", revised, error="OUTPUT_EXISTS")
    assert sha256(revised) == revised_hash
    after_spec_file = output / "after.room.json"
    cli.call("after-extract", "room-extract", revised, "--mapping", mapping,
             "--output", after_spec_file)
    after_spec = json.loads(after_spec_file.read_text(encoding="utf-8"))
    revised_oracle = {**oracle, "dimensions": revised_dimensions,
                      "clear_area_mm2": revised_dimensions["length"] * revised_dimensions["width"],
                      "clear_perimeter_mm": 2 * (revised_dimensions["length"] + revised_dimensions["width"])}
    compare_spec(after_spec, revised_oracle)
    after_entities = cli.query_all("after-all", revised)
    after_measure = measured_footprint(cli, "after", revised, revised_oracle, after_entities)
    cli.call("after-info", "info", revised)
    cli.call("after-render", "render", revised, "--output", previews / "after.svg")
    after_models = build_and_verify_models(cli, "after", after_spec_file, output / "models", revised, mapping)

    # Negative evidence: a late invalid handle must roll back an earlier valid op.
    rollback_plan = plans / "rollback-invalid-handle.json"
    save_json(rollback_plan, {"version": 1, "operations": [
        {"op": "add", "entity": {"type": "LINE", "start": [1, 1], "end": [2, 2]}},
        {"op": "delete", "handles": ["FFFFFFFFFFFFFFFF"]},
    ]})
    rollback_output = output / "must-not-exist.dxf"
    rollback = cli.call("rollback", "apply", "--input", dwg, "--plan", rollback_plan,
                        "--output", rollback_output, error="ENTITY_NOT_FOUND")
    assert rollback["details"]["operation_index"] == 1 and not rollback_output.exists()

    for case, layer, operation, expected_error in (
        ("missing-height", "ROOM_HEIGHT", "delete", "ROOM_EVIDENCE_MISSING"),
        ("misleading-height-text", "DIM_HEIGHT", "update", "ROOM_DIMENSION_MISMATCH"),
    ):
        selector = cli.call(f"{case}-query", "query", source, "--layer", layer)
        assert selector["total"] == 1
        change = {"op": operation, "handles": [selector["entities"][0]["handle"]]}
        if operation == "update":
            change["attributes"] = {"text": "9000 mm"}
        negative_plan = plans / f"{case}.json"
        save_json(negative_plan, {"version": 1, "operations": [change]})
        negative_drawing = fixtures / f"{case}.dxf"
        cli.call(f"{case}-apply", "apply", "--input", source, "--plan", negative_plan,
                 "--output", negative_drawing)
        negative_spec = output / f"{case}.room.json"
        cli.call(f"{case}-extract", "room-extract", negative_drawing, "--mapping", mapping,
                 "--output", negative_spec, error=expected_error)
        assert not negative_spec.exists()

    assert all(sha256(path) == digest for path, digest in source_hashes.items())
    summary = {
        "passed": True,
        "fixture_classification": oracle["provenance"],
        "source_sha256": source_hashes,
        "source_preserved": True,
        "before": {"dimensions_mm": before_specs["dwg"]["dimensions"],
                   "footprint": before_measurements, "models": before_models},
        "after": {"dimensions_mm": after_spec["dimensions"], "footprint": after_measure,
                  "drawing": str(revised), "models": after_models},
        "dwg_vs_dxf_semantics_match": True,
        "checks": {"exact_area_perimeter": True, "geometry_text_cross_validation": True,
                   "all_serialized_models_verified": True, "atomic_rollback": True,
                    "all_models_verified_against_current_source": True,
                   "dry_run_no_output": True, "explicit_no_overwrite": True,
                   "missing_height_rejected": True, "misleading_text_rejected": True},
        "height_basis": oracle["height_evidence"],
        "evidence_manifest": str(output / "commands.json"),
    }
    save_json(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(PROJECT / "artifacts" / "room-validation"))
    parser.add_argument("--dwg", help="Existing controlled complete-room DWG conforming to room-map.json")
    parser.add_argument("--converter", help="Official workspace-local dwg2dxf executable")
    args = parser.parse_args()
    result = run_workflow(args.output, args.dwg, args.converter)
    print(json.dumps({"passed": result["passed"], "summary": str(Path(args.output).resolve() / "summary.json")}, indent=2))


if __name__ == "__main__":
    main()
