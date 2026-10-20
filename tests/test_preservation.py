# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Regression cases discovered in independent CAD review."""

import math

import ezdxf
from ezdxf.acis import api as acis_api
from ezdxf.render import forms
import pytest

from agentcad.engine import apply_plan, measure_entities
from agentcad.errors import CadError
from agentcad.io import save_document


@pytest.mark.parametrize("source_version,target_version", [("R2010", "R2013"), ("R2013", "R2010")])
def test_existing_acis_version_change_rejected_without_losing_source(
    tmp_path, source_version, target_version
):
    source = tmp_path / "solid.dxf"
    output = tmp_path / "unsafe-conversion.dxf"
    drawing = ezdxf.new(source_version)
    acis_api.export_dxf(drawing.modelspace().add_3dsolid(), [acis_api.body_from_mesh(forms.cube())])
    drawing.saveas(source)
    before = source.read_bytes()
    assert len(ezdxf.readfile(source).modelspace()) == 1
    with pytest.raises(CadError) as error:
        apply_plan(
            {"version": 1, "dxf_version": target_version, "operations": []},
            output,
            input_path=source,
        )
    assert error.value.code == "UNSUPPORTED_VERSION_CHANGE"
    assert not output.exists()
    assert source.read_bytes() == before


def test_serialization_inventory_guard_rejects_dropped_acis(tmp_path):
    drawing = ezdxf.new("R2010")
    acis_api.export_dxf(drawing.modelspace().add_3dsolid(), [acis_api.body_from_mesh(forms.cube())])
    drawing.dxfversion = "R2013"  # Simulate an upstream serializer dropping incompatible content.
    output = tmp_path / "must-not-publish.dxf"
    with pytest.raises(CadError) as error:
        save_document(drawing, output)
    assert error.value.code == "SERIALIZATION_MISMATCH"
    assert not output.exists()


def test_imported_polyline_transform_overflow_does_not_publish(tmp_path):
    source = tmp_path / "polyline.dxf"
    output = tmp_path / "overflow.dxf"
    drawing = ezdxf.new("R2013")
    polyline = drawing.modelspace().add_polyline3d([(1e200, 0, 0), (2e200, 0, 0)])
    drawing.saveas(source)
    with pytest.raises(CadError) as error:
        apply_plan(
            {
                "version": 1,
                "operations": [
                    {"op": "transform", "handles": [polyline.dxf.handle], "scale": 1e200}
                ],
            },
            output,
            input_path=source,
        )
    assert error.value.code == "INVALID_PLAN"
    assert not output.exists()
    assert all(
        math.isfinite(vertex.dxf.location.x)
        for entity in ezdxf.readfile(source).modelspace()
        for vertex in entity.vertices
    )


def test_small_area_far_from_origin_is_translation_invariant(tmp_path):
    source = tmp_path / "large-coordinates.dxf"
    drawing = ezdxf.new("R2013")
    polyline = drawing.modelspace().add_lwpolyline(
        [(1e9, 1e9), (1e9 + 1, 1e9), (1e9 + 1, 1e9 + 1), (1e9, 1e9 + 1)], close=True
    )
    drawing.saveas(source)
    measured = measure_entities(source, [polyline.dxf.handle])["measurements"][0]
    assert measured["length"] == 4
    assert measured["area"] == 1


@pytest.mark.parametrize("kind", ["INSERT", "HATCH"])
def test_compound_geometry_transform_overflow_does_not_publish(tmp_path, kind):
    source = tmp_path / "compound.dxf"
    output = tmp_path / "overflow-compound.dxf"
    drawing = ezdxf.new("R2013")
    if kind == "INSERT":
        drawing.blocks.new("LABEL")
        entity = drawing.modelspace().add_blockref("LABEL", (0, 0))
        entity.add_attrib("TAG", "value", (1e300, 0))
    else:
        entity = drawing.modelspace().add_hatch()
        entity.paths.add_polyline_path([(1e300, 0), (1e300, 1), (0, 1)], is_closed=True)
    drawing.saveas(source)
    before = source.read_bytes()
    with pytest.raises(CadError) as error:
        apply_plan(
            {
                "version": 1,
                "operations": [{"op": "transform", "handles": [entity.dxf.handle], "scale": 1e20}],
            },
            output,
            input_path=source,
        )
    assert error.value.code == "INVALID_PLAN"
    assert not output.exists()
    assert source.read_bytes() == before
