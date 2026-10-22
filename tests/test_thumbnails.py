# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

import hashlib

from PIL import Image
import pytest

from agentcad.errors import CadError
from agentcad.thumbnails import create_thumbnail


@pytest.mark.parametrize("dimensions", [
    'width="420mm" height="296.5mm"',
    'width="100%" height="100%"',
    'width="1000" height="705.952"',
    '',
])
def test_physical_percent_and_fractional_svg_have_exact_requested_pixels(tmp_path, dimensions):
    svg = tmp_path / "source.svg"
    png = tmp_path / "preview.png"
    svg.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" {dimensions} viewBox="0 0 1000000 705952">'
                   '<path d="M0 0L1000000 705952" stroke="black"/></svg>', encoding="utf-8")
    before = svg.read_bytes()
    receipt = create_thumbnail(svg, png, max_side=320)
    assert receipt["width"] == 320
    assert receipt["height"] == round(320 * .705952)
    with Image.open(png) as image:
        assert image.size == (receipt["width"], receipt["height"])
        assert image.format == "PNG"
        image.verify()
    assert hashlib.sha256(png.read_bytes()).hexdigest() == receipt["sha256"]
    assert svg.read_bytes() == before


@pytest.mark.parametrize("body", [
    '<!DOCTYPE svg [<!ENTITY payload SYSTEM "file:///secret">]><svg viewBox="0 0 10 10"/>',
    '<svg viewBox="0 0 10 10"><image href="file:///private.png"/></svg>',
    '<svg viewBox="0 0 10 10"><script>alert(1)</script></svg>',
    '<svg viewBox="0 0 10 10"><style>.a{fill:url(https://example.test/a)}</style></svg>',
    '<svg viewBox="0 0 nan 10"><path d="M0 0L1 1"/></svg>',
    '<svg viewBox="0 0 10 10"><text>needs system font</text></svg>',
])
def test_thumbnail_refuses_unsafe_or_non_static_svg(tmp_path, body):
    svg = tmp_path / "source.svg"
    png = tmp_path / "preview.png"
    svg.write_text(body, encoding="utf-8")
    with pytest.raises(CadError) as error:
        create_thumbnail(svg, png)
    assert error.value.code == "INVALID_SVG"
    assert not png.exists()
