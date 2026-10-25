"""Phomemo T02 wire protocol (ESC/POS-shaped raster over BLE).

Byte sequence mirrors what the official Phomemo app sends, as captured by
TiMini-Print (`phomemo_esc` / packets_type "t02") and cross-checked with
phomymo and vivier/phomemo-tools:

    1B 40                 ESC @            init
    1F 11 02 <d>          density          (1, 2, 4, 4)
    1F 11 37 <c>          density coeff.   (100, 100, 100, 150)
    1F 11 0B              media: continuous / plain paper
    1F 11 35 00           raster data is uncompressed
    -- per page --
    1D 76 30 00 wL wH hL hH <rows>    GS v 0, ≤255 rows per block, MSB first
    1B 64 02              ESC d 2          print & feed (page separator)
    -- end --
    1B 64 02              ESC d 2          (×2 in total on the last page)
    1B 4A <n>             ESC J n          optional extra feed, n dots
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from .imaging import PRINT_WIDTH, pack_rows

INIT = b"\x1b\x40"
DENSITY = b"\x1f\x11\x02"
DENSITY_COEFF = b"\x1f\x11\x37"
MEDIA_PLAIN = b"\x1f\x11\x0b"
UNCOMPRESSED = b"\x1f\x11\x35\x00"
FEED_LINES_2 = b"\x1b\x64\x02"
FEED_DOTS = b"\x1b\x4a"

# level → (density byte, coefficient) as sent by the official app
DENSITY_LEVELS = {1: (1, 100), 2: (2, 100), 3: (4, 100), 4: (4, 150)}

TOP_PADDING_ROWS = 4      # the app adds 4 blank rows on top
MAX_ROWS_PER_BLOCK = 255  # conservative, like phomemo-tools

# Status notifications (BLE notify char ff03): 1A <type> <value>
STATUS_PAPER, PAPER_OUT = 0x06, 0x88
STATUS_COVER, COVER_OPEN = 0x05, 0x98
STATUS_HEAT, OVERHEAT = 0x03, 0xA9
STATUS_BATTERY = 0x04


@dataclass
class JobSettings:
    density: int = 2       # 1..4
    extra_feed: int = 0    # dots fed after the last page (0..255)


def header(settings: JobSettings) -> bytes:
    level = min(4, max(1, settings.density))
    d, c = DENSITY_LEVELS[level]
    return INIT + DENSITY + bytes([d]) + DENSITY_COEFF + bytes([c]) + MEDIA_PLAIN + UNCOMPRESSED


def raster_blocks(bw: Image.Image) -> bytes:
    """GS v 0 blocks for one bilevel page (with the app's top padding)."""
    if bw.width != PRINT_WIDTH:
        raise ValueError(f"page must be {PRINT_WIDTH} px wide, got {bw.width}")
    width_bytes = PRINT_WIDTH // 8
    rows = bytes(width_bytes * TOP_PADDING_ROWS) + pack_rows(bw)
    height = len(rows) // width_bytes
    out = bytearray()
    for start in range(0, height, MAX_ROWS_PER_BLOCK):
        n = min(MAX_ROWS_PER_BLOCK, height - start)
        out += b"\x1d\x76\x30\x00"
        out += width_bytes.to_bytes(2, "little") + n.to_bytes(2, "little")
        out += rows[start * width_bytes:(start + n) * width_bytes]
    return bytes(out)


def build_job(pages: list[Image.Image], settings: JobSettings) -> bytes:
    """Whole print job as one byte string, ready to be streamed over BLE."""
    if not pages:
        raise ValueError("nothing to print")
    out = bytearray(header(settings))
    for i, page in enumerate(pages):
        out += raster_blocks(page)
        out += FEED_LINES_2
        if i == len(pages) - 1:
            out += FEED_LINES_2
    if settings.extra_feed:
        out += FEED_DOTS + bytes([min(255, settings.extra_feed)])
    return bytes(out)


def estimated_print_seconds(pages: list[Image.Image]) -> float:
    """Rough mechanical print time (TiMini's model: 0.125 mm/row × 0.11 s/mm)."""
    rows = sum(p.height + TOP_PADDING_ROWS for p in pages)
    return rows * 0.125 * 0.110


def describe_status(frame: bytes) -> str | None:
    """Human-readable warning for a 1A-prefixed status notification, else None."""
    if len(frame) < 3 or frame[0] != 0x1A:
        return None
    kind, value = frame[1], frame[2]
    if kind == STATUS_PAPER and value == PAPER_OUT:
        return "Plus de papier"
    if kind == STATUS_COVER and value == COVER_OPEN:
        return "Capot ouvert"
    if kind == STATUS_HEAT and value == OVERHEAT:
        return "Tête d'impression en surchauffe"
    return None
