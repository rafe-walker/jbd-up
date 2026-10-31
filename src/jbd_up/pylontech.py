"""Pylontech low-voltage RS485 ASCII protocol (fallback dialect).

When the UP16S's RS485 protocol selector (DIP / PC tool) is set to "Pylon"
(value 0, the factory default on Eco-Worthy packs), the inverter-facing
RS485-1 port speaks the Pylontech LV protocol that most hybrid inverters
understand.  Frames are printable ASCII:

    ~ VER ADR CID1 CID2 LENGTH INFO CHKSUM \\r
    ~ 20  02  46   42   E002   02   FD33   \\r     (request analog data of pack 2)

* ``~`` (0x7E) SOI, ``\\r`` (0x0D) EOI.
* VER ``20`` (protocol 2.0), ADR = pack address (Pylontech master is 02;
  JBD/Eco-Worthy packs answer on the DIP address, usually 01).
* CID1 ``46`` = battery.  CID2: ``42`` analog, ``44`` alarm, ``47`` system
  parameters, ``4F`` protocol version, ``51`` manufacturer info, ``92``
  charge/discharge management, ``93`` serial number.
* LENGTH = 4 hex chars: high nibble is LCHKSUM over the 3 LENID nibbles
  (sum mod 16, invert, +1), low 12 bits LENID = number of INFO ASCII chars.
* CHKSUM = 4 hex chars: sum of all ASCII bytes between SOI and CHKSUM,
  inverted, +1, mod 65536.

Reference implementation: Frankkkkk/python-pylontech (MIT) and the
"PYLON low voltage protocol RS485 V3.3" document circulated on
powerforum.co.za / diysolarforum.  The two request frames above are
reproduced byte-for-byte by :func:`encode`, which CONFIRMS the checksum
and LENGTH rules.  Analog layout (0x42) is CONFIRMED by python-pylontech
against real US2000/US3000 packs; whether a UP16S in Pylon mode fills every
field identically is INFERRED until someone posts a capture.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .model import Alarms, Dialect, Pack

SOI = b"~"
EOI = b"\r"
VER = 0x20
CID1_BATTERY = 0x46

CID2_ANALOG = 0x42
CID2_ALARM = 0x44
CID2_SYSTEM_PARAMS = 0x47
CID2_PROTOCOL_VERSION = 0x4F
CID2_MANUFACTURER = 0x51
CID2_MANAGEMENT = 0x92
CID2_SERIAL = 0x93

# RTN codes (CID2 in a response).  00 = ok.
RTN_OK = 0x00
RTN_NAMES = {
    0x00: "ok",
    0x01: "version_error",
    0x02: "checksum_error",
    0x03: "lchksum_error",
    0x04: "cid2_invalid",
    0x05: "command_format_error",
    0x06: "invalid_data",
    0x90: "adr_error",
    0x91: "communication_error",
}


class PylontechError(ValueError):
    pass


def checksum(body: bytes) -> int:
    """Frame checksum over the ASCII bytes between SOI and CHKSUM."""
    s = sum(body)
    s = ~s
    s %= 0x10000
    s += 1
    return s & 0xFFFF


def length_field(info_len: int) -> int:
    """LENGTH = LCHKSUM nibble << 12 | LENID (info length in ASCII chars)."""
    if info_len == 0:
        return 0
    n = info_len & 0xFFF
    lsum = (n & 0xF) + ((n >> 4) & 0xF) + ((n >> 8) & 0xF)
    lchk = (0b1111 - (lsum % 16) + 1) & 0xF
    return (lchk << 12) | n


def encode(address: int, cid2: int, info: bytes = b"", cid1: int = CID1_BATTERY, ver: int = VER) -> bytes:
    """Build a complete ASCII frame.  ``info`` is already ASCII-hex encoded."""
    body = f"{ver:02X}{address:02X}{cid1:02X}{cid2:02X}{length_field(len(info)):04X}".encode() + info
    return SOI + body + f"{checksum(body):04X}".encode() + EOI


def encode_analog_request(address: int) -> bytes:
    """``~20{adr}4642E002{adr}CHK\\r`` - get analog values for one pack."""
    return encode(address, CID2_ANALOG, f"{address:02X}".encode())


def encode_alarm_request(address: int) -> bytes:
    return encode(address, CID2_ALARM, f"{address:02X}".encode())


def encode_serial_request(address: int) -> bytes:
    return encode(address, CID2_SERIAL, f"{address:02X}".encode())


def encode_protocol_version_request(address: int) -> bytes:
    return encode(address, CID2_PROTOCOL_VERSION)


@dataclass(frozen=True)
class PylonFrame:
    ver: int
    address: int
    cid1: int
    cid2: int  # command in a request, RTN code in a response
    info: bytes  # decoded binary INFO
    raw: bytes

    @property
    def rtn(self) -> str:
        return RTN_NAMES.get(self.cid2, f"rtn_{self.cid2:02x}")


def decode(raw: bytes) -> PylonFrame:
    """Validate SOI/EOI, LENGTH and CHKSUM; return the binary INFO."""
    if not raw.startswith(SOI) or not raw.endswith(EOI):
        raise PylontechError("missing SOI/EOI")
    if len(raw) < 1 + 12 + 4 + 1:
        raise PylontechError("frame too short")
    body = raw[1:-5]
    chk_ascii = raw[-5:-1]
    try:
        got = int(chk_ascii, 16)
    except ValueError as exc:
        raise PylontechError("checksum not hex") from exc
    want = checksum(body)
    if got != want:
        raise PylontechError(f"checksum mismatch: got {got:04X} want {want:04X}")
    try:
        ver = int(body[0:2], 16)
        adr = int(body[2:4], 16)
        cid1 = int(body[4:6], 16)
        cid2 = int(body[6:8], 16)
        length = int(body[8:12], 16)
    except ValueError as exc:
        raise PylontechError("header not hex") from exc
    lenid = length & 0xFFF
    if length != length_field(lenid):
        raise PylontechError("LENGTH checksum nibble mismatch")
    info_ascii = body[12:]
    if len(info_ascii) != lenid:
        raise PylontechError(f"INFO length {len(info_ascii)} != LENID {lenid}")
    try:
        info = bytes.fromhex(info_ascii.decode("ascii")) if lenid else b""
    except ValueError as exc:
        raise PylontechError("INFO not hex") from exc
    return PylonFrame(ver, adr, cid1, cid2, info, bytes(raw))


class PylonSplitter:
    """Split a byte stream on ``~ ... \\r`` boundaries (sniff + serial read)."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.errors = 0

    def feed(self, data: bytes) -> list[PylonFrame]:
        self.buf.extend(data)
        out: list[PylonFrame] = []
        while True:
            start = self.buf.find(SOI)
            if start < 0:
                self.buf.clear()
                break
            if start:
                del self.buf[:start]
            end = self.buf.find(EOI)
            if end < 0:
                break
            chunk = bytes(self.buf[: end + 1])
            del self.buf[: end + 1]
            try:
                out.append(decode(chunk))
            except PylontechError:
                self.errors += 1
        if len(self.buf) > 4096:
            del self.buf[:-4096]
        return out


# ---------------------------------------------------------------------------
# INFO decoders
# ---------------------------------------------------------------------------
def _c(raw: int) -> float:
    return round((raw - 2731) / 10, 1)


def decode_analog(info: bytes, address: int) -> Pack:
    """CID2 0x42 response INFO for a single pack request (``INFO = adr``).

    Layout (python-pylontech ``get_values_single_fmt``):
      INFOFLAG(1) PACK#(1) NCELL(1) cells[N]*u16 mV  NTEMP(1) temps[M]*u16 0.1K
      current i16 0.1A  voltage u16 mV  remain u16 mAh  USER(1)  total u16 mAh
      cycles u16  [if USER>2: remain u24 mAh, total u24 mAh]
    """
    if len(info) < 4:
        raise PylontechError("analog INFO too short")
    pos = 0
    infoflag = info[pos]
    pos += 1
    pack_no = info[pos]
    pos += 1
    ncell = info[pos]
    pos += 1
    if ncell > 32 or len(info) < pos + 2 * ncell + 1:
        raise PylontechError("analog INFO truncated in cells")
    cells = [struct.unpack_from(">h", info, pos + 2 * i)[0] / 1000 for i in range(ncell)]
    pos += 2 * ncell
    ntemp = info[pos]
    pos += 1
    if ntemp > 16 or len(info) < pos + 2 * ntemp + 2 + 2 + 2 + 1 + 2 + 2:
        raise PylontechError("analog INFO truncated in temps")
    temps = [_c(struct.unpack_from(">h", info, pos + 2 * i)[0]) for i in range(ntemp)]
    pos += 2 * ntemp
    current = struct.unpack_from(">h", info, pos)[0] / 10
    pos += 2
    voltage = struct.unpack_from(">H", info, pos)[0] / 1000
    pos += 2
    remain = struct.unpack_from(">H", info, pos)[0] / 1000
    pos += 2
    user = info[pos]
    pos += 1
    total = struct.unpack_from(">H", info, pos)[0] / 1000
    pos += 2
    cycles = struct.unpack_from(">H", info, pos)[0]
    pos += 2
    if user > 2 and len(info) >= pos + 6:
        remain = int.from_bytes(info[pos : pos + 3], "big") / 1000
        total = int.from_bytes(info[pos + 3 : pos + 6], "big") / 1000

    pack = Pack(address=address, dialect=Dialect.PYLONTECH, payload_length=len(info))
    pack.raw_unknown["infoflag"] = infoflag
    pack.raw_unknown["pack_number"] = pack_no
    pack.cell_voltages = cells
    # Pylontech: first temperature is the BMS board, the rest are cell groups.
    if temps:
        pack.mosfet_temperature = temps[0]
        pack.temperatures = temps[1:]
    pack.current = round(current, 1)
    pack.voltage = round(voltage, 3)
    pack.remaining_capacity_ah = round(remain, 2)
    pack.full_capacity_ah = round(total, 2)
    pack.cycles = cycles
    if total:
        pack.soc = round(100 * remain / total, 1)
    if current > 0.05:
        pack.operation_status = "charging"
    elif current < -0.05:
        pack.operation_status = "discharging"
    else:
        pack.operation_status = "idle"
    return pack


# Alarm INFO (0x44) per-item codes: 0 normal, 1 below lower limit, 2 above upper limit, 0xF0 other.
ALARM_CODES = {0: "ok", 1: "low", 2: "high", 0xF0: "other"}


def decode_alarm(info: bytes) -> dict:
    """CID2 0x44 response INFO.  Layout: INFOFLAG PACK# NCELL cell[N] NTEMP temp[M] current voltage status1..5.

    INFERRED for UP16S (layout from the Pylontech V3.3 document via python-pylontech issues).
    """
    if len(info) < 4:
        raise PylontechError("alarm INFO too short")
    pos = 2
    ncell = info[pos]
    pos += 1
    cells = list(info[pos : pos + ncell])
    pos += ncell
    ntemp = info[pos]
    pos += 1
    temps = list(info[pos : pos + ntemp])
    pos += ntemp
    out = {
        "cells": [ALARM_CODES.get(c, f"code_{c:02x}") for c in cells],
        "temperatures": [ALARM_CODES.get(c, f"code_{c:02x}") for c in temps],
    }
    if len(info) >= pos + 2:
        out["current"] = ALARM_CODES.get(info[pos], f"code_{info[pos]:02x}")
        out["voltage"] = ALARM_CODES.get(info[pos + 1], f"code_{info[pos + 1]:02x}")
        pos += 2
    out["status_bytes"] = info[pos:].hex(" ")
    return out


def decode_management(info: bytes) -> dict:
    """CID2 0x92: charge V limit mV, discharge V limit mV, charge A 0.1A, discharge A 0.1A, status byte."""
    if len(info) < 10:
        raise PylontechError("management INFO too short")
    cv, dv, cc, dc = struct.unpack_from(">HHhh", info, 1)
    status = info[9]
    return {
        "charge_voltage_limit": cv / 1000,
        "discharge_voltage_limit": dv / 1000,
        "charge_current_limit": cc / 10,
        "discharge_current_limit": dc / 10,
        "charge_enable": bool(status & 0x80),
        "discharge_enable": bool(status & 0x40),
        "charge_immediately": bool(status & 0x30),
        "full_charge_request": bool(status & 0x08),
    }


def decode_serial(info: bytes) -> str:
    if len(info) < 17:
        raise PylontechError("serial INFO too short")
    return info[1:17].decode("ascii", errors="replace").strip("\x00 ")


@dataclass
class PylonDecoded:
    kind: str  # request | analog | alarm | management | serial | response | invalid
    frame: PylonFrame
    pack: Pack | None = None
    data: dict = field(default_factory=dict)


class PylonContext:
    """Pair responses with the preceding request, since a response carries RTN, not the command."""

    REQUEST_CID2 = {CID2_ANALOG, CID2_ALARM, CID2_SYSTEM_PARAMS, CID2_PROTOCOL_VERSION, CID2_MANAGEMENT, CID2_SERIAL}

    def __init__(self) -> None:
        self.last_cid2: int | None = None
        self.last_address: int | None = None

    def decode(self, pf: PylonFrame) -> PylonDecoded:
        if pf.cid2 in self.REQUEST_CID2 and len(pf.info) <= 1:
            self.last_cid2, self.last_address = pf.cid2, pf.address
            return PylonDecoded("request", pf, data={"cid2": pf.cid2})
        if pf.cid2 != RTN_OK:
            return PylonDecoded("response", pf, data={"rtn": pf.rtn})
        try:
            if self.last_cid2 == CID2_ANALOG:
                return PylonDecoded("analog", pf, pack=decode_analog(pf.info, pf.address))
            if self.last_cid2 == CID2_ALARM:
                return PylonDecoded("alarm", pf, data=decode_alarm(pf.info))
            if self.last_cid2 == CID2_MANAGEMENT:
                return PylonDecoded("management", pf, data=decode_management(pf.info))
            if self.last_cid2 == CID2_SERIAL:
                return PylonDecoded("serial", pf, data={"serial": decode_serial(pf.info)})
        except PylontechError as exc:
            return PylonDecoded("invalid", pf, data={"error": str(exc)})
        return PylonDecoded("response", pf, data={"rtn": pf.rtn})


def alarms_from_alarm_info(alarm: dict) -> Alarms:
    """Reduce an 0x44 decode to the generic Alarms shape (names only, no bitmask)."""
    names = []
    for i, code in enumerate(alarm.get("cells", [])):
        if code != "ok":
            names.append(f"cell_{i + 1}_{code}")
    for i, code in enumerate(alarm.get("temperatures", [])):
        if code != "ok":
            names.append(f"temperature_{i + 1}_{code}")
    for k in ("current", "voltage"):
        if alarm.get(k, "ok") != "ok":
            names.append(f"{k}_{alarm[k]}")
    return Alarms(fault_bitmask=0, alarm_bitmask=0, faults=[], alarms=names)
