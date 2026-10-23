"""Public workset reuse, document IO and refined selection contracts."""
from pathlib import Path

import ezdxf
import pytest

from agentcad.cli import parser
from agentcad.io import document_read_scope, read_document
from agentcad.protocol import execute_request


def request(command, **args):
    response = execute_request({"command": command, "args": args})
    assert response["ok"], response
    return response["result"]


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    document = ezdxf.new("R2013", units=4)
    for layer in ("FRAME", "TARGET", "LABEL", "AXIS", "AX_LABEL"):
        document.layers.add(layer)
    model = document.modelspace()
    model.add_lwpolyline([(0, 0), (1000, 0), (1000, 700), (0, 700)], close=True,
                        dxfattribs={"layer": "FRAME"})
    model.add_text("1#楼 基础平面图", dxfattribs={"height": 20, "insert": (300, 50)})
    line = model.add_line((10, 10, 0), (20, 10, 0), dxfattribs={"layer": "TARGET"})
    model.add_lwpolyline([(10, 20), (20, 20), (20, 30), (10, 30)], close=True,
                        dxfattribs={"layer": "TARGET"})
    model.add_text("C1", dxfattribs={"insert": (15, 25), "height": 1, "layer": "LABEL"})
    model.add_line((5, 5), (5, 40), dxfattribs={"layer": "AXIS"})
    model.add_line((5, 5), (40, 5), dxfattribs={"layer": "AXIS"})
    model.add_text("A", dxfattribs={"insert": (5, 3), "height": 1, "layer": "AX_LABEL"})
    model.add_text("1", dxfattribs={"insert": (3, 5), "height": 1, "layer": "AX_LABEL"})
    model.add_line((5000, 5000), (5010, 5000))
    path = tmp_path / "source.dxf"
    document.saveas(path)
    return path, line.dxf.handle


def test_same_readonly_request_decodes_source_once(source, monkeypatch):
    path, handle = source
    calls = []
    actual = ezdxf.readfile
    def counted(filename, *args, **kwargs):
        calls.append(Path(filename).resolve())
        return actual(filename, *args, **kwargs)
    monkeypatch.setattr(ezdxf, "readfile", counted)
    result = request("measure", input=str(path), sheet="sheet-001", handles=[handle])
    assert result["totals"]["length"] == 10
    assert calls.count(path.resolve()) == 1
    assert result["document_io"]["document_loads"] == 1
    assert result["document_io"]["source_cache_hits"] >= 1
    request("measure", input=str(path), handles=[handle])
    assert calls.count(path.resolve()) == 2  # objects never cross request boundaries


def test_read_cache_invalidates_changed_file_and_does_not_escape_scope(source):
    path, _ = source
    with document_read_scope() as state:
        first = read_document(path)
        assert read_document(path) is first
        path.write_bytes(path.read_bytes() + b"\n")
        assert read_document(path) is not first
        assert state["document_loads"] == 2 and state["source_cache_hits"] == 1
    assert read_document(path) is not first


def test_workset_reuse_across_all_analysis_commands_never_parses_original(source, tmp_path, monkeypatch):
    path, _ = source
    before = path.read_bytes()
    prepared = request("workset", input=str(path), bboxes=[[0, 0, 100, 100]], output_dir=str(tmp_path / "work"))
    workset = prepared["workset"]
    original_read = ezdxf.readfile
    calls = []
    def only_subset(filename, *args, **kwargs):
        assert Path(filename).resolve() != path.resolve(), "original was parsed after preparation"
        calls.append(Path(filename).resolve())
        return original_read(filename, *args, **kwargs)
    monkeypatch.setattr(ezdxf, "readfile", only_subset)
    selection = {"workset": workset}
    query = request("query", **selection, types=["LINE"], layers=["TARGET"], fields=["handle"])
    handle = query["entities"][0]["handle"]
    assert request("measure", **selection, handles=[handle])["totals"]["length"] == 10
    assert request("info", **selection)["entity_count"] < 10
    assert request("index", **selection)["entity_count"] < 10
    assert request("export", **selection, output=str(tmp_path / "selected.jsonl"))["entity_count"] < 10
    assert request("area", **selection, layers=["TARGET"])["union_area"] == 100
    assert request("associate", **selection, label_layers=["LABEL"], target_layers=["TARGET"])["summary"]["pair_count"] == 1
    assert request("axes", **selection, line_layers=["AXIS"], label_layers=["AX_LABEL"], max_distance=3)["summary"]["intersection_count"] == 1
    rendered = request("render", **selection, output=str(tmp_path / "selected.svg"))
    assert rendered["bytes"] > 0 and rendered["bbox"] == [0, 0, 100, 100]
    assert "regions" not in rendered["workset"] and "selected_sheets" not in rendered["workset"]
    assert calls and all(item.name == "subset.dxf" for item in calls)
    assert path.read_bytes() == before


@pytest.mark.parametrize("destination", ["manifest", "subset", "original"])
def test_result_artifact_must_not_replace_workset_inputs(source, tmp_path, destination):
    path, _ = source
    prepared = request("workset", input=str(path), bboxes=[[0, 0, 100, 100]], output_dir=str(tmp_path / "work"))
    target = {"manifest": Path(prepared["workset"]), "subset": Path(prepared["drawing"]), "original": path}[destination]
    before = target.read_bytes()
    response = execute_request({"command": "info", "args": {"workset": prepared["workset"],
        "result_file": str(target), "overwrite_result_file": True}})
    assert not response["ok"] and response["error"]["code"] == "OUTPUT_CONFLICT", response
    assert target.read_bytes() == before


def test_cli_and_schema_workset_discovery():
    args = parser().parse_args(["measure", "--workset", "foundation", "--handle", "ABC"])
    assert args.input is None and args.workset == "foundation"
    args = parser().parse_args(["workset", "source.dxf", "--bbox", "0", "0", "10", "10",
                               "--bbox", "20", "20", "30", "30", "--output-dir", "foundation"])
    assert len(args.bboxes) == 2
    response = execute_request({"command": "measure", "args": {"handles": ["ABC"]}})
    assert not response["ok"] and response["error"]["code"] == "INVALID_REQUEST"
    capabilities = request("capabilities", fields=["selection_workflow"])
    assert "measure" in capabilities["selection_workflow"]["commands"]
    schema = execute_request({"command": "schema", "args": {"target": "request"}})
    assert schema["ok"], schema
    assert "$defs" in schema["result"]


@pytest.mark.parametrize("args,valid", [
    ({"workset": "foundation", "handles": ["ABC"], "max_output_bytes": 4096}, True),
    ({"input": "source.dxf", "sheet": "sheet-001", "bbox": [0, 0, 20, 20]}, True),
    ({"handles": ["ABC"]}, False),
    ({"workset": "foundation", "arbitrary": True}, False),
    ({"workset": "foundation", "max_output_bytes": 1}, False),
])
def test_shared_discovery_schema_preserves_request_validation(args, valid):
    from jsonschema import Draft202012Validator
    from agentcad.protocol import REQUEST_SCHEMA
    assert Draft202012Validator(REQUEST_SCHEMA).is_valid({"command": "measure", "args": args}) is valid


def test_workset_and_optional_input_identity_mismatch_is_structured(source, tmp_path):
    path, _ = source
    prepared = request("workset", input=str(path), bboxes=[[0, 0, 100, 100]], output_dir=str(tmp_path / "work"))
    other = tmp_path / "other.dxf"
    other.write_text("different source", encoding="utf-8")
    response = execute_request({"command": "info", "args": {"workset": prepared["workset"], "input": str(other)}})
    assert not response["ok"] and response["error"]["code"] == "STALE_WORKSET"


def test_workset_saved_regions_can_be_refined_or_batch_rendered(source, tmp_path, monkeypatch):
    path, _ = source
    prepared = request("workset", input=str(path), bboxes=[[0, 0, 100, 100], [4990, 4990, 5020, 5020]],
                       output_dir=str(tmp_path / "related"))
    read = ezdxf.readfile
    def subset_only(filename, *args, **kwargs):
        assert Path(filename).resolve() != path.resolve()
        return read(filename, *args, **kwargs)
    monkeypatch.setattr(ezdxf, "readfile", subset_only)
    result = request("query", workset=prepared["workset"], sheet="region-002", fields=["type"])
    assert result["entities"] == [{"type": "LINE"}]
    result = request("render", workset=prepared["workset"], sheet="region-001", bbox=[0, 0, 40, 40],
                     output=str(tmp_path / "one.svg"))
    assert result["sheet"]["selection_bbox"] == [0, 0, 40, 40]
    result = request("render", workset=prepared["workset"], sheet="all", output_dir=str(tmp_path / "previews"))
    assert result["sheet_count"] == 2
    assert (tmp_path / "previews/region-001.svg").is_file()
    assert (tmp_path / "previews/region-002.svg").is_file()
    page = (tmp_path / "previews/index.html").read_text(encoding="utf-8")
    assert 'href="render-manifest.json"' in page
    assert '"thumbnail":"region-001.png"' in page
