# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Geometry and transaction checks for the data-only DXF backend."""

import math
from pathlib import Path
import tempfile
import unittest

import ezdxf

from agentcad.engine import apply_plan, inspect_drawing, measure_entities, query_entities
from agentcad.errors import CadError
from agentcad.schema import validate_plan


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / "source.dxf"
        self.output = self.root / "output.dxf"

    def tearDown(self):
        self.directory.cleanup()

    def drawing(self):
        doc = ezdxf.new("R2013", units=4)
        return doc, doc.modelspace()

    def test_create_inspect_query_measure_exact(self):
        result = apply_plan(
            {
                "version": 1,
                "units": "mm",
                "operations": [
                    {"op": "layer", "name": "GEOMETRY", "color": 3},
                    {
                        "op": "add",
                        "entity": {
                            "type": "LINE",
                            "start": [0, 0, 0],
                            "end": [3, 4, 12],
                            "layer": "GEOMETRY",
                        },
                    },
                    {"op": "add", "entity": {"type": "CIRCLE", "center": [0, 0], "radius": 2}},
                    {
                        "op": "add",
                        "entity": {
                            "type": "ARC",
                            "center": [0, 0],
                            "radius": 2,
                            "start_angle": 350,
                            "end_angle": 10,
                        },
                    },
                    {
                        "op": "add",
                        "entity": {
                            "type": "RECTANGLE",
                            "insert": [1, 2, 7],
                            "width": 10,
                            "height": 5,
                        },
                    },
                    {
                        "op": "add",
                        "entity": {"type": "TEXT", "text": "room A", "insert": [1, 1], "height": 2},
                    },
                ],
            },
            self.output,
        )
        self.assertTrue(result["written"])
        handles = [
            operation["handle"] for operation in result["operations"] if operation["op"] == "add"
        ]
        measured = measure_entities(self.output, handles)["measurements"]
        self.assertEqual(measured[0]["length"], 13)
        self.assertAlmostEqual(measured[1]["length"], 4 * math.pi)
        self.assertAlmostEqual(measured[1]["area"], 4 * math.pi)
        self.assertAlmostEqual(measured[2]["length"], 2 * math.radians(20))
        self.assertIsNone(measured[2]["area"])
        self.assertEqual(measured[3]["length"], 30)
        self.assertEqual(measured[3]["area"], 50)
        self.assertFalse(measured[4]["supported"])
        inspected = inspect_drawing(self.output)
        self.assertEqual(inspected["entity_count"], 5)
        self.assertEqual(inspected["units"], {"name": "mm", "code": 4})
        self.assertEqual(
            query_entities(self.output, types=["line"], layer="GEOMETRY")["entities"][0]["handle"],
            handles[0],
        )
        self.assertEqual(query_entities(self.output, text="room")["total"], 1)
        self.assertEqual(query_entities(self.output, bbox=[100, 100, 101, 101])["total"], 0)
        self.assertEqual(query_entities(self.output, offset=2, limit=1)["total"], 5)

    def test_bulge_and_open_and_3d_semantics(self):
        doc, space = self.drawing()
        circle = space.add_lwpolyline([(1, 0, 1), (-1, 0, 1)], format="xyb", close=True)
        arc = space.add_lwpolyline([(1, 0, 1), (-1, 0, 0)], format="xyb")
        spatial = space.add_polyline3d([(0, 0, 0), (3, 4, 12)], close=True)
        doc.saveas(self.source)
        measured = measure_entities(self.source, [e.dxf.handle for e in (circle, arc, spatial)])[
            "measurements"
        ]
        self.assertAlmostEqual(measured[0]["length"], math.tau)
        self.assertAlmostEqual(measured[0]["area"], math.pi)
        self.assertAlmostEqual(measured[1]["length"], math.pi)
        self.assertIsNone(measured[1]["area"])
        self.assertEqual(measured[2]["length"], 26)
        self.assertIsNone(measured[2]["area"])
        self.assertIn("unsupported", measured[2]["area_kind"])

    def test_rollback_on_late_failure_and_source_is_unchanged(self):
        doc, space = self.drawing()
        line = space.add_line((0, 0), (1, 0))
        doc.saveas(self.source)
        original = self.source.read_bytes()
        self.output.write_bytes(b"existing artifact")
        with self.assertRaises(CadError) as context:
            apply_plan(
                {
                    "version": 1,
                    "operations": [
                        {"op": "delete", "handles": [line.dxf.handle]},
                        {"op": "delete", "handles": ["FFFFFF"]},
                    ],
                },
                self.output,
                input_path=self.source,
                overwrite=True,
            )
        self.assertEqual(context.exception.code, "ENTITY_NOT_FOUND")
        self.assertEqual(context.exception.details["operation_index"], 1)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.output.read_bytes(), b"existing artifact")

    def test_dry_run_evaluates_handles_and_writes_nothing(self):
        doc, space = self.drawing()
        line = space.add_line((0, 0), (1, 0))
        doc.saveas(self.source)
        result = apply_plan(
            {"version": 1, "operations": [{"op": "delete", "handles": [line.dxf.handle]}]},
            self.output,
            input_path=self.source,
            dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["written"])
        self.assertEqual(result["entity_count"], 0)
        self.assertFalse(self.output.exists())
        with self.assertRaises(CadError):
            apply_plan(
                {"version": 1, "operations": [{"op": "delete", "handles": ["FFFFFF"]}]},
                self.output,
                input_path=self.source,
                dry_run=True,
            )

    def test_transform_order_and_text_update(self):
        doc, space = self.drawing()
        line = space.add_line((1, 0), (2, 0))
        text = space.add_mtext("old", dxfattribs={"insert": (0, 0), "char_height": 1})
        doc.saveas(self.source)
        result = apply_plan(
            {
                "version": 1,
                "operations": [
                    {
                        "op": "transform",
                        "handles": [line.dxf.handle],
                        "scale": 2,
                        "rotate": 90,
                        "translate": [10, 0],
                    },
                    {
                        "op": "update",
                        "handles": [text.dxf.handle],
                        "attributes": {"text": "new", "layer": "LABELS", "color": 5},
                    },
                ],
            },
            self.output,
            input_path=self.source,
        )
        self.assertEqual(result["summary"]["transformed"], 1)
        records = query_entities(self.output, handles=[line.dxf.handle, text.dxf.handle])[
            "entities"
        ]
        self.assertAlmostEqual(records[0]["start"][0], 10)
        self.assertAlmostEqual(records[0]["start"][1], 2)
        self.assertAlmostEqual(records[0]["end"][1], 4)
        self.assertEqual(records[1]["text"], "new")
        self.assertEqual(records[1]["layer"], "LABELS")

    def test_schema_rejects_executable_unknown_and_nonfinite_values(self):
        bad = [
            {"version": True, "operations": []},
            {"version": 1.0, "operations": []},
            {"version": 1, "operations": [], "python": "print('hi')"},
            {"version": 1, "operations": [{"op": "execute", "code": "print('hi')"}]},
            {
                "version": 1,
                "operations": [
                    {"op": "add", "entity": {"type": "CIRCLE", "center": [0, True], "radius": 1}}
                ],
            },
            {
                "version": 1,
                "operations": [
                    {
                        "op": "add",
                        "entity": {"type": "CIRCLE", "center": [0, 0], "radius": float("nan")},
                    }
                ],
            },
            {
                "version": 1,
                "operations": [
                    {
                        "op": "add",
                        "entity": {"type": "CIRCLE", "center": [0, 0], "radius": 10**1000},
                    }
                ],
            },
            {
                "version": 1,
                "operations": [
                    {"op": "add", "entity": {"type": "CIRCLE", "center": [0, 0], "radius": -1}}
                ],
            },
            {
                "version": 1,
                "operations": [{"op": "update", "handles": ["a", "A"], "attributes": {"color": 1}}],
            },
        ]
        for plan in bad:
            with self.subTest(plan=plan), self.assertRaises(CadError) as context:
                validate_plan(plan)
            self.assertEqual(context.exception.code, "INVALID_PLAN")
        validate_plan({"version": 1, "operations": []})

    def test_no_overwrite_without_explicit_flag(self):
        self.output.write_text("do not replace")
        with self.assertRaises(CadError) as context:
            apply_plan({"version": 1, "operations": []}, self.output)
        self.assertEqual(context.exception.code, "OUTPUT_EXISTS")
        self.assertEqual(self.output.read_text(), "do not replace")


if __name__ == "__main__":
    unittest.main()
