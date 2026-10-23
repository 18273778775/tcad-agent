# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Local-only preview resources and the SVG raster backend missing in ezdxf.

DXF resource names are untrusted. A document can read images only under its own
directory or user supplied roots; it cannot request downloads or arbitrary files.
Images are decoded and re-encoded, never copied into SVG as active markup.
"""

from base64 import b64encode
from io import BytesIO
from pathlib import Path, PureWindowsPath
import re
from xml.etree import ElementTree as ET

from .errors import CadError


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
IMAGE_FORMATS = {"PNG", "JPEG", "BMP", "GIF", "TIFF", "WEBP"}
FONT_SUFFIXES = {".ttf", ".ttc", ".otf", ".shx", ".shp", ".lff"}


def validate_asset_options(resource_paths, font_paths, font_map, max_resource_bytes, strict):
    for name, value in (("resource_paths", resource_paths), ("font_paths", font_paths)):
        if value is not None and (not isinstance(value, list) or len(value) > 16 or
                                  any(not isinstance(v, str) or not v or len(v) > 4096 for v in value)):
            raise CadError("INVALID_ARGUMENT", f"{name} must be a list of at most 16 local directories.")
    if font_map is not None and (not isinstance(font_map, dict) or len(font_map) > 128 or
                                any(not isinstance(k, str) or not k or len(k) > 256 or
                                    not isinstance(v, str) or not v or len(v) > 4096
                                    for k, v in font_map.items())):
        raise CadError("INVALID_ARGUMENT", "font_map must map at most 128 style/font names to local font files.")
    if type(max_resource_bytes) is not int or not 1024 <= max_resource_bytes <= 256 * 1024 * 1024:
        raise CadError("INVALID_ARGUMENT", "max_resource_bytes must be between 1024 and 268435456.")
    if type(strict) is not bool:
        raise CadError("INVALID_ARGUMENT", "strict must be boolean.")


def _local_path(value):
    if (re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value) or value.startswith(("\\\\", "//")) or
            "\x00" in value):
        raise CadError("RESOURCE_ACCESS_DENIED", "Preview resources must be local files; network paths are not allowed.")
    return Path(value).expanduser().resolve()


class PreviewResources:
    def __init__(self, document, diagnostics, resource_paths=None, font_paths=None,
                 font_map=None, max_resource_bytes=8 * 1024 * 1024):
        self.diagnostics = diagnostics
        self.maximum = max_resource_bytes
        self.bytes_read = 0
        self.decoded_bytes = 0
        self.images = {}
        self.image_count = 0
        self.font_count = 0
        self.font_aliases = {}
        self._original_manager = None
        self.roots = [_local_path(value) for value in resource_paths or []]
        filename = getattr(document, "filename", None)
        if filename:
            self.roots.insert(0, _local_path(str(filename)).parent)
        for root in self.roots:
            if not root.is_dir():
                raise CadError("INVALID_ARGUMENT", "A preview resource directory does not exist.", {"directory": str(root)})
        self.font_paths = [_local_path(value) for value in font_paths or []]
        self.font_map = font_map or {}

    def _read(self, path):
        size = path.stat().st_size
        if size > self.maximum - self.bytes_read:
            raise CadError("RESOURCE_BUDGET_EXCEEDED", "Preview resources exceed the cumulative byte budget.",
                           {"max_resource_bytes": self.maximum, "bytes_read": self.bytes_read,
                            "resource": path.name, "resource_bytes": size})
        # Bounded read also detects growth after stat rather than reading to EOF.
        with path.open("rb") as stream:
            data = stream.read(self.maximum - self.bytes_read + 1)
        if len(data) > self.maximum - self.bytes_read:
            raise CadError("RESOURCE_BUDGET_EXCEEDED", "A preview resource grew beyond the byte budget.")
        self.bytes_read += len(data)
        return data

    def _image_path(self, requested):
        if not isinstance(requested, str) or not requested or len(requested) > 4096:
            raise CadError("RESOURCE_ACCESS_DENIED", "Invalid raster resource name.")
        # Backslashes in DXF are Windows separators even on Unix.
        normalized = requested.replace("\\", "/")
        win = PureWindowsPath(requested)
        if (re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", requested) or normalized.startswith("//") or
                ".." in normalized.split("/") or "\x00" in requested or
                (":" in normalized and not win.drive)):
            raise CadError("RESOURCE_ACCESS_DENIED", "Raster resource network names and parent traversal are not allowed.")
        if Path(normalized).suffix.lower() not in IMAGE_SUFFIXES:
            raise CadError("RESOURCE_FORMAT_UNSUPPORTED", "Only supported raster image formats can be embedded.")
        absolute = Path(normalized).is_absolute() or win.is_absolute()
        candidates = [Path(normalized).resolve()] if absolute else [root / normalized for root in self.roots]
        # A relocated drawing can recover an absolute CAD link by basename, but
        # only from an explicitly permitted directory, never its original drive.
        if absolute:
            candidates.extend(root / win.name for root in self.roots)
        for candidate in candidates:
            resolved = candidate.resolve()
            if not any(resolved.is_relative_to(root) for root in self.roots):
                continue
            if resolved.is_file():
                return resolved
        raise CadError("RESOURCE_NOT_FOUND", "The linked raster is absent from the permitted resource directories.",
                       {"resource": win.name})

    def image(self, requested, handle):
        from PIL import Image, UnidentifiedImageError
        try:
            path = self._image_path(requested)
            if path in self.images:
                image = self.images[path]
                return image.copy() if image is not None else None
            data = self._read(path)
            with Image.open(BytesIO(data)) as original:
                if original.format not in IMAGE_FORMATS:
                    raise CadError("RESOURCE_FORMAT_UNSUPPORTED", "Raster content does not match a supported image format.")
                decoded = original.width * original.height * 4
                limit = min(self.maximum * 4, 128 * 1024 * 1024)
                if decoded > limit - self.decoded_bytes:
                    raise CadError("RESOURCE_BUDGET_EXCEEDED", "Decoded raster pixels exceed the resource memory budget.",
                                   {"max_decoded_bytes": limit, "resource": path.name})
                self.decoded_bytes += decoded
                if getattr(original, "n_frames", 1) > 1:
                    self.diagnostics.add("IMAGE_MULTIFRAME_FIRST_ONLY", handle=handle, resource=path.name)
                image = original.convert("RGBA")
            self.images[path] = image
            self.image_count += 1
            self.diagnostics.add("IMAGE_EMBEDDED", handle=handle, resource=path.name,
                                 width=image.width, height=image.height)
            return image.copy()
        except (CadError, OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            code = exc.code if isinstance(exc, CadError) else "RESOURCE_DECODE_FAILED"
            self.diagnostics.add(code, handle=handle, resource=PureWindowsPath(str(requested)).name,
                                 reason=str(exc))
            return None

    def install_fonts(self, fonts):
        """Use an isolated manager; restore it even after a strict render fails."""
        if not self.font_paths and not self.font_map:
            return
        files = []
        for folder in self.font_paths:
            if not folder.is_dir():
                raise CadError("INVALID_ARGUMENT", "A font directory does not exist.", {"directory": str(folder)})
            # Deliberately nonrecursive: a font directory cannot cause an
            # unbounded walk through a user's drive or symlink tree.
            for path in folder.iterdir():
                if path.is_file() and path.suffix.lower() in FONT_SUFFIXES:
                    if len(files) >= 256:
                        raise CadError("RESOURCE_BUDGET_EXCEEDED", "At most 256 explicit font files can be loaded.")
                    files.append(path.resolve())
        mapped = {key.casefold(): _local_path(value) for key, value in self.font_map.items()}
        files.extend(mapped.values())
        manager = fonts.FontManager()
        manager.loads(fonts.font_manager.dumps())
        from ezdxf.fonts.font_manager import get_ttf_font_face, get_shape_file_font_face
        for path in dict.fromkeys(files):
            if not path.is_file() or path.suffix.lower() not in FONT_SUFFIXES:
                raise CadError("RESOURCE_FORMAT_UNSUPPORTED", "An explicit font is missing or has an unsupported format.",
                               {"resource": path.name})
            self._read(path)
            try:
                face = (get_ttf_font_face(path) if path.suffix.lower() in {".ttf", ".ttc", ".otf"}
                        else get_shape_file_font_face(path))
            except Exception as exc:
                raise CadError("FONT_LOAD_FAILED", "Cannot read an explicit font file.",
                               {"resource": path.name, "reason": str(exc)}) from exc
            manager._font_cache.add_entry(path, face)
            self.font_count += 1
        self.font_aliases = {key: manager.get_font_face(path.name) for key, path in mapped.items()}
        self._original_manager = fonts.font_manager
        fonts.font_manager = manager

    def restore_fonts(self, fonts):
        if self._original_manager is not None:
            fonts.font_manager = self._original_manager
            self._original_manager = None

    def result(self):
        return {"policy": "local source directory and explicit resource roots; no network retrieval",
                "embedded_images": self.image_count, "explicit_fonts_loaded": self.font_count,
                "bytes_read": self.bytes_read, "max_resource_bytes": self.maximum,
                "decoded_raster_bytes": self.decoded_bytes,
                "font_directory_scope": "immediate files only, at most 256"}


def make_svg_backend(clip_region=None):
    """Add safe inline raster output; upstream SVGRenderBackend drops images."""
    from ezdxf.addons.drawing import svg
    from ezdxf.math import Vec3

    class RasterSVGRenderBackend(svg.SVGRenderBackend):
        def draw_image(self, image_data, properties):
            from PIL import Image
            image = Image.fromarray(image_data.image)
            stream = BytesIO()
            image.save(stream, format="PNG")
            # Pixel row origin is top left, DXF IMAGE origin is bottom left.
            transform = image_data.flip_matrix() @ image_data.transform
            origin = transform.transform(Vec3(0, 0, 0))
            ux = transform.transform_direction(Vec3(1, 0, 0))
            uy = transform.transform_direction(Vec3(0, 1, 0))
            parent = self.entities
            if clip_region is not None:
                p1, p2 = recording.transformation_matrix.transform_vertices(
                    [(clip_region.extmin.x, clip_region.extmin.y, 0),
                     (clip_region.extmax.x, clip_region.extmax.y, 0)])
                index = len(self.root.findall(".//clipPath"))
                clip_id = f"tcad-raster-region-{index}"
                clip = ET.SubElement(self.root.find("defs"), "clipPath", id=clip_id,
                                     clipPathUnits="userSpaceOnUse")
                ET.SubElement(clip, "rect", x=str(min(p1.x, p2.x)), y=str(min(p1.y, p2.y)),
                              width=str(abs(p2.x - p1.x)), height=str(abs(p2.y - p1.y)))
                parent = ET.SubElement(parent, "g", {"clip-path": f"url(#{clip_id})"})
            width, height = image.size
            ET.SubElement(parent, "image", {
                "width": str(width), "height": str(height), "preserveAspectRatio": "none",
                "transform": f"matrix({ux.x:g} {ux.y:g} {uy.x:g} {uy.y:g} {origin.x:g} {origin.y:g})",
                "href": "data:image/png;base64," + b64encode(stream.getvalue()).decode("ascii")})

    class RasterSVGBackend(svg.SVGBackend):
        @staticmethod
        def make_backend(page, settings):
            return RasterSVGRenderBackend(page, settings)

    recording = RasterSVGBackend()
    return recording


def preview_quality(diagnostics, stats, annotation_result, strict):
    content_codes = {"MISSING_GLYPHS", "TEXT_ENCODING_REPLACED", "TEXT_RENDER_FAILED",
                     "TEXT_STYLE_UNAVAILABLE", "EXTERNAL_CONTENT_PLACEHOLDER", "XREF_UNRESOLVED",
                     "ENTITY_SKIPPED", "RESOURCE_ACCESS_DENIED", "RESOURCE_NOT_FOUND",
                     "RESOURCE_FORMAT_UNSUPPORTED", "RESOURCE_BUDGET_EXCEEDED", "RESOURCE_DECODE_FAILED",
                     "BBOX_UNAVAILABLE", "RENDERER_MESSAGE"}
    fidelity_codes = {"FONT_SUBSTITUTED", "FONT_FILE_UNAVAILABLE", "BIGFONT_COMPOSITION_UNSUPPORTED",
                      "FONT_MAPPED", "HATCH_SIMPLIFIED", "CUSTOM_CAD_TEXT_CODES",
                      "IMAGE_MULTIFRAME_FIRST_ONLY", "IMAGE_ADJUSTMENT_APPROXIMATED",
                      "RELATIVE_POINT_SIZE_APPROXIMATED"}
    counts = diagnostics.counts
    content_issues = sorted(code for code in content_codes if counts[code])
    fidelity_issues = sorted(code for code in fidelity_codes if counts[code])
    if not annotation_result.get("complete", True):
        content_issues.append("ANNOTATIONS_TRUNCATED")
    content_complete = not content_issues
    fidelity_complete = content_complete and not fidelity_issues
    return {"status": "complete" if fidelity_complete else "degraded" if content_complete else "incomplete",
            "complete": fidelity_complete, "content_complete": content_complete,
            "fidelity_complete": fidelity_complete, "strict": strict,
            "scope": "selected visible 2D entities after native, bbox and resolved layer filtering",
            "content_issues": content_issues, "fidelity_issues": fidelity_issues,
            "issue_counts": {code: counts[code] for code in sorted(set(content_issues + fidelity_issues))},
            "selected_native_entities": stats["selected_native_entities"],
            "checked_leaf_draw_calls": sum(stats["drawn_layers"].values()),
            "original_cad_equivalence_verified": False,
            "limits": ["Checks reported renderer omissions and substitutions; not a pixel comparison against the originating CAD application.",
                       "Top projection; overlapping source text is retained without repositioning or inferring engineering meaning."]}
