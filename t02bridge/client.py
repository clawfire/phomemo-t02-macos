"""Talk to the running bridge over its local socket (same door as CUPS).

Print data is sent as-is; control commands are short text messages prefixed
with "#T02:" (e.g. "#T02:status"), which the bridge never spools.
"""

from __future__ import annotations

import socket

from .config import Config

COMMAND_PREFIX = b"#T02:"
COMMANDS = ("status", "testpage")


def send(cfg: Config, data: bytes, timeout: float = 5.0) -> None:
    """Send bytes and wait until the bridge has taken them (it closes the socket)."""
    with socket.create_connection((cfg.host, cfg.port), timeout=timeout) as s:
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        s.recv(1)


def command(cfg: Config, name: str) -> None:
    if name not in COMMANDS:
        raise ValueError(f"commande inconnue : {name}")
    send(cfg, COMMAND_PREFIX + name.encode() + b"\n")
