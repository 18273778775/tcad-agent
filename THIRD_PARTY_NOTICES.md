# Third-party software

TCAD Agent CLI's original source, documentation and controlled example drawings
are copyright 2026 LiTao and licensed under Apache-2.0. This does not relicense
third-party software or claim its authorship. Dependencies are installed as their
own packages; their license and copyright files remain in those distributions.

## Direct Python dependencies

| Project | Upstream license | Use |
| --- | --- | --- |
| [ezdxf](https://github.com/mozman/ezdxf/blob/master/LICENSE) | MIT | DXF document model, geometry and SVG rendering |
| [trimesh](https://github.com/mikedh/trimesh/blob/main/LICENSE.md) | MIT | Mesh creation, loading and serialized room verification |
| [Pillow](https://github.com/python-pillow/Pillow/blob/main/LICENSE) | MIT-CMU | Image/font support used by rendering |
| [jsonschema](https://github.com/python-jsonschema/jsonschema/blob/main/COPYING) | MIT | Declarative command and editing-plan validation |

Transitive and development dependencies, including NumPy, fontTools, pyparsing,
typing_extensions, attrs, referencing, rpds-py, pytest and Ruff, retain their
upstream terms. `uv.lock` records the resolved packages and source archives.
No dependency source or wheel is vendored in this repository.

## Optional external DWG tools

[GNU LibreDWG](https://github.com/LibreDWG/libredwg) is licensed under
GPL-3.0-or-later. The optional installation script downloads a pinned official
release into the ignored `.tools/` directory and retains its `COPYING` file.
Those executables, DLLs and downloaded archives are not included in this
repository or the Python distribution. Their upstream license continues to
apply; the project's Apache-2.0 license does not replace it.

The optional upstream `sample_2000.dwg` integration-test fixture is downloaded
with its upstream GPL license and provenance into ignored `artifacts/`.
It is not the controlled room example published in `examples/room/`.

## Controlled examples and published preview

The `examples/room/` drawings are independently generated controlled acceptance
data. They are not copies of the proprietary viewer's files or the upstream
LibreDWG sample. The room preview image shows that controlled example and does
not bundle font files, a CAD runtime, or an external building project.

## Proprietary reference package

An old third-party viewer was inspected locally to understand its architecture.
Its executable, libraries, installer, patch and distribution are excluded. No
rights to that software are granted by this project's license. The independent
implementation and static-analysis limitations are documented in
[docs/reverse-engineering.md](docs/reverse-engineering.md).
