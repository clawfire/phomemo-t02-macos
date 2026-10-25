"""Run:  python -m pytest -q tests/   (no printer, no Bluetooth needed)"""

from __future__ import annotations

import asyncio
import io
import struct

import pytest
from PIL import Image, ImageDraw

from t02bridge import ble, pipeline, protocol, raster, server
from t02bridge.config import Config


# ---------------------------------------------------------------- helpers
def make_page(w=384, h=120) -> Image.Image:
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 7, 7], fill=0)          # black square top-left
    d.text((20, 40), "Hello T02", fill=0)
    return img


def header_bytes(endian, w, h, bpp=8, cspace=raster.CS_SW, ints=(), dpi=203):
    raw = bytearray(raster.HEADER_SIZE)
    put = lambda off, v: struct.pack_into(endian + "I", raw, off, v)  # noqa: E731
    put(276, dpi); put(280, dpi); put(372, w); put(376, h)
    put(384, bpp if bpp < 24 else 8); put(388, bpp); put(392, (w * bpp + 7) // 8)
    put(396, 0); put(400, cspace); put(420, 1 if bpp <= 16 else 3)
    for i, v in enumerate(ints):
        put(452 + 4 * i, v)
    raw[1732:1740] = b"w48h100\x00"
    return bytes(raw)


def encode_v2(data: bytes, bpl: int, h: int) -> bytes:
    """Minimal CUPS v2 encoder (line repeat + literal runs) for round-trip tests."""
    out = bytearray()
    lines = [data[i * bpl:(i + 1) * bpl] for i in range(h)]
    y = 0
    while y < h:
        rep = 0
        while y + rep + 1 < h and lines[y + rep + 1] == lines[y] and rep < 255:
            rep += 1
        out.append(rep)
        line, x = lines[y], 0
        while x < bpl:
            run = 1
            while x + run < bpl and line[x + run] == line[x] and run < 128:
                run += 1
            if run > 1:
                out += bytes([run - 1, line[x]]); x += run
            else:
                n = min(128, bpl - x)
                out += bytes([257 - n]) + line[x:x + n]; x += n
        y += rep + 1
    return bytes(out)


def raster_stream(img, version=3, endian=">", ints=()):
    sync = {(">", 1): b"RaSt", ("<", 1): b"tSaR", (">", 2): b"RaS2",
            ("<", 2): b"2SaR", (">", 3): b"RaS3", ("<", 3): b"3SaR"}[(endian, version)]
    data = img.tobytes()
    body = encode_v2(data, img.width, img.height) if version == 2 else data
    return sync + header_bytes(endian, img.width, img.height, ints=ints) + body


# ---------------------------------------------------------------- raster
@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("endian", [">", "<"])
def test_raster_roundtrip(version, endian):
    img = make_page()
    pages = raster.pages_from_bytes(raster_stream(img, version, endian) * 1)
    assert len(pages) == 1
    assert pages[0].image.tobytes() == img.tobytes()
    assert pages[0].header.hw_resolution == (203, 203)


def test_multipage_and_k_space():
    img = make_page()
    blob = b"RaS3" + header_bytes(">", 384, 120) + img.tobytes()
    inv = bytes(255 - b for b in img.tobytes())
    blob += header_bytes(">", 384, 120, cspace=raster.CS_K) + inv
    pages = raster.pages_from_bytes(blob)
    assert len(pages) == 2
    assert pages[1].image.tobytes() == img.tobytes()  # K inverted back to luminance


def test_rle_clear_to_eol():
    # one line: 2 black pixels then "clear rest" (code 128) → white
    blob = b"RaS2" + header_bytes(">", 16, 1) + bytes([0, 1, 0, 128])
    px = list(raster.pages_from_bytes(blob)[0].image.tobytes())
    assert px[:2] == [0, 0] and set(px[2:]) == {255}


def test_rejects_garbage():
    with pytest.raises(ValueError):
        raster.pages_from_bytes(b"%PDF-1.7 nope")


# ---------------------------------------------------------------- protocol
def test_job_bytes_shape():
    cfg = Config(trim=False, dither="threshold")
    job = pipeline.prepare(raster_stream(make_page(h=300)), cfg)
    p = job.payload
    assert p.startswith(b"\x1b\x40\x1f\x11\x02\x02\x1f\x11\x37\x64\x1f\x11\x0b\x1f\x11\x35\x00")
    # 300 rows + 4 padding = 304 → blocks of 255 + 49
    first = p.index(b"\x1d\x76\x30\x00")
    assert p[first + 4:first + 8] == bytes([48, 0, 255, 0])
    second = first + 8 + 48 * 255
    assert p[second:second + 8] == b"\x1d\x76\x30\x00" + bytes([48, 0, 49, 0])
    # top padding is white, then the black square: MSB first, 1 = black
    data = p[first + 8:]
    assert data[:48 * 4] == bytes(48 * 4)
    assert data[48 * 4] == 0xFF and data[48 * 4 + 1] == 0x00
    assert p.endswith(b"\x1b\x64\x02\x1b\x64\x02")


def test_options_from_ppd_integers():
    cfg = Config()
    blob = raster_stream(make_page(), ints=(4, 2, 80, 2))  # Darkest, Threshold, +10mm, no trim
    job = pipeline.prepare(blob, cfg)
    assert job.settings.density == 4
    assert b"\x1f\x11\x02\x04\x1f\x11\x37\x96" in job.payload
    assert job.payload.endswith(b"\x1b\x4a\x50")


def test_trim_and_width_fit():
    tall = Image.new("L", (300, 2000), 255)
    ImageDraw.Draw(tall).rectangle([0, 0, 299, 99], fill=0)
    buf = io.BytesIO(); tall.save(buf, "PNG")
    job = pipeline.prepare(buf.getvalue(), Config())
    rows = sum(int.from_bytes(job.payload[i + 6:i + 8], "little")
               for i in range(len(job.payload) - 4) if job.payload[i:i + 4] == b"\x1d\x76\x30\x00")
    assert rows < 200  # 2000 px of mostly white trimmed down


def test_atkinson_runs():
    grad = Image.linear_gradient("L").resize((384, 64))
    buf = io.BytesIO(); grad.save(buf, "PNG")
    job = pipeline.prepare(buf.getvalue(), Config(dither="atkinson", trim=False))
    assert len(job.payload) > 48 * 64


def test_status_decoding():
    assert protocol.describe_status(b"\x1a\x06\x88") == "Plus de papier"
    assert protocol.describe_status(b"\x1a\x06\x89") is None


# ---------------------------------------------------------------- bridge
def test_bridge_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SPOOL_DIR", tmp_path / "spool")
    monkeypatch.setattr(server, "FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(server, "BACKOFF", [0])
    monkeypatch.setattr(server, "notify", lambda *a, **k: None)
    (tmp_path / "spool").mkdir(); (tmp_path / "failed").mkdir()

    printed, calls = [], {"n": 0}

    async def flaky_ble(job):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ble.PrinterUnreachable("asleep")
        printed.append(job.payload)

    async def scenario():
        bridge = server.Bridge(Config(port=0), transport_factory=flaky_ble)
        srv = await asyncio.start_server(bridge.handle_client, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        worker = asyncio.create_task(bridge.worker())
        # behave like the CUPS socket backend: send, half-close, wait for EOF
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(raster_stream(make_page())); await w.drain(); w.write_eof()
        assert await r.read() == b""
        w.close()
        await asyncio.wait_for(bridge.queue.join(), 5)
        worker.cancel(); srv.close()

    asyncio.run(scenario())
    assert calls["n"] == 2 and len(printed) == 1
    assert not list((tmp_path / "spool").glob("*.job"))


def test_bad_job_goes_to_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SPOOL_DIR", tmp_path)
    monkeypatch.setattr(server, "FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(server, "notify", lambda *a, **k: None)
    (tmp_path / "failed").mkdir()
    job = tmp_path / "x.job"; job.write_bytes(b"not a raster")

    async def never(_):
        raise AssertionError("should not print")

    async def scenario():
        b = server.Bridge(Config(), transport_factory=never)
        await b._process(job, asyncio.get_running_loop())

    asyncio.run(scenario())
    assert (tmp_path / "failed" / "x.job").exists()


def test_auto_dither_keeps_text_sharp_and_dithers_greys():
    from t02bridge.imaging import RenderOptions, to_bilevel
    img = Image.new("L", (384, 200), 255)
    d = ImageDraw.Draw(img)
    d.text((10, 10), "SHARP TEXT", fill=0)
    d.rectangle([0, 100, 383, 199], fill=128)       # flat 50 % grey
    bw = to_bilevel(img, RenderOptions(dither="auto")).convert("L")
    grey = list(bw.crop((50, 120, 330, 180)).tobytes())
    ratio = grey.count(0) / len(grey)
    assert 0.3 < ratio < 0.7                          # dithered, not a solid block
    text = set(bw.crop((0, 0, 384, 40)).tobytes())
    assert text == {0, 255}


# ---------------------------------------------------------------- v1.1: warm BLE, purge, retry
class FakeConn:
    opened = 0

    def __init__(self, dev, chunk, delay):
        self.connected, self.sent, self.battery = False, [], None

    @property
    def is_connected(self):
        return self.connected

    async def open(self):
        FakeConn.opened += 1
        self.connected = True
        return self

    async def close(self):
        self.connected = False

    async def preflight(self):
        pass

    async def send(self, payload, settle_seconds=0):
        if payload == b"boom":
            raise ble.PrinterError("Plus de papier")
        self.sent.append(payload)


def _fake_ble(monkeypatch):
    class Dev:
        address = "UUID-1"

    async def find(name, address, timeout=10.0):
        return Dev()

    FakeConn.opened = 0
    monkeypatch.setattr(ble, "find_printer", find)
    monkeypatch.setattr(ble, "T02Connection", FakeConn)
    monkeypatch.setattr(Config, "save", lambda self: None)


def test_ble_link_is_reused_then_closed_when_idle(monkeypatch):
    _fake_ble(monkeypatch)

    async def scenario():
        b = server.Bridge(Config(keepalive_seconds=0.05))
        job = pipeline.PreparedJob(b"x", 1, 0.0, protocol.JobSettings())
        await b._print_over_ble(job)
        await b._print_over_ble(job)
        assert FakeConn.opened == 1 and b._conn.is_connected     # warm link reused
        await asyncio.sleep(0.15)
        assert b._conn is None                                    # closed after idle

    asyncio.run(scenario())


def test_ble_link_dropped_after_error(monkeypatch):
    _fake_ble(monkeypatch)

    async def scenario():
        b = server.Bridge(Config(keepalive_seconds=60))
        bad = pipeline.PreparedJob(b"boom", 1, 0.0, protocol.JobSettings())
        with pytest.raises(ble.PrinterError):
            await b._print_over_ble(bad)
        assert b._conn is None
        await b._print_over_ble(pipeline.PreparedJob(b"ok", 1, 0.0, protocol.JobSettings()))
        assert FakeConn.opened == 2                               # fresh link after the error
        b._idle_task.cancel()

    asyncio.run(scenario())


def test_purge_failed(tmp_path, monkeypatch):
    import os, time
    monkeypatch.setattr(server, "FAILED_DIR", tmp_path)
    old, new = tmp_path / "old.job", tmp_path / "new.job"
    old.write_bytes(b"a"); new.write_bytes(b"b")
    t = time.time() - 8 * 86400
    os.utime(old, (t, t))
    assert server.Bridge(Config(failed_retention_days=7)).purge_failed() == 1
    assert not old.exists() and new.exists()


def test_retry_command_requeues_through_socket(tmp_path, monkeypatch):
    from t02bridge import __main__ as cli, config

    monkeypatch.setattr(config, "FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(server, "SPOOL_DIR", tmp_path / "spool")
    (tmp_path / "failed").mkdir(); (tmp_path / "spool").mkdir()
    (tmp_path / "failed" / "a.job").write_bytes(raster_stream(make_page()))

    async def scenario():
        b = server.Bridge(Config())
        srv = await asyncio.start_server(b.handle_client, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        rc = await asyncio.get_running_loop().run_in_executor(
            None, cli._cmd_retry, Config(port=port), False)
        srv.close()
        return rc, b.queue.qsize()

    rc, queued = asyncio.run(scenario())
    assert rc == 0 and queued == 1
    assert not (tmp_path / "failed" / "a.job").exists()
    assert len(list((tmp_path / "spool").glob("*.job"))) == 1


def test_rotating_log_in_daemon_mode(tmp_path, monkeypatch):
    import logging
    from t02bridge import __main__ as cli, config
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    cli._setup_logging(False, to_file=True)
    h = logging.getLogger().handlers[0]
    assert h.__class__.__name__ == "RotatingFileHandler" and h.maxBytes == 1_000_000
    logging.getLogger().handlers.clear()
