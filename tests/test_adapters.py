# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

import subprocess
import re
from pathlib import Path
from xml.etree import ElementTree

import ezdxf
import pytest

from agentcad import converters
from agentcad.errors import CadError
from agentcad.render import render_drawing


@pytest.fixture
def dxf_file(tmp_path):
    document = ezdxf.new("R2018")
    document.modelspace().add_line((0, 0), (50, 0))
    document.modelspace().add_circle((25, 20), 10)
    source = tmp_path / "test drawing.dxf"
    document.saveas(source)
    return source


@pytest.fixture
def dwg_file(tmp_path):
    source = tmp_path / "drawing with spaces.dwg"
    # A signature is enough to reach the mocked converter, not evidence of a
    # valid DWG. Actual DWG decoding is deliberately outside the Python engine.
    source.write_bytes(b"AC1032" + bytes(64))
    return source


@pytest.fixture
def configured_converter(monkeypatch, tmp_path):
    executable = tmp_path / "installed converter" / "dwg2dxf.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"Mock converter identity; never executed")
    monkeypatch.setenv("TCAD_DWG2DXF", str(executable))
    monkeypatch.setenv("TCAD_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("TCAD_ODA_CONVERTER", raising=False)
    monkeypatch.setattr(
        converters.shutil,
        "which",
        lambda name: str(executable) if "dwg2dxf" in name else None,
    )


def _error(code, function, *args, **kwargs):
    with pytest.raises(CadError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def test_converter_status_only_detects(monkeypatch):
    monkeypatch.setenv("TCAD_DWG2DXF", "custom dwg2dxf.exe")
    monkeypatch.setenv("TCAD_ODA_CONVERTER", "custom ODA.exe")
    monkeypatch.setattr(converters.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        converters.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Detection must not execute a program"),
    )
    status = converters.converter_status()
    assert status["libredwg"]["available"]
    assert status["libredwg"]["executable"] == "custom dwg2dxf.exe"
    assert status["oda"]["available"]
    assert status["oda"]["adapter_enabled"] is False


def test_missing_converter_is_actionable(dwg_file, tmp_path, monkeypatch):
    monkeypatch.delenv("TCAD_DWG2DXF", raising=False)
    monkeypatch.setattr(converters.shutil, "which", lambda name: None)
    monkeypatch.setattr("agentcad.install.find_installed_dwg2dxf", lambda: None)
    error = _error(
        "CONVERTER_NOT_FOUND",
        converters.convert_file,
        dwg_file,
        tmp_path / "output.dxf",
    )
    assert "TCAD_DWG2DXF" in error.message
    assert error.details["environment_variable"] == "TCAD_DWG2DXF"


def test_conversion_uses_list_and_validates_real_dxf(
    dwg_file,
    tmp_path,
    configured_converter,
    monkeypatch,
):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        document = ezdxf.new("R2018")
        document.modelspace().add_circle((0, 0), 4)
        document.saveas(command[command.index("-o") + 1])
        return subprocess.CompletedProcess(
            command, 0, b"converted", b"entity approximation warning"
        )

    monkeypatch.setattr(converters, "_run_converter", run)
    target = tmp_path / "converted.dxf"
    result = converters.convert_file(dwg_file, target, timeout=12)
    assert len(ezdxf.readfile(target).modelspace()) == 1
    assert result["backend"] == "libredwg"
    assert result["diagnostics"]["stderr"] == "entity approximation warning"
    command, options = calls[0]
    assert isinstance(command, list)
    assert command[-1] == str(dwg_file.resolve())
    assert options == {"timeout": 12}
    assert Path(command[command.index("-o") + 1]).suffix == ".dxf"


@pytest.mark.parametrize("failure", ["exit", "timeout", "unreadable", "empty", "oserror"])
def test_failed_conversion_is_atomic(
    dwg_file,
    tmp_path,
    configured_converter,
    monkeypatch,
    failure,
):
    def run(command, **kwargs):
        output = Path(command[command.index("-o") + 1])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 3, stderr=b"stalled")
        if failure == "oserror":
            raise OSError("cannot launch")
        if failure == "unreadable":
            output.write_text("not DXF", encoding="ascii")
        if failure == "exit":
            output.write_text("partial", encoding="ascii")
            return subprocess.CompletedProcess(command, 1, b"", b"bad input")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(converters, "_run_converter", run)
    target = tmp_path / "output.dxf"
    target.write_bytes(b"keep original")
    code = {
        "exit": "CONVERSION_FAILED",
        "timeout": "CONVERSION_TIMEOUT",
        "unreadable": "CONVERSION_INVALID_OUTPUT",
        "empty": "CONVERSION_INVALID_OUTPUT",
        "oserror": "CONVERTER_EXECUTION_FAILED",
    }[failure]
    _error(code, converters.convert_file, dwg_file, target, overwrite=True)
    assert target.read_bytes() == b"keep original"
    assert not list(tmp_path.glob(".tcad-*"))


def test_converter_no_clobber(dwg_file, tmp_path, configured_converter, monkeypatch):
    monkeypatch.setattr(
        converters.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Must fail before launch"),
    )
    target = tmp_path / "output.dxf"
    target.write_bytes(b"keep original")
    _error("OUTPUT_EXISTS", converters.convert_file, dwg_file, target)
    assert target.read_bytes() == b"keep original"


def test_bad_dwg_signature_rejected(tmp_path, configured_converter, monkeypatch):
    source = tmp_path / "renamed.dwg"
    source.write_bytes(b"not a DWG")
    monkeypatch.setattr(
        converters.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Must reject signature"),
    )
    _error("INVALID_DWG", converters.convert_file, source, tmp_path / "output.dxf")


def test_dxf_to_dwg_explicitly_unsupported(dxf_file, tmp_path):
    _error("UNSUPPORTED_CONVERSION", converters.convert_file, dxf_file, tmp_path / "output.dwg")


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "60"])
def test_invalid_conversion_timeout(dwg_file, tmp_path, timeout):
    _error(
        "INVALID_TIMEOUT",
        converters.convert_file,
        dwg_file,
        tmp_path / "output.dxf",
        timeout=timeout,
    )


def test_readable_dxf_preserves_original(dxf_file):
    with converters.readable_dxf(dxf_file) as path:
        assert path == dxf_file.resolve()
    assert dxf_file.exists()


def test_readable_dwg_keeps_stable_cache_until_explicit_cleanup(
    dwg_file, tmp_path, configured_converter, monkeypatch
):
    def run(command, **kwargs):
        ezdxf.new("R2018").saveas(command[command.index("-o") + 1])
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(converters, "_run_converter", run)
    with converters.readable_dxf(dwg_file) as path:
        original = path
        assert path.is_file()
        ezdxf.readfile(path)
    assert original.exists()
    monkeypatch.setattr(converters, "_run_converter", lambda *a, **kw: pytest.fail("Must reuse cache"))
    with converters.readable_dxf(dwg_file) as path:
        assert path == original
    # Explicit removal when no reader holds the conversion invalidates the cache.
    original.unlink()
    original.with_suffix(".json").unlink()
    assert not original.exists()


def test_implicit_dwg_read_preserves_diagnostics_and_original_path(
    dwg_file, configured_converter, monkeypatch
):
    from agentcad.protocol import execute_request

    def run(command, **kwargs):
        document = ezdxf.new("R2018")
        document.modelspace().add_circle((0, 0), 4)
        document.saveas(command[command.index("-o") + 1])
        return subprocess.CompletedProcess(command, 0, b"converted", b"unsupported object warning")

    monkeypatch.setattr(converters, "_run_converter", run)
    response = execute_request({"command": "info", "args": {"input": str(dwg_file)}})
    assert response["ok"], response
    result = response["result"]
    assert result["path"] == str(dwg_file.resolve())
    assert result["conversion"]["diagnostics"]["stderr"] == "unsupported object warning"
    assert "output" not in result["conversion"]
    assert result["conversion"]["warnings"][0] in result["warnings"]


def test_render_fixture_to_valid_svg(dxf_file, tmp_path):
    target = tmp_path / "preview.svg"
    result = render_drawing(dxf_file, target)
    root = ElementTree.parse(target).getroot()
    assert root.tag.endswith("svg")
    assert "viewBox" in root.attrib
    assert root.findall(".//{http://www.w3.org/2000/svg}path")
    assert result["layout"] == "Model"
    assert result["entity_count"] == 2
    assert len(result["warnings"]) >= 3


def test_render_horizontal_line_has_nonzero_page(tmp_path):
    source = tmp_path / "line.dxf"
    document = ezdxf.new()
    document.modelspace().add_line((0, 0), (50, 0))
    document.saveas(source)
    target = tmp_path / "line.svg"
    render_drawing(source, target)
    viewbox = ElementTree.parse(target).getroot().attrib["viewBox"].split()
    assert float(viewbox[2]) > 0
    assert float(viewbox[3]) > 0


def test_architectural_preview_keeps_lineweights_visible_when_fitted(tmp_path):
    source = tmp_path / "architectural-size.dxf"
    document = ezdxf.new("R2018")
    document.modelspace().add_lwpolyline([(0, 0), (27000, 0), (27000, 8000), (0, 8000)], close=True)
    document.saveas(source)
    target = tmp_path / "sheet.svg"
    render_drawing(source, target)
    root = ElementTree.parse(target).getroot()
    assert float(root.attrib["width"].removesuffix("mm")) <= 420.001
    assert float(root.attrib["height"].removesuffix("mm")) <= 420.001
    style = next(root.iter("{http://www.w3.org/2000/svg}style")).text
    stroke = float(re.search(r"stroke-width: ([0-9.]+)", style).group(1))
    viewbox_width = float(root.attrib["viewBox"].split()[2])
    assert stroke / viewbox_width * 500 > 0.1


def test_render_no_clobber(dxf_file, tmp_path):
    target = tmp_path / "preview.svg"
    target.write_text("original", encoding="utf-8")
    _error("OUTPUT_EXISTS", render_drawing, dxf_file, target)
    assert target.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob(".tcad-*"))


def test_render_empty_or_unknown_layout_is_structured(tmp_path):
    source = tmp_path / "empty.dxf"
    ezdxf.new().saveas(source)
    target = tmp_path / "empty.svg"
    _error("EMPTY_DRAWING", render_drawing, source, target)
    _error("LAYOUT_NOT_FOUND", render_drawing, source, target, layout="missing")
    assert not target.exists()


def test_pdf_render_is_explicitly_unsupported(dxf_file, tmp_path):
    _error("UNSUPPORTED_FORMAT", render_drawing, dxf_file, tmp_path / "preview.pdf")
