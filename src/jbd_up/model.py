"""Dataclasses describing a decoded battery pack.

Everything that reaches MQTT / Home Assistant goes through these types so the
UP native decoder (``frame.py``), the inter-pack 0x45 decoder and the Pylontech
fallback (``pylontech.py``) all produce the same shape.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum


class Dialect(IntEnum):
    """Which wire protocol a pack answered on."""

    UP_NATIVE = 1  # JBD UP Modbus-RTU variant, FC 0x78 register 0x1000
    UP_INTERPACK = 2  # JBD UP FC 0x45 "ReadSlaveData" (sniffed or requested)
    PYLONTECH = 3  # Pylontech RS485 ASCII (~20xx46xx...) fallback


# Level-2 protection (fault) bit names, bit index -> name.
# Sources: dmitrych5 gist (fw 10.2.1 .. 13.3.2), dbus-serialbattery lltjbd_up16s.py,
# syssi jbd_bms_up_pack.cpp.  All three agree on bits 0..26; bit 27 only in dbus-serialbattery.
FAULT_BITS: dict[int, str] = {
    0: "cell_overvoltage",
    1: "cell_undervoltage",
    2: "pack_overvoltage",
    3: "pack_undervoltage",
    4: "charge_overcurrent_1",
    5: "charge_overcurrent_2",
    6: "discharge_overcurrent_1",
    7: "discharge_overcurrent_2",
    8: "charge_high_temperature",
    9: "charge_low_temperature",
    10: "discharge_high_temperature",
    11: "discharge_low_temperature",
    12: "mosfet_high_temperature",
    13: "ambient_high_temperature",
    14: "ambient_low_temperature",
    15: "cell_voltage_difference",
    16: "temperature_difference",
    17: "soc_too_low",
    18: "short_circuit",
    19: "cell_offline",
    20: "temperature_sensor_failure",
    21: "charge_mosfet_fault",
    22: "discharge_mosfet_fault",
    23: "current_limiting_anomaly",
    24: "aerosol_fault",
    25: "full_charge_protection",
    26: "afe_communication_abnormal",
    27: "reverse_protection",
}

# Level-1 alarm (warning) bit names.  Same three sources agree on bits 0..18.
ALARM_BITS: dict[int, str] = {
    0: "cell_overvoltage",
    1: "cell_undervoltage",
    2: "pack_overvoltage",
    3: "pack_undervoltage",
    4: "charge_overcurrent",
    5: "discharge_overcurrent",
    6: "charge_high_temperature",
    7: "charge_low_temperature",
    8: "discharge_high_temperature",
    9: "discharge_low_temperature",
    10: "mosfet_high_temperature",
    11: "ambient_high_temperature",
    12: "ambient_low_temperature",
    13: "cell_voltage_difference",
    14: "temperature_difference",
    15: "soc_too_low",
    16: "eeprom_fault",
    17: "rtc_abnormal",
    18: "full_charge_protection",
}

# MOSFET / actuator state bits (register 0x1000 offset 32).
MOSFET_BITS: dict[int, str] = {
    0: "discharge_mosfet",
    1: "charge_mosfet",
    2: "precharge_mosfet",
    3: "heater",
    4: "fan",
    5: "dry_contact_1",
    6: "dry_contact_2",
    7: "current_limiting",
}

OPERATION_STATUS = {0: "idle", 1: "charging", 2: "discharging"}


@dataclass
class Alarms:
    """Decoded protection / alarm state."""

    fault_bitmask: int = 0
    alarm_bitmask: int = 0
    # Names of active bits.  Unknown bits are reported as ``fault_bit_N``.
    faults: list[str] = field(default_factory=list)
    alarms: list[str] = field(default_factory=list)

    @classmethod
    def from_bitmasks(cls, fault_bitmask: int, alarm_bitmask: int) -> Alarms:
        return cls(
            fault_bitmask=fault_bitmask,
            alarm_bitmask=alarm_bitmask,
            faults=_names(fault_bitmask, FAULT_BITS, "fault_bit_"),
            alarms=_names(alarm_bitmask, ALARM_BITS, "alarm_bit_"),
        )

    @property
    def any_fault(self) -> bool:
        return self.fault_bitmask != 0

    @property
    def any_alarm(self) -> bool:
        return self.alarm_bitmask != 0


def _names(mask: int, table: dict[int, str], unknown_prefix: str) -> list[str]:
    out = []
    bit = 0
    while mask >> bit:
        if mask & (1 << bit):
            out.append(table.get(bit, f"{unknown_prefix}{bit}"))
        bit += 1
    return out


@dataclass
class Pack:
    """One battery pack as seen on the bus.

    Units are SI-ish and human: volts, amps, degrees C, Ah, percent.
    ``None`` means "this dialect / firmware did not provide it".
    """

    address: int
    dialect: Dialect

    voltage: float | None = None
    current: float | None = None  # + charging, - discharging
    soc: float | None = None  # percent, may be 0.01 % resolution
    soh: float | None = None  # percent
    remaining_capacity_ah: float | None = None
    full_capacity_ah: float | None = None
    rated_capacity_ah: float | None = None
    cycles: int | None = None
    operation_status: str | None = None

    mosfet_bitmask: int | None = None
    charge_mosfet_on: bool | None = None
    discharge_mosfet_on: bool | None = None

    cell_voltages: list[float] = field(default_factory=list)
    balancing_bitmask: int | None = None

    temperatures: list[float] = field(default_factory=list)  # cell / probe temps
    mosfet_temperature: float | None = None
    ambient_temperature: float | None = None

    charge_voltage_limit: float | None = None
    charge_current_limit: float | None = None
    discharge_voltage_limit: float | None = None
    discharge_current_limit: float | None = None

    alarms: Alarms = field(default_factory=Alarms)

    firmware_version: str | None = None
    pack_serial: str | None = None  # "JBD48100000"-style pack SN code
    bms_serial: str | None = None  # "UP16S019..." BMS code (0x1C00 block)
    model: str | None = None  # "UP16S019.16S100A" (0x2810 block)
    manufacturer_code: str | None = None  # "ECO-LFP48100-3U-..." (0x1C00 block)
    parallel_count: int | None = None  # master only
    parallel_bitmask: int | None = None  # master only

    # Bookkeeping
    payload_length: int | None = None
    raw_unknown: dict[str, int] = field(default_factory=dict)

    # ---- derived -------------------------------------------------------
    @property
    def power(self) -> float | None:
        if self.voltage is None or self.current is None:
            return None
        return round(self.voltage * self.current, 1)

    @property
    def cell_min(self) -> float | None:
        return min(self.cell_voltages) if self.cell_voltages else None

    @property
    def cell_max(self) -> float | None:
        return max(self.cell_voltages) if self.cell_voltages else None

    @property
    def cell_delta(self) -> float | None:
        if not self.cell_voltages:
            return None
        return round(max(self.cell_voltages) - min(self.cell_voltages), 3)

    @property
    def cell_avg(self) -> float | None:
        if not self.cell_voltages:
            return None
        return round(sum(self.cell_voltages) / len(self.cell_voltages), 3)

    @property
    def cell_min_index(self) -> int | None:
        """1-based index of the lowest cell."""
        if not self.cell_voltages:
            return None
        return self.cell_voltages.index(min(self.cell_voltages)) + 1

    @property
    def cell_max_index(self) -> int | None:
        if not self.cell_voltages:
            return None
        return self.cell_voltages.index(max(self.cell_voltages)) + 1

    @property
    def balancing_cells(self) -> list[int]:
        if not self.balancing_bitmask:
            return []
        return [i + 1 for i in range(32) if self.balancing_bitmask & (1 << i)]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["dialect"] = self.dialect.name
        d["power"] = self.power
        d["cell_min"] = self.cell_min
        d["cell_max"] = self.cell_max
        d["cell_delta"] = self.cell_delta
        d["cell_avg"] = self.cell_avg
        d["cell_min_index"] = self.cell_min_index
        d["cell_max_index"] = self.cell_max_index
        d["balancing_cells"] = self.balancing_cells
        return d


@dataclass
class LinkStats:
    """Counters exposed as diagnostic sensors."""

    frames_ok: int = 0
    crc_errors: int = 0
    timeouts: int = 0
    short_frames: int = 0
    resyncs: int = 0
    reconnects: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)
