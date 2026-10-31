"""JBD UP-series ("UP16S") serial framing: encode, decode, checksum, field maps.

The UP dialect is NOT the classic JBD ``DD A5 .. 77`` protocol.  It is a
Modbus-RTU-like binary frame on RS485-1 / RS232 at 9600 8N1:

    [addr:1][fc:1][start:2 BE][end:2 BE][len:2 BE][payload:len][crc16:2 LE]

CRC is CRC-16/MODBUS (poly 0xA001 reflected, init 0xFFFF) over everything
before the CRC, transmitted low byte first.

Confidence tags used in the field tables below:

* ``CONFIRMED`` - byte offset/scale agrees across at least two independent
  sources *and* a real capture in ``tests/fixtures`` decodes to a value that
  cross-checks against another field (e.g. 0x45 cycles == 0x1000 cycles).
* ``INFERRED`` - one source, or plausible but not cross-checked.

Sources (short names used in the tables; full URLs in docs/PROTOCOL.md):

* ``GIST``  - dmitrych5 / PhracturedBlue gist "Info related to Eco Worthy JBD
  UP16S010" (fw 10.2.1, 12.1.7, 13.2.4, 13.3.2).
* ``DBUS``  - mr-manuel/venus-os_dbus-serialbattery ``bms/lltjbd_up16s.py``
  (tested UP16S015 fw 10.2.1 / 12.1.7).
* ``SYSSI`` - syssi/esphome-jbd-bms ``components/jbd_bms_up_pack`` + real
  UP16S020 capture ``docs/pdus/JBD-UP16S020-two-devices.txt``.
* ``RAR``   - RAR/esphome-ecoworthy-bms ``REGISTER_MAP.md``.
* ``NODERED`` - R-j-walker/jbd-up-bms-nodered ``PROTOCOL.md`` (Datou Boss).
* ``SRNE``  - daniel-sullivan/srne-solar-controller ``bms/interpack`` (Go,
  0x45 inter-pack frames, 10 x UP16S015).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .model import OPERATION_STATUS, Alarms, Dialect, Pack

# ---------------------------------------------------------------------------
# Function codes (CONFIRMED: GIST table "per marionw", DBUS, SYSSI, SRNE)
# ---------------------------------------------------------------------------
FC_READ_HOST_STARTUP = 0x42  # ReadHostStartupData (untested)
FC_STATUS_DUMP = 0x44  # master-only (untested)
FC_READ_SLAVE_DATA = 0x45  # "Alternate pack status" - what the master polls slaves with
FC_SLAVE_RESET = 0x46  # SlaveResetCommand (untested, write-class)
FC_SLAVE_CHARGE_INSTR = 0x47  # SlaveChargingInstruction (untested, write-class)
FC_READ_HOST_DATA = 0x50  # Aggregated status (master only, untested)
FC_AGGREGATED_PUSH = 0x51  # unsolicited aggregated status on BT/WiFi UART only
FC_READ = 0x78  # ReadBMSInsideInformation
FC_WRITE = 0x79  # SetBMSInsideInformation

KNOWN_FUNCTION_CODES = frozenset(
    {
        FC_READ_HOST_STARTUP,
        0x43,
        FC_STATUS_DUMP,
        FC_READ_SLAVE_DATA,
        FC_SLAVE_RESET,
        FC_SLAVE_CHARGE_INSTR,
        FC_READ_HOST_DATA,
        FC_AGGREGATED_PUSH,
        FC_READ,
        FC_WRITE,
    }
)

# Register blocks (start, end) as the stock PC tool / Eco-Worthy app request them.
# CONFIRMED by identical request bytes in GIST and the SYSSI capture.
REG_PACK_STATUS = (0x1000, 0x10A0)
REG_PROTECTION_PARAMS = (0x1800, 0x1900)
REG_PACK_PARAMS_1 = (0x1C00, 0x1CA0)
REG_PACK_PARAMS_2 = (0x2000, 0x2050)
REG_WIFI_INFO = (0x2400, 0x2450)
REG_RTC = (0x2800, 0x280F)
REG_PRODUCT_INFO = (0x2810, 0x283C)
REG_DEBUG_INFO = (0x2900, 0x2910)
REG_SLAVE_DATA = (0x0000, 0x0054)  # with FC 0x45
REG_AGGREGATED = (0x0000, 0x0036)  # with FC 0x50

HEADER_LEN = 8
CRC_LEN = 2
MAX_PAYLOAD = 512  # syssi uses the same plausibility bound
MAX_ADDRESS = 247
BROADCAST_ADDRESS = 0xFF  # GIST: only the master answers a broadcast


class FrameError(ValueError):
    """Raised when bytes do not form a valid UP frame."""


class CrcError(FrameError):
    pass


class ShortFrame(FrameError):
    pass


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------
def _build_crc_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC_TABLE = _build_crc_table()


def crc16(data: bytes) -> int:
    """CRC-16/MODBUS. CONFIRMED (GIST, DBUS, SYSSI, SRNE; every fixture verifies)."""
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return crc


# ---------------------------------------------------------------------------
# Frame container
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Frame:
    address: int
    function: int
    start: int
    end: int
    payload: bytes
    raw: bytes = field(repr=False, default=b"")

    @property
    def length(self) -> int:
        return len(self.payload)

    def is_request(self) -> bool:
        """Requests from a host carry a zero-length payload (reads) or a write body."""
        return self.length == 0 or (self.function == FC_WRITE and self.payload[:1] in (b"\x11", b"\x18", b"\x26"))

    def hex(self) -> str:
        return self.raw.hex(" ")


def encode(address: int, function: int, start: int, end: int, payload: bytes = b"") -> bytes:
    """Build a complete frame including CRC (LSB first).

    CONFIRMED: request `01 78 10 00 10 a0 00 00 7f b2` reproduced byte-for-byte
    (GIST, SYSSI capture).
    """
    if not 0 <= address <= 0xFF:
        raise ValueError(f"address out of range: {address}")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too long")
    body = struct.pack(">BBHHH", address, function, start, end, len(payload)) + payload
    return body + struct.pack("<H", crc16(body))


def encode_read(address: int, block: tuple[int, int], function: int = FC_READ) -> bytes:
    """Convenience: read request for one of the ``REG_*`` blocks."""
    return encode(address, function, block[0], block[1])


def decode(raw: bytes) -> Frame:
    """Decode and validate one complete frame.  Raises FrameError subclasses."""
    if len(raw) < HEADER_LEN + CRC_LEN:
        raise ShortFrame(f"frame too short: {len(raw)} bytes")
    address, function, start, end, length = struct.unpack_from(">BBHHH", raw, 0)
    if length > MAX_PAYLOAD:
        raise FrameError(f"implausible payload length {length}")
    total = HEADER_LEN + length + CRC_LEN
    if len(raw) < total:
        raise ShortFrame(f"need {total} bytes, have {len(raw)}")
    if len(raw) > total:
        raise FrameError(f"trailing bytes: have {len(raw)}, frame is {total}")
    got = struct.unpack_from("<H", raw, total - 2)[0]
    want = crc16(raw[: total - 2])
    if got != want:
        raise CrcError(f"crc mismatch: got {got:04x} want {want:04x}")
    return Frame(address, function, start, end, bytes(raw[HEADER_LEN : HEADER_LEN + length]), bytes(raw))


def expected_length(header: bytes) -> int | None:
    """Total frame length implied by an 8-byte header, or None if implausible."""
    if len(header) < HEADER_LEN:
        return None
    length = (header[6] << 8) | header[7]
    if length > MAX_PAYLOAD:
        return None
    return HEADER_LEN + length + CRC_LEN


class FrameSplitter:
    """Stateful resynchronising splitter for a raw byte stream.

    Feed arbitrary chunks; get back validated frames.  Used by the passive
    sniff mode (where we never see request boundaries) and by the transport
    reader.  A candidate frame start must have a plausible address and a
    known function code; anything that fails CRC advances one byte and
    retries, so a corrupted frame costs at most one lost frame.
    """

    def __init__(self) -> None:
        self.buf = bytearray()
        self.crc_errors = 0
        self.resyncs = 0

    def feed(self, data: bytes) -> list[Frame]:
        self.buf.extend(data)
        out: list[Frame] = []
        while True:
            self._skip_to_candidate()
            if len(self.buf) < HEADER_LEN:
                break
            total = expected_length(self.buf)
            if total is None:
                self._drop(1)
                continue
            if len(self.buf) < total:
                break
            try:
                out.append(decode(bytes(self.buf[:total])))
                del self.buf[:total]
            except CrcError:
                self.crc_errors += 1
                self._drop(1)
            except FrameError:
                self._drop(1)
        # keep memory bounded on pure noise
        if len(self.buf) > 4 * (HEADER_LEN + MAX_PAYLOAD + CRC_LEN):
            del self.buf[: len(self.buf) - (HEADER_LEN + MAX_PAYLOAD + CRC_LEN)]
        return out

    def _drop(self, n: int) -> None:
        del self.buf[:n]
        self.resyncs += 1

    def _skip_to_candidate(self) -> None:
        while len(self.buf) >= 2 and not _plausible_start(self.buf[0], self.buf[1]):
            del self.buf[0]
            self.resyncs += 1


def _plausible_start(address: int, function: int) -> bool:
    return (address <= MAX_ADDRESS or address == BROADCAST_ADDRESS) and function in KNOWN_FUNCTION_CODES


# ---------------------------------------------------------------------------
# Scaling helpers (all CONFIRMED by the UP16S020 capture cross-checks)
# ---------------------------------------------------------------------------
def _u16(p: bytes, o: int) -> int:
    return (p[o] << 8) | p[o + 1]


def _u32(p: bytes, o: int) -> int:
    return struct.unpack_from(">I", p, o)[0]


def temp_offset500(raw: int) -> float:
    """0x1000-block temperatures: C = (raw - 500) / 10.  CONFIRMED (GIST, DBUS, SYSSI, RAR)."""
    return round((raw - 500) / 10, 1)


def temp_decikelvin(raw: int) -> float:
    """0x45 / 0x50 / Pylontech temperatures: C = (raw - 2731) / 10.

    CONFIRMED by cross-check: UP16S020 capture 0x45 MOSFET temp 0x0B7D (2941)
    -> 21.0 C equals the 0x1000 MOSFET temp 0x02C6 (710) -> 21.0 C.  (The
    older gist text that read these as 28.5 C is wrong; SRNE and the gist's
    0x3000 register notes agree with deci-kelvin.)
    """
    return round((raw - 2731) / 10, 1)


def current_offset300000(raw: int) -> float:
    """A = (raw - 300000) / 100.  CONFIRMED (GIST, DBUS, SYSSI; 0x000493E0 == 0.00 A in all captures)."""
    return round((raw - 300000) / 100, 2)


def ascii_z(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


def ascii_swapped(b: bytes) -> str:
    """0x45 serial: 11-char string stored LSB-first in 16-bit words ('BJ4D18...' -> 'JBD481...').

    CONFIRMED: both the marionw/gist frame and the UP16S020 capture decode to 'JBD48100000'.
    """
    out = bytearray()
    for i in range(0, len(b) - 1, 2):
        out.append(b[i + 1])
        out.append(b[i])
    return ascii_z(bytes(out))


# ---------------------------------------------------------------------------
# 0x78 / 0x1000  Pack status  (the primary telemetry block)
# ---------------------------------------------------------------------------
# (name, payload_offset, size, unit/scale, confidence, sources)
# Offsets are into the PAYLOAD (frame byte 8 == payload byte 0).
PACK_STATUS_FIELDS: tuple[tuple[str, int, int, str, str, str], ...] = (
    ("pack_voltage", 0, 2, "V = raw/100", "CONFIRMED", "GIST,DBUS,SYSSI,RAR,NODERED"),
    ("pack_voltage_2", 2, 2, "V = raw/100; 0 on master, pack V on slaves", "INFERRED", "GIST,RAR,SYSSI-capture"),
    ("current", 4, 4, "A = (raw-300000)/100, + = charging", "CONFIRMED", "GIST,DBUS,SYSSI,RAR,NODERED"),
    ("soc", 8, 2, "% = raw/100 (slaves via master: whole %)", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("remaining_capacity", 10, 2, "Ah = raw/100", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("full_capacity", 12, 2, "Ah = raw/100", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("rated_capacity", 14, 2, "Ah = raw/100", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("mosfet_temperature", 16, 2, "C = (raw-500)/10", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("ambient_temperature", 18, 2, "C = (raw-500)/10", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("operation_status", 20, 2, "0 idle,1 charging,2 discharging", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("soh", 22, 2, "%", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("fault_bitmask", 24, 4, "level-2 protection bits (see FAULT_BITS)", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("alarm_bitmask", 28, 4, "level-1 alarm bits (see ALARM_BITS)", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("mosfet_bitmask", 32, 2, "bit0 DSG,1 CHG,2 pre,3 heat,4 fan,5/6 dry,7 limit", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("external_state", 34, 2, "bit0 charger,1 load,2 switch", "INFERRED", "GIST,RAR"),
    ("cycles", 36, 2, "count", "CONFIRMED", "GIST,DBUS,SYSSI,RAR + 0x45 cross-check"),
    ("max_cell_index", 38, 2, "1-based", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("max_cell_voltage", 40, 2, "mV", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("min_cell_index", 42, 2, "1-based", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("min_cell_voltage", 44, 2, "mV", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("avg_cell_voltage", 46, 2, "mV", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("max_temp_index", 48, 2, "1-based", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("max_cell_temperature", 50, 2, "C = (raw-500)/10", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("min_temp_index", 52, 2, "1-based", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("min_cell_temperature", 54, 2, "C = (raw-500)/10", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("avg_cell_temperature", 56, 2, "C = (raw-500)/10", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("charge_voltage_limit", 58, 2, "V = raw/10 (CVL to inverter)", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("charge_current_limit", 60, 2, "A = raw/10 (aggregated on master)", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("discharge_voltage_limit", 62, 2, "V = raw/10 (DVL)", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("discharge_current_limit", 64, 2, "A = raw/10 (aggregated on master)", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("cell_count", 66, 2, "N; cell list follows", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("cell_voltages", 68, 0, "N x u16 mV", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    # --- variable tail, offsets relative to T = 68 + 2*N ---
    ("temp_count", -1, 2, "M at T", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("temperatures", -1, 0, "M x u16 at T+2, C = (raw-500)/10", "CONFIRMED", "GIST,DBUS,SYSSI,RAR"),
    ("unknown_tail_0", -1, 2, "at T+2+2M, always 0 so far", "INFERRED", "GIST,DBUS"),
    ("balancing_bitmask", -1, 2, "at T+4+2M, cell1 = bit0 (direct connection only?)", "INFERRED", "GIST,DBUS,SYSSI"),
    (
        "firmware_version",
        -1,
        2,
        "at T+6+2M, major.minor (0x0C04 = 12.4); 0x0004 via master",
        "CONFIRMED",
        "GIST,DBUS,SYSSI",
    ),
    ("pack_serial", -1, 30, "at T+8+2M, ASCII 'JBD48100000'", "CONFIRMED", "GIST,DBUS,SYSSI"),
    ("parallel_count", -1, 2, "at T+38+2M (master only)", "INFERRED", "GIST,SYSSI-capture"),
    ("parallel_bitmask", -1, 2, "at T+40+2M bit per pack (master only)", "INFERRED", "GIST,SYSSI-capture"),
    ("constant_5aa6", -1, 2, "at T+42+2M, 0x5AA6", "INFERRED", "GIST,SYSSI-capture"),
    ("can_protocol", -1, 2, "at T+44+2M 0 Pylon,1 Growatt,.. (gist table)", "INFERRED", "GIST"),
    ("rs485_protocol", -1, 2, "at T+46+2M 0 Pylon,1 Growatt,2 Voltronic,..", "INFERRED", "GIST"),
)


def decode_pack_status(payload: bytes, address: int) -> Pack:
    """Decode FC 0x78 register 0x1000 payload into a Pack.

    Handles the three payload lengths seen in the wild: 162 (UP16S010
    fw 13.2.4), 158 (UP16S020 master) and 150 (UP16S020 slave relayed by the
    master, which drops the trailing master-only fields).
    """
    if len(payload) < 68:
        raise FrameError(f"pack status payload too short: {len(payload)}")
    p = payload
    pack = Pack(address=address, dialect=Dialect.UP_NATIVE, payload_length=len(p))
    pack.voltage = round(_u16(p, 0) / 100, 2)
    pack.raw_unknown["pack_voltage_2"] = _u16(p, 2)
    pack.current = current_offset300000(_u32(p, 4))
    pack.soc = round(_u16(p, 8) / 100, 2)
    pack.remaining_capacity_ah = round(_u16(p, 10) / 100, 2)
    pack.full_capacity_ah = round(_u16(p, 12) / 100, 2)
    pack.rated_capacity_ah = round(_u16(p, 14) / 100, 2)
    pack.mosfet_temperature = temp_offset500(_u16(p, 16))
    pack.ambient_temperature = temp_offset500(_u16(p, 18))
    op = _u16(p, 20)
    pack.operation_status = OPERATION_STATUS.get(op, f"unknown_{op}")
    pack.soh = float(_u16(p, 22))
    pack.alarms = Alarms.from_bitmasks(_u32(p, 24), _u32(p, 28))
    mos = _u16(p, 32)
    pack.mosfet_bitmask = mos
    pack.discharge_mosfet_on = bool(mos & 0x01)
    pack.charge_mosfet_on = bool(mos & 0x02)
    pack.raw_unknown["external_state"] = _u16(p, 34)
    pack.cycles = _u16(p, 36)
    pack.raw_unknown["max_cell_index_reported"] = _u16(p, 38)
    pack.raw_unknown["min_cell_index_reported"] = _u16(p, 42)
    pack.charge_voltage_limit = round(_u16(p, 58) / 10, 1)
    pack.charge_current_limit = round(_u16(p, 60) / 10, 1)
    pack.discharge_voltage_limit = round(_u16(p, 62) / 10, 1)
    pack.discharge_current_limit = round(_u16(p, 64) / 10, 1)

    n = _u16(p, 66)
    if n > 32:
        raise FrameError(f"implausible cell count {n}")
    t = 68
    if len(p) < t + 2 * n + 2:
        raise FrameError("pack status truncated inside cell list")
    pack.cell_voltages = [_u16(p, t + 2 * i) / 1000 for i in range(n)]
    t += 2 * n
    m = _u16(p, t)
    if m > 8:
        raise FrameError(f"implausible temperature sensor count {m}")
    t += 2
    if len(p) < t + 2 * m:
        raise FrameError("pack status truncated inside temperature list")
    pack.temperatures = [temp_offset500(_u16(p, t + 2 * i)) for i in range(m)]
    t += 2 * m

    # Optional tail.  Each field is guarded so a short/odd firmware never crashes us.
    if len(p) >= t + 2:
        pack.raw_unknown["unknown_tail_0"] = _u16(p, t)
    if len(p) >= t + 4:
        pack.balancing_bitmask = _u16(p, t + 2)
    if len(p) >= t + 6:
        fw = _u16(p, t + 4)
        pack.firmware_version = f"{fw >> 8}.{fw & 0xFF}" if fw >= 0x100 else str(fw)
    if len(p) >= t + 36:
        pack.pack_serial = ascii_z(p[t + 6 : t + 36])
    if len(p) >= t + 40:
        pack.parallel_count = _u16(p, t + 36)
        pack.parallel_bitmask = _u16(p, t + 38)
    if len(p) >= t + 42:
        pack.raw_unknown["constant_5aa6"] = _u16(p, t + 40)
    if len(p) >= t + 46:
        pack.raw_unknown["can_protocol"] = _u16(p, t + 42)
        pack.raw_unknown["rs485_protocol"] = _u16(p, t + 44)
    return pack


# ---------------------------------------------------------------------------
# 0x45  "ReadSlaveData" / alternate pack status  (what the master polls slaves with)
# ---------------------------------------------------------------------------
# Fixed layout, 16 cell slots and 4 temp slots regardless of counts (GIST).
# Payload lengths seen: 116 (UP16S020 fw 12.4, UP16S015 fw 12.1.7 per SRNE),
# 124 (fw 13.3.2 adds balance bitmask + rated capacity + 2 unknown).
SLAVE_DATA_FIELDS: tuple[tuple[str, int, int, str, str, str], ...] = (
    ("cell_count", 0, 2, "N (list below is always 16 slots)", "CONFIRMED", "GIST,SRNE,SYSSI-capture"),
    ("cell_voltages", 2, 32, "16 x u16 mV", "CONFIRMED", "GIST,SRNE,SYSSI-capture"),
    ("max_cell_voltage", 34, 2, "mV", "CONFIRMED", "GIST,SRNE"),
    ("min_cell_voltage", 36, 2, "mV", "CONFIRMED", "GIST,SRNE"),
    ("pack_voltage", 38, 2, "V = raw/100", "CONFIRMED", "GIST,SRNE,SYSSI-capture"),
    ("current", 40, 4, "A: 4 bytes, 0 at rest in all captures; scale INFERRED", "INFERRED", "GIST(4B),SRNE(4B signed)"),
    ("max_cell_temperature", 44, 2, "C = (raw-2731)/10", "CONFIRMED", "GIST,SRNE + 0x1000 cross-check"),
    ("min_cell_temperature", 46, 2, "C = (raw-2731)/10", "CONFIRMED", "GIST,SRNE + 0x1000 cross-check"),
    ("full_capacity", 48, 2, "Ah = raw/100", "CONFIRMED", "GIST,SRNE,SYSSI-capture"),
    ("remaining_capacity", 50, 2, "Ah = raw/100", "CONFIRMED", "GIST,SRNE,SYSSI-capture"),
    ("cycles", 52, 2, "count (== 0x1000 cycles in UP16S020 capture)", "CONFIRMED", "GIST,SRNE + cross-check"),
    ("soc", 54, 2, "% whole", "CONFIRMED", "GIST,SRNE,SYSSI-capture"),
    ("soh", 56, 2, "%", "CONFIRMED", "GIST,SRNE,SYSSI-capture"),
    (
        "fault_bitmask",
        58,
        4,
        "level-2 bits, DIFFERENT order from 0x1000 (see SLAVE_FAULT_BITS)",
        "INFERRED",
        "GIST,SRNE",
    ),
    ("mosfet_bitmask", 62, 2, "bit0 DSG,1 CHG,2 discharging,3 charging,4 limit,5/6 dry,7 fan", "INFERRED", "GIST,SRNE"),
    (
        "alarm_bitmask",
        64,
        2,
        "level-1 bits, DIFFERENT order from 0x1000 (see SLAVE_ALARM_BITS)",
        "INFERRED",
        "GIST,SRNE",
    ),
    ("pack_serial", 66, 30, "ASCII, byte-swapped per u16", "CONFIRMED", "GIST,SRNE,SYSSI-capture"),
    ("charge_current_limit", 96, 2, "A = raw/10 non-aggregated", "CONFIRMED", "GIST,DBUS,RAR"),
    ("discharge_current_limit", 98, 2, "A = raw/10 non-aggregated", "CONFIRMED", "GIST,DBUS,RAR"),
    ("firmware_patch", 100, 2, "fw patch version (0x0008 for 12.4.8?)", "INFERRED", "GIST"),
    ("mosfet_temperature", 102, 2, "C = (raw-2731)/10", "CONFIRMED", "GIST,SRNE + 0x1000 cross-check"),
    ("ambient_temperature", 104, 2, "C = (raw-2731)/10", "CONFIRMED", "GIST,SRNE + 0x1000 cross-check"),
    ("temp_count", 106, 1, "M (list below is always 4 slots)", "INFERRED", "GIST"),
    ("firmware_minor", 107, 1, "fw minor version", "INFERRED", "GIST"),
    ("temperatures", 108, 8, "4 x u16, C = (raw-2731)/10", "CONFIRMED", "GIST,SRNE + 0x1000 cross-check"),
    ("balancing_bitmask", 116, 2, "fw >= 13.3.2 only", "INFERRED", "GIST"),
    ("rated_capacity", 118, 2, "Ah = raw/100, fw >= 13.3.2 only", "INFERRED", "GIST"),
)

# Bit tables for the 0x45 frame (GIST fw 13.3.2 decoding; INFERRED).
SLAVE_FAULT_BITS: dict[int, str] = {
    0: "charge_low_temperature",
    1: "charge_high_temperature",
    2: "discharge_low_temperature",
    3: "discharge_high_temperature",
    4: "mosfet_high_temperature",
    5: "ambient_high_temperature",
    6: "ambient_low_temperature",
    7: "charge_mosfet_fault",
    8: "discharge_mosfet_fault",
    9: "temperature_sensor_failure",
    16: "cell_overvoltage",
    17: "cell_undervoltage",
    18: "discharge_overcurrent_1",
    19: "discharge_overcurrent_2",
    20: "charge_overcurrent_1",
    22: "pack_overvoltage",
    23: "pack_undervoltage",
    24: "cell_offline",
    25: "afe_communication_abnormal",
    26: "cell_voltage_difference",
    27: "reverse_protection",
    28: "full_charge_protection",
}
SLAVE_ALARM_BITS: dict[int, str] = {
    0: "cell_overvoltage",
    1: "cell_undervoltage",
    2: "discharge_overcurrent",
    3: "charge_overcurrent",
    4: "pack_overvoltage",
    5: "pack_undervoltage",
    6: "charge_high_temperature",
    7: "charge_low_temperature",
    8: "discharge_high_temperature",
    9: "discharge_low_temperature",
    10: "mosfet_high_temperature",
    11: "soc_too_low",
    12: "ambient_high_temperature",
    13: "ambient_low_temperature",
    14: "cell_voltage_difference",
    15: "full_charge_protection",
}


def decode_slave_data(payload: bytes, address: int) -> Pack:
    """Decode an FC 0x45 payload (116 or 124 bytes) into a Pack."""
    if len(payload) < 116:
        raise FrameError(f"0x45 payload too short: {len(payload)}")
    p = payload
    pack = Pack(address=address, dialect=Dialect.UP_INTERPACK, payload_length=len(p))
    n = min(_u16(p, 0), 16)
    pack.cell_voltages = [_u16(p, 2 + 2 * i) / 1000 for i in range(n)]
    pack.voltage = round(_u16(p, 38) / 100, 2)
    cur = struct.unpack_from(">i", p, 40)[0]
    pack.raw_unknown["current_raw"] = cur
    # INFERRED scale: treat as signed 0.01 A like the 0x1000 field minus its offset.
    # Every public capture is at rest (0), so this cannot be confirmed yet.
    pack.current = round(cur / 100, 2) if cur != 0 else 0.0
    pack.full_capacity_ah = round(_u16(p, 48) / 100, 2)
    pack.remaining_capacity_ah = round(_u16(p, 50) / 100, 2)
    pack.cycles = _u16(p, 52)
    pack.soc = float(_u16(p, 54))
    pack.soh = float(_u16(p, 56))
    fault = _u32(p, 58)
    mos = _u16(p, 62)
    alarm = _u16(p, 64)
    pack.alarms = Alarms(
        fault_bitmask=fault,
        alarm_bitmask=alarm,
        faults=[SLAVE_FAULT_BITS.get(b, f"fault_bit_{b}") for b in range(32) if fault & (1 << b)],
        alarms=[SLAVE_ALARM_BITS.get(b, f"alarm_bit_{b}") for b in range(16) if alarm & (1 << b)],
    )
    pack.mosfet_bitmask = mos
    pack.discharge_mosfet_on = bool(mos & 0x01)
    pack.charge_mosfet_on = bool(mos & 0x02)
    if mos & 0x08:
        pack.operation_status = "charging"
    elif mos & 0x04:
        pack.operation_status = "discharging"
    else:
        pack.operation_status = "idle"
    pack.pack_serial = ascii_swapped(p[66:96])
    pack.charge_current_limit = round(_u16(p, 96) / 10, 1)
    pack.discharge_current_limit = round(_u16(p, 98) / 10, 1)
    pack.raw_unknown["firmware_patch"] = _u16(p, 100)
    pack.mosfet_temperature = temp_decikelvin(_u16(p, 102))
    pack.ambient_temperature = temp_decikelvin(_u16(p, 104))
    m = min(p[106], 4)
    pack.raw_unknown["firmware_minor"] = p[107]
    pack.temperatures = [temp_decikelvin(_u16(p, 108 + 2 * i)) for i in range(m)]
    if len(p) >= 118:
        pack.balancing_bitmask = _u16(p, 116)
    if len(p) >= 120:
        pack.rated_capacity_ah = round(_u16(p, 118) / 100, 2)
    return pack


# ---------------------------------------------------------------------------
# 0x78 / 0x1C00  Pack parameters 1 (identity, balance settings)
# ---------------------------------------------------------------------------
# Payload 134 (UP16S020, SRNE) or 136 (UP16S010 fw 13.2.4).  Offsets below are
# the same in both; the extra 2 bytes are at the tail.
PACK_PARAMS_1_FIELDS: tuple[tuple[str, int, int, str, str, str], ...] = (
    ("balance_mode", 0, 2, "0 static,1 charge,2 always (NODERED)", "INFERRED", "NODERED"),
    ("balance_start_voltage", 4, 2, "mV", "CONFIRMED", "GIST,RAR,NODERED,SRNE"),
    ("balance_delta_voltage", 6, 2, "mV", "CONFIRMED", "GIST,RAR,NODERED,SRNE"),
    ("heater_start_temperature", 8, 2, "C = (raw-500)/10", "INFERRED", "GIST,RAR"),
    ("heater_stop_temperature", 10, 2, "C = (raw-500)/10", "INFERRED", "GIST,RAR"),
    ("full_charge_voltage", 12, 2, "V = raw/100", "CONFIRMED", "GIST,RAR,NODERED,SRNE"),
    ("full_charge_current", 14, 2, "A = raw/100", "CONFIRMED", "GIST,RAR,NODERED"),
    ("bms_serial", 16, 30, "ASCII 'UP16S019...'", "CONFIRMED", "GIST,DBUS,RAR,NODERED,SYSSI-capture"),
    ("bms_year", 46, 2, "", "CONFIRMED", "GIST,DBUS,RAR"),
    ("bms_month", 48, 2, "", "CONFIRMED", "GIST,DBUS,RAR"),
    ("bms_day", 50, 2, "", "CONFIRMED", "GIST,DBUS,RAR"),
    ("pack_serial", 52, 30, "ASCII 'JBD48100000'", "CONFIRMED", "GIST,DBUS,RAR,SYSSI-capture"),
    ("pack_year", 82, 2, "", "CONFIRMED", "GIST,DBUS"),
    ("pack_month", 84, 2, "", "CONFIRMED", "GIST,DBUS"),
    ("pack_day", 86, 2, "", "CONFIRMED", "GIST,DBUS"),
    ("manufacturer_code", 88, 30, "ASCII 'ECO-LFP48100-3U-...'", "CONFIRMED", "GIST,NODERED(bar_code),SYSSI-capture"),
    ("sleep_voltage", 120, 2, "mV", "INFERRED", "GIST,NODERED"),
    ("sleep_delay", 122, 2, "min?", "INFERRED", "GIST,NODERED"),
    ("rs485_type", 126, 2, "", "INFERRED", "GIST"),
    ("can_baud", 128, 2, "", "INFERRED", "GIST"),
    ("rs485_baud", 130, 2, "", "INFERRED", "GIST"),
    ("sleep_current", 132, 2, "A = raw/10", "INFERRED", "GIST,NODERED"),
)


def decode_pack_params_1(payload: bytes) -> dict:
    if len(payload) < 118:
        raise FrameError(f"0x1C00 payload too short: {len(payload)}")
    p = payload
    out = {
        "balance_mode": _u16(p, 0),
        "balance_start_voltage": _u16(p, 4) / 1000,
        "balance_delta_voltage": _u16(p, 6) / 1000,
        "heater_start_temperature": temp_offset500(_u16(p, 8)),
        "heater_stop_temperature": temp_offset500(_u16(p, 10)),
        "full_charge_voltage": _u16(p, 12) / 100,
        "full_charge_current": _u16(p, 14) / 100,
        "bms_serial": ascii_z(p[16:46]),
        "bms_date": f"{_u16(p, 46):04d}-{_u16(p, 48):02d}-{_u16(p, 50):02d}",
        "pack_serial": ascii_z(p[52:82]),
        "pack_date": f"{_u16(p, 82):04d}-{_u16(p, 84):02d}-{_u16(p, 86):02d}",
        "manufacturer_code": ascii_z(p[88:118]),
    }
    if len(p) >= 134:
        out["sleep_voltage"] = _u16(p, 120) / 1000
        out["sleep_delay"] = _u16(p, 122)
        out["rs485_type"] = _u16(p, 126)
        out["can_baud"] = _u16(p, 128)
        out["rs485_baud"] = _u16(p, 130)
        out["sleep_current"] = _u16(p, 132) / 10
    return out


# ---------------------------------------------------------------------------
# 0x78 / 0x2000  Pack parameters 2 (capacity bookkeeping, high-res SOC)
# ---------------------------------------------------------------------------
PACK_PARAMS_2_FIELDS: tuple[tuple[str, int, int, str, str, str], ...] = (
    ("rated_capacity", 0, 2, "Ah = raw/100", "CONFIRMED", "GIST,NODERED,SYSSI-capture"),
    ("full_capacity", 2, 2, "Ah = raw/100", "CONFIRMED", "GIST,NODERED,SYSSI-capture"),
    ("remaining_capacity", 4, 2, "Ah = raw/100", "CONFIRMED", "GIST,NODERED,SYSSI-capture"),
    ("soc_high_res", 6, 2, "% = raw/100 (true 0.01 %, even via master)", "CONFIRMED", "GIST,DBUS,NODERED"),
    ("soh", 8, 2, "%", "CONFIRMED", "GIST,NODERED"),
    ("cycles", 10, 2, "count", "CONFIRMED", "GIST,NODERED,SYSSI-capture"),
    ("total_charge_ah", 16, 4, "Ah = raw/10 cumulative (1050.7 Ah at 12 cycles in capture)", "CONFIRMED", "GIST,DBUS"),
    ("total_discharge_ah", 20, 4, "Ah = raw/10 cumulative", "CONFIRMED", "GIST,DBUS"),
    ("charge_voltage_limit", 42, 2, "V = raw/10", "CONFIRMED", "GIST,NODERED,SYSSI-capture"),
    ("charge_current_limit", 44, 2, "A = raw/10", "CONFIRMED", "GIST,NODERED,SYSSI-capture"),
    ("discharge_voltage_limit", 46, 2, "V = raw/10", "CONFIRMED", "GIST,NODERED,SYSSI-capture"),
    ("discharge_current_limit", 48, 2, "A = raw/10", "CONFIRMED", "GIST,NODERED,SYSSI-capture"),
    ("cell_count", 50, 2, "N", "CONFIRMED", "GIST,SYSSI-capture"),
)


def decode_pack_params_2(payload: bytes) -> dict:
    if len(payload) < 52:
        raise FrameError(f"0x2000 payload too short: {len(payload)}")
    p = payload
    out = {
        "rated_capacity": _u16(p, 0) / 100,
        "full_capacity": _u16(p, 2) / 100,
        "remaining_capacity": _u16(p, 4) / 100,
        "soc_high_res": _u16(p, 6) / 100,
        "soh": _u16(p, 8),
        "cycles": _u16(p, 10),
        "charge_voltage_limit": _u16(p, 42) / 10,
        "charge_current_limit": _u16(p, 44) / 10,
        "discharge_voltage_limit": _u16(p, 46) / 10,
        "discharge_current_limit": _u16(p, 48) / 10,
        "cell_count": _u16(p, 50),
    }
    # DBUS reads 0x2006 .. +2+8+4+4: high_res_soc u16, 8 unused, total_charge u32, total_discharge u32 -> payload
    # offsets 16 and 20.  The newer dmitrych5 gist agrees ("total charge/discharge, Ah = val/10").  Plausibility:
    # the UP16S020 capture reports 1050.7 Ah charged / 973.3 Ah discharged at 12 cycles of a 100 Ah pack.
    out["total_charge_ah"] = _u32(p, 16) / 10
    out["total_discharge_ah"] = _u32(p, 20) / 10
    return out


# ---------------------------------------------------------------------------
# 0x78 / 0x2810  Product information
# ---------------------------------------------------------------------------
def decode_product_info(payload: bytes) -> dict:
    """44-byte block: model id, hw rev, fw major/minor/patch, model string, project code.  INFERRED (GIST, DBUS)."""
    if len(payload) < 44:
        raise FrameError(f"0x2810 payload too short: {len(payload)}")
    p = payload
    return {
        "model_id": _u16(p, 0),
        "hardware_revision": _u16(p, 2),
        "firmware_version": f"{_u16(p, 4)}.{_u16(p, 6)}.{_u16(p, 8)}",
        "unknown_18": _u16(p, 10),
        "model": ascii_z(p[12:28]),
        "project_code": ascii_z(p[28:44]),
    }


# ---------------------------------------------------------------------------
# 0x78 / 0x2800  RTC
# ---------------------------------------------------------------------------
def decode_rtc(payload: bytes) -> dict:
    """12-byte block: year, month, day, hour, minute, second as u16.  CONFIRMED (GIST, SYSSI-capture)."""
    if len(payload) < 12:
        raise FrameError(f"0x2800 payload too short: {len(payload)}")
    y, mo, d, h, mi, s = struct.unpack_from(">HHHHHH", payload, 0)
    return {"year": y, "month": mo, "day": d, "hour": h, "minute": mi, "second": s}


# ---------------------------------------------------------------------------
# 0x78 / 0x1800  Protection parameters (thresholds) - subset
# ---------------------------------------------------------------------------
PROTECTION_FIELDS: tuple[tuple[str, int, str], ...] = (
    # (name, payload offset, scale) - RAR REGISTER_MAP + NODERED agree (CONFIRMED), SRNE confirms 0,6,24,30.
    ("cell_ovp_alarm", 0, "mV"),
    ("cell_ovp_alarm_release", 2, "mV"),
    ("cell_ovp_protect", 6, "mV"),
    ("cell_ovp_protect_release", 8, "mV"),
    ("cell_uvp_alarm", 12, "mV"),
    ("cell_uvp_alarm_release", 14, "mV"),
    ("cell_uvp_protect", 18, "mV"),
    ("cell_uvp_protect_release", 20, "mV"),
    ("pack_ovp_alarm", 24, "cV"),
    ("pack_ovp_protect", 30, "cV"),
    ("pack_uvp_alarm", 36, "cV"),
    ("pack_uvp_protect", 42, "cV"),
    ("charge_oc_alarm", 48, "dA"),
    ("charge_oc_protect", 54, "dA"),
    ("charge_oc2_protect", 62, "dA"),
    ("discharge_oc_alarm", 68, "dA"),
    ("discharge_oc_protect", 74, "dA"),
    ("discharge_oc2_protect", 82, "dA"),
    ("charge_ot_alarm", 88, "T500"),
    ("charge_ot_protect", 94, "T500"),
    ("charge_ut_alarm", 100, "T500"),
    ("charge_ut_protect", 106, "T500"),
    ("discharge_ot_alarm", 112, "T500"),
    ("discharge_ot_protect", 118, "T500"),
    ("discharge_ut_alarm", 124, "T500"),
    ("discharge_ut_protect", 130, "T500"),
)


def decode_protection_params(payload: bytes) -> dict:
    if len(payload) < 136:
        raise FrameError(f"0x1800 payload too short: {len(payload)}")
    out: dict[str, float] = {}
    for name, off, scale in PROTECTION_FIELDS:
        raw = _u16(payload, off)
        if scale == "mV":
            out[name] = raw / 1000
        elif scale == "cV":
            out[name] = raw / 100
        elif scale == "dA":
            out[name] = raw / 10
        else:
            out[name] = temp_offset500(raw)
    return out


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
@dataclass
class Decoded:
    kind: str
    frame: Frame
    pack: Pack | None = None
    data: dict = field(default_factory=dict)


def decode_frame_payload(frame: Frame) -> Decoded:
    """Route a validated response frame to the right block decoder.

    Requests (zero-length payload) come back as ``kind='request'``.  Unknown
    blocks come back as ``kind='unknown'`` with the raw payload hex in data.
    """
    if frame.length == 0:
        return Decoded("request", frame)
    fc, start = frame.function, frame.start
    try:
        if fc == FC_READ and start == REG_PACK_STATUS[0]:
            return Decoded("pack_status", frame, pack=decode_pack_status(frame.payload, frame.address))
        if fc == FC_READ_SLAVE_DATA and start == 0:
            return Decoded("slave_data", frame, pack=decode_slave_data(frame.payload, frame.address))
        if fc == FC_READ and start == REG_PACK_PARAMS_1[0]:
            return Decoded("pack_params_1", frame, data=decode_pack_params_1(frame.payload))
        if fc == FC_READ and start == REG_PACK_PARAMS_2[0]:
            return Decoded("pack_params_2", frame, data=decode_pack_params_2(frame.payload))
        if fc == FC_READ and start == REG_PRODUCT_INFO[0]:
            return Decoded("product_info", frame, data=decode_product_info(frame.payload))
        if fc == FC_READ and start == REG_RTC[0]:
            return Decoded("rtc", frame, data=decode_rtc(frame.payload))
        if fc == FC_READ and start == REG_PROTECTION_PARAMS[0]:
            return Decoded("protection_params", frame, data=decode_protection_params(frame.payload))
    except FrameError as exc:
        return Decoded("undecodable", frame, data={"error": str(exc), "payload": frame.payload.hex(" ")})
    return Decoded("unknown", frame, data={"payload": frame.payload.hex(" ")})
