# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Small PNG gallery images from the CLI's self-contained glyph-path SVGs."""

import hashlib
import base64
import binascii
from io import BytesIO
from math import isfinite
from pathlib import Path
import re
from xml.etree import ElementTree

from .errors import CadError
from .io import atomic_output

MAX_INPUT_BYTES = 64 * 1024 * 1024


def validate_static_svg_resources(root):
    """Allow local fragments and verified inline PNGs, never active/remote data."""
    from PIL import Image
    identifiers = {node.attrib["id"] for node in root.iter() if "id" in node.attrib}
    decoded_pixels = 0
    image_count = 0
    for element in root.iter():
        kind = element.tag.rsplit("}", 1)[-1]
        if kind in {"script", "foreignObject", "text", "tspan"}:
            raise ValueError("Only static glyph-path previews are supported")
        for key, value in element.attrib.items():
            if key.rsplit("}", 1)[-1] in {"href", "src"}:
                if value.startswith("#") and value[1:] in identifiers:
                    continue
                if kind != "image" or not value.startswith("data:image/png;base64,"):
                    raise ValueError("Only internal fragments and embedded PNG images are supported")
                try:
                    raw = base64.b64decode(value.split(",", 1)[1], validate=True)
                    with Image.open(BytesIO(raw)) as image:
                        if image.format != "PNG":
                            raise ValueError("Embedded raster must be PNG")
                        decoded_pixels += image.width * image.height
                        if decoded_pixels > 32 * 1024 * 1024:
                            raise ValueError("Embedded raster pixel budget exceeded")
                        image.verify()
                    image_count += 1
                except (binascii.Error, OSError, Image.DecompressionBombError) as exc:
                    raise ValueError("Invalid embedded PNG") from exc
        values = list(element.attrib.values())
        if kind == "style":
            values.append(element.text or "")
        for value in values:
            if re.search(r"@import|@font-face|file://", value, re.I):
                raise ValueError("External CSS resources are not supported")
            if "\\" in value and (kind == "style" or "url" in value.lower()):
                raise ValueError("Escaped resource syntax is not supported")
            for match in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", value, re.I):
                reference = match.group(2).strip()
                if not reference.startswith("#") or reference[1:] not in identifiers:
                    raise ValueError("Only existing internal SVG resource URLs are supported")
    return image_count


def create_thumbnail(svg_path, png_path, max_side=1280,
                     max_png_bytes=4 * 1024 * 1024, overwrite=False):
    """Create one bounded PNG without Node, GUI tools, fonts, or linked resources.

    Only self-contained static SVGs with an explicit viewBox are accepted. This
    helper rasterizes already-generated previews; it does not reopen a CAD file.
    """
    source = Path(svg_path).expanduser().resolve()
    target = Path(png_path).expanduser().resolve()
    if source.suffix.lower() != ".svg" or target.suffix.lower() != ".png":
        raise CadError("UNSUPPORTED_FORMAT", "Thumbnail input must be SVG and output must be PNG.")
    if type(max_side) is not int or not 16 <= max_side <= 4096:
        raise CadError("INVALID_ARGUMENT", "max_side must be between 16 and 4096 pixels.")
    if type(max_png_bytes) is not int or not 1024 <= max_png_bytes <= 16 * 1024 * 1024:
        raise CadError("INVALID_ARGUMENT", "max_png_bytes must be between 1024 and 16777216.")
    try:
        # Reading at most the cap plus one byte also protects against a source
        # growing between stat and read, without allocating an unbounded buffer.
        with source.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise CadError("FILE_NOT_FOUND", "Cannot read the thumbnail source SVG.",
                       {"input": str(source), "reason": str(exc)}) from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise CadError("PREVIEW_TOO_LARGE", "Thumbnail source SVG exceeds its byte budget.",
                       {"max_bytes": MAX_INPUT_BYTES})
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.IGNORECASE):
        raise CadError("INVALID_SVG", "Thumbnail SVG must not contain document declarations or entities.")
    try:
        text = raw.decode("utf-8-sig")
        root = ElementTree.fromstring(text)
        if root.tag.rsplit("}", 1)[-1] != "svg":
            raise ValueError("Root element must be svg")
        box = [float(value) for value in re.split(r"[\s,]+", root.attrib["viewBox"].strip())]
        if len(box) != 4 or not all(isfinite(value) for value in box) or min(box[2:]) <= 0:
            raise ValueError("viewBox must have finite positive width and height")
        validate_static_svg_resources(root)
    except (UnicodeError, ElementTree.ParseError, KeyError, ValueError) as exc:
        raise CadError("INVALID_SVG", "Cannot rasterize this self-contained preview.",
                       {"reason": str(exc)}) from exc
    scale = max_side / max(box[2], box[3])
    width, height = max(1, round(box[2] * scale)), max(1, round(box[3] * scale))
    # Physical/percentage SVG root sizes can be converted and rounded by usvg
    # before rescaling, producing a one-pixel mismatch or an invalid zero size.
    # Set the requested intrinsic pixel size first; keep the exact source viewBox
    # so geometry and clipping remain unchanged. Explicit namespaces are valid
    # XML and avoid modifying namespace registration globally during rendering.
    root.set("width", str(width))
    root.set("height", str(height))
    raster_svg = ElementTree.tostring(root, encoding="unicode")
    try:
        import resvg_py
        from PIL import Image
    except ImportError as exc:
        raise CadError("DEPENDENCY_MISSING", "PNG thumbnails require resvg_py and Pillow.",
                       {"reason": str(exc)}) from exc
    try:
        png = resvg_py.svg_to_bytes(svg_string=raster_svg, width=width, height=height,
                                    dpi=96,
                                    skip_system_fonts=True, background="#ffffff",
                                    log_information=False)
        if len(png) > max_png_bytes:
            raise CadError("THUMBNAIL_TOO_LARGE", "PNG thumbnail exceeds its output byte budget.",
                           {"bytes": len(png), "max_bytes": max_png_bytes})
        with Image.open(BytesIO(png)) as check:
            if check.format != "PNG" or check.size != (width, height):
                raise ValueError("Renderer output dimensions or format differ from the request")
            check.verify()
        with atomic_output(target, overwrite=overwrite) as temporary:
            temporary.write_bytes(png)
    except CadError:
        raise
    except Exception as exc:
        raise CadError("THUMBNAIL_RENDER_FAILED", "Cannot create this PNG thumbnail.",
                       {"reason": str(exc)}) from exc
    return {"output": str(target), "width": width, "height": height,
            "bytes": len(png), "sha256": hashlib.sha256(png).hexdigest(),
            "renderer": "resvg_py", "renderer_version": resvg_py.__version__}
