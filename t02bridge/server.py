"""Background bridge: CUPS socket backend  →  spool  →  worker  →  BLE.

CUPS talks to socket://127.0.0.1:9100 like it would to any network printer.
We swallow the job instantly (so the macOS print queue empties right away),
spool it to disk, and a single worker converts + prints it in the background,
retrying while the printer is asleep or out of range.

The bridge also publishes its state in status.json (read by the menu-bar app)
and accepts tiny "#T02:<command>" messages on the same socket.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, ble, pipeline
from .client import COMMAND_PREFIX, COMMANDS
from .config import FAILED_DIR, SPOOL_DIR, STATUS_FILE, Config, ensure_dirs
from .testpage import make_testpage

log = logging.getLogger("t02.bridge")

BACKOFF = [5, 15, 30, 60, 120, 300]  # seconds between BLE attempts
HEARTBEAT = 30                        # status.json refresh, so the menu can tell we're alive
_counter = itertools.count()


def notify(cfg: Config, message: str, subtitle: str = "") -> None:
    """Native macOS notification (best effort, never blocks the worker)."""
    if not cfg.notify or not shutil.which("osascript"):
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    script = f'display notification "{esc(message)}" with title "Phomemo T02" subtitle "{esc(subtitle)}"'
    subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Bridge:
    def __init__(self, cfg: Config, transport_factory=None):
        self.cfg = cfg
        self.queue: asyncio.Queue[Path | str] = asyncio.Queue()
        self.cpu = ThreadPoolExecutor(max_workers=1, thread_name_prefix="t02-render")
        # Injected in tests to replace real Bluetooth.
        self.print_bytes = transport_factory or self._print_over_ble
        self._conn: ble.T02Connection | None = None      # warm BLE link
        self._idle_task: asyncio.Future | None = None    # closes it after keepalive
        self.status: dict = {
            "version": __version__, "state": "idle", "detail": "",
            "connected": False, "battery": None, "battery_at": None, "paper": None,
            "queue": 0, "failed": 0, "last_job_at": None, "last_job_ok": None,
            "last_error": None, "updated": time.time(), "pid": os.getpid(),
        }

    # ---- status --------------------------------------------------------
    def set_status(self, **changes) -> None:
        """Update and atomically publish status.json (never raises)."""
        self.status.update(changes)
        self.status["queue"] = self.queue.qsize()
        self.status["failed"] = len(list(FAILED_DIR.glob("*.job"))) if FAILED_DIR.exists() else 0
        self.status["connected"] = bool(self._conn and self._conn.is_connected)
        self.status["updated"] = time.time()
        try:
            STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.status, ensure_ascii=False, indent=1))
            tmp.replace(STATUS_FILE)
        except OSError as exc:
            log.debug("status.json non écrit : %s", exc)

    def _absorb_printer_state(self, conn: ble.T02Connection) -> None:
        changes: dict = {}
        if conn.battery is not None:
            changes.update(battery=conn.battery, battery_at=time.time())
        if conn.paper is not None:
            changes["paper"] = conn.paper
        if changes:
            self.set_status(**changes)

    async def _heartbeat(self) -> None:
        while True:
            self.set_status()
            await asyncio.sleep(HEARTBEAT)

    # ---- intake --------------------------------------------------------
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{next(_counter):04d}"
        tmp = SPOOL_DIR / f".{job_id}.part"
        size = 0
        head = b""
        try:
            with tmp.open("wb") as fh:
                while chunk := await reader.read(65536):
                    if not head:
                        head = chunk[:64]
                    fh.write(chunk)
                    size += len(chunk)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        if size == 0:  # CUPS / snmp probes open & close without data
            tmp.unlink(missing_ok=True)
            return
        if size < 64 and head.startswith(COMMAND_PREFIX):  # control message, not a job
            tmp.unlink(missing_ok=True)
            name = head[len(COMMAND_PREFIX):].strip().decode("ascii", "replace")
            if name in COMMANDS:
                log.info("commande « %s » reçue", name)
                await self.queue.put(name)
                self.set_status()
            return
        final = SPOOL_DIR / f"{job_id}.job"
        tmp.replace(final)
        log.info("job %s reçu (%d octets) → file d'attente", job_id, size)
        await self.queue.put(final)
        self.set_status()

    # ---- worker --------------------------------------------------------
    async def worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            item = await self.queue.get()
            try:
                if isinstance(item, str):
                    await self._command(item)
                else:
                    await self._process(item, loop)
            except Exception:  # never let one bad job kill the bridge
                log.exception("erreur inattendue sur %s", item)
                if isinstance(item, Path):
                    self._fail(item, "erreur inattendue (voir le log)")
            finally:
                self.queue.task_done()
                if self.status["state"] in ("printing", "connecting", "checking"):
                    self.set_status(state="idle", detail="")
                else:
                    self.set_status()

    async def _command(self, name: str) -> None:
        if name == "testpage":
            path = SPOOL_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-testpage.job"
            path.write_bytes(make_testpage())
            await self._process(path, asyncio.get_running_loop())
        elif name == "status":
            await self.check_printer()

    async def check_printer(self) -> None:
        """Connect (or reuse the link), query paper + battery, publish them."""
        self.set_status(state="checking", detail="Interrogation de la T02…")
        try:
            conn = await self._connection()
            try:
                await conn.preflight()
            except ble.PrinterError:
                pass  # paper out is exactly what we want to report
            self._absorb_printer_state(conn)
            self.set_status(state="idle", detail="", last_error=None)
            self._schedule_idle_close()
        except ble.PrinterUnreachable as exc:
            await self._drop_connection()
            self.set_status(state="offline", detail="T02 injoignable (éteinte ?)", last_error=str(exc))

    async def _process(self, path: Path, loop: asyncio.AbstractEventLoop) -> None:
        try:
            blob = path.read_bytes()
            job = await loop.run_in_executor(self.cpu, pipeline.prepare, blob, self.cfg)
        except Exception as exc:
            log.error("conversion impossible pour %s : %s", path.name, exc)
            self._fail(path, f"conversion impossible : {exc}")
            return
        log.info("job %s : %d page(s), %d octets, ~%.0fs d'impression, densité %d",
                 path.stem, job.pages, len(job.payload), job.seconds, job.settings.density)

        for attempt in range(1, self.cfg.max_attempts + 1):
            self.set_status(state="printing", detail=f"{job.pages} page(s) · ~{job.seconds:.0f} s")
            try:
                await self.print_bytes(job)
                path.unlink(missing_ok=True)
                log.info("job %s imprimé ✓", path.stem)
                self.set_status(state="idle", detail="", last_job_at=time.time(),
                                last_job_ok=True, last_error=None)
                return
            except ble.PrinterError as exc:
                log.warning("job %s : %s (tentative %d)", path.stem, exc, attempt)
                if "papier" in str(exc).lower():
                    self.status["paper"] = "out"
                self.set_status(state="paper_out" if "papier" in str(exc).lower() else "waiting",
                                detail=str(exc), last_error=str(exc))
                notify(self.cfg, str(exc), "Impression en pause — je réessaie")
            except ble.PrinterUnreachable as exc:
                log.warning("job %s : %s (tentative %d)", path.stem, exc, attempt)
                self.set_status(state="waiting", detail="T02 injoignable — nouvel essai bientôt",
                                last_error=str(exc))
                if attempt == 2:  # first retry failed: tell the user once
                    notify(self.cfg, "Allume la T02 : le job attend dans la file", "Imprimante injoignable")
            except Exception as exc:
                log.warning("job %s : erreur BLE %r (tentative %d)", path.stem, exc, attempt)
                self.set_status(state="waiting", detail="Erreur Bluetooth — nouvel essai bientôt",
                                last_error=repr(exc))
            if attempt < self.cfg.max_attempts:
                await asyncio.sleep(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)])
        self._fail(path, "imprimante injoignable après plusieurs essais")

    def purge_failed(self) -> int:
        """Delete failed jobs older than failed_retention_days (0 = keep forever)."""
        days = self.cfg.failed_retention_days
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        removed = 0
        for p in FAILED_DIR.glob("*.job"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
        if removed:
            log.info("%d vieux job(s) raté(s) supprimé(s) (> %d jours)", removed, days)
        return removed

    async def _purge_loop(self) -> None:
        while True:
            self.purge_failed()
            await asyncio.sleep(86400)

    def _fail(self, path: Path, reason: str) -> None:
        if path.exists():
            dest = FAILED_DIR / path.name
            path.replace(dest)
            os.utime(dest)  # retention counts from the failure, not the original print
        self.set_status(state="idle", detail="", last_job_at=time.time(),
                        last_job_ok=False, last_error=reason)
        notify(self.cfg, reason, f"Job {path.stem} mis de côté")

    # ---- Bluetooth -----------------------------------------------------
    async def _connection(self) -> ble.T02Connection:
        """Reuse the warm link if it is still up, otherwise scan + connect."""
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        if self._conn is not None and self._conn.is_connected:
            log.debug("connexion BLE réutilisée")
            return self._conn
        await self._drop_connection()
        dev = await ble.find_printer(self.cfg.printer_name, self.cfg.address)
        if dev.address != self.cfg.address:  # remember it: next connect is instant
            self.cfg.address = dev.address
            self.cfg.save()
        self._conn = await ble.T02Connection(dev, self.cfg.chunk_size, self.cfg.delay_ms).open()
        log.info("connecté à %s", self.cfg.printer_name)
        self.set_status()
        return self._conn

    async def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()
            self.set_status()

    def _schedule_idle_close(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
        if self.cfg.keepalive_seconds <= 0:
            self._idle_task = asyncio.ensure_future(self._drop_connection())
            return

        async def close_later() -> None:
            await asyncio.sleep(self.cfg.keepalive_seconds)
            if self.queue.empty():  # a job may have just arrived
                log.info("inactif depuis %ds → déconnexion BLE", self.cfg.keepalive_seconds)
                await self._drop_connection()

        self._idle_task = asyncio.ensure_future(close_later())

    async def _print_over_ble(self, job: pipeline.PreparedJob) -> None:
        try:
            conn = await self._connection()
            await conn.preflight()
            await conn.send(job.payload, settle_seconds=job.seconds)
            self._absorb_printer_state(conn)
            if conn.battery is not None:
                log.info("batterie : %s", conn.battery)
        except Exception:
            if self._conn is not None:
                self._absorb_printer_state(self._conn)
            await self._drop_connection()  # never reuse a link in an unknown state
            raise
        self._schedule_idle_close()

    # ---- lifecycle -----------------------------------------------------
    async def serve(self) -> None:
        ensure_dirs()
        for leftover in SPOOL_DIR.glob(".*.part"):  # half-received at shutdown
            leftover.unlink(missing_ok=True)
        pending = sorted(SPOOL_DIR.glob("*.job"))
        for p in pending:
            await self.queue.put(p)
        if pending:
            log.info("%d job(s) repris depuis le spool", len(pending))

        server = await asyncio.start_server(self.handle_client, self.cfg.host, self.cfg.port)
        log.info("bridge prêt sur %s:%d (imprimante BLE « %s »)", self.cfg.host, self.cfg.port, self.cfg.printer_name)
        self.set_status(state="idle", detail="")
        async with server:
            await asyncio.gather(server.serve_forever(), self.worker(), self._purge_loop(), self._heartbeat())
