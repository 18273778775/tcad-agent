# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Native annotation evidence is distinct from geometry and bounded explicitly."""
import json
from unittest.mock import patch

import ezdxf
import pytest

from agentcad.engine import _record, query_entities, export_entities
from agentcad import query_cache


@pytest.fixture
def annotations(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    doc = ezdxf.new("R2013", units=4)
    space = doc.modelspace()
    style = doc.dimstyles.new("TAKEOFF", dxfattribs={"dimlfac": 2.0})
    linear = space.add_linear_dim(
        base=(0, 100), p1=(0, 0), p2=(1200, 0), dimstyle=style.dxf.name,
        override={"dimlfac": 0.001}, text="USER 9.5",
    )
    linear.commit()
    linear.dimension.dxf.actual_measurement = 777
    doc.blocks.new("CAP").add_attdef("BLOCK_ONLY", text="BLOCK SECRET", insert=(0, 0))
    insert = space.add_blockref("CAP", (10, 20))
    insert.add_attrib("CAP_TYPE", "CT5", insert=(11, 21))
    attrib = space.new_entity("ATTRIB", {"tag": "TYPE", "text": "CT2", "insert": (1, 2), "height": 2})
    attdef = space.add_attdef("DEFAULT_TYPE", text="CT3", insert=(3, 4), height=3)
    source = tmp_path / "annotations.dxf"
    doc.saveas(source)
    return source, linear.dimension.dxf.handle, insert.dxf.handle, attrib.dxf.handle, attdef.dxf.handle


def test_dimension_distinguishes_geometry_stored_measurement_and_display(annotations):
    source, handle, *_ = annotations
    result = query_entities(source, handles=[handle], fields=[
        "handle", "dimension_type", "dimension_type_raw", "defpoint2", "defpoint3",
        "measurement", "measure_kind", "actual_measurement", "text_override", "dimstyle", "dimlfac",
    ])["entities"][0]
    assert result["measurement"] == 1200.0
    assert result["measure_kind"] == "linear_distance"
    assert result["actual_measurement"] == 777
    assert result["text_override"] == "USER 9.5"
    assert result["dimstyle"] == "TAKEOFF"
    assert result["dimlfac"] == 0.001  # Entity override wins over the base style's 2.0.
    assert result["dimension_type"] == 0
    assert result["dimension_type_raw"] & 15 == 0
    assert result["defpoint3"] == [1200.0, 0.0, 0.0]
    assert "rendered display text is not evaluated" in " ".join(result["warnings"])


@pytest.mark.parametrize("dimtype,points,expected,kind", [
    (2, {"defpoint2": (0, 0), "defpoint3": (1, 0), "defpoint4": (0, 0), "defpoint": (0, 1)}, 90.0, "angular_degrees"),
    (3, {"defpoint": (-5, 0), "defpoint4": (5, 0)}, 10.0, "diameter"),
    (4, {"defpoint": (0, 0), "defpoint4": (5, 0)}, 5.0, "radius"),
    (6, {"defpoint": (3, 5), "defpoint2": (8, 12)}, [5.0, 7.0, 0.0], "ordinate_vector_wcs"),
])
def test_dimension_measurement_categories(dimtype, points, expected, kind):
    doc = ezdxf.new("R2013")
    dimension = doc.modelspace().new_entity("DIMENSION", {"dimtype": dimtype, **points})
    record = _record(dimension, fields=["measurement", "measure_kind"])
    assert record["measurement"] == expected
    assert record["measure_kind"] == kind


def test_dimension_missing_definition_or_unsupported_type_is_explicit():
    doc = ezdxf.new("R2013")
    dimension = doc.modelspace().new_entity("DIMENSION", {"dimtype": 0})
    record = _record(dimension, fields=["measurement", "measure_kind", "actual_measurement", "defpoint2"])
    assert record["measurement"] is None
    assert record["actual_measurement"] is None
    assert record["defpoint2"] is None
    assert record["measure_kind"] == "unavailable"
    assert record["warnings"]
    dimension.dxf.dimtype = 8
    record = _record(dimension, fields=["measurement", "measure_kind"])
    assert record["measurement"] is None
    assert record["measure_kind"] == "unsupported"


def test_projection_avoids_unrequested_dimension_calculations(annotations):
    source, handle, *_ = annotations
    with patch("ezdxf.entities.Dimension.get_measurement", side_effect=AssertionError("computed measurement")), \
            patch("ezdxf.entities.Dimension.override", side_effect=AssertionError("resolved style")):
        record = query_entities(source, handles=[handle], fields=["handle", "dimension_type"])["entities"][0]
    assert record == {"handle": handle, "dimension_type": 0}


def test_native_attributes_are_queryable_and_indexed_without_insert_expansion(annotations):
    source, _, insert_handle, attrib_handle, attdef_handle = annotations
    for text, handle, tag, location, height in [("CT2", attrib_handle, "TYPE", [1, 2, 0], 2),
                                               ("CT3", attdef_handle, "DEFAULT_TYPE", [3, 4, 0], 3)]:
        result = query_entities(source, text=text, fields=["handle", "tag", "text", "plain_text", "insert", "height"])
        assert result["total"] == 1
        assert result["entities"] == [{"handle": handle, "tag": tag, "text": text, "plain_text": text,
                                        "insert": location, "height": height}]
    assert query_entities(source, text="CT5", fields=["handle"])["total"] == 0
    assert query_entities(source, text="BLOCK SECRET", fields=["handle"])["total"] == 0
    record = query_entities(source, handles=[insert_handle], fields=["attributes"])["entities"][0]
    assert record["attributes"][0]["tag"] == "CAP_TYPE"
    assert record["attributes"][0]["text"] == "CT5"
    assert record["attribute_count"] == 1
    assert record["attribute_complete"] is True


def test_summary_and_projection_do_not_read_attached_attribute_text(annotations):
    source, _, insert_handle, *_ = annotations
    with patch("ezdxf.entities.Attrib.plain_text", side_effect=AssertionError("expanded attached attributes")):
        # The cold text index reads direct ATTRIB/ATTDEF entities by design, so
        # prepare it first before testing the INSERT-only projection.
        doc = ezdxf.readfile(source)
        insert = doc.entitydb[insert_handle]
        assert "attributes" not in _record(insert, detail="summary")
        assert _record(insert, fields=["handle", "attribute_count"]) == {"handle": insert_handle, "attribute_count": 1}


def test_insert_attribute_caps_and_byte_cropping_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    doc = ezdxf.new("R2013")
    doc.blocks.new("MANY")
    insert = doc.modelspace().add_blockref("MANY", (0, 0))
    for index in range(140):
        insert.add_attrib(str(index), "标" * 2000, (index, 0))
    record = _record(insert, fields=["attributes"])
    assert record["attribute_count"] == 140
    assert len(record["attributes"]) == 128
    assert record["attribute_complete"] is False
    assert len(record["attributes"][0]["text"]) == 1024
    assert record["attributes"][0]["truncation"]["text"]["total_characters"] == 2000
    source = tmp_path / "many.dxf"
    doc.saveas(source)
    page = query_entities(source, handles=[insert.dxf.handle], fields=["handle", "attributes"], max_output_bytes=4096)
    record = page["entities"][0]
    assert len(json.dumps(page, ensure_ascii=False).encode()) < 4096
    assert record["attribute_complete"] is False
    assert record["truncation"]["attributes"]["total"] == 140
    assert record["truncation"]["attributes"]["reason"] == "byte_budget"
    assert page["warnings"]


def test_annotation_snapshot_warm_page_needs_no_geometry_or_parse(annotations):
    source, *_ = annotations
    first = query_entities(source, fields=["handle", "tag", "measurement"], limit=1)
    assert query_cache.CACHE_VERSION >= 3
    with patch("agentcad.engine.read_document", side_effect=AssertionError("reparsed source")), \
            patch("agentcad.engine._record", side_effect=AssertionError("formatted source")):
        second = query_entities(source, fields=["handle", "tag", "measurement"], limit=1, cursor=first["next_cursor"])
    assert second["cache"]["hit"] is True
    assert first["entities"][0]["handle"] != second["entities"][0]["handle"]


def _matrix_point(point, matrix):
    vector = [*point, 1.0]
    return [sum(vector[row] * matrix[row][column] for row in range(4)) for column in range(3)]


def test_explicit_block_inspection_retains_local_geometry_and_correct_instance_matrix(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    doc = ezdxf.new("R2013", units=4)
    block = doc.blocks.new("OFFSET_CAP", base_point=(100, 200, 3))
    line = block.add_line((100, 200, 3), (102, 203, 4))
    insert = doc.modelspace().add_blockref("OFFSET_CAP", (10, 20, 30), dxfattribs={
        "xscale": 2, "yscale": 3, "zscale": 4, "rotation": 90,
    })
    source = tmp_path / "offset-block.dxf"
    doc.saveas(source)
    fields = ["handle", "matrix44", "matrix44_convention", "block_base_point", "block_entities"]
    with patch("ezdxf.entities.Insert.virtual_entities", side_effect=AssertionError("expanded instance")):
        result = query_entities(source, handles=[insert.dxf.handle], fields=fields)["entities"][0]
    assert result["block_base_point"] == [100, 200, 3]
    assert result["block_entity_count"] == 1
    assert result["block_complete"] is True
    child = result["block_entities"][0]
    assert child["handle"] == line.dxf.handle
    assert child["start"] == [100, 200, 3]  # Remains native definition data.
    assert _matrix_point(child["start"], result["matrix44"]) == pytest.approx([10, 20, 30])
    assert _matrix_point(child["end"], result["matrix44"]) == pytest.approx([1, 24, 34])
    assert "row_vector" in result["matrix44_convention"]
    assert "relative to the definition" in result["block_coordinate_system"]


def test_block_matrix_accounts_for_insert_ocs_extrusion():
    doc = ezdxf.new("R2013")
    doc.blocks.new("OCS_CAP", base_point=(100, 200, 3))
    insert = doc.modelspace().add_blockref("OCS_CAP", (10, 20, 30), dxfattribs={"extrusion": (0, 1, 0)})
    row = _record(insert, fields=["matrix44", "block_base_point"])
    assert _matrix_point(row["block_base_point"], row["matrix44"]) == pytest.approx([-10, 30, 20])


def test_block_child_inspection_requires_explicit_field_and_never_recurses():
    doc = ezdxf.new("R2013")
    nested = doc.blocks.new("NESTED")
    nested.add_line((0, 0), (3, 4))
    outer = doc.blocks.new("OUTER")
    child_insert = outer.add_blockref("NESTED", (20, 30))
    insert = doc.modelspace().add_blockref("OUTER", (0, 0))
    assert "block_entities" not in _record(insert, detail="summary")
    assert "block_entities" not in _record(insert, detail="full")
    with patch("ezdxf.entities.Insert.virtual_entities", side_effect=AssertionError("expanded nested INSERT")):
        row = _record(insert, fields=["block_entities"])
    assert row["block_complete"] is False
    assert len(row["block_entities"]) == 1
    assert row["block_entities"][0]["handle"] == child_insert.dxf.handle
    assert row["block_entities"][0]["name"] == "NESTED"
    assert "block_entities" not in row["block_entities"][0]
    assert "Nested INSERT" in row["block_omissions"][0]["reason"]


def test_block_caps_children_vertices_and_utf8_byte_output(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    doc = ezdxf.new("R2013")
    block = doc.blocks.new("BIG_CAP")
    block.add_lwpolyline([(index, 0) for index in range(2000)])
    for index in range(140):
        block.add_line((index, 0), (index, 3))
    insert = doc.modelspace().add_blockref("BIG_CAP", (0, 0))
    direct = _record(insert, fields=["handle", "block_entities"])
    assert direct["block_entity_count"] == 141
    assert len(direct["block_entities"]) == 128
    assert direct["block_complete"] is False
    assert direct["block_entities"][0]["geometry_page"]["has_more"] is True
    assert len(direct["block_entities"][0]["points"]) == 128
    source = tmp_path / "large-block.dxf"
    doc.saveas(source)
    page = query_entities(source, handles=[insert.dxf.handle], fields=["handle", "block_entities"], max_output_bytes=4096)
    assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) < 4096
    row = page["entities"][0]
    assert row["block_complete"] is False
    assert row["truncation"]["block_entities"]["reason"] == "byte_budget"


def test_block_missing_definition_and_external_reference_are_explicit():
    doc = ezdxf.new("R2013")
    missing = doc.modelspace().add_blockref("ABSENT", (0, 0))
    row = _record(missing, fields=["block_entities", "matrix44", "block_base_point"])
    assert row["matrix44"] is None
    assert row["block_base_point"] is None
    assert row["block_entity_count"] is None
    assert row["block_complete"] is False
    assert row["block_omissions"]
    external = doc.blocks.new("EXTERNAL", dxfattribs={"flags": 4, "xref_path": "external.dxf"})
    reference = doc.modelspace().add_blockref(external.name, (0, 0))
    row = _record(reference, fields=["block_entities"])
    assert row["block_complete"] is False
    assert "External" in row["block_omissions"][0]["reason"]


def test_block_export_is_explicit_and_preserves_native_definition_handles(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    doc = ezdxf.new("R2013")
    block = doc.blocks.new("EXPORT_CAP", base_point=(100, 200))
    label = block.add_text("CT2", dxfattribs={"insert": (100, 200)})
    insert = doc.modelspace().add_blockref("EXPORT_CAP", (1, 2))
    source, output = tmp_path / "block.dxf", tmp_path / "blocks.jsonl"
    doc.saveas(source)
    export_entities(source, output, fields=["handle", "matrix44", "block_entities"])
    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["handle"] == insert.dxf.handle
    assert row["block_entities"][0]["handle"] == label.dxf.handle
    assert row["block_entities"][0]["text"] == "CT2"
    assert row["block_complete"] is True
