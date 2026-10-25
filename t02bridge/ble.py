"""BLE transport for the T02 (CoreBluetooth on macOS, via bleak).

GATT layout (same across phomymo / TiMini-Print / phomemo-tools):
    service 0xFF00 · write 0xFF02 · notify 0xFF03
Data is streamed in 128-byte chunks with a 20 ms pause, like the official app.
"""

from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from .protocol import describe_status

log = logging.getLogger("t02.ble")

SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff03-0000-1000-8000-00805f9b34fb"

QUERY_PAPER = b"\x1f\x11\x11"
QUERY_BATTERY = b"\x1f\x11\x08"


class PrinterError(RuntimeError):
    """Something the user must fix (paper, cover…) — worth a notification."""


class PrinterUnreachable(RuntimeError):
    """Printer off / out of range — the job stays queued and is retried."""


async def scan(timeout: float = 8.0) -> list[BLEDevice]:
    """Return nearby devices that look like a T02 (or any Phomemo)."""
    devices = await BleakScanner.discover(timeout=timeout, service_uuids=None)
    return [d for d in devices if d.name and (d.name.upper().startswith(("T02", "Q02", "C02")) or "PHOMEMO" in d.name.upper())]


async def find_printer(name: str, address: str | None, timeout: float = 10.0) -> BLEDevice:
    if address:
        dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if dev:
            return dev
        log.info("cached address %s not seen, falling back to name scan", address)
    dev = await BleakScanner.find_device_by_filter(
        lambda d, _adv: bool(d.name) and d.name.strip().upper() == name.upper(), timeout=timeout
    )
    if not dev:
        raise PrinterUnreachable(f"imprimante « {name} » introuvable en Bluetooth (éteinte ? hors de portée ?)")
    return dev


class T02Connection:
    """A BLE link to the T02. Usable as `async with` (one-shot) or kept open
    across several jobs with open()/close() — the bridge does the latter."""

    def __init__(self, device: BLEDevice, chunk_size: int = 128, delay_ms: int = 20):
        self.device = device
        self.chunk_size = chunk_size
        self.delay = delay_ms / 1000
        self.client = BleakClient(device, timeout=15.0)
        self.warnings: list[str] = []
        self.battery: int | None = None
        self._paper_out = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        try:
            return bool(self.client.is_connected)
        except Exception:
            return False

    async def open(self) -> "T02Connection":
        try:
            await self.client.connect()
        except Exception as exc:  # bleak raises a zoo of backend errors
            raise PrinterUnreachable(f"connexion BLE impossible : {exc}") from exc
        # CoreBluetooth negotiates the MTU itself; never send more than it allows.
        mtu = getattr(self.client, "mtu_size", 0) or 0
        if mtu > 3:
            self.chunk_size = min(self.chunk_size, mtu - 3)
        try:
            await self.client.start_notify(NOTIFY_UUID, self._on_notify)
        except Exception as exc:
            log.warning("notifications indisponibles (%s) — on imprime quand même", exc)
        return self

    async def close(self) -> None:
        try:
            await self.client.disconnect()
        except Exception:
            pass

    def reset_job_state(self) -> None:
        """Forget warnings from the previous job (the link may be reused)."""
        self.warnings.clear()
        self._paper_out.clear()

    async def __aenter__(self) -> "T02Connection":
        return await self.open()

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    def _on_notify(self, _char, data: bytearray) -> None:
        frame = bytes(data)
        log.debug("notify %s", frame.hex(" "))
        if len(frame) >= 3 and frame[0] == 0x1A and frame[1] == 0x04:
            self.battery = frame[2]
        msg = describe_status(frame)
        if msg:
            self.warnings.append(msg)
            if msg.startswith("Plus de papier"):
                self._paper_out.set()

    async def _write(self, payload: bytes) -> None:
        await self.client.write_gatt_char(WRITE_UUID, payload, response=False)

    async def preflight(self) -> None:
        """Ask for paper / battery status; abort early if there is no paper."""
        self.reset_job_state()
        for q in (QUERY_PAPER, QUERY_BATTERY):
            await self._write(q)
            await asyncio.sleep(0.25)
        if self._paper_out.is_set():
            raise PrinterError("Plus de papier dans la T02")

    async def send(self, job: bytes, settle_seconds: float = 0.0) -> None:
        total = len(job)
        for off in range(0, total, self.chunk_size):
            await self._write(job[off:off + self.chunk_size])
            await asyncio.sleep(self.delay)
            if self._paper_out.is_set():
                raise PrinterError("Plus de papier pendant l'impression")
        # Let the head finish before dropping the link (the printer has no 'done' ACK).
        await asyncio.sleep(max(1.0, settle_seconds))
        if self._paper_out.is_set():
            raise PrinterError("Plus de papier pendant l'impression")
