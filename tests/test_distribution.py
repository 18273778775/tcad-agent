# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Installer boundaries and an opt-in real wheel/uv tool installation smoke test."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from agentcad import __version__
from agentcad.errors import CadError
from agentcad.install import _download, _extract_verified_zip, find_installed_dwg2dxf, user_data_dir

ROOT = Path(__file__).resolve().parents[1]


def test_version_and_tool_entrypoints_agree():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert metadata["version"] == __version__ == "0.3.0"
    assert metadata["scripts"]["tcad"] == "agentcad.cli:main"
    assert metadata["license"] == "Apache-2.0"


@pytest.mark.parametrize("name", ["../escape.exe", "/absolute.exe", "C:/escape.exe", "x/../../escape.exe", "..\\escape.exe"])
def test_dwg_zip_rejects_escape_paths(tmp_path, name):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, b"unsafe")
    target = tmp_path / "tools"
    target.mkdir()
    with pytest.raises(CadError, match="unsafe|escapes"):
        _extract_verified_zip(archive, target)
    assert not list(target.iterdir())


def test_dwg_zip_rejects_symlink(tmp_path):
    archive = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("dwg2dxf.exe")
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(info, "../../elsewhere")
    target = tmp_path / "tools"
    target.mkdir()
    with pytest.raises(CadError, match="unsafe"):
        _extract_verified_zip(archive, target)


def test_installed_converter_discovery_verifies_bytes_and_location(tmp_path):
    executable = tmp_path / "tools" / "dwg2dxf.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"controlled fixture, never executed")
    record = {
        "executable": str(executable),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }
    (tmp_path / "dwg-install.json").write_text(json.dumps(record), encoding="utf-8")
    assert find_installed_dwg2dxf(data_dir=tmp_path) == str(executable.resolve())
    executable.write_bytes(b"tampered")
    assert find_installed_dwg2dxf(data_dir=tmp_path) is None
    outside = tmp_path / "outside.exe"
    outside.write_bytes(b"external")
    record.update(executable=str(outside), executable_sha256=hashlib.sha256(b"external").hexdigest())
    (tmp_path / "dwg-install.json").write_text(json.dumps(record), encoding="utf-8")
    assert find_installed_dwg2dxf(data_dir=tmp_path) is None


def test_data_home_is_explicit_and_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("TCAD_DATA_HOME", str(tmp_path / "user-tools"))
    monkeypatch.chdir(tmp_path)
    assert user_data_dir() == tmp_path / "user-tools"


def test_download_rejects_checksum_before_publication(tmp_path):
    source = tmp_path / "source.zip"
    source.write_bytes(b"wrong archive")
    destination = tmp_path / "archive.zip"
    with pytest.raises(CadError) as error:
        _download(source.as_uri(), destination, "0" * 64, 1024)
    assert error.value.code == "CHECKSUM_MISMATCH"
    assert not destination.exists()


@pytest.mark.skipif(os.environ.get("TCAD_TEST_DISTRIBUTION") != "1", reason="Set TCAD_TEST_DISTRIBUTION=1 to build and install a clean wheel with uv.")
def test_wheel_installs_as_isolated_cli(tmp_path):
    uv = shutil.which("uv")
    assert uv, "Distribution CI must explicitly install uv."
    build = subprocess.run([uv, "build", "--wheel", "--out-dir", str(tmp_path / "dist")], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert build.returncode == 0, build.stderr
    wheel = next((tmp_path / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "agentcad/install.py" in names
        assert not any(name.startswith(("scripts/", "tests/", ".tools/")) for name in names)
        assert any(name.endswith("/licenses/LICENSE") for name in names)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.update(UV_TOOL_DIR=str(tmp_path / "tools"), UV_TOOL_BIN_DIR=str(tmp_path / "bin"), TCAD_DATA_HOME=str(tmp_path / "data"))
    installed = subprocess.run([uv, "tool", "install", "--python", sys.executable, str(wheel)], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=240)
    assert installed.returncode == 0, installed.stderr
    command = tmp_path / "bin" / ("tcad.exe" if os.name == "nt" else "tcad")
    version = subprocess.run([str(command), "--version"], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    assert version.returncode == 0 and "0.3.0" in version.stdout
    result = subprocess.run([str(command), "capabilities"], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"version": 1, "units": "mm", "operations": [{"op": "add", "entity": {"type": "LINE", "start": [0, 0], "end": [3, 4]}}]}), encoding="utf-8")
    drawing = tmp_path / "installed.dxf"
    created = subprocess.run([str(command), "apply", "--plan", str(plan), "--output", str(drawing)], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60)
    assert created.returncode == 0, created.stdout + created.stderr
    queried = subprocess.run([str(command), "query", str(drawing)], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60)
    assert queried.returncode == 0, queried.stdout + queried.stderr
    assert json.loads(queried.stdout)["ok"] is True
