"""Calibration page (PNG bytes), shared by the CLI and the menu-bar command."""

from __future__ import annotations

from . import __version__


def make_testpage() -> bytes:
    """A small calibration page: text, gradient, fine lines, checkerboard."""
    import io

    from PIL import Image, ImageDraw, ImageFont

    w, h = 384, 300
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    try:
        big, small = ImageFont.load_default(size=28), ImageFont.load_default(size=16)
    except TypeError:  # Pillow < 10.1
        big = small = ImageFont.load_default()
    d.text((8, 6), "Phomemo T02", font=big, fill=0)
    d.text((8, 42), f"macOS bridge v{__version__} · 203 dpi · 384 px", font=small, fill=0)
    for x in range(w):  # gradient
        d.line([(x, 72), (x, 132)], fill=int(255 * x / (w - 1)))
    for i, lw in enumerate((1, 2, 3, 4)):  # line weights
        d.line([(8, 150 + i * 12), (w - 8, 150 + i * 12)], fill=0, width=lw)
    for y in range(205, 285, 10):  # checkerboard
        for x in range(8, w - 8, 10):
            if ((x // 10) + (y // 10)) % 2:
                d.rectangle([x, y, x + 9, y + 9], fill=0)
    d.rectangle([0, 0, w - 1, h - 1], outline=0)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
