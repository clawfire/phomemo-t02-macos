"""Background bridge: CUPS socket backend  →  spool  →  worker  →  BLE.

CUPS talks to socket://127.0.0.1:9100 like it would to any network printer.
We swallow the job instantly (so the macOS print queue empties right away),
spool it to disk, and a single worker converts + prints it in the background,
retrying while the printer is asleep or out of range.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ble, pipeline
from .config import FAILED_DIR, SPOOL_DIR, Config, ensure_dirs

log = logging.getLogger("t02.bridge")

BACKOFF = [5, 15, 30, 60, 120, 300]  # seconds between BLE attempts
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
        self.queue: asyncio.Queue[Path] = asyncio.Queue()
        self.cpu = ThreadPoolExecutor(max_workers=1, thread_name_prefix="t02-render")
        # Injected in tests to replace real Bluetooth.
        self.print_bytes = transport_factory or self._print_over_ble
        self._conn: ble.T02Connection | None = None      # warm BLE link
        self._idle_task: asyncio.Future | None = None    # closes it after keepalive

    # ---- intake --------------------------------------------------------
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{next(_counter):04d}"
        tmp = SPOOL_DIR / f".{job_id}.part"
        size = 0
        try:
            with tmp.open("wb") as fh:
                while chunk := await reader.read(65536):
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
        final = SPOOL_DIR / f"{job_id}.job"
        tmp.replace(final)
        log.info("job %s reçu (%d octets) → file d'attente", job_id, size)
        await self.queue.put(final)

    # ---- worker --------------------------------------------------------
    async def worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            path = await self.queue.get()
            try:
                await self._process(path, loop)
            except Exception:  # never let one bad job kill the bridge
                log.exception("erreur inattendue sur %s", path.name)
                self._fail(path, "erreur inattendue (voir le log)")
            finally:
                self.queue.task_done()

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
            try:
                await self.print_bytes(job)
                path.unlink(missing_ok=True)
                log.info("job %s imprimé ✓", path.stem)
                return
            except ble.PrinterError as exc:
                log.warning("job %s : %s (tentative %d)", path.stem, exc, attempt)
                notify(self.cfg, str(exc), "Impression en pause — je réessaie")
            except ble.PrinterUnreachable as exc:
                log.warning("job %s : %s (tentative %d)", path.stem, exc, attempt)
                if attempt == 2:  # first retry failed: tell the user once
                    notify(self.cfg, "Allume la T02 : le job attend dans la file", "Imprimante injoignable")
            except Exception as exc:
                log.warning("job %s : erreur BLE %r (tentative %d)", path.stem, exc, attempt)
            if attempt < self.cfg.max_attempts:
                await asyncio.sleep(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)])
        self._fail(path, "imprimante injoignable après plusieurs essais")

    def _fail(self, path: Path, reason: str) -> None:
        if path.exists():
            path.replace(FAILED_DIR / path.name)
        notify(self.cfg, reason, f"Job {path.stem} mis de côté")

    # ---- Bluetooth -----------------------------------------------------
    async def _connection(self) -> ble.T02Connection:
        """Reuse the warm link if it is still up, otherwise scan + connect."""
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
        return self._conn

    async def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()

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
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        try:
            conn = await self._connection()
            await conn.preflight()
            await conn.send(job.payload, settle_seconds=job.seconds)
            if conn.battery is not None:
                log.info("batterie : %s", conn.battery)
        except Exception:
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
        async with server:
            await asyncio.gather(server.serve_forever(), self.worker())
