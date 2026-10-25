"""Config & paths. Everything lives in the user's Library — no root daemons."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_DIR = Path(os.environ.get("T02_HOME", Path.home() / "Library/Application Support/PhomemoT02"))
SPOOL_DIR = APP_DIR / "spool"
FAILED_DIR = APP_DIR / "failed"
CONFIG_FILE = APP_DIR / "config.json"
LOG_DIR = Path(os.environ.get("T02_LOG_DIR", Path.home() / "Library/Logs/PhomemoT02"))


@dataclass
class Config:
    printer_name: str = "T02"          # BLE advertised name
    address: str | None = None         # CoreBluetooth UUID, cached after first success
    host: str = "127.0.0.1"            # only ever listen on loopback
    port: int = 9100                   # CUPS queue points at socket://127.0.0.1:9100
    chunk_size: int = 128
    delay_ms: int = 20
    density: int = 2                   # 1 light … 4 darkest (PPD can override per job)
    dither: str = "auto"               # auto | floyd | atkinson | threshold
    threshold: int = 160
    trim: bool = True
    extra_feed: int = 0
    max_attempts: int = 6              # BLE attempts before a job lands in failed/
    notify: bool = True                # macOS notifications on errors
    keepalive_seconds: int = 60        # keep BLE link open between jobs (0 = disconnect each time)
    failed_retention_days: int = 7     # failed/ jobs older than this are purged

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text())
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(cfg, k, v)
        return cfg

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
        tmp.replace(CONFIG_FILE)


def ensure_dirs() -> None:
    for d in (APP_DIR, SPOOL_DIR, FAILED_DIR):
        d.mkdir(parents=True, exist_ok=True)
