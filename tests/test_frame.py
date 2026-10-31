"""Frame codec: CRC, encode, decode, round trip, splitter resync."""

import pytest

from jbd_up import frame as upf

PACK_STATUS_REQ = bytes.fromhex("01 78 10 00 10 a0 00 00 7f b2".replace(" ", ""))
PARAMS1_REQ = bytes.fromhex("01781c001ca000007c2e")
PARAMS2_REQ = bytes.fromhex("01782000205000007571")
RTC_REQ = bytes.fromhex("01782800280f0000464b")
SLAVE_REQ = bytes.fromhex("0145000000540000d4d3")
PRODUCT_REQ = bytes.fromhex("017828102 83c00007787".replace(" ", ""))
ADDR2_REQ = bytes.fromhex("0278100010a000003fa7")


def test_crc16_modbus_check_value():
    # CRC-16/MODBUS check value for "123456789" is 0x4B37
    assert upf.crc16(b"123456789") == 0x4B37


@pytest.mark.parametrize(
    "addr,block,fc,expected",
    [
        (1, upf.REG_PACK_STATUS, upf.FC_READ, PACK_STATUS_REQ),
        (1, upf.REG_PACK_PARAMS_1, upf.FC_READ, PARAMS1_REQ),
        (1, upf.REG_PACK_PARAMS_2, upf.FC_READ, PARAMS2_REQ),
        (1, upf.REG_RTC, upf.FC_READ, RTC_REQ),
        (1, upf.REG_SLAVE_DATA, upf.FC_READ_SLAVE_DATA, SLAVE_REQ),
        (1, upf.REG_PRODUCT_INFO, upf.FC_READ, PRODUCT_REQ),
        (2, upf.REG_PACK_STATUS, upf.FC_READ, ADDR2_REQ),
    ],
)
def test_encode_matches_public_request_frames(addr, block, fc, expected):
    """Byte-for-byte reproduction of requests seen in the gist and the UP16S020 capture."""
    assert upf.encode_read(addr, block, fc) == expected


def test_decode_request_frame():
    fr = upf.decode(PACK_STATUS_REQ)
    assert fr.address == 1
    assert fr.function == upf.FC_READ
    assert fr.start == 0x1000
    assert fr.end == 0x10A0
    assert fr.payload == b""
    assert fr.is_request()


def test_round_trip_with_payload():
    payload = bytes(range(1, 60))
    raw = upf.encode(7, upf.FC_READ, 0x1000, 0x10A0, payload)
    fr = upf.decode(raw)
    assert fr.address == 7 and fr.payload == payload and fr.raw == raw


def test_decode_rejects_bad_crc():
    bad = bytearray(PACK_STATUS_REQ)
    bad[-1] ^= 0x01
    with pytest.raises(upf.CrcError):
        upf.decode(bytes(bad))


def test_decode_rejects_short_and_long():
    with pytest.raises(upf.ShortFrame):
        upf.decode(PACK_STATUS_REQ[:-1])
    with pytest.raises(upf.FrameError):
        upf.decode(PACK_STATUS_REQ + b"\x00")


def test_decode_rejects_implausible_length():
    raw = bytearray(PACK_STATUS_REQ)
    raw[6] = 0xFF
    with pytest.raises(upf.FrameError):
        upf.decode(bytes(raw) + bytes(10))


def test_splitter_resyncs_through_noise_and_corruption():
    good1 = upf.encode(1, upf.FC_READ, 0x1000, 0x10A0, bytes(20))
    corrupted = bytearray(good1)
    corrupted[12] ^= 0xFF
    good2 = upf.encode(2, upf.FC_READ, 0x1000, 0x10A0, bytes(30))
    stream = b"\x99\x99" + bytes(corrupted) + b"\x01" + good2 + good1[:5]
    sp = upf.FrameSplitter()
    frames = []
    for i in range(0, len(stream), 3):  # arbitrary chunking
        frames.extend(sp.feed(stream[i : i + 3]))
    assert [f.address for f in frames] == [2]
    assert sp.crc_errors >= 1
    frames.extend(sp.feed(good1[5:]))
    assert [f.address for f in frames] == [2, 1]


def test_splitter_bounds_memory_on_noise():
    sp = upf.FrameSplitter()
    noise = bytes(i % 251 for i in range(20000))
    assert sp.feed(noise) == []
    assert len(sp.buf) <= upf.HEADER_LEN + upf.MAX_PAYLOAD + upf.CRC_LEN


def test_temperature_scales():
    assert upf.temp_offset500(710) == 21.0
    assert upf.temp_decikelvin(2941) == 21.0
    assert upf.current_offset300000(0x000493E0) == 0.0
    assert upf.current_offset300000(305000) == 50.0
    assert upf.current_offset300000(298770) == -12.3


def test_ascii_swapped_serial():
    raw = bytes.fromhex("424a3444313830303030003000000000000000000000000000000000")
    assert upf.ascii_swapped(raw) == "JBD48100000"


def test_pack_status_too_short_raises():
    with pytest.raises(upf.FrameError):
        upf.decode_pack_status(bytes(10), 1)


def test_pack_status_implausible_cell_count_raises():
    p = bytearray(200)
    p[66] = 0x01  # 256 cells
    with pytest.raises(upf.FrameError):
        upf.decode_pack_status(bytes(p), 1)


def test_pack_status_minimal_payload_without_tail():
    """A firmware that stops right after the temperature list must still decode."""
    p = bytearray(68)
    p[0:2] = (5317).to_bytes(2, "big")
    p[4:8] = (300000).to_bytes(4, "big")
    p[66:68] = (2).to_bytes(2, "big")
    p += (3300).to_bytes(2, "big") + (3310).to_bytes(2, "big")
    p += (1).to_bytes(2, "big") + (710).to_bytes(2, "big")
    pack = upf.decode_pack_status(bytes(p), 3)
    assert pack.address == 3
    assert pack.voltage == 53.17
    assert pack.cell_voltages == [3.3, 3.31]
    assert pack.temperatures == [21.0]
    assert pack.firmware_version is None
    assert pack.pack_serial is None


def test_field_tables_have_confidence_tags():
    for table in (upf.PACK_STATUS_FIELDS, upf.SLAVE_DATA_FIELDS, upf.PACK_PARAMS_1_FIELDS, upf.PACK_PARAMS_2_FIELDS):
        for row in table:
            assert row[4] in ("CONFIRMED", "INFERRED"), row
            assert row[5], row
