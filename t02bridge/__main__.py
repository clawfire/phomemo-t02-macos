"""CLI:  python -m t02bridge {serve|scan|print|testpage|dry-run}

  serve            run the background bridge (what the LaunchAgent starts)
  scan [--save]    find nearby T02s; --save pins the first one in config.json
  print FILE…      print images or a CUPS raster file directly (no CUPS)
  testpage         print a calibration page
  dry-run FILE -o OUT.bin   build printer bytes without Bluetooth (debug)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__, ble, pipeline
from .config import Config, ensure_dirs


LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _setup_logging(verbose: bool, to_file: bool = False) -> None:
    """Console for CLI commands; a rotating file (1 MB × 3) for the daemon.

    In daemon mode launchd's stdout/stderr (launchd.log) only catches crashes,
    so nothing grows forever.
    """
    level = logging.DEBUG if verbose else logging.INFO
    if to_file:
        from logging.handlers import RotatingFileHandler

        from .config import LOG_DIR

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            LOG_DIR / "bridge.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT, "%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("bleak").setLevel(logging.WARNING)


def _overrides(args) -> dict:
    o = {}
    if getattr(args, "density", None):
        o["density"] = args.density
    if getattr(args, "dither", None):
        o["dither"] = args.dither
    if getattr(args, "no_trim", False):
        o["trim"] = False
    if getattr(args, "feed", None) is not None:
        o["extra_feed"] = args.feed
    return o


async def _cmd_scan(cfg: Config, save: bool) -> int:
    found = await ble.scan()
    if not found:
        print("Aucune T02 trouvée. Allume-la (bouton 2 s) et réessaie.")
        return 1
    for d in found:
        print(f"{d.name:<12} {d.address}")
    if save:
        cfg.printer_name, cfg.address = found[0].name, found[0].address
        cfg.save()
        print(f"→ enregistrée : {cfg.printer_name} ({cfg.address})")
    return 0


async def _print_blobs(cfg: Config, blobs: list[bytes], override: dict) -> int:
    from .server import Bridge

    bridge = Bridge(cfg)
    try:
        for blob in blobs:  # one BLE connection for all files
            job = pipeline.prepare(blob, cfg, override)
            print(f"{job.pages} page(s), {len(job.payload)} octets → T02…")
            await bridge._print_over_ble(job)
    finally:
        if bridge._idle_task is not None:
            bridge._idle_task.cancel()
        await bridge._drop_connection()
    print("OK ✓")
    return 0


def _testpage() -> bytes:
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="t02bridge", description="Phomemo T02 ↔ macOS bridge")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    s = sub.add_parser("scan")
    s.add_argument("--save", action="store_true")
    for name in ("print", "dry-run", "testpage"):
        sp = sub.add_parser(name)
        if name != "testpage":
            sp.add_argument("files", nargs="+")
        sp.add_argument("--density", type=int, choices=[1, 2, 3, 4])
        sp.add_argument("--dither", choices=["auto", "floyd", "atkinson", "threshold"])
        sp.add_argument("--no-trim", action="store_true")
        sp.add_argument("--feed", type=int)
        if name == "dry-run":
            sp.add_argument("-o", "--output", required=True)
    args = p.parse_args(argv)
    _setup_logging(args.verbose, to_file=args.cmd in (None, "serve"))
    cfg = Config.load()
    ensure_dirs()

    if args.cmd in (None, "serve"):
        from .server import Bridge

        try:
            asyncio.run(Bridge(cfg).serve())
        except KeyboardInterrupt:
            pass
        return 0
    if args.cmd == "scan":
        return asyncio.run(_cmd_scan(cfg, args.save))
    if args.cmd == "testpage":
        return asyncio.run(_print_blobs(cfg, [_testpage()], _overrides(args)))
    blobs = [open(f, "rb").read() for f in args.files]
    if args.cmd == "print":
        return asyncio.run(_print_blobs(cfg, blobs, _overrides(args)))
    if args.cmd == "dry-run":
        with open(args.output, "wb") as out:
            for blob in blobs:
                job = pipeline.prepare(blob, cfg, _overrides(args))
                out.write(job.payload)
                print(f"{job.pages} page(s), {len(job.payload)} octets, ~{job.seconds:.1f}s", file=sys.stderr)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
