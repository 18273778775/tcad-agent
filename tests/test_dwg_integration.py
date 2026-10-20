# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Optional, real LibreDWG/CLI integration; no downloads occur during tests.

Provision on Windows with scripts/install-libredwg.ps1 -IncludeTestFixture.
The upstream fixture is independent of our DXF writer and pinned by SHA-256.
"""

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import ezdxf
import pytest

PROJECT = Path(__file__).resolve().parents[1]
FIXTURE_SHA256 = "4b8d9b1396a6d0decaa328dfc7cf17a09f2afa716ff8f40e9ca62ad1f1504ffa"


def _real_converter():
    configured = os.environ.get("TCAD_DWG2DXF", "").strip()
    local = PROJECT / ".tools" / "libredwg-0.14" / "dwg2dxf.exe"
    executable = shutil.which(configured or "dwg2dxf")
    if not executable and not configured and os.name == "nt" and local.is_file():
        executable = str(local)
    if not executable:
        pytest.skip("Real LibreDWG is absent; run scripts/install-libredwg.ps1 -IncludeTestFixture")
    return executable


def test_official_dwg_conversion_and_geometry_with_spaces(tmp_path, monkeypatch):
    converter = _real_converter()
    fixture = PROJECT / "artifacts" / "dwg-fixtures" / "sample_2000.dwg"
    if not fixture.is_file():
        pytest.skip(
            "Pinned DWG fixture is absent; run scripts/install-libredwg.ps1 -IncludeTestFixture"
        )
    data = fixture.read_bytes()
    assert hashlib.sha256(data).hexdigest() == FIXTURE_SHA256
    assert data[:6] == b"AC1015"

    drawings = tmp_path / "drawings with spaces"
    drawings.mkdir()
    source = drawings / "official sample 2000.dwg"
    shutil.copyfile(fixture, source)
    output = drawings / "converted output.dxf"
    monkeypatch.setenv("TCAD_DWG2DXF", converter)
    monkeypatch.setenv("PYTHONPATH", str(PROJECT / "src"))

    def cli(*arguments):
        result = subprocess.run(
            [sys.executable, "-m", "agentcad", *map(str, arguments)],
            cwd=drawings,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        response = json.loads(result.stdout)
        assert response["ok"], response
        return response["result"]

    converted = cli("convert", source, "--output", output, "--backend", "libredwg")
    assert converted["backend"] == "libredwg"
    assert converted["dxf_version"] == "AC1015"
    drawing = ezdxf.readfile(output)
    entities = list(drawing.modelspace())
    assert Counter(e.dxftype() for e in entities) == {
        "CIRCLE": 1,
        "TEXT": 1,
        "LWPOLYLINE": 1,
        "LINE": 3,
    }
    circle = drawing.modelspace().query("CIRCLE")[0]
    assert circle.dxf.radius == pytest.approx(20)
    assert tuple(circle.dxf.center) == pytest.approx((199.6812627452187, 24.69575912391304, 0))
    lines = list(drawing.modelspace().query("LINE"))
    assert sorted((e.dxf.end - e.dxf.start).magnitude for e in lines) == pytest.approx(
        [100, 100, math.sqrt(20000)]
    )

    # Read the DWG through the user-facing measurement command, exercising the
    # temporary conversion path as well as explicit convert and DXF inspection.
    arguments = ["measure", source]
    for entity in [circle, *lines]:
        arguments.extend(["--handle", entity.dxf.handle])
    measured = cli(*arguments)
    records = {item["handle"]: item for item in measured["measurements"]}
    assert records[circle.dxf.handle]["radius"] == pytest.approx(20)
    assert records[circle.dxf.handle]["area"] == pytest.approx(math.pi * 400)
    assert sorted(records[e.dxf.handle]["length"] for e in lines) == pytest.approx(
        [100, 100, math.sqrt(20000)]
    )
    assert measured["totals"]["length_complete"] is True
