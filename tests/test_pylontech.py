import pytest

from jbd_up import pylontech as pyl


def test_encode_matches_canonical_community_frames():
    assert pyl.encode_analog_request(2) == b"~20024642E00202FD33\r"
    assert pyl.encode_analog_request(1) == b"~20014642E00201FD35\r"
    # zero-length INFO -> LENGTH 0000; checksum of "2000464F0000" is 0xFD9A
    assert pyl.encode(0, pyl.CID2_PROTOCOL_VERSION) == b"~2000464F0000FD9A\r"


def test_length_field_lchksum():
    assert pyl.length_field(0) == 0
    assert pyl.length_field(2) == 0xE002
    assert pyl.length_field(0x7E) == 0xB07E


def test_decode_round_trip_and_errors():
    fr = pyl.decode(pyl.encode_analog_request(2))
    assert (fr.ver, fr.address, fr.cid1, fr.cid2, fr.info) == (0x20, 2, 0x46, 0x42, b"\x02")
    with pytest.raises(pyl.PylontechError):
        pyl.decode(b"~20024642E00202FD34\r")  # bad checksum
    with pytest.raises(pyl.PylontechError):
        pyl.decode(b"20024642E00202FD33\r")  # no SOI
    with pytest.raises(pyl.PylontechError):
        pyl.decode(b"~20024642E00302FD32\r")  # LENGTH nibble wrong


def test_splitter_handles_fragments_and_junk():
    f1 = pyl.encode_analog_request(1)
    f2 = pyl.encode_alarm_request(2)
    stream = b"junk" + f1 + b"\x00\x00~garbage\r" + f2[:5]
    sp = pyl.PylonSplitter()
    out = sp.feed(stream)
    assert [f.address for f in out] == [1]
    assert sp.errors == 1
    out = sp.feed(f2[5:])
    assert [f.address for f in out] == [2]


def test_decode_analog_two_byte_capacities():
    info = bytes([0, 1, 2]) + (3300).to_bytes(2, "big") + (3310).to_bytes(2, "big")
    info += bytes([2]) + (2981).to_bytes(2, "big") + (2951).to_bytes(2, "big")
    info += (55).to_bytes(2, "big", signed=True) + (6610).to_bytes(2, "big")
    info += (1500).to_bytes(2, "big") + bytes([2]) + (2000).to_bytes(2, "big") + (7).to_bytes(2, "big")
    p = pyl.decode_analog(info, 1)
    assert p.cell_voltages == [3.3, 3.31]
    assert p.mosfet_temperature == 25.0 and p.temperatures == [22.0]
    assert p.current == 5.5 and p.voltage == 6.61
    assert p.remaining_capacity_ah == 1.5 and p.full_capacity_ah == 2.0 and p.soc == 75.0
    assert p.cycles == 7 and p.operation_status == "charging"


def test_decode_analog_truncated_raises():
    with pytest.raises(pyl.PylontechError):
        pyl.decode_analog(bytes([0, 1, 16, 0, 0]), 1)


def test_decode_management():
    info = bytes([1]) + (56000).to_bytes(2, "big") + (46000).to_bytes(2, "big")
    info += (1000).to_bytes(2, "big") + (1000).to_bytes(2, "big") + bytes([0xC0])
    m = pyl.decode_management(info)
    assert m["charge_voltage_limit"] == 56.0 and m["discharge_voltage_limit"] == 46.0
    assert m["charge_current_limit"] == 100.0 and m["charge_enable"] and m["discharge_enable"]


def test_alarm_reduction():
    alarm = {"cells": ["ok", "high"], "temperatures": ["low"], "current": "ok", "voltage": "high"}
    a = pyl.alarms_from_alarm_info(alarm)
    assert a.alarms == ["cell_2_high", "temperature_1_low", "voltage_high"]
    assert not a.any_fault


def test_context_pairs_requests_and_responses():
    ctx = pyl.PylonContext()
    req = pyl.decode(pyl.encode_serial_request(1))
    assert ctx.decode(req).kind == "request"
    resp = pyl.decode(pyl.encode(1, pyl.RTN_OK, (bytes([1]) + b"UP16S0190001234A").hex().upper().encode()))
    d = ctx.decode(resp)
    assert d.kind == "serial" and d.data["serial"] == "UP16S0190001234A"
    err = pyl.decode(pyl.encode(1, 0x04))
    assert ctx.decode(err).data["rtn"] == "cid2_invalid"
