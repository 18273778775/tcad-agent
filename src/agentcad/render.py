# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Headless 2D SVG previews using ezdxf's native drawing backend."""

from pathlib import Path
from xml.etree import ElementTree

from .converters import readable_dxf
from .errors import CadError
from .io import atomic_output, read_document


def render_drawing(input_path, output_path, layout="Model", overwrite=False):
    """Render one named layout to SVG without launching a GUI."""
    source = Path(input_path).expanduser().resolve()
    target = Path(output_path).expanduser().resolve()
    if target.suffix.lower() != ".svg":
        raise CadError("UNSUPPORTED_FORMAT", "Preview output must have a .svg extension.")
    if not isinstance(layout, str) or not layout.strip():
        raise CadError("INVALID_LAYOUT", "A nonempty layout name is required.")
    try:
        from ezdxf.addons.drawing import Frontend, RenderContext, svg
        from ezdxf.addons.drawing import layout as page_layout
        from ezdxf.addons.drawing.config import Configuration, ImagePolicy
        from ezdxf.math import BoundingBox2d
    except ImportError as exc:
        raise CadError(
            "DEPENDENCY_MISSING",
            "SVG rendering requires the ezdxf drawing dependencies (Pillow).",
            {"reason": str(exc)},
        ) from exc

    with readable_dxf(source) as dxf_path:
        document = read_document(dxf_path)
        try:
            selected = document.layouts.get(layout)
        except KeyError as exc:
            raise CadError(
                "LAYOUT_NOT_FOUND",
                "The drawing has no layout with this name.",
                {"layout": layout, "available": document.layouts.names()},
            ) from exc
        if len(selected) == 0:
            raise CadError(
                "EMPTY_DRAWING", "The selected layout has no entities.", {"layout": selected.name}
            )
        try:
            backend = svg.SVGBackend()
            # Linked raster files are represented by their rectangles. A preview
            # must remain self-contained and must not depend on linked file paths.
            config = Configuration(image_policy=ImagePolicy.RECT)
            Frontend(RenderContext(document), backend, config=config).draw_layout(selected)
            bounds = backend.player().bbox()
            if not backend.records or not bounds.has_data:
                raise CadError(
                    "EMPTY_DRAWING", "The selected layout has no visible renderable geometry."
                )
            # Automatic page sizing otherwise produces an empty SVG for a single
            # horizontal/vertical line or point because one extent is zero.
            minimum = bounds.extmin
            maximum = bounds.extmax
            size = bounds.size
            padding = max(size.x, size.y, 1.0) * 0.01
            bounds = BoundingBox2d(
                [
                    (
                        minimum.x - (padding if size.x == 0 else 0),
                        minimum.y - (padding if size.y == 0 else 0),
                    ),
                    (
                        maximum.x + (padding if size.x == 0 else 0),
                        maximum.y + (padding if size.y == 0 else 0),
                    ),
                ]
            )
            # Plot on a preview-sized page. Treating a 27 m architectural sheet
            # as a 27 m SVG makes physical lineweights disappear when fitted to
            # a browser window. Keep geometry aspect ratio and fit the longest
            # page side to 420 mm; this is a preview, not a 1:1 print export.
            preview_scale = 420.0 / max(bounds.size.x, bounds.size.y)
            page = page_layout.Page(
                max(bounds.size.x * preview_scale, 1.0), max(bounds.size.y * preview_scale, 1.0)
            )
            rendered = backend.get_string(page, render_box=bounds)
            root = ElementTree.fromstring(rendered)
            if "viewBox" not in root.attrib:
                raise CadError("EMPTY_DRAWING", "The renderer produced an empty preview.")
            with atomic_output(target, overwrite=overwrite) as temporary:
                temporary.write_text(rendered, encoding="utf-8")
                ElementTree.parse(temporary)
        except CadError:
            raise
        except Exception as exc:
            raise CadError(
                "RENDER_FAILED",
                "Cannot render this layout.",
                {"reason": str(exc)},
            ) from exc
    warnings = [
        "SVG is fitted to a preview page of at most 420 mm per side; its physical size is not drawing scale.",
        "Preview uses a 2D top projection; ACIS solids and some 3D features are unsupported.",
        "Text appearance depends on installed fonts; substitutions may change its appearance.",
        "External references are not resolved; linked raster images are shown as rectangles.",
    ]
    if source.suffix.lower() == ".dwg":
        warnings.append("DWG conversion may lose unsupported entities or properties.")
    return {
        "input": str(source),
        "output": str(target),
        "format": "svg",
        "layout": selected.name,
        "entity_count": len(selected),
        "warnings": warnings,
    }
