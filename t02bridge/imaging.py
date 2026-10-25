"""Turn a rendered page into the T02's 1-bit, 384-dot-wide world.

Pipeline per page:  fit to 384 px  →  (optional) trim trailing blank  →  dither  →  pack bits
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageChops, ImageFilter, ImageOps

PRINT_WIDTH = 384  # dots — 48 mm at 203 dpi
PRINT_DPI = 203


@dataclass
class RenderOptions:
    dither: str = "auto"      # auto | floyd | atkinson | threshold
    photo_dither: str = "atkinson"  # what "auto" uses on grey areas: atkinson | floyd
    threshold: int = 160      # used by "threshold" (and "auto" on line art)
    trim: bool = True         # drop trailing white rows (receipt-style printing)
    min_height: int = 16      # never print less than this many rows


def fit_width(img: Image.Image, src_dpi: int | None = None) -> Image.Image:
    """Scale/pad an L image so it is exactly PRINT_WIDTH wide, centred."""
    img = img.convert("L")
    if src_dpi and src_dpi != PRINT_DPI and abs(img.width * PRINT_DPI / src_dpi - PRINT_WIDTH) < 8:
        # rendered at another resolution but for the right physical width
        img = img.resize((PRINT_WIDTH, round(img.height * PRINT_DPI / src_dpi)), Image.LANCZOS)
    if img.width > PRINT_WIDTH:
        ratio = PRINT_WIDTH / img.width
        img = img.resize((PRINT_WIDTH, max(1, round(img.height * ratio))), Image.LANCZOS)
    if img.width < PRINT_WIDTH:
        canvas = Image.new("L", (PRINT_WIDTH, img.height), 255)
        canvas.paste(img, ((PRINT_WIDTH - img.width) // 2, 0))
        img = canvas
    return img


def trim_blank(img: Image.Image, min_height: int, margin: int = 8) -> Image.Image:
    """Crop leading and trailing near-white rows (saves paper on short receipts).

    macOS centres a short page on a long medium, so blank shows up on top too.
    """
    ink = ImageOps.invert(img).point(lambda v: 255 if v > 24 else 0)  # darker than ~230
    bbox = ink.getbbox()
    if bbox is None:
        return img.crop((0, 0, img.width, min(img.height, min_height)))
    top = max(0, bbox[1] - margin)
    bottom = min(img.height, max(top + min_height, bbox[3] + margin))
    return img.crop((0, top, img.width, bottom))


def _halftone_mask(img: Image.Image) -> Image.Image:
    """Mask of *flat* mid-tone areas (photos, gradients, fills).

    Anti-aliased text edges are mid-tone too, but only 1–2 px thin: a 5×5
    min/max test rejects them, so text stays crisp while real greys get dithered.
    """
    flat_mid = ImageChops.multiply(
        img.filter(ImageFilter.MaxFilter(5)).point(lambda v: 255 if v < 225 else 0),
        img.filter(ImageFilter.MinFilter(5)).point(lambda v: 255 if v > 30 else 0),
    )
    return flat_mid.filter(ImageFilter.MaxFilter(5))  # grow a bit to cover region borders


def _atkinson(img: Image.Image) -> Image.Image:
    """Atkinson dithering — lighter, punchier result on thermal paper than Floyd–Steinberg."""
    w, h = img.size
    px = [float(v) for v in img.convert("L").tobytes()]
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            old = px[i]
            new = 255.0 if old >= 128 else 0.0
            err = (old - new) / 8.0
            px[i] = new
            if err:
                if x + 1 < w: px[i + 1] += err
                if x + 2 < w: px[i + 2] += err
                if y + 1 < h:
                    j = i + w
                    if x > 0: px[j - 1] += err
                    px[j] += err
                    if x + 1 < w: px[j + 1] += err
                if y + 2 < h: px[i + 2 * w] += err
    out = Image.frombytes("L", (w, h), bytes(int(v) for v in px))
    return out.convert("1", dither=Image.Dither.NONE)


def to_bilevel(img: Image.Image, opts: RenderOptions) -> Image.Image:
    """Return a mode-"1" image (Pillow convention: 1 = white)."""
    mode = opts.dither
    t = opts.threshold
    sharp = img.point(lambda v: 255 if v >= t else 0)
    if mode == "auto":
        mask = _halftone_mask(img)
        bbox = mask.getbbox()
        if bbox is None:  # pure line art
            return sharp.convert("1", dither=Image.Dither.NONE)
        # Only the grey region is dithered (cheaper on long receipts), with the
        # configured photo algorithm — Atkinson by default: higher contrast,
        # fewer isolated dots, survives thermal dot gain better than FS.
        region = img.crop(bbox)
        if opts.photo_dither == "floyd":
            dithered = region.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
        else:
            dithered = _atkinson(region)
        layer = sharp.copy()
        layer.paste(dithered.convert("L"), bbox[:2])
        return Image.composite(layer, sharp, mask).convert("1", dither=Image.Dither.NONE)
    if mode == "threshold":
        return sharp.convert("1", dither=Image.Dither.NONE)
    if mode == "atkinson":
        return _atkinson(img)
    return img.convert("1", dither=Image.Dither.FLOYDSTEINBERG)


def render_page(img: Image.Image, opts: RenderOptions, src_dpi: int | None = None) -> Image.Image:
    img = fit_width(img, src_dpi)
    if opts.trim:
        img = trim_blank(img, opts.min_height)
    return to_bilevel(img, opts)


def pack_rows(bw: Image.Image) -> bytes:
    """Pack a mode-1 image into printer bytes: MSB first, 1 = burn (black)."""
    if bw.mode != "1" or bw.width % 8:
        raise ValueError("expected a mode-1 image whose width is a multiple of 8")
    return bytes(b ^ 0xFF for b in bw.tobytes())


def load_image_file(path: str) -> Image.Image:
    """PNG/JPEG/etc. → L, for the CLI `print` command (no CUPS involved)."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    return img.convert("L")
