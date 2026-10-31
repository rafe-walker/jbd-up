"""Every public capture must decode to the values the original authors reported."""

from collections import Counter

import pytest

from jbd_up import frame as upf
from jbd_up.cli import decode_capture
from jbd_up.model import Dialect
from jbd_up.transport import read_capture

from .conftest import FIXTURES, fixture

ALL_FIXTURES = sorted(p.name for p in FIXTURES.glob("*.txt"))


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_fixture_parses_and_all_up_frames_pass_crc(name):
    lines = read_capture(fixture(name))
    assert lines, name
    for cl in lines:
        if cl.data.startswith(b"~"):
            continue
        fr = upf.decode(cl.data)  # raises on CRC error
        assert fr.address in (1, 2)


def test_every_fixture_has_provenance_header():
    for name in ALL_FIXTURES:
        head = fixture(name).read_text(encoding="utf-8").splitlines()[0]
        assert head.startswith("#") and ("REAL" in head or "SYNTHETIC" in head), name


# --------------------------------------------------------------------------
# UP16S020 two devices (syssi) - the exact model on the user's bench
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def syssi():
    return decode_capture(fixture("up16s020_two_devices_syssi.txt"))


def test_syssi_frame_kinds(syssi):
    kinds = Counter(d.kind for d in syssi)
    # 8 polling rounds in the log; the slave (addr 2) only answers 0x1000.
    assert kinds["pack_status"] == 16
    assert kinds["slave_data"] == 8
    assert kinds["pack_params_1"] == 8
    assert kinds["protection_params"] == 8
    assert kinds["pack_params_2"] == 8
    assert kinds["rtc"] == 8
    assert kinds["request"] > 60
    assert "undecodable" not in kinds


def test_syssi_master_pack_status_matches_syssi_decoded_values(syssi):
    """Values as annotated in syssi's tests/components/jbd_bms_up_pack/frames.h."""
    p = next(d.pack for d in syssi if d.kind == "pack_status" and d.frame.address == 1)
    assert p.dialect is Dialect.UP_NATIVE
    assert p.payload_length == 158
    assert p.voltage == 53.17
    assert p.current == 0.0
    assert p.soc == 60.95
    assert p.remaining_capacity_ah == 60.95
    assert p.full_capacity_ah == 100.0
    assert p.rated_capacity_ah == 100.0
    assert p.cycles == 12
    assert p.mosfet_temperature == 21.0
    assert p.ambient_temperature == 22.6
    assert p.operation_status == "idle"
    assert p.soh == 100
    assert p.mosfet_bitmask == 0x0003 and p.charge_mosfet_on and p.discharge_mosfet_on
    assert p.cell_max == 3.331 and p.cell_max_index == 3
    assert p.cell_min == 3.317 and p.cell_min_index == 8
    assert p.cell_avg == 3.323
    assert p.cell_delta == 0.014
    assert len(p.cell_voltages) == 16
    assert p.temperatures == [20.9, 20.4, 21.0, 21.2]
    assert p.charge_voltage_limit == 58.4
    assert p.charge_current_limit == 200.0  # aggregated over 2 packs on the master
    assert p.discharge_voltage_limit == 44.8
    assert p.discharge_current_limit == 200.0
    assert p.pack_serial == "JBD48100000"
    assert p.firmware_version == "12.4"
    assert p.balancing_bitmask == 0
    assert p.parallel_count == 2 and p.parallel_bitmask == 0b11
    assert p.raw_unknown["constant_5aa6"] == 0x5AA6
    assert p.raw_unknown["can_protocol"] == 0 and p.raw_unknown["rs485_protocol"] == 0  # Pylon / Pylon
    assert not p.alarms.faults and not p.alarms.alarms


def test_syssi_slave_pack_status_relayed_by_master(syssi):
    p = next(d.pack for d in syssi if d.kind == "pack_status" and d.frame.address == 2)
    assert p.payload_length == 150
    assert p.voltage == 53.21
    assert p.raw_unknown["pack_voltage_2"] == 5321  # slaves echo their voltage here, master sends 0
    assert p.soc == 73.0  # whole percent only when relayed
    assert p.remaining_capacity_ah == 73.27
    assert p.cycles == 2
    assert p.cell_max == 3.329 and p.cell_min == 3.322 and p.cell_delta == 0.007
    assert p.temperatures == [21.4, 21.3, 22.0, 21.8]
    assert p.charge_current_limit == 100.0  # non-aggregated
    assert p.firmware_version == "4"  # master does not forward the full version
    assert p.pack_serial == "JBD48100000"


def test_syssi_fc45_cross_checks_against_0x1000(syssi):
    """The 0x45 frame for pack 1 must agree with the 0x1000 frame captured 14 s earlier."""
    s = next(d.pack for d in syssi if d.kind == "slave_data")
    n = next(d.pack for d in syssi if d.kind == "pack_status" and d.frame.address == 1)
    assert s.dialect is Dialect.UP_INTERPACK
    assert s.payload_length == 116
    assert s.cell_voltages == n.cell_voltages[:16] or len(s.cell_voltages) == 16
    assert s.voltage == n.voltage
    assert s.cycles == n.cycles == 12  # settles the cycles-vs-current ambiguity at offset 52
    assert s.soc == 60.0 and abs(s.soc - n.soc) < 1
    assert s.soh == n.soh
    assert s.full_capacity_ah == n.full_capacity_ah
    assert s.remaining_capacity_ah == n.remaining_capacity_ah
    assert s.mosfet_temperature == n.mosfet_temperature == 21.0  # proves the (raw-2731)/10 scale
    assert s.ambient_temperature == n.ambient_temperature == 22.6
    assert s.temperatures == n.temperatures
    assert s.pack_serial == "JBD48100000"
    assert s.charge_current_limit == 100.0 and s.discharge_current_limit == 100.0
    assert s.raw_unknown["firmware_minor"] == 4 and s.raw_unknown["firmware_patch"] == 8  # fw 12.4.8
    assert s.mosfet_bitmask == 0x0003 and s.operation_status == "idle"


def test_syssi_identity_blocks(syssi):
    pp1 = next(d.data for d in syssi if d.kind == "pack_params_1")
    assert pp1["bms_serial"].startswith("UP16S019")
    assert pp1["pack_serial"] == "JBD48100000"
    assert pp1["manufacturer_code"] == "ECO-LFP48100-3U-024EDD"
    assert pp1["bms_date"] == "2025-04-17"
    assert pp1["pack_date"] == "2025-03-18"
    assert pp1["balance_start_voltage"] == 3.4
    assert pp1["balance_delta_voltage"] == 0.03
    assert pp1["full_charge_voltage"] == 56.8
    assert pp1["full_charge_current"] == 15.0
    pp2 = next(d.data for d in syssi if d.kind == "pack_params_2")
    assert pp2["soc_high_res"] == 60.95
    assert pp2["cycles"] == 12
    assert pp2["cell_count"] == 16
    assert pp2["total_charge_ah"] == 1050.7
    assert pp2["total_discharge_ah"] == 973.3
    rtc = next(d.data for d in syssi if d.kind == "rtc")
    assert (rtc["year"], rtc["month"], rtc["day"]) == (2026, 6, 6)
    prot = next(d.data for d in syssi if d.kind == "protection_params")
    assert prot["cell_ovp_protect"] == 3.65
    assert prot["cell_uvp_protect"] == 2.5
    assert prot["pack_ovp_protect"] == 58.4
    assert prot["charge_oc_protect"] == 120.0
    assert prot["discharge_ut_protect"] == -20.0


# --------------------------------------------------------------------------
# UP16S010 fw 13.2.4 (gist)
# --------------------------------------------------------------------------
def test_gist_pack_status_values():
    res = decode_capture(fixture("up16s010_fw13.2.4_pack_status_gist.txt"))
    p = next(d.pack for d in res if d.kind == "pack_status")
    assert p.payload_length == 162
    assert p.voltage == 52.63
    assert p.current == 0.0
    assert p.soc == 73.2
    assert p.remaining_capacity_ah == 73.2
    assert p.mosfet_temperature == 13.2 and p.ambient_temperature == 14.2
    assert p.soh == 100 and p.cycles == 2
    assert p.cell_max == 3.291 and p.cell_min == 3.289
    assert p.raw_unknown["max_cell_index_reported"] == 9
    assert p.raw_unknown["min_cell_index_reported"] == 1
    assert p.temperatures == [12.8, 12.9, 12.9, 13.0]
    assert p.charge_voltage_limit == 58.4 and p.charge_current_limit == 200.0
    assert p.firmware_version == "13.2"
    assert p.pack_serial == "JBD48100000"
    assert p.parallel_count == 2 and p.parallel_bitmask == 3


def test_gist_params_values():
    res = decode_capture(fixture("up16s010_fw13.2.4_params_gist.txt"))
    pp2 = next(d.data for d in res if d.kind == "pack_params_2")
    assert pp2["remaining_capacity"] == 72.82 and pp2["soc_high_res"] == 72.82
    assert pp2["cycles"] == 2
    assert pp2["total_charge_ah"] == 354.9 and pp2["total_discharge_ah"] == 227.8
    rtc = next(d.data for d in res if d.kind == "rtc")
    assert rtc == {"year": 2025, "month": 10, "day": 26, "hour": 6, "minute": 42, "second": 7}
    prot = next(d.data for d in res if d.kind == "protection_params")
    assert prot["cell_ovp_alarm"] == 3.6 and prot["pack_uvp_protect"] == 40.0


# --------------------------------------------------------------------------
# 0x45 124-byte layout (marionw)
# --------------------------------------------------------------------------
def test_marionw_slave_data_124_byte_layout():
    res = decode_capture(fixture("up16s_fc45_slave_data_marionw.txt"))
    p = next(d.pack for d in res if d.kind == "slave_data")
    assert p.payload_length == 124
    assert len(p.cell_voltages) == 16
    assert p.cell_voltages[0] == 3.288 and p.cell_voltages[9] == 3.287 and p.cell_voltages[8] == 3.29
    assert p.voltage == 52.61
    assert p.full_capacity_ah == 100.0 and p.remaining_capacity_ah == 72.94
    assert p.cycles == 2 and p.soc == 72.0 and p.soh == 100
    assert p.pack_serial == "JBD48100000"
    assert p.charge_current_limit == 100.0
    assert p.mosfet_temperature == 12.2 and p.ambient_temperature == 13.3
    assert p.temperatures == [11.8, 11.8, 11.9, 12.0]
    assert p.balancing_bitmask == 0
    assert p.rated_capacity_ah == 0.0  # the 124-byte tail here is zero; the gist's 13.3.2 sample had 100.00


# --------------------------------------------------------------------------
# Pylontech (synthetic)
# --------------------------------------------------------------------------
def test_pylontech_synthetic_fixture_decodes():
    res = decode_capture(fixture("pylontech_analog_synthetic.txt"))
    kinds = [r.kind for r in res]
    assert kinds == ["request", "analog", "request", "alarm"]
    p = res[1].pack
    assert p.dialect is Dialect.PYLONTECH
    assert p.voltage == 53.17 and p.current == -12.3
    assert p.remaining_capacity_ah == 60.95 and p.full_capacity_ah == 100.0
    assert p.soc == 61.0 and p.cycles == 12
    assert p.mosfet_temperature == 21.0 and p.temperatures == [20.9, 20.4, 21.0, 21.2]
    assert len(p.cell_voltages) == 16 and p.cell_delta == 0.014
    assert p.operation_status == "discharging"
    assert res[3].data["cells"] == ["ok"] * 16
