"""CUPS / PWG raster stream parser.

macOS renders every print job (PDF from Preview, Pages, Safari…) to a CUPS
raster stream with its own built-in filter (cgpdftoraster). Our PPD declares
that raster is the final format, so CUPS hands us exactly this stream over
the socket backend. This module turns it into Pillow images, one per page.

Supports:
  * sync words RaSt (v1), RaS2 (v2, compressed), RaS3 (v3) — both endiannesses
  * PWG raster (RaS2 + "PwgRaster" header) — same layout, same compression
  * 1/8-bit gray (W, SW, K) and 8-bit RGB / sRGB, chunky order
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass, field
from typing import BinaryIO, Iterator

from PIL import Image, ImageOps

HEADER_SIZE = 1796

# cups_cspace_t values we care about
CS_W, CS_RGB, CS_K, CS_SW, CS_SRGB, CS_ADOBERGB = 0, 1, 3, 18, 19, 20
LUMINANCE_SPACES = {CS_W, CS_SW}
RGB_SPACES = {CS_RGB, CS_SRGB, CS_ADOBERGB}

_SYNC = {
    b"RaSt": (">", 1), b"tSaR": ("<", 1),
    b"RaS2": (">", 2), b"2SaR": ("<", 2),
    b"RaS3": (">", 3), b"3SaR": ("<", 3),
}


@dataclass
class PageHeader:
    """The subset of cups_page_header2_t the bridge actually uses."""

    hw_resolution: tuple[int, int]
    width: int
    height: int
    bits_per_color: int
    bits_per_pixel: int
    bytes_per_line: int
    color_order: int
    color_space: int
    num_colors: int
    advance_distance: int
    page_size_name: str
    integers: list[int] = field(default_factory=list)  # cupsInteger[0..15]

    @property
    def bytes_per_pixel(self) -> int:
        return max(1, self.bits_per_pixel // 8)


@dataclass
class RasterPage:
    header: PageHeader
    image: Image.Image  # mode "L": 0 = black, 255 = white


def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace")


def _parse_header(raw: bytes, endian: str) -> PageHeader:
    u32 = lambda off: struct.unpack_from(endian + "I", raw, off)[0]  # noqa: E731
    return PageHeader(
        hw_resolution=(u32(276), u32(280)),
        width=u32(372),
        height=u32(376),
        bits_per_color=u32(384),
        bits_per_pixel=u32(388),
        bytes_per_line=u32(392),
        color_order=u32(396),
        color_space=u32(400),
        num_colors=u32(420),
        advance_distance=u32(256),
        page_size_name=_cstr(raw[1732:1796]),
        integers=list(struct.unpack_from(endian + "16I", raw, 452)),
    )


def _read_exact(stream: BinaryIO, n: int) -> bytes:
    buf = stream.read(n)
    if len(buf) != n:
        raise EOFError(f"truncated raster: wanted {n} bytes, got {len(buf)}")
    return buf


def _blank_byte(h: PageHeader) -> int:
    """Value of a 'white' byte in this colour space (used by RLE code 128)."""
    return 0xFF if (h.color_space in LUMINANCE_SPACES or h.color_space in RGB_SPACES) else 0x00


def _read_compressed(stream: BinaryIO, h: PageHeader) -> bytes:
    """Decode CUPS v2 / PWG PackBits-style compression for one page."""
    bpl, unit = h.bytes_per_line, h.bytes_per_pixel
    out = bytearray()
    y = 0
    while y < h.height:
        repeat = _read_exact(stream, 1)[0] + 1
        line = bytearray()
        while len(line) < bpl:
            code = _read_exact(stream, 1)[0]
            if code == 128:  # clear to end of line
                line.extend(bytes([_blank_byte(h)]) * (bpl - len(line)))
            elif code < 128:  # one pixel repeated code+1 times
                line.extend(_read_exact(stream, unit) * (code + 1))
            else:  # 257-code literal pixels
                line.extend(_read_exact(stream, (257 - code) * unit))
        line = line[:bpl]
        for _ in range(min(repeat, h.height - y)):
            out.extend(line)
        y += repeat
    return bytes(out)


def _to_gray(h: PageHeader, data: bytes) -> Image.Image:
    """Convert raw page bytes into a mode-L image (0 = black)."""
    size = (h.width, h.height)
    if h.color_order != 0 and h.num_colors > 1:
        raise ValueError("only chunky colour order is supported")

    if h.color_space in LUMINANCE_SPACES | {CS_K}:
        if h.bits_per_pixel == 8:
            img = Image.frombytes("L", size, data, "raw", "L", h.bytes_per_line)
        elif h.bits_per_pixel == 1:
            img = Image.frombytes("1", size, data, "raw", "1", h.bytes_per_line).convert("L")
        elif h.bits_per_pixel == 16:
            # keep the most significant byte of each (big-endian) 16-bit sample
            img = Image.frombytes("L", size, data[0::2])
        else:
            raise ValueError(f"unsupported gray depth: {h.bits_per_pixel} bpp")
        # In K space 0 = white / max = black, so flip to luminance.
        return ImageOps.invert(img) if h.color_space == CS_K else img

    if h.color_space in RGB_SPACES and h.bits_per_pixel == 24:
        return Image.frombytes("RGB", size, data, "raw", "RGB", h.bytes_per_line).convert("L")

    raise ValueError(
        f"unsupported raster: colorspace={h.color_space} bpp={h.bits_per_pixel} "
        "(the PPD asks for 8-bit gray; check that the right PPD is installed)"
    )


def iter_pages(stream: BinaryIO) -> Iterator[RasterPage]:
    """Yield every page of a CUPS/PWG raster stream."""
    sync = stream.read(4)
    if sync not in _SYNC:
        raise ValueError(f"not a CUPS/PWG raster stream (sync={sync!r})")
    endian, version = _SYNC[sync]

    while True:
        raw = stream.read(HEADER_SIZE)
        if not raw:
            return
        if len(raw) < HEADER_SIZE:
            raise EOFError("truncated page header")
        h = _parse_header(raw, endian)
        if h.width == 0 or h.height == 0:
            continue
        if version == 2:
            data = _read_compressed(stream, h)
        else:
            data = _read_exact(stream, h.bytes_per_line * h.height)
        yield RasterPage(h, _to_gray(h, data))


def is_raster(blob: bytes) -> bool:
    return blob[:4] in _SYNC


def pages_from_bytes(blob: bytes) -> list[RasterPage]:
    return list(iter_pages(io.BytesIO(blob)))
