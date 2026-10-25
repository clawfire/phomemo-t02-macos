"""Spooled bytes → printer bytes. Pure CPU work, no Bluetooth (easy to test)."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

from . import raster
from .config import Config
from .imaging import RenderOptions, load_image_file, render_page
from .protocol import JobSettings, build_job, estimated_print_seconds

# cupsInteger slots written by the PPD options (see ppd/Phomemo-T02.ppd)
OPT_DENSITY, OPT_DITHER, OPT_FEED, OPT_TRIM = 0, 1, 2, 3
DITHER_CODES = {1: "floyd", 2: "threshold", 3: "atkinson"}


@dataclass
class PreparedJob:
    payload: bytes
    pages: int
    seconds: float
    settings: JobSettings


def _options_from_header(h: raster.PageHeader, cfg: Config) -> tuple[JobSettings, RenderOptions]:
    ints = h.integers + [0] * 16
    density = ints[OPT_DENSITY] if 1 <= ints[OPT_DENSITY] <= 4 else cfg.density
    dither = DITHER_CODES.get(ints[OPT_DITHER], cfg.dither)
    feed = ints[OPT_FEED] if ints[OPT_FEED] else cfg.extra_feed
    trim = {1: True, 2: False}.get(ints[OPT_TRIM], cfg.trim)
    return (
        JobSettings(density=density, extra_feed=feed),
        RenderOptions(dither=dither, photo_dither=cfg.photo_dither, threshold=cfg.threshold, trim=trim),
    )


def prepare(blob: bytes, cfg: Config, override: dict | None = None) -> PreparedJob:
    """Decode a CUPS/PWG raster stream (or a plain image) into a T02 job."""
    override = override or {}
    if raster.is_raster(blob):
        pages = raster.pages_from_bytes(blob)
        if not pages:
            raise ValueError("flux raster vide")
        settings, ropts = _options_from_header(pages[0].header, cfg)
        sources = [(p.image, p.header.hw_resolution[0]) for p in pages]
    elif blob[:5] == b"%PDF-":
        raise ValueError("PDF : imprime-le via ⌘P ou « lp -d Phomemo_T02 fichier.pdf » (macOS fait le rendu)")
    else:
        try:
            img = load_image_file(io.BytesIO(blob))  # type: ignore[arg-type]
        except Exception as exc:
            raise ValueError("format non reconnu (ni raster CUPS, ni image)") from exc
        settings = JobSettings(density=cfg.density, extra_feed=cfg.extra_feed)
        ropts = RenderOptions(dither=cfg.dither, photo_dither=cfg.photo_dither, threshold=cfg.threshold, trim=cfg.trim)
        sources = [(img, None)]

    for k, v in override.items():  # CLI flags win
        if hasattr(settings, k):
            setattr(settings, k, v)
        if hasattr(ropts, k):
            setattr(ropts, k, v)

    bw_pages: list[Image.Image] = [render_page(img, ropts, dpi) for img, dpi in sources]
    return PreparedJob(
        payload=build_job(bw_pages, settings),
        pages=len(bw_pages),
        seconds=estimated_print_seconds(bw_pages),
        settings=settings,
    )
