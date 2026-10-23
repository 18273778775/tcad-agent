# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Headless, bounded SVG previews, reusable by a parsed-document sheet batch."""

from collections import Counter
from math import isfinite
from pathlib import Path
import re
from time import perf_counter
from xml.etree import ElementTree

from .converters import readable_dxf
from .errors import CadError
from .io import atomic_output, read_document


class _Diagnostics:
    """Count every event, but retain only bounded samples."""

    def __init__(self):
        self.counts = Counter()
        self.samples = {}

    def add(self, code, **sample):
        self.counts[code] += 1
        samples = self.samples.setdefault(code, [])
        sample = {key: str(value).encode("utf-8", "backslashreplace").decode("utf-8")[:240]
                  for key, value in sample.items()}
        if len(samples) < 4 and sample not in samples:
            samples.append(sample)

    def result(self):
        return [{"code": code, "count": count, "samples": self.samples.get(code, [])}
                for code, count in sorted(self.counts.items())]


def _validate_options(layout, types, layer, handles, max_entities, max_svg_bytes,
                      bbox, background, foreground, lineweight_scale, font_family,
                      hatch_mode):
    if not isinstance(layout, str) or not layout.strip():
        raise CadError("INVALID_LAYOUT", "A nonempty layout name is required.")
    if type(max_entities) is not int or not 1 <= max_entities <= 100000:
        raise CadError("INVALID_ARGUMENT", "max_entities must be between 1 and 100000.")
    if type(max_svg_bytes) is not int or not 1024 <= max_svg_bytes <= 64 * 1024 * 1024:
        raise CadError("INVALID_ARGUMENT", "max_svg_bytes must be between 1024 and 67108864.")
    if types is not None and (not isinstance(types, list) or
                              any(not isinstance(item, str) or not item for item in types)):
        raise CadError("INVALID_ARGUMENT", "Types must be entity type names.")
    if layer is not None and not isinstance(layer, str):
        raise CadError("INVALID_ARGUMENT", "Layer must be a string.")
    if bbox is not None:
        if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or
                any(type(value) not in (int, float) or not isfinite(value) for value in bbox) or
                bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
            raise CadError("INVALID_ARGUMENT", "bbox must be finite [xmin,ymin,xmax,ymax] with positive size.")
    if background not in ("white", "black", "layout"):
        raise CadError("INVALID_ARGUMENT", "background must be white, black, or layout.")
    if foreground not in ("cad", "monochrome"):
        raise CadError("INVALID_ARGUMENT", "foreground must be cad or monochrome.")
    if hatch_mode not in ("adaptive", "accurate"):
        raise CadError("INVALID_ARGUMENT", "hatch_mode must be adaptive or accurate.")
    if (type(lineweight_scale) not in (int, float) or not isfinite(lineweight_scale) or
            not 0.05 <= lineweight_scale <= 10):
        raise CadError("INVALID_ARGUMENT", "lineweight_scale must be between 0.05 and 10.")
    if font_family is not None and (not isinstance(font_family, str) or
                                    not font_family.strip() or len(font_family) > 128):
        raise CadError("INVALID_ARGUMENT", "font_family must be a nonempty installed font family, at most 128 characters.")
    selected_types = {item.upper() for item in types} if types is not None else None
    if handles is not None:
        from .engine import _validate_handles
        handles = set(_validate_handles(handles))
    return selected_types, handles


def _cjk_face(fonts, font_family):
    if font_family:
        face = fonts.find_best_match(family=font_family)
        if face is None:
            raise CadError("FONT_NOT_FOUND", "The requested font family is not installed.",
                           {"font_family": font_family})
        return face
    # Use installed fonts. Never download/copy platform fonts into an artifact.
    for name in ("simhei.ttf", "msyh.ttc", "Deng.ttf", "simsun.ttc",
                 "NotoSansCJK-Regular.ttc", "NotoSansSC-Regular.ttf", "wqy-zenhei.ttc"):
        if fonts.font_manager.has_font(name):
            return fonts.font_manager.get_font_face(name)
    for family in ("Noto Sans CJK SC", "Noto Sans SC", "WenQuanYi Zen Hei",
                   "PingFang SC", "Heiti SC", "Arial Unicode MS"):
        face = fonts.find_best_match(family=family)
        if face is not None:
            return face
    return None


def _text_renderer(diagnostics, fallback_face):
    from ezdxf.addons.drawing.unified_text_renderer import UnifiedTextRenderer

    class GlyphRenderer(UnifiedTextRenderer):
        """Use a real CJK cmap before producing portable glyph paths."""

        def _select(self, text, face):
            invalid = [ch for ch in text if 0xD800 <= ord(ch) <= 0xDFFF or
                       (ord(ch) < 32 and ch not in "\t\n\r")]
            if invalid:
                diagnostics.add("TEXT_ENCODING_REPLACED", codepoints=",".join(
                    f"U+{ord(ch):04X}" for ch in invalid[:8]))
                text = "".join("\ufffd" if ch in invalid else ch for ch in text)
            font = self.get_font(face)
            cmap = getattr(getattr(font, "glyph_cache", None), "cmap", None)
            if cmap is not None:
                missing = {ord(ch) for ch in text if not ch.isspace() and ord(ch) not in cmap}
            else:
                # SHX bigfont composition is unsupported by ezdxf.
                missing = {ord(ch) for ch in text if 0x2E80 <= ord(ch) <= 0x3134F}
            if missing and fallback_face is not None:
                fallback = self.get_font(fallback_face)
                fallback_cmap = getattr(getattr(fallback, "glyph_cache", None), "cmap", {})
                if all(code in fallback_cmap for code in missing):
                    diagnostics.add("FONT_SUBSTITUTED", source=face.filename or face.family,
                                    replacement=fallback_face.filename,
                                    scope="text measurement/path operation")
                    face, missing = fallback_face, set()
            if missing:
                diagnostics.add("MISSING_GLYPHS", font=face.filename or face.family,
                                codepoints=",".join(f"U+{code:04X}" for code in sorted(missing)[:16]))
            return text, face

        def get_text_line_width(self, text, font_face, cap_height=1.0):
            text, face = self._select(text, font_face)
            return super().get_text_line_width(text, face, cap_height)

        def get_text_path(self, text, font_face, cap_height=1.0):
            text, face = self._select(text, font_face)
            return super().get_text_path(text, face, cap_height)

        def get_text_glyph_paths(self, text, font_face, cap_height=1.0):
            text, face = self._select(text, font_face)
            try:
                return super().get_text_glyph_paths(text, face, cap_height)
            except (RuntimeError, ValueError) as exc:
                # The pipeline catches these exceptions; record the omission first.
                diagnostics.add("TEXT_RENDER_FAILED", reason=exc)
                raise

    return GlyphRenderer()


def render_document(document, output_path, layout="Model", overwrite=False,
                    types=None, layer=None, handles=None, max_entities=20000,
                    max_svg_bytes=8 * 1024 * 1024, bbox=None, background="white",
                    foreground="cad", lineweight_scale=1.0, font_family=None,
                    entities=None, bbox_cache=None, hatch_mode="adaptive",
                    layers=None, exclude_layers=None, annotate=None, legend=False,
                    max_annotations=200, resource_paths=None, font_paths=None,
                    font_map=None, max_resource_bytes=8 * 1024 * 1024, strict=False):
    """Render a parsed document, optionally clipping a sheet region in WCS.

    ``entities`` is an already assigned iterable of native entities; sheet batches
    reuse one document and spatial index without reparsing/traversing the full
    drawing per sheet. No source entities or text styles are modified. Text is
    stored as glyph paths, so display does not require platform fonts.
    """
    target = Path(output_path).expanduser().resolve()
    from .render_assets import validate_asset_options, PreviewResources, make_svg_backend, preview_quality
    validate_asset_options(resource_paths, font_paths, font_map, max_resource_bytes, strict)
    if target.suffix.lower() != ".svg":
        raise CadError("UNSUPPORTED_FORMAT", "Preview output must have a .svg extension.")
    selected_types, handles = _validate_options(
        layout, types, layer, handles, max_entities, max_svg_bytes, bbox,
        background, foreground, lineweight_scale, font_family, hatch_mode)
    for value in (layers, exclude_layers):
        if value is not None and (not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value)):
            raise CadError("INVALID_ARGUMENT", "layers/exclude_layers must be lists of layer names.")
    if annotate not in (None, "labels", "areas", "axes") or type(legend) is not bool:
        raise CadError("INVALID_ARGUMENT", "annotate must be labels, areas, or axes; legend must be boolean.")
    if type(max_annotations) is not int or not 1 <= max_annotations <= 2000:
        raise CadError("INVALID_ARGUMENT", "max_annotations must be between 1 and 2000.")
    allowed_layers = set(layers) if layers is not None else None
    blocked_layers = set(exclude_layers or [])
    try:
        from ezdxf import bbox as dxf_bbox
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing import layout as page_layout
        from ezdxf.addons.drawing.config import (
            Configuration, ImagePolicy, BackgroundPolicy, ColorPolicy)
        from ezdxf.fonts import fonts
        from ezdxf.math import BoundingBox2d
    except ImportError as exc:
        raise CadError("DEPENDENCY_MISSING", "SVG rendering requires the ezdxf drawing dependencies (Pillow).",
                       {"reason": str(exc)}) from exc
    try:
        selected_layout = document.layouts.get(layout)
    except KeyError as exc:
        raise CadError("LAYOUT_NOT_FOUND", "The drawing has no layout with this name.",
                       {"layout": layout, "available": document.layouts.names()}) from exc
    if not len(selected_layout):
        raise CadError("EMPTY_DRAWING", "The selected layout has no entities.", {"layout": layout})
    diagnostics = _Diagnostics()
    resources = PreviewResources(document, diagnostics, resource_paths, font_paths,
                                 font_map, max_resource_bytes)
    region = BoundingBox2d([(bbox[0], bbox[1]), (bbox[2], bbox[3])]) if bbox else None
    cache = bbox_cache if bbox_cache is not None else dxf_bbox.Cache()
    candidates = selected_layout if entities is None else entities
    selection, rejected_by_bbox, selected_count = [], 0, 0
    for entity in candidates:
        if ((selected_types is not None and entity.dxftype() not in selected_types) or
                (layer is not None and entity.dxf.get("layer", "0") != layer) or
                (handles is not None and (entity.dxf.handle or "").upper() not in handles)):
            continue
        selected_count += 1
        selection.append(entity)
    if not selected_count:
        raise CadError("EMPTY_DRAWING", "No entities match this preview selection.")
    if region is not None and entities is None:
        from .spatial import entity_bounds
        bounded, missing, _ = entity_bounds(selection)
        keep = set()
        for handle in missing[:4]:
            diagnostics.add("BBOX_UNAVAILABLE", handle=handle, selection="excluded; matches indexed bbox policy")
        for entity, box in bounded:
            if box[2] >= bbox[0] and box[0] <= bbox[2] and box[3] >= bbox[1] and box[1] <= bbox[3]:
                keep.add(entity.dxf.handle)
        original = len(selection)
        selection = [entity for entity in selection if entity.dxf.handle in keep]
        rejected_by_bbox = original - len(selection)
        selected_count = len(selection)
    if selected_count > max_entities:
        raise CadError("RENDER_SELECTION_TOO_LARGE", "Preview selection exceeds its entity budget; filter by type/layer/handle/bbox or explicitly raise max_entities.",
                       {"selected": selected_count, "max_entities": max_entities})
    stats = {"selected_native_entities": selected_count,
             "excluded_by_bbox": rejected_by_bbox,
             "selected_types": dict(sorted(Counter(entity.dxftype() for entity in selection).items())),
             "draw_calls": 0, "drawn_types": Counter(), "skipped_types": Counter(),
             "skip_reasons": Counter(), "drawn_layers": Counter(), "excluded_layers": Counter()}

    class ReportingFrontend(Frontend):
        def draw_image_entity(self, entity, properties):
            from ezdxf.addons.drawing.backend import ImageData
            from ezdxf.npshapes import NumpyPoints2d
            import numpy as np
            image_def = entity.image_def
            image = None
            if image_def is not None and entity.dxf.flags & entity.SHOW_IMAGE:
                image = resources.image(image_def.dxf.filename, entity.dxf.handle)
            if image is None:
                diagnostics.add("EXTERNAL_CONTENT_PLACEHOLDER", type="IMAGE", handle=entity.dxf.handle)
                return super().draw_image_entity(entity, properties)
            from PIL import ImageEnhance
            from ezdxf.addons.drawing.frontend import _blend_image_towards, _multiply_alpha
            from ezdxf.colors import RGB, RGBA
            if entity.dxf.contrast != 50:
                image = ImageEnhance.Contrast(image).enhance(entity.dxf.contrast / 50)
            if entity.dxf.fade:
                image = _blend_image_towards(image, entity.dxf.fade / 100,
                                             RGB.from_hex(self.ctx.current_layout_properties.background_color))
            if entity.dxf.brightness != 50:
                amount = entity.dxf.brightness / 50 - 1
                image = _blend_image_towards(image, abs(amount),
                                             RGBA(255, 255, 255, 255) if amount > 0 else RGBA(0, 0, 0, 255))
            if (entity.dxf.contrast != 50 or entity.dxf.fade or entity.dxf.brightness != 50):
                diagnostics.add("IMAGE_ADJUSTMENT_APPROXIMATED", handle=entity.dxf.handle)
            if not entity.dxf.flags & entity.USE_TRANSPARENCY:
                image.putalpha(255)
            if entity.transparency:
                image = _multiply_alpha(image, 1.0 - entity.transparency)
            self.pipeline.draw_image(ImageData(
                image=np.array(image), transform=entity.get_wcs_transform(),
                pixel_boundary_path=NumpyPoints2d(entity.pixel_boundary_path()),
                use_clipping_boundary=bool(entity.dxf.flags & entity.USE_CLIPPING_BOUNDARY),
                remove_outside=entity.dxf.clip_mode == 0), properties)
            points = [v.vec2 for v in entity.boundary_path_wcs()]
            self.pipeline.draw_solid_lines(list(zip(points, points[1:])), properties)

        def draw_composite_entity(self, entity, properties):
            if entity.dxftype() == "DIMENSION":
                # Anonymous dimension-block primitives commonly use layer 0.
                # Preserve their parent dimension layer during virtual drawing.
                self.ctx.push_state(properties)
                try:
                    return super().draw_composite_entity(entity, properties)
                finally:
                    self.ctx.pop_state()
            return super().draw_composite_entity(entity, properties)

        def draw_hatch_pattern(self, polygon, paths, properties):
            """Buffer a bounded complete pattern; never publish a timed-out prefix.

            Uses ezdxf's MIT-licensed public pattern/hatch geometry helpers. Dense
            patterns become solid boundary fills through the frontend's existing
            DenseHatchingLinesError fallback; accurate mode rejects instead.
            """
            from ezdxf.render import hatching
            if polygon.pattern is None or not len(polygon.pattern.lines):
                return
            minimum_spacing = self.config.min_hatch_line_distance
            if hatch_mode == "adaptive":
                if region is not None:
                    span = max(region.size.x, region.size.y)
                else:
                    extent = dxf_bbox.extents([polygon], fast=True)
                    span = max(extent.size.x, extent.size.y) if extent.has_data else 0
                minimum_spacing = max(minimum_spacing, span / 10000)

            def fallback(reason):
                detail = {"handle": polygon.dxf.handle,
                          "pattern": polygon.dxf.get("pattern_name", ""),
                          "reason": reason, "minimum_spacing": minimum_spacing,
                          "segment_limit": 20000, "time_limit_seconds": 2.0}
                if hatch_mode == "accurate":
                    raise CadError("RENDER_COMPLEXITY_EXCEEDED",
                                   "Accurate hatch preview exceeds a pattern budget; use adaptive mode or a smaller bbox.", detail)
                diagnostics.add("HATCH_SIMPLIFIED", **detail,
                                replacement="complete solid boundary fill; no partial pattern")
                raise hatching.DenseHatchingLinesError(reason)

            try:
                baselines = list(hatching.pattern_baselines(
                    polygon, min_hatch_line_distance=minimum_spacing, jiggle_origin=True))
            except hatching.DenseHatchingLinesError:
                fallback("pattern line spacing below preview density threshold")
            start = perf_counter()
            timed_out = False

            def timeout():
                nonlocal timed_out
                timed_out = perf_counter() - start > 2.0
                return timed_out

            lines = []
            ocs, elevation = polygon.ocs(), polygon.dxf.elevation.z
            properties.linetype_pattern = tuple()
            for baseline in baselines:
                for line in hatching.hatch_paths(baseline, paths, timeout):
                    pattern = baseline.pattern_renderer(line.distance)
                    for first, second in pattern.render(line.start, line.end):
                        if len(lines) >= 20000:
                            fallback("pattern segment budget exceeded")
                        if ocs.transform:
                            first = ocs.to_wcs((first.x, first.y, elevation))
                            second = ocs.to_wcs((second.x, second.y, elevation))
                        lines.append((first, second))
                if timed_out or timeout():
                    fallback("pattern time budget exceeded")
            self.pipeline.draw_solid_lines(lines, properties)

        def draw_entity(self, entity, properties):
            # INSERTs are traversed even if their own layer is excluded. The
            # resolved properties layer includes layer-0 inheritance in blocks.
            effective = properties.layer
            if entity.dxftype() != "INSERT" and ((allowed_layers is not None and effective not in allowed_layers)
                                                  or effective in blocked_layers):
                stats["excluded_layers"][effective] += 1
                return
            if entity.dxftype() != "INSERT" and region is not None:
                # Nested block leaves outside the chosen frame must not load
                # resources or poison this frame's quality report.
                extent = dxf_bbox.extents([entity], fast=True, cache=cache)
                if extent.has_data and not region.has_intersection(BoundingBox2d(extent)):
                    return
            stats["draw_calls"] += 1
            stats["drawn_types"][entity.dxftype()] += 1
            if entity.dxftype() == "POINT" and diagnostics.counts["RELATIVE_POINT_SIZE_CONFIGURATION"]:
                diagnostics.add("RELATIVE_POINT_SIZE_APPROXIMATED", handle=entity.dxf.handle)
            if entity.dxftype() != "INSERT":
                stats["drawn_layers"][effective] += 1
            if entity.dxftype() == "OLE2FRAME":
                diagnostics.add("EXTERNAL_CONTENT_PLACEHOLDER", type=entity.dxftype(), handle=entity.dxf.handle)
            if entity.dxftype() in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
                raw = entity.text if entity.dxftype() == "MTEXT" else entity.dxf.text
                codes = re.findall(r"%%\d{3}", raw)
                if codes:
                    diagnostics.add("CUSTOM_CAD_TEXT_CODES", handle=entity.dxf.handle, codes=",".join(codes[:8]))
            if entity.dxftype() == "INSERT":
                block = entity.block()
                if block is not None and block.block.is_xref:
                    diagnostics.add("XREF_UNRESOLVED", name=entity.dxf.name, handle=entity.dxf.handle)
            return super().draw_entity(entity, properties)

        def skip_entity(self, entity, msg):
            stats["skipped_types"][entity.dxftype()] += 1
            reason = str(msg)[:160]
            if reason not in stats["skip_reasons"] and len(stats["skip_reasons"]) >= 16:
                reason = "other reasons (sample limit reached)"
            stats["skip_reasons"][reason] += 1
            diagnostics.add("ENTITY_SKIPPED", type=entity.dxftype(),
                            handle=entity.dxf.get("handle", ""), reason=msg)

        def log_message(self, message):
            # This constructor warning occurs even in drawings without POINTs.
            # Escalate it only if the chosen selection actually renders a point.
            code = ("RELATIVE_POINT_SIZE_CONFIGURATION" if str(message) == "relative point size is not supported"
                    else "RENDERER_MESSAGE")
            diagnostics.add(code, message=message)

    try:
        resources.install_fonts(fonts)
        backend = make_svg_backend(region)
        context = RenderContext(document)
        fallback_face = _cjk_face(fonts, font_family)
        color_policy = ColorPolicy.COLOR
        if foreground == "monochrome":
            color_policy = ColorPolicy.WHITE if background == "black" else ColorPolicy.BLACK
        config = Configuration(image_policy=ImagePolicy.RECT,
                               background_policy={"white": BackgroundPolicy.WHITE,
                                                  "black": BackgroundPolicy.BLACK,
                                                  "layout": BackgroundPolicy.DEFAULT}[background],
                               color_policy=color_policy, lineweight_scaling=float(lineweight_scale))
        frontend = ReportingFrontend(context, backend, config=config, bbox_cache=cache)
        frontend.pipeline.text_engine = _text_renderer(diagnostics, fallback_face)

        def font_override(entity, properties):
            if entity.dxftype() not in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
                return
            style_name = entity.dxf.get("style", "Standard")
            mapped = resources.font_aliases.get(style_name.casefold())
            try:
                style = document.styles.get(style_name)
                for role in ("font", "bigfont"):
                    requested = style.dxf.get(role, "")
                    if role == "font" and mapped is None:
                        mapped = resources.font_aliases.get(requested.casefold())
                    if role == "bigfont" and requested:
                        diagnostics.add("BIGFONT_COMPOSITION_UNSUPPORTED", style=style_name,
                                        requested=requested)
                    if requested and mapped is None and not fonts.font_manager.has_font(Path(requested).name):
                        diagnostics.add("FONT_FILE_UNAVAILABLE", style=style_name,
                                        role=role, requested=requested)
            except KeyError:
                diagnostics.add("TEXT_STYLE_UNAVAILABLE", style=style_name)
            text = entity.plain_text()
            cjk = {ord(ch) for ch in text if 0x2E80 <= ord(ch) <= 0x3134F}
            old = properties.font
            if mapped is not None:
                properties.font = mapped
                diagnostics.add("FONT_MAPPED", style=style_name, source=old.filename if old else "default",
                                replacement=mapped.filename)
                return
            needs_fallback = False
            if cjk:
                original = frontend.text_engine.get_font(old or frontend.pipeline.default_font_face)
                cmap = getattr(getattr(original, "glyph_cache", None), "cmap", {})
                needs_fallback = not all(code in cmap for code in cjk)
            if fallback_face is not None and (font_family or needs_fallback):
                if old is None or old.filename != fallback_face.filename:
                    diagnostics.add("FONT_SUBSTITUTED", source=old.filename if old else "default",
                                    replacement=fallback_face.filename, scope="text entity")
                properties.font = fallback_face

        frontend.push_property_override_function(font_override)
        if entities is None:
            selected_ids = {id(entity) for entity in selection}
            frontend.draw_layout(selected_layout, filter_func=lambda entity: id(entity) in selected_ids)
        else:
            context.set_current_layout(selected_layout)
            frontend.set_background(context.current_layout_properties.background_color)
            frontend.draw_entities(selection)
            frontend.pipeline.finalize()
        records_before_clip = len(backend.records)
        if region is not None:
            player = backend.player()
            from ezdxf.addons.drawing.recorder import ImageRecord, crop_records_rect
            from ezdxf.npshapes import NumpyPoints2d
            from ezdxf.math import Vec2
            # ezdxf 1.4 drops partially intersecting IMAGE records while cropping.
            # Keep draw order and exact SVG image clipping instead. The record's
            # bound is clipped for page fitting; its raster transform stays intact.
            clipped = []
            distance = max(region.size.x, region.size.y) * 1e-6
            for record in player.records:
                if isinstance(record, ImageRecord):
                    box = record.bbox()
                    if region.has_intersection(box):
                        left, bottom = max(box.extmin.x, region.extmin.x), max(box.extmin.y, region.extmin.y)
                        right, top = min(box.extmax.x, region.extmax.x), min(box.extmax.y, region.extmax.y)
                        record.boundary = NumpyPoints2d(Vec2.generate(
                            [(left, bottom), (right, bottom), (right, top), (left, top)]))
                        clipped.append(record)
                else:
                    clipped.extend(crop_records_rect([record], region, distance))
            backend.records = clipped
        bounds = backend.player().bbox()
        if not backend.records or not bounds.has_data:
            raise CadError("EMPTY_DRAWING", "The selected region has no visible renderable geometry.",
                           {"diagnostics": diagnostics.result()})
        stats["records_before_clip"] = records_before_clip
        stats["records_after_clip"] = len(backend.records)
        stats["geometry_bounds"] = [bounds.extmin.x, bounds.extmin.y, bounds.extmax.x, bounds.extmax.y]
        annotation_result = {"count": 0, "complete": True, "items": []}
        if annotate or legend:
            from .annotations import draw_annotations
            annotation_result = draw_annotations(
                frontend, document, selection, bbox, bounds, annotate, legend,
                allowed_layers, blocked_layers, fallback_face, max_annotations)
            if annotation_result["count"]:
                bounds = backend.player().bbox()
        # Fit the requested frame, including its intentional blank margins.
        if region is not None:
            bounds.extend([region.extmin, region.extmax])
        minimum, maximum, size = bounds.extmin, bounds.extmax, bounds.size
        padding = max(size.x, size.y, 1.0) * 0.01
        bounds = BoundingBox2d([(minimum.x - padding, minimum.y - padding),
                                (maximum.x + padding, maximum.y + padding)])
        preview_scale = 420.0 / max(bounds.size.x, bounds.size.y)
        page = page_layout.Page(max(bounds.size.x * preview_scale, 1.0),
                                max(bounds.size.y * preview_scale, 1.0))
        rendered = backend.get_string(page, render_box=bounds)
        output_bytes = len(rendered.encode("utf-8"))
        if output_bytes > max_svg_bytes:
            raise CadError("PREVIEW_TOO_LARGE", "SVG exceeds its output file budget; reduce the preview selection or explicitly raise max_svg_bytes.",
                           {"bytes": output_bytes, "max_bytes": max_svg_bytes})
        root = ElementTree.fromstring(rendered)
        if "viewBox" not in root.attrib:
            raise CadError("EMPTY_DRAWING", "The renderer produced an empty preview.")
        quality = preview_quality(diagnostics, stats, annotation_result, strict)
        if strict and not quality["complete"]:
            raise CadError("PREVIEW_INCOMPLETE", "Strict preview refused publication because selected content is missing or substituted.",
                           {"preview_quality": quality, "diagnostics": diagnostics.result(),
                            "resources": resources.result()})
        with atomic_output(target, overwrite=overwrite) as temporary:
            temporary.write_bytes(rendered.encode("utf-8"))
            ElementTree.parse(temporary)
    except CadError:
        raise
    except Exception as exc:
        raise CadError("RENDER_FAILED", "Cannot render this layout.",
                       {"reason": str(exc), "diagnostics": diagnostics.result()}) from exc
    finally:
        resources.restore_fonts(fonts)
    for field in ("drawn_types", "skipped_types", "skip_reasons", "drawn_layers", "excluded_layers"):
        stats[field] = dict(sorted(stats[field].items()))
    return {"output": str(target), "format": "svg", "layout": selected_layout.name,
            "entity_count": selected_count, "layout_entity_count": len(selected_layout),
            "bytes": output_bytes, "max_entities": max_entities,
            "selection_scope": "native layout entities; nested block rendering is supervised by resource limits",
            "bbox": list(bbox) if bbox else None, "background": background, "foreground": foreground,
            "lineweight_scale": float(lineweight_scale),
            "hatch": {"mode": hatch_mode, "maximum_pattern_segments": 20000,
                      "maximum_pattern_seconds": 2.0,
                      "density_spacing": "frame longest side / 10000 in adaptive mode"},
            "font": {"fallback_family": fallback_face.family if fallback_face else None,
                     "fallback_file": fallback_face.filename if fallback_face else None,
                     "text_representation": "embedded glyph paths; no external font dependency",
                     "substitution_metrics": "installed fallback metrics; CAD SHX/bigfont metrics may differ"},
            "render_stats": stats, "diagnostics": diagnostics.result(), "diagnostics_complete": True,
            "layer_selection": {"include": layers, "exclude": exclude_layers or [],
                                "scope": "resolved leaf layers including INSERT layer-0 inheritance"},
            "annotations": annotation_result,
            "preview_quality": quality, "resources": resources.result(),
            "warnings": [
                "SVG is fitted to a preview page of at most 420 mm per side; its physical size is not drawing scale.",
                "Preview uses a 2D top projection; ACIS solids and some 3D features are unsupported.",
                "Font substitutions use installed font glyph paths; inspect diagnostics for missing glyphs and custom CAD codes.",
                "Local linked rasters are embedded when found under permitted resource roots; missing rasters and unresolved xrefs remain explicit diagnostics.",
                "Diagnostic counts include nested entities and repeated text measurement/path operations; samples are bounded."]}


def render_drawing(input_path, output_path=None, layout="Model", overwrite=False,
                   types=None, layer=None, handles=None, max_entities=20000,
                   max_svg_bytes=8 * 1024 * 1024, bbox=None, background="white",
                   foreground="cad", lineweight_scale=1.0, font_family=None,
                   hatch_mode="adaptive", layers=None, exclude_layers=None,
                   annotate=None, legend=False, max_annotations=200, width=1600,
                   sheet=None, manifest=None, output_dir=None, source_info=None,
                   resource_paths=None, font_paths=None, font_map=None,
                   max_resource_bytes=8 * 1024 * 1024, strict=False):
    """Convert/read a drawing once, then render a layout or clipped region."""
    source = Path(input_path).expanduser().resolve()
    from .render_assets import validate_asset_options
    validate_asset_options(resource_paths, font_paths, font_map, max_resource_bytes, strict)
    _validate_options(layout, types, layer, handles, max_entities, max_svg_bytes,
                      bbox, background, foreground, lineweight_scale, font_family, hatch_mode)
    with readable_dxf(source) as dxf_path:
        from .render_jobs import render_selection
        selected_roots = None
        if bbox is not None and sheet is None:
            from .spatial import select_handles
            selected_roots = select_handles(dxf_path, layout=layout, bbox=bbox)["handles"]
        document = read_document(dxf_path)
        entities = [document.entitydb[h] for h in selected_roots] if selected_roots is not None else None
        result = render_selection(document, dxf_path, output_path, layout=layout,
                                 overwrite=overwrite, types=types, layer=layer, handles=handles,
                                 max_entities=max_entities, max_svg_bytes=max_svg_bytes,
                                 bbox=bbox, background=background, foreground=foreground,
                                 lineweight_scale=lineweight_scale, font_family=font_family,
                                 hatch_mode=hatch_mode, layers=layers, exclude_layers=exclude_layers,
                                 annotate=annotate, legend=legend, max_annotations=max_annotations,
                                 width=width, sheet=sheet, manifest=manifest, output_dir=output_dir,
                                 source_info=source_info, entities=entities,
                                 resource_paths=resource_paths, font_paths=font_paths,
                                 font_map=font_map, max_resource_bytes=max_resource_bytes, strict=strict)
    result["input"] = str(source)
    if source.suffix.lower() == ".dwg":
        result["warnings"].append("DWG conversion may lose unsupported entities or properties.")
    return result
