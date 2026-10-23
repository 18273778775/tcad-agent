# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Exercise installer interpreter selection and publication without a network."""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell installer")


@pytest.fixture
def installer_wheel(tmp_path):
    wheel = tmp_path / "tcad_installer_fixture-0.0.1-py3-none-any.whl"
    metadata_dir = "tcad_installer_fixture-0.0.1.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("tcad_installer_fixture.py", "def main():\n    print('tcad-installer-fixture')\n")
        archive.writestr(f"{metadata_dir}/METADATA", "Metadata-Version: 2.1\nName: tcad-installer-fixture\nVersion: 0.0.1\n")
        archive.writestr(f"{metadata_dir}/WHEEL", "Wheel-Version: 1.0\nGenerator: tcad-installer-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{metadata_dir}/entry_points.txt", "[console_scripts]\ntcad = tcad_installer_fixture:main\n")
        archive.writestr(f"{metadata_dir}/RECORD", "")
    return wheel


def _run_installer(tmp_path, wheel, *, python=None, env=None, shell=None):
    powershell = shell or shutil.which("pwsh") or shutil.which("powershell")
    assert powershell, "Windows CI must have PowerShell."
    command = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/install.ps1"),
               "-UsePip", "-PackageSource", str(wheel), "-InstallRoot", str(tmp_path / "installed"), "-NoModifyUserPath"]
    if python:
        command += ["-Python", str(python)]
    environment = (env or os.environ).copy()
    environment.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    environment.pop("PYTHONPATH", None)
    return subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=90)


def test_active_path_python_precedes_unrelated_windows_launcher(tmp_path, installer_wheel):
    commands = tmp_path / "commands"
    commands.mkdir()
    launcher_log = tmp_path / "launcher-used.txt"
    (commands / "python.cmd").write_text(f'@echo off\r\n"{sys.executable}" %*\r\nexit /b %errorlevel%\r\n', encoding="utf-8")
    (commands / "py.cmd").write_text(f'@echo off\r\necho unrelated-launcher>"{launcher_log}"\r\nexit /b 1\r\n', encoding="utf-8")
    environment = os.environ.copy()
    environment["PATH"] = str(commands) + os.pathsep + environment["PATH"]
    result = _run_installer(tmp_path, installer_wheel, env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not launcher_log.exists(), "The launcher must not override the active Python."
    python = next((tmp_path / "installed/venvs").glob("*/Scripts/python.exe"))
    probe = subprocess.run([str(python), "-c", "import json,sys; print(json.dumps(sys.version_info[:2]))"], capture_output=True, text=True, check=True)
    assert json.loads(probe.stdout) == list(sys.version_info[:2])
    shim = tmp_path / "installed/bin/tcad.cmd"
    assert shim.is_file()
    launched = subprocess.run([str(shim), "--version"], capture_output=True, text=True, check=True)
    assert "tcad-installer-fixture" in launched.stdout


def test_explicit_python_installs_offline_on_windows_powershell_51(tmp_path, installer_wheel):
    powershell = shutil.which("powershell")
    assert powershell, "Windows PowerShell 5.1 compatibility is part of the installer contract."
    result = _run_installer(tmp_path, installer_wheel, python=sys.executable, shell=powershell)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "installed/bin/tcad.cmd").is_file()


def test_unsupported_explicit_python_fails_before_install_and_preserves_shim(tmp_path, installer_wheel):
    commands = tmp_path / "commands"
    commands.mkdir()
    unsupported = commands / "unsupported-python.cmd"
    unsupported.write_text('@echo off\r\necho {"executable":"unused", "major":3, "minor":15, "implementation":"cpython"}\r\n', encoding="ascii")
    shim = tmp_path / "installed/bin/tcad.cmd"
    shim.parent.mkdir(parents=True)
    previous = b"@echo previous installation\r\n"
    shim.write_bytes(previous)
    result = _run_installer(tmp_path, installer_wheel, python=unsupported)
    assert result.returncode != 0
    assert "3.11-3.14" in result.stdout + result.stderr
    assert "3.15 wheels" in result.stdout + result.stderr
    assert shim.read_bytes() == previous
    assert not list((tmp_path / "installed/venvs").iterdir())
