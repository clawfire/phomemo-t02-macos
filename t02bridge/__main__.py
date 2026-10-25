"""CLI:  python -m t02bridge {serve|status|scan|retry|print|testpage|dry-run}

  serve            run the background bridge (what the LaunchAgent starts)
  scan [--save]    find nearby T02s; --save pins the first one in config.json
  status [--ping]  bridge + printer state (--ping queries the T02 first)
  retry [--list]   send failed jobs back to the running bridge
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
from .testpage import make_testpage


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


def _cmd_status(cfg: Config, ping: bool) -> int:
    import json
    import time

    from .client import command
    from .config import STATUS_FILE

    if ping:
        try:
            command(cfg, "status")
        except OSError as exc:
            print(f"✗ le pont ne répond pas ({exc})")
            return 1
        time.sleep(6)  # leave time for scan + connect + query
    if not STATUS_FILE.exists():
        print("Pas encore d'état publié (le pont tourne-t-il ?)")
        return 1
    st = json.loads(STATUS_FILE.read_text())
    age = time.time() - st.get("updated", 0)
    alive = age < 90
    print(f"Pont      : {'actif' if alive else f'muet depuis {age/60:.0f} min'} (v{st.get('version')})")
    print(f"État      : {st.get('state')} {st.get('detail') or ''}".rstrip())
    print(f"Bluetooth : {'connecté' if st.get('connected') else 'déconnecté'}")
    if st.get("battery") is not None:
        mins = (time.time() - st["battery_at"]) / 60
        print(f"Batterie  : {st['battery']} (il y a {mins:.0f} min)")
    if st.get("paper"):
        print(f"Papier    : {'OK' if st['paper'] == 'ok' else 'VIDE'}")
    print(f"File      : {st.get('queue', 0)} en attente · {st.get('failed', 0)} raté(s)")
    if st.get("last_error"):
        print(f"Dernière erreur : {st['last_error']}")
    return 0 if alive else 1


def _cmd_retry(cfg: Config, list_only: bool) -> int:
    """Hand failed jobs back to the running bridge, exactly like CUPS does (socket)."""
    import time

    from .config import FAILED_DIR

    jobs = sorted(FAILED_DIR.glob("*.job"))
    if not jobs:
        print("Aucun job raté. ✨")
        return 0
    for j in jobs:
        age_h = (time.time() - j.stat().st_mtime) / 3600
        print(f"{j.stem}  {j.stat().st_size // 1024:>5} Ko  raté il y a {age_h:.0f} h")
    if list_only:
        return 0

    from .client import send

    sent = 0
    for j in jobs:
        try:
            send(cfg, j.read_bytes())
        except OSError as exc:
            print(f"✗ le pont ne répond pas sur {cfg.host}:{cfg.port} ({exc}).")
            print("  Relance-le : launchctl kickstart -k gui/$(id -u)/net.clawfire.t02bridge")
            break
        j.unlink()
        sent += 1
    print(f"→ {sent}/{len(jobs)} job(s) remis en file d'attente (suis le log pour l'impression)")
    return 0 if sent == len(jobs) else 1


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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="t02bridge", description="Phomemo T02 ↔ macOS bridge")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    s = sub.add_parser("scan")
    s.add_argument("--save", action="store_true")
    st = sub.add_parser("status", help="état du pont et de la T02")
    st.add_argument("--ping", action="store_true", help="interroge la T02 avant d'afficher")
    r = sub.add_parser("retry", help="renvoie les jobs ratés (failed/) au pont")
    r.add_argument("--list", action="store_true", help="liste sans renvoyer")
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
    if args.cmd == "status":
        return _cmd_status(cfg, args.ping)
    if args.cmd == "retry":
        return _cmd_retry(cfg, args.list)
    if args.cmd == "testpage":
        return asyncio.run(_print_blobs(cfg, [make_testpage()], _overrides(args)))
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
