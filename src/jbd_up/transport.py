"""Serial transport: request/response, passive sniffing, capture files.

Works for both the RS485-1 port (USB-RS485 adapter) and the RS232 "PC"
port (USB-RS232 TTL/level adapter) - both are 9600 8N1 by default.  The
inter-pack RS485-2 daisy chain has been reported at 19200 (SRNE repo);
pass ``--baud 19200`` when sniffing that bus.

Design rules:
* Never raise out of a poll because of bad bytes.  CRC / short frames are
  counted in :class:`~jbd_up.model.LinkStats` and surfaced as sensors.
* Reconnect on serial errors with exponential back-off, capped.
* Sniff mode never writes to the port.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import frame as upf
from .model import LinkStats

log = logging.getLogger(__name__)

DEFAULT_BAUD = 9600
DEFAULT_TIMEOUT = 1.5  # dbus-serialbattery: replies take 0.3-0.6 s
INTER_FRAME_GAP = 0.05


class TransportError(Exception):
    pass


class Transport:
    """Abstract byte transport.  Implementations: SerialTransport, MockTransport."""

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def read(self, n: int, timeout: float) -> bytes:
        raise NotImplementedError

    def flush_input(self) -> None:
        pass

    def close(self) -> None:
        pass

    def reopen(self) -> None:
        pass


class SerialTransport(Transport):
    def __init__(
        self, port: str, baud: int = DEFAULT_BAUD, read_only: bool = False, retry_forever: bool = False
    ) -> None:
        """``retry_forever`` keeps retrying a failed open with back-off (daemon mode); otherwise the first
        failure raises TransportError so one-shot commands fail fast on a wrong ``--port``."""
        import serial  # local import so tests run without hardware deps being touched

        self._serial_mod = serial
        self.port = port
        self.baud = baud
        self.read_only = read_only
        self.retry_forever = retry_forever
        self._ser = None
        self.reconnects = 0
        self._backoff = 0.5
        self.reopen()

    def reopen(self) -> None:
        self.close()
        while True:
            try:
                self._ser = self._serial_mod.Serial(
                    self.port,
                    baudrate=self.baud,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                    timeout=0.1,
                    write_timeout=1.0,
                    exclusive=True,
                )
                self._backoff = 0.5
                return
            except Exception as exc:  # serial.SerialException, OSError
                self.reconnects += 1
                if not self.retry_forever:
                    raise TransportError(f"cannot open {self.port}: {exc}") from exc
                log.warning("cannot open %s: %s (retry in %.1fs)", self.port, exc, self._backoff)
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 30)

    def close(self) -> None:
        if self._ser is not None:
            with contextlib.suppress(Exception):
                self._ser.close()
            self._ser = None

    def write(self, data: bytes) -> None:
        if self.read_only:
            raise TransportError("transport is read-only (sniff mode)")
        try:
            self._ser.reset_input_buffer()
            self._ser.write(data)
            self._ser.flush()
        except Exception as exc:  # noqa: BLE001
            raise TransportError(str(exc)) from exc

    def read(self, n: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        out = bytearray()
        try:
            while len(out) < n and time.monotonic() < deadline:
                chunk = self._ser.read(n - len(out))
                if chunk:
                    out.extend(chunk)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(str(exc)) from exc
        return bytes(out)

    def read_available(self, timeout: float) -> bytes:
        """Read whatever arrives within ``timeout`` (for sniffing)."""
        try:
            self._ser.timeout = timeout
            data = self._ser.read(4096)
            self._ser.timeout = 0.1
            return data
        except Exception as exc:  # noqa: BLE001
            raise TransportError(str(exc)) from exc

    def flush_input(self) -> None:
        with contextlib.suppress(Exception):
            self._ser.reset_input_buffer()


class MockTransport(Transport):
    """Scripted transport for tests: maps request bytes -> response bytes."""

    def __init__(self, responses: dict[bytes, bytes] | None = None, stream: bytes = b"") -> None:
        self.responses = responses or {}
        self.stream = bytearray(stream)
        self.writes: list[bytes] = []
        self._pending = bytearray()

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        self._pending = bytearray(self.responses.get(bytes(data), b""))

    def read(self, n: int, timeout: float) -> bytes:
        out = bytes(self._pending[:n])
        del self._pending[:n]
        return out

    def read_available(self, timeout: float) -> bytes:
        out = bytes(self.stream[:4096])
        del self.stream[:4096]
        return out


# ---------------------------------------------------------------------------
# UP request / response
# ---------------------------------------------------------------------------
def up_request(
    tr: Transport,
    request: bytes,
    stats: LinkStats,
    timeout: float = DEFAULT_TIMEOUT,
    on_bytes: Callable[[str, bytes], None] | None = None,
) -> upf.Frame | None:
    """Send one UP frame and read exactly one validated reply.

    Returns None on timeout / CRC error (counted in ``stats``).  Never raises
    for protocol problems; raises TransportError only for a dead port.
    """
    if on_bytes:
        on_bytes("TX", request)
    tr.write(request)
    header = tr.read(upf.HEADER_LEN, timeout)
    if len(header) < upf.HEADER_LEN:
        if header and on_bytes:
            on_bytes("RX", header)
        if header:
            stats.short_frames += 1
        else:
            stats.timeouts += 1
        return None
    total = upf.expected_length(header)
    if total is None:
        # garbage header: drain what we can so the next request starts clean
        if on_bytes:
            on_bytes("RX", header)
        stats.resyncs += 1
        tr.read(4096, INTER_FRAME_GAP)
        tr.flush_input()
        return None
    rest = tr.read(total - upf.HEADER_LEN, timeout)
    raw = header + rest
    if on_bytes:
        on_bytes("RX", raw)
    try:
        fr = upf.decode(raw)
    except upf.CrcError as exc:
        stats.crc_errors += 1
        stats.last_error = str(exc)
        return None
    except upf.FrameError as exc:
        stats.short_frames += 1
        stats.last_error = str(exc)
        return None
    if fr.address != request[0] and request[0] != upf.BROADCAST_ADDRESS:
        stats.last_error = f"address mismatch {fr.address} != {request[0]}"
        return None
    stats.frames_ok += 1
    time.sleep(INTER_FRAME_GAP)
    return fr


def pylon_request(
    tr: Transport,
    request: bytes,
    stats: LinkStats,
    timeout: float = DEFAULT_TIMEOUT,
    on_bytes: Callable[[str, bytes], None] | None = None,
) -> bytes | None:
    """Send a Pylontech ASCII request, return the raw ``~...\\r`` reply or None."""
    if on_bytes:
        on_bytes("TX", request)
    tr.write(request)
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = tr.read(1, max(0.0, deadline - time.monotonic()))
        if not chunk:
            continue
        if not buf and chunk != b"~":
            continue
        buf.extend(chunk)
        if chunk == b"\r":
            break
    if not buf:
        stats.timeouts += 1
        return None
    if on_bytes:
        on_bytes("RX", bytes(buf))
    if buf[-1:] != b"\r":
        stats.short_frames += 1
        return None
    stats.frames_ok += 1
    return bytes(buf)


# ---------------------------------------------------------------------------
# Passive sniff
# ---------------------------------------------------------------------------
def sniff(tr: Transport, seconds: float, chunk_timeout: float = 0.2) -> Iterator[tuple[float, bytes]]:
    """Yield (t_rel, chunk) for everything heard on the bus, without transmitting."""
    t0 = time.monotonic()
    deadline = t0 + seconds if seconds > 0 else float("inf")
    while time.monotonic() < deadline:
        data = tr.read_available(chunk_timeout)
        if data:
            yield (round(time.monotonic() - t0, 3), data)


# ---------------------------------------------------------------------------
# Capture file format
# ---------------------------------------------------------------------------
CAPTURE_HEADER = "# jbd-up capture v1"


@dataclass
class CaptureLine:
    t: float | None
    direction: str  # TX / RX / ""
    data: bytes


class CaptureWriter:
    """Writes ``T+<sec> <TX|RX> <hex>`` lines.  Tiny and greppable."""

    def __init__(self, path: Path, meta: dict | None = None) -> None:
        self.path = Path(path)
        self.fh = self.path.open("w", encoding="utf-8")
        self.t0 = time.monotonic()
        self.fh.write(CAPTURE_HEADER + "\n")
        for k, v in (meta or {}).items():
            self.fh.write(f"# {k}: {v}\n")
        self.fh.flush()

    def write(self, direction: str, data: bytes, t: float | None = None) -> None:
        if t is None:
            t = time.monotonic() - self.t0
        self.fh.write(f"T+{t:09.3f} {direction} {data.hex(' ')}\n")
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


_HEX_RUN = re.compile(r"(?:[0-9A-Fa-f]{2}[\s:]?)+")
_T_PREFIX = re.compile(r"^T\+(\d+(?:\.\d+)?)\s+")


def parse_capture_line(line: str) -> CaptureLine | None:
    """Parse one line of a capture or fixture file.

    Accepts the native ``T+1.234 RX 01 78 ...`` form, ESPHome ``uart_debug``
    logs (``[..] >>> 01:78:...`` / ``<<< ...``) so public captures can be used
    verbatim, and bare hex lines.  Returns None for comments / blanks / text.
    """
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    t: float | None = None
    m = _T_PREFIX.match(s)
    if m:
        t = float(m.group(1))
        s = s[m.end() :]
    direction = ""
    for marker, d in ((">>>", "TX"), ("<<<", "RX"), ("TX", "TX"), ("RX", "RX")):
        idx = s.find(marker)
        if idx >= 0 and (marker in (">>>", "<<<") or idx == 0):
            direction = d
            s = s[idx + len(marker) :].strip()
            break
    if "~" in s:
        # Pylontech ASCII frame recorded as text
        start = s.find("~")
        end = s.find("\\r", start)
        text = s[start : end if end > 0 else len(s)]
        return CaptureLine(t, direction, text.encode("ascii") + b"\r")
    s = s.replace(":", " ")
    m2 = _HEX_RUN.match(s + " ")
    if not m2:
        return None
    hexstr = m2.group(0).replace(" ", "").replace(":", "")
    if len(hexstr) % 2 or len(hexstr) < 2:
        return None
    try:
        return CaptureLine(t, direction, bytes.fromhex(hexstr))
    except ValueError:
        return None


def read_capture(path: Path) -> list[CaptureLine]:
    lines: list[CaptureLine] = []
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            cl = parse_capture_line(raw)
            if cl is not None:
                lines.append(cl)
    return lines
