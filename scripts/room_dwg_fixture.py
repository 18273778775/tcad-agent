# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Create a controlled metric DWG fixture with the pinned official dwgadd tool.

This intentionally finite fixture helper is not a general DXF-to-DWG exporter.
It accepts the CLI's LINE, ARC, CIRCLE, LWPOLYLINE, RECTANGLE and ASCII TEXT
specifications, preserves their layers, and requires an exact semantic roundtrip.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile

import ezdxf


ARCHIVE_URL = "https://github.com/LibreDWG/libredwg/releases/download/0.14/libredwg-0.14-win64.zip"
ARCHIVE_SHA256 = "1ad7e15344d20b3426c3435b078d82fb84b35062815946b2cca9c5fc9810fea8"
PINNED_FILES = {
    "examples/dwgadd.exe": "4857f30250ac0366d265c21c432bfc65aa9416642883a32c15368ed3ca4e3420",
    "dwg2dxf.exe": "955f5f2855bc0c5a8bd7a93424ea259ab14405799638b889b2b3c5918c529e3e",
    "dwgread.exe": "4b4e829cc545b237860f8c1359a80c48bda0e6821ac71727307f58bcdc8459c0",
    "dwgwrite.exe": "5939d6e8e8ab9e86126de3f26d27762f32142831df2f5a6ef0ca32b3fa5ae932",
    "libredwg-0.dll": "cc86e25e755ac225eadc839eaa232c2f2233c7a1b60fcd77b2cd1915761aef12",
    "libiconv-2.dll": "ff31fa811f9c07cc7fdaa68c9e8bca3a7b4fdf6e0a079a58175ea58ba139c7ae",
    "libpcre2-8-0.dll": "a499fb8a7eff453d6e4f0842576bc5171d40a1505d8f7f13fff6cee51b738a62",
    "libpcre2-16-0.dll": "3e8dbd40f92e96ae24f7386bd129fb055865da8f55f2fb03eb7744315976942f",
}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Fixture coordinates must be finite numbers.")
    if not math.isfinite(value):
        raise ValueError("Fixture coordinates must be finite numbers.")
    return float(value)


def _point(value, *, dimensions=3):
    if not isinstance(value, (tuple, list)) or len(value) not in (2, 3):
        raise ValueError("Fixture points need two or three coordinates.")
    point = tuple(_number(v) for v in value)
    if len(point) == 2:
        point += (0.0,)
    if point[2] != 0:
        raise ValueError("The controlled fixture helper supports only Z=0.")
    return point[:dimensions]


def _positive(value):
    value = _number(value)
    if value <= 0:
        raise ValueError("Fixture radii, widths and heights must be positive.")
    return value


def _normalize(specs):
    if not isinstance(specs, list) or not 1 <= len(specs) <= 10000:
        raise ValueError("Fixture specs need one to 10000 declarative entities.")
    normalized = []
    for raw in specs:
        spec = dict(raw)
        kind = spec["type"]
        layer = spec.get("layer", "0")
        if not isinstance(layer, str) or not re.fullmatch(r"[A-Za-z0-9_. -]{1,119}", layer):
            raise ValueError("The fixture helper requires bounded ASCII layer names.")
        if "color" in spec:
            raise ValueError("The controlled fixture helper does not support per-entity colors.")
        item = {"type": kind, "layer": layer}
        if kind == "RECTANGLE":
            x, y = _point(spec["insert"], dimensions=2)
            width, height = _positive(spec["width"]), _positive(spec["height"])
            item.update(type="LWPOLYLINE", points=((x, y), (x + width, y),
                        (x + width, y + height), (x, y + height)), closed=True)
        elif kind == "LWPOLYLINE":
            points = spec["points"]
            if not 2 <= len(points) <= 10000:
                raise ValueError("Fixture polylines need two to 10000 vertices.")
            if type(spec.get("closed", False)) is not bool:
                raise ValueError("Polyline closed must be boolean.")
            item.update(points=tuple(_point(p, dimensions=2) for p in points),
                        closed=spec.get("closed", False))
        elif kind == "LINE":
            item.update(start=_point(spec["start"]), end=_point(spec["end"]))
        elif kind in {"ARC", "CIRCLE"}:
            item.update(center=_point(spec["center"]), radius=_positive(spec["radius"]))
            if kind == "ARC":
                item.update(start_angle=_number(spec["start_angle"]) % 360,
                            end_angle=_number(spec["end_angle"]) % 360)
        elif kind == "TEXT":
            text = spec["text"]
            if not isinstance(text, str) or not 1 <= len(text) <= 119:
                raise ValueError("The official dwgadd parser requires 1-119 text characters.")
            if any(ord(c) < 32 or ord(c) > 126 or c in '\\"' for c in text):
                raise ValueError("Fixture text must be printable ASCII without quotes or backslashes.")
            item.update(text=text, insert=_point(spec["insert"]),
                        height=_positive(spec["height"]),
                        rotation=_number(spec.get("rotation", 0)) % 360)
        else:
            raise ValueError(f"Unsupported controlled fixture entity: {kind}")
        normalized.append(item)
    return normalized


def _format_number(value):
    return format(_number(value), ".17g")


def _format_point(value):
    return "(" + " ".join(_format_number(v) for v in value) + ")"


def _instructions(specs):
    commands = ["version 2000", "HEADER.INSUNITS = 4", "HEADER.MEASUREMENT = 1"]
    for layer in sorted({s["layer"] for s in specs} - {"0"}):
        commands.append(f'layer "{layer}"')
        commands.append('layer.ltype = "Continuous"')
    for spec in specs:
        kind = spec["type"]
        name = kind.lower()
        if kind == "LINE":
            commands.append(f'line {_format_point(spec["start"])} {_format_point(spec["end"])}')
        elif kind in {"ARC", "CIRCLE"}:
            command = f'{name} {_format_point(spec["center"])} {_format_number(spec["radius"])}'
            if kind == "ARC":
                command += f' {_format_number(spec["start_angle"])} {_format_number(spec["end_angle"])}'
            commands.append(command)
        elif kind == "LWPOLYLINE":
            points = " ".join(_format_point(p) for p in spec["points"])
            commands.append(f'lwpolyline {len(spec["points"])} ({points})')
            # LibreDWG's native closed flag is 512; DXF's is 1.
            commands.append(f'lwpolyline.flag = {512 if spec["closed"] else 0}')
        elif kind == "TEXT":
            commands.append(f'text "{spec["text"]}" {_format_point(spec["insert"])} {_format_number(spec["height"])}')
            if spec["rotation"]:
                commands.append(f'text.rotation = {_format_number(spec["rotation"])}')
        commands.append(f'{name}.layer = "{spec["layer"]}"')
    return "\n".join(commands) + "\n"


def _round(value):
    if isinstance(value, (tuple, list)):
        return tuple(_round(v) for v in value)
    if isinstance(value, float):
        return round(value, 8)
    return value


def _signature(spec):
    kind, layer = spec["type"], spec["layer"]
    if kind == "LINE":
        return kind, layer, tuple(sorted((_round(spec["start"]), _round(spec["end"]))))
    if kind == "LWPOLYLINE":
        return kind, layer, _round(spec["points"]), spec["closed"]
    if kind == "TEXT":
        return kind, layer, spec["text"], _round(spec["insert"]), _round(spec["height"]), _round(spec["rotation"])
    if kind == "ARC":
        return kind, layer, _round(spec["center"]), _round(spec["radius"]), _round(spec["start_angle"]), _round(spec["end_angle"])
    return kind, layer, _round(spec["center"]), _round(spec["radius"])


def _read_specs(doc):
    specs = []
    for entity in doc.modelspace():
        kind = entity.dxftype()
        spec = {"type": kind, "layer": entity.dxf.layer}
        if kind == "LINE":
            spec.update(start=tuple(entity.dxf.start), end=tuple(entity.dxf.end))
        elif kind == "LWPOLYLINE":
            if any(p[2] or p[3] or p[4] for p in entity.get_points("xyseb")):
                raise ValueError("DWG roundtrip unexpectedly introduced bulge or widths.")
            spec.update(points=tuple(entity.get_points("xy")), closed=bool(entity.closed))
        elif kind == "TEXT":
            spec.update(text=entity.dxf.text, insert=tuple(entity.dxf.insert),
                        height=entity.dxf.height, rotation=entity.dxf.rotation % 360)
        elif kind in {"CIRCLE", "ARC"}:
            spec.update(center=tuple(entity.dxf.center), radius=entity.dxf.radius)
            if kind == "ARC":
                spec.update(start_angle=entity.dxf.start_angle % 360,
                            end_angle=entity.dxf.end_angle % 360)
        else:
            raise ValueError(f"DWG roundtrip introduced unexpected entity {kind}.")
        specs.append(spec)
    return specs


def create_dwg_from_specs(specs: list[dict], output, tool_directory=None) -> dict:
    """Publish a new verified R2000 millimetre DWG; existing files are rejected.

    Returns output, roundtrip_dxf, source_instructions, provenance, counts and
    validation evidence. Missing/unpinned binaries fail with an installer hint;
    this helper never downloads tools or changes the machine's PATH.
    """
    if os.name != "nt":
        raise RuntimeError("This pinned official toolchain is Windows x64 only.")
    specs = _normalize(specs)
    output = Path(output).resolve()
    if output.suffix.lower() != ".dwg":
        raise ValueError("The fixture output must have a .dwg extension.")
    if not output.parent.is_dir():
        raise ValueError("The fixture output parent directory must already exist.")
    root = Path(__file__).resolve().parents[1]
    tools = Path(tool_directory or root / ".tools/libredwg-0.14").resolve()
    for relative, expected in PINNED_FILES.items():
        binary = tools / relative
        if not binary.is_file() or sha256(binary.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Missing or unpinned LibreDWG 0.14 file: {binary}. Run scripts/install-libredwg.ps1.")
    roundtrip = output.with_name(output.stem + ".roundtrip.dxf")
    instructions = output.with_name(output.stem + ".dwgadd.txt")
    configured_source = output.with_name(output.stem + ".dwg-source.json")
    provenance = output.with_name(output.stem + ".dwg-provenance.json")
    for artifact in (output, roundtrip, instructions, configured_source, provenance):
        if artifact.exists():
            raise FileExistsError(f"The fixture helper does not overwrite: {artifact}")
    env = os.environ.copy()
    env["PATH"] = str(tools) + os.pathsep + env.get("PATH", "")
    records = []
    with tempfile.TemporaryDirectory(prefix=".room-dwg-", dir=output.parent) as tmp:
        tmp = Path(tmp)
        script = tmp / "source.add"
        native_dwg = tmp / "native.dwg"
        json_source = tmp / "configured-source.json"
        dwg = tmp / "fixture.dwg"
        dxf = tmp / "roundtrip.dxf"
        script.write_text(_instructions(specs), encoding="ascii")
        def invoke(args):
            completed = subprocess.run([str(arg) for arg in args], env=env,
                cwd=tmp, capture_output=True, text=True, errors="replace", timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW)
            records.append({"command": [str(arg) for arg in args],
                "exit_code": completed.returncode, "stdout": completed.stdout,
                "stderr": completed.stderr, "process_only_path_prefix": str(tools)})
            if completed.returncode:
                raise RuntimeError("Official fixture tool failed: " + json.dumps(records[-1]))

        invoke([tools / "examples/dwgadd.exe", "--as", "r2000", "-o", native_dwg, script])
        invoke([tools / "dwgread.exe", "-O", "JSON", "-o", json_source, native_dwg])
        # dwgadd's generic numeric setter is not safe for its compound CMC
        # color struct. The official JSON source represents R2000 layer color
        # as one integer, so configure it explicitly before the final write.
        source = json.loads(json_source.read_text(encoding="utf-8"))
        layer_objects = [obj for obj in source["OBJECTS"] if obj.get("object") == "LAYER"]
        if not layer_objects:
            raise ValueError("Official native fixture contains no layer table entries.")
        for layer in layer_objects:
            layer["color"] = 7
        json_source.write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
        invoke([tools / "dwgwrite.exe", "-I", "JSON", "--as", "r2000", "-o", dwg, json_source])
        invoke([tools / "dwg2dxf.exe", "--as", "r2000", "-o", dxf, dwg])
        doc = ezdxf.readfile(dxf)
        actual = _read_specs(doc)
        expected_counts = Counter(_signature(spec) for spec in specs)
        actual_counts = Counter(_signature(spec) for spec in actual)
        audit = doc.audit()
        if (doc.units != 4 or doc.dxfversion != "AC1015" or actual_counts != expected_counts
                or audit.errors or audit.fixes
                or any(layer.dxf.color != 7 for layer in doc.layers)):
            raise ValueError("Fixture DWG semantic roundtrip failed: " + json.dumps({
                "units": doc.units, "dxf_version": doc.dxfversion,
                "missing": list((expected_counts - actual_counts).elements()),
                "unexpected": list((actual_counts - expected_counts).elements()),
                "layer_colors": {layer.dxf.name: layer.dxf.color for layer in doc.layers},
                "audit_errors": [{"code": v.code, "message": v.message} for v in audit.errors],
                "audit_fixes": [{"code": v.code, "message": v.message} for v in audit.fixes]}))
        report = {"fixture_kind": "controlled illustrative room; not an independent construction drawing",
            "output": str(output), "roundtrip_dxf": str(roundtrip),
            "source_instructions": str(instructions), "provenance": str(provenance),
            "configured_json_source": str(configured_source),
            "dwg_version": "R2000", "units": "mm", "insunits": 4,
            "entity_counts": dict(Counter(spec["type"] for spec in actual)),
            "validation": {"all_entity_geometry_text_layers_match": True,
                           "all_layer_colors_are_7": True,
                           "audit_errors": 0, "audit_fixes": 0},
            "source_configuration": {"layer_color": 7, "layer_count": len(layer_objects)},
            "tool_archive": {"url": ARCHIVE_URL, "sha256": ARCHIVE_SHA256},
            "verified_tool_files": PINNED_FILES, "commands": records,
            "sha256": sha256(dwg.read_bytes()).hexdigest(),
            "roundtrip_sha256": sha256(dxf.read_bytes()).hexdigest(),
            "instructions_sha256": sha256(script.read_bytes()).hexdigest()}
        report["configured_source_sha256"] = sha256(json_source.read_bytes()).hexdigest()
        provenance_tmp = tmp / "provenance.json"
        provenance_tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        # Same-volume hard links publish complete files and reject an existing
        # destination atomically. Publish the DWG only after all checks pass.
        for source, destination in ((script, instructions), (dxf, roundtrip),
                                    (json_source, configured_source),
                                    (provenance_tmp, provenance), (dwg, output)):
            os.link(source, destination)
    return report
