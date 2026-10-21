# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Preview filtering and limits must protect sources and published artifacts."""

import re
from xml.etree import ElementTree as ET

import ezdxf
from ezdxf.addons.drawing import Frontend
import pytest

from agentcad.errors import CadError
from agentcad.render import render_drawing


@pytest.fixture
def mixed_drawing(tmp_path):
    source = tmp_path / "mixed.dxf"
    doc = ezdxf.new("R2013", units=ezdxf.units.MM)
    doc.layers.new("TARGET")
    doc.layers.new("BACKGROUND")
    space = doc.modelspace()
    entities = [
        space.add_line((0, 0), (100, 0), dxfattribs={"layer": "TARGET"}),
        space.add_line((0, 0), (0, 100), dxfattribs={"layer": "TARGET"}),
        space.add_circle((50, 50), 10, dxfattribs={"layer": "TARGET"}),
        space.add_circle((50000, 50000), 100, dxfattribs={"layer": "BACKGROUND"}),
        space.add_line((60000, 0), (60000, 200), dxfattribs={"layer": "BACKGROUND"}),
    ]
    doc.saveas(source)
    return source, [entity.dxf.handle for entity in entities]


@pytest.mark.parametrize("selection,expected", [
    ({"types": ["line"]}, [0, 1, 4]),
    ({"layer": "TARGET"}, [0, 1, 2]),
    ({"handles": [1, 3]}, [1, 3]),
    ({"types": ["LINE"], "layer": "TARGET", "handles": [0, 2, 4]}, [0]),
])
def test_selection_is_applied_before_frontend_draws_each_entity(mixed_drawing, tmp_path,
                                                               monkeypatch, selection, expected):
    source, handles = mixed_drawing
    original = source.read_bytes()
    options = dict(selection)
    if "handles" in options:
        options["handles"] = [handles[index].lower() for index in options["handles"]]
    drawn = []
    draw_entity = Frontend.draw_entity

    def tracked(self, entity, properties):
        drawn.append(entity.dxf.handle)
        return draw_entity(self, entity, properties)

    monkeypatch.setattr(Frontend, "draw_entity", tracked)
    target = tmp_path / "selected.svg"
    result = render_drawing(source, target, max_entities=len(expected), **options)
    assert set(drawn) == {handles[index] for index in expected}
    assert len(drawn) == result["entity_count"] == len(expected)
    assert result["layout_entity_count"] == 5
    assert source.read_bytes() == original
    assert ET.parse(target).getroot().tag.endswith("svg")


@pytest.mark.parametrize("existing", [False, True])
def test_entity_limit_rejects_before_frontend_and_does_not_publish(mixed_drawing, tmp_path,
                                                                 monkeypatch, existing):
    source, _ = mixed_drawing
    original = source.read_bytes()
    target = tmp_path / "limited.svg"
    if existing:
        target.write_bytes(b"old preview must survive")

    def forbidden(*args, **kwargs):
        pytest.fail("Frontend must not draw an over-budget selection")

    monkeypatch.setattr(Frontend, "draw_layout", forbidden)
    with pytest.raises(CadError) as error:
        render_drawing(source, target, max_entities=2, overwrite=True)
    assert error.value.code == "RENDER_SELECTION_TOO_LARGE"
    assert error.value.details["selected"] == 5
    assert source.read_bytes() == original
    if existing:
        assert target.read_bytes() == b"old preview must survive"
    else:
        assert not target.exists()
    assert not list(tmp_path.glob(".tcad-*"))


def test_svg_byte_limit_does_not_overwrite_existing_preview_or_modify_source(tmp_path):
    source, target = tmp_path / "many-circles.dxf", tmp_path / "existing.svg"
    doc = ezdxf.new("R2013", units=ezdxf.units.MM)
    for index in range(50):
        doc.modelspace().add_circle((index * 10, index % 3 * 10), 3)
    doc.saveas(source)
    original = source.read_bytes()
    target.write_bytes(b"existing preview")
    with pytest.raises(CadError) as error:
        render_drawing(source, target, max_svg_bytes=1024, overwrite=True)
    assert error.value.code == "PREVIEW_TOO_LARGE"
    assert error.value.details["bytes"] > 1024
    assert target.read_bytes() == b"existing preview"
    assert source.read_bytes() == original
    assert not list(tmp_path.glob(".tcad-*"))


def rectangle_path_points(data):
    """Read the M/L polyline commands emitted for this rectangle fixture only."""
    tokens = re.findall(r"[MmLlZz]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", data)
    assert not re.sub(r"[MmLlZz\s,0-9.eE+\-]", "", data)
    points, cursor, first, index, command = [], (0.0, 0.0), None, 0, None
    while index < len(tokens):
        if tokens[index] in "MmLlZz":
            command, index = tokens[index], index + 1
            if command in "Zz":
                points.append(first)
                cursor = first
                continue
        assert command in "MmLl" and index + 1 < len(tokens)
        x, y = float(tokens[index]), float(tokens[index + 1])
        index += 2
        cursor = (cursor[0] + x, cursor[1] + y) if command.islower() else (x, y)
        points.append(cursor)
        if first is None:
            first = cursor
        command = "l" if command == "m" else "L" if command == "M" else command
    return points


def test_heavy_rectangle_stroke_envelope_remains_inside_svg_viewbox(tmp_path):
    source, target = tmp_path / "heavy.dxf", tmp_path / "heavy.svg"
    doc = ezdxf.new("R2013", units=ezdxf.units.MM)
    doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 100), (0, 100)],
                                  close=True, dxfattribs={"lineweight": 211})
    doc.saveas(source)
    original = source.read_bytes()
    render_drawing(source, target)
    root = ET.parse(target).getroot()
    x, y, width, height = map(float, root.attrib["viewBox"].split())
    style = "\n".join(node.text or "" for node in root.findall(".//{*}style"))
    widths = {}
    for name, body in re.findall(r"\.([\w-]+)\s*\{([^}]+)\}", style):
        stroke = re.search(r"stroke-width\s*:\s*([0-9.eE+\-]+)", body)
        if stroke:
            widths[name] = float(stroke.group(1))
    paths = root.findall(".//{*}path")
    assert paths and widths
    for path in paths:
        assert "transform" not in path.attrib
        points = rectangle_path_points(path.attrib["d"])
        half_stroke = max(widths[name] for name in path.attrib["class"].split()) / 2
        assert min(p[0] for p in points) - half_stroke >= x
        assert min(p[1] for p in points) - half_stroke >= y
        assert max(p[0] for p in points) + half_stroke <= x + width
        assert max(p[1] for p in points) + half_stroke <= y + height
    assert source.read_bytes() == original
