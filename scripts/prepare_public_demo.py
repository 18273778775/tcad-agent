# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Publish a path-normalized copy of controlled room acceptance evidence.

Only the explicit artifact allowlist is copied. CAD drawings, serialized models,
and SVG previews retain their bytes; JSON/transcript paths are normalized before
the existing offline viewer is regenerated. The original run is never modified.
"""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import re
import shutil
from urllib.parse import unquote, urlsplit


FIXED = (
    "fixtures/controlled-complete-room.dxf",
    "fixtures/controlled-complete-room.dwg",
    "fixtures/room-map.json",
    "fixtures/expected-oracle.json",
    "fixtures/dwg-provenance.json",
    "before-dxf.room.json", "before-dwg.room.json", "after.room.json",
    "revised-complete-room.dxf", "summary.json", "commands.json",
    *(f"models/{state}.{ext}" for state in ("before", "after")
      for ext in ("glb", "obj", "dxf")),
)
GENERATED = {"viewer.html", "README.md", "publication-manifest.json"}
PRIVATE_PATTERNS = (
    r"\b[A-Za-z]:[\\/]", r"\bAdministrator\b", r"/Users/[^/\s]+/",
    r"[\w.+-]+@qq\.com", r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    r"\bAKIA[0-9A-Z]{16}\b", r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{30,}",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
                    + "\n", encoding="utf-8", newline="\n")


def path_pattern(path: str) -> str:
    return r"[\\/]".join(re.escape(part) for part in re.split(r"[\\/]", path))


class Normalizer:
    def __init__(self, source: Path, workspace: Path, commands: dict, summary: dict):
        sources = {str(source)}
        workspaces = {str(workspace)}
        for original in summary["source_sha256"]:
            match = re.match(r"(.+)[\\/]fixtures[\\/][^\\/]+$", original)
            if match:
                sources.add(match.group(1))
        for command in commands["commands"]:
            if command.get("cwd"):
                workspaces.add(command["cwd"])
        self.sources = [re.compile(path_pattern(path) + r"[\\/]?", re.I)
                        for path in sorted(sources, key=len, reverse=True)]
        self.workspaces = [re.compile(path_pattern(path), re.I)
                           for path in sorted(workspaces, key=len, reverse=True)]
        self.replacements = []
        for key, label in (("python", "<python>"), ("converter", "<libredwg>/dwg2dxf.exe")):
            original = commands.get(key)
            if original:
                self.replacements.append((re.compile(path_pattern(original), re.I), label))
        converter = commands.get("converter")
        if converter:
            parent = re.split(r"[\\/]", converter)[:-1]
            self.replacements.append((re.compile(path_pattern("/".join(parent)), re.I),
                                      "<libredwg>"))

    def text(self, value: str) -> str:
        original = value
        for pattern, replacement in self.replacements:
            value = pattern.sub(lambda match: replacement, value)
        # Converter diagnostics record disposable profile-local DXF files.
        value = re.sub(r"[A-Za-z]:[\\/]Users[\\/][^\\/\r\n]+[\\/]"
                       r"AppData[\\/]Local[\\/]Temp[\\/][^\s\"<>]+",
                       "<temporary-file>", value, flags=re.I)
        for pattern in self.sources:
            value = pattern.sub("", value)
        for pattern in self.workspaces:
            value = pattern.sub("<workspace>", value)
        if value != original:
            value = value.replace("\\", "/")
        value = re.sub(r"fixtures/\.room-dwg-[^/\s]+/", "<temporary-dwg>/", value)
        return value

    def value(self, value):
        if isinstance(value, dict):
            return {self.text(key): self.value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value


README = """<!-- SPDX-FileCopyrightText: 2026 LiTao -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Controlled room acceptance demo

Download this directory and open [viewer.html](viewer.html) in a browser. The
viewer embeds the actual GLB triangles, CAD SVG previews, and saved verification
results. All rendering runs locally; there are no remote assets or dependencies.
GitHub displays HTML source instead of running the interactive viewer.

This is user-authored, controlled test data for one mapped, axis-aligned
rectangular room. It is not an independently sourced building project, an
arbitrary floor-plan recognition benchmark, or a construction compliance check.
Clear interior dimensions change from **__BEFORE_DIMENSIONS__ mm** to
**__AFTER_DIMENSIONS__ mm**. Wall/slab geometry and through-wall door/window voids
are checked by reopening GLB, OBJ, and 3D DXF with a **0.01 mm** tolerance.
Individual components are closed solids; the assembly is not claimed to be a
single Boolean union.

## Published evidence

- [Acceptance summary](summary.json) and [55 CLI requests](commands.json)
- [Original DXF](fixtures/controlled-complete-room.dxf),
  [original DWG](fixtures/controlled-complete-room.dwg), and
  [revised DXF](revised-complete-room.dxf)
- [Semantic mapping](fixtures/room-map.json), [expected-value oracle](fixtures/expected-oracle.json),
  and [DWG generation provenance](fixtures/dwg-provenance.json)
- [Before spec](before-dwg.room.json), [after spec](after.room.json),
  [revision plan](plans/revise-length-width-height.json), and
  [byte/hash publication manifest](publication-manifest.json)
- Models: [before GLB](models/before.glb), [before OBJ](models/before.obj),
  [before 3D DXF](models/before.dxf), [after GLB](models/after.glb),
  [after OBJ](models/after.obj), [after 3D DXF](models/after.dxf)
- Raw previews: [before DWG](previews/before-dwg.svg),
  [before DXF](previews/before-dxf.svg), [after](previews/after.svg)

## Public-copy normalization

This is a sanitized public copy of a saved acceptance run. JSON keys and string
values, including converter diagnostics, use paths relative to this directory.
`<workspace>`, `<python>`, `<libredwg>`, `<temporary-file>`, and
`<temporary-dwg>` replace local execution-environment paths. Command arguments
are historical records; substitute installed tools and a writable output
directory before replaying them. Numeric results, entity handles, status/error
codes, and all recorded original SHA-256 identities are retained.

The two source drawings, revised drawing, all six models, and three SVG previews
are copied byte for byte. JSON files change only by path normalization and the
explicit publication annotations in summary/command/provenance records. The
viewer is rebuilt against this public copy and validates GLB hashes, component
counts, and specification dimensions against the saved verification. The
manifest records both original and public-copy hashes for each copied file.
It excludes itself from its file hash list.

All 55 requests and their 110 response files are included. Negative-test fixture
paths and deliberately absent outputs remain historical references, not download
links. The missing-height and misleading-text drawings can be reconstructed
from the main fixture using the published negative-test plans. Temporary DWG
generation intermediates, converter/tool binaries, font binaries, and external
sample drawings are omitted. Their hashes and historical provenance remain in
the records where originally present. SVGs contain exported glyph outlines.

Regenerate from an existing private acceptance directory, at repository root:

```powershell
.venv\\Scripts\\python.exe scripts/prepare_public_demo.py --input artifacts/room-validation/acceptance-003 --output docs/demo
```

The output must be empty unless `--overwrite` is supplied. Existing unrecognized
files are rejected even with `--overwrite`; the helper never deletes files and
never modifies the source run. The original run is required for regeneration;
the public demo alone does not reproduce the private converter installation.

Copyright 2026 LiTao. The original TCAD code and controlled demo data are licensed
under [Apache-2.0](../../LICENSE). Third-party tools are not distributed here.
"""


class LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {"src", "href"} and value:
                self.links.append(value)


def validate_output(output: Path, viewer) -> dict:
    files = sorted(path for path in output.rglob("*") if path.is_file())
    checked = 0
    for path in files:
        if path.suffix in {".dwg", ".glb"}:
            continue
        raw = path.read_text(encoding="utf-8")
        for pattern in PRIVATE_PATTERNS:
            if re.search(pattern, raw, re.I):
                raise ValueError(f"Private path or credential pattern in {path.relative_to(output)}")
        checked += 1
    html = (output / "viewer.html").read_text(encoding="utf-8")
    match = re.search(r'<script type="application/json" id="evidence">(.*?)</script>',
                      html, re.S)
    if match is None:
        raise ValueError("Viewer evidence payload was not serialized")
    payload = json.loads(match.group(1))
    if payload != viewer.build_payload(output, output / "viewer.html"):
        raise ValueError("Serialized viewer payload differs from the public evidence")
    collector = LinkCollector()
    collector.feed(html)
    links = list(collector.links)
    links.extend(item["href"] for item in payload["manifest"])
    for state in payload["states"].values():
        links.extend(state["model_links"].values())
        links.extend([state["spec_link"], state["preview_link"]])
    local_links = set()
    for link in links:
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc:
            raise ValueError("Viewer references a remote asset")
        if not parsed.path:
            continue
        target = (output / unquote(parsed.path)).resolve()
        if not target.is_relative_to(output) or not target.is_file():
            raise ValueError(f"Missing or escaping viewer link: {link}")
        local_links.add(parsed.path)
    # Provenance URLs in inert JSON are records, not network dependencies.
    executable = re.sub(r'<script type="application/json" id="evidence">.*?</script>',
                        "", html, flags=re.S)
    if re.search(r"@import|\bfetch\s*\(|XMLHttpRequest|WebSocket|https?://", executable, re.I):
        raise ValueError("Viewer contains a possible network dependency")
    readme_links = re.findall(r"\[[^\]]+\]\(([^)]+)\)",
                              (output / "README.md").read_text(encoding="utf-8"))
    for link in readme_links:
        if not urlsplit(link).scheme and not (output / unquote(link)).is_file():
            raise ValueError(f"Missing README link: {link}")
    return {"text_files_scanned": checked, "private_pattern_matches": 0,
            "serialized_viewer_payload_matches": True, "network_dependencies": False,
            "existing_local_viewer_links": len(local_links),
            "existing_local_readme_links": len(readme_links)}


def prepare(source: Path, output: Path, overwrite: bool = False) -> dict:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if source == output or source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("Source and output must be separate, non-nested directories")
    workspace = Path(__file__).resolve().parents[1]
    selected = list(FIXED)
    for folder, pattern in (("plans", "*.json"), ("previews", "*.svg"),
                            ("responses", "*.json"), ("responses", "*.txt")):
        selected.extend(path.relative_to(source).as_posix()
                        for path in sorted((source / folder).glob(pattern)))
    if len(selected) != len(set(selected)):
        raise ValueError("Duplicate curated artifact")
    expected = set(selected) | GENERATED
    for relative in selected:
        path = source / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or linked source artifact: {relative}")
    if output.exists():
        existing = list(output.rglob("*"))
        if any(path.is_symlink() for path in existing):
            raise ValueError("Linked output paths are unsupported")
        if existing and not overwrite:
            raise FileExistsError("Output is not empty; pass --overwrite explicitly")
        unexpected = [path.relative_to(output).as_posix() for path in existing
                      if path.is_file() and path.relative_to(output).as_posix() not in expected]
        if unexpected:
            raise ValueError(f"Unrecognized existing output files: {unexpected}")
    original_summary = read_json(source / "summary.json")
    original_commands = read_json(source / "commands.json")
    normalizer = Normalizer(source, workspace, original_commands, original_summary)
    before_hashes = {relative: sha256(source / relative) for relative in selected}
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for relative in selected:
        src, dst = source / relative, output / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix == ".json":
            data = normalizer.value(read_json(src))
            if relative in {"summary.json", "commands.json", "fixtures/dwg-provenance.json"}:
                data["publication_paths_normalized"] = True
                data["publication_note"] = (
                    "Sanitized public copy; execution paths normalized; original numeric "
                    "evidence and recorded artifact hashes retained. See README.md.")
            write_json(dst, data)
        elif src.suffix == ".txt":
            dst.write_text(normalizer.text(src.read_text(encoding="utf-8")),
                           encoding="utf-8", newline="\n")
        else:
            shutil.copyfile(src, dst)
        records.append({"path": relative, "original_sha256": before_hashes[relative],
                        "public_sha256": sha256(dst),
                        "bytes_unchanged": before_hashes[relative] == sha256(dst)})
    summary = read_json(output / "summary.json")
    for state in ("before", "after"):
        for extension in ("glb", "obj", "dxf"):
            relative = f"models/{state}.{extension}"
            if sha256(output / relative) != original_summary[state]["models"][extension]["sha256"]:
                raise ValueError(f"Model identity differs from original evidence: {relative}")
    for original, digest in original_summary["source_sha256"].items():
        if sha256(output / normalizer.text(original)) != digest:
            raise ValueError("Source drawing identity differs from original evidence")
    if summary["source_sha256"] != normalizer.value(original_summary["source_sha256"]):
        raise ValueError("Recorded source drawing hashes were changed")
    module_path = Path(__file__).with_name("build_room_viewer.py")
    module_spec = importlib.util.spec_from_file_location("tcad_public_room_viewer", module_path)
    viewer = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(viewer)
    viewer.build_viewer(output, output / "viewer.html", overwrite=overwrite)
    readme = README
    for state in ("before", "after"):
        dimensions = " × ".join(f"{summary[state]['dimensions_mm'][axis]:g}"
                                for axis in ("length", "width", "height"))
        readme = readme.replace(f"__{state.upper()}_DIMENSIONS__", dimensions)
    (output / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    checks = validate_output(output, viewer)
    if any(sha256(source / relative) != digest for relative, digest in before_hashes.items()):
        raise ValueError("Original acceptance artifacts changed during publication")
    manifest = {
        "publication_paths_normalized": True,
        "fixture_classification": original_summary["fixture_classification"],
        "source_artifacts_preserved": True, "six_model_hashes_match_original_summary": True,
        "source_drawing_hashes_match_original_summary": True,
        "command_count": len(original_commands["commands"]),
        "response_file_count": sum(name.startswith("responses/") for name in selected),
        "checks": checks,
        "files": records,
        "generated_files": sorted(GENERATED),
        "generated_file_sha256": {name: sha256(output / name)
                                  for name in sorted(GENERATED - {"publication-manifest.json"})},
        "scope": "Byte-preserved controlled data; normalized historical transcripts; "
                 "regenerated offline viewer. Not an independent building benchmark.",
    }
    write_json(output / "publication-manifest.json", manifest)
    checks = validate_output(output, viewer)
    # The manifest's own second-pass scan adds one text file; avoid a self hash.
    manifest["checks"] = checks
    write_json(output / "publication-manifest.json", manifest)
    validate_output(output, viewer)
    files = [path for path in output.rglob("*") if path.is_file()]
    largest = max(files, key=lambda path: path.stat().st_size)
    return {"ok": True, "output": str(output), "files": len(files),
            "total_bytes": sum(path.stat().st_size for path in files),
            "largest_file": largest.relative_to(output).as_posix(),
            "largest_bytes": largest.stat().st_size, "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare(args.input, args.output, args.overwrite), ensure_ascii=False))


if __name__ == "__main__":
    main()
