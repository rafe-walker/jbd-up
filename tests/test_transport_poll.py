"""Transport request/response, capture parsing, Poller and discover against a scripted bus."""

from jbd_up import frame as upf
from jbd_up import pylontech as pyl
from jbd_up.model import Dialect, LinkStats
from jbd_up.poll import Poller, discover
from jbd_up.transport import MockTransport, parse_capture_line, read_capture, up_request

from .conftest import fixture


def _bus_from_fixture(name: str) -> dict[bytes, bytes]:
    """Build a request->response map from a capture's TX/RX pairs."""
    lines = read_capture(fixture(name))
    bus: dict[bytes, bytes] = {}
    last_tx = None
    for cl in lines:
        if cl.direction == "TX":
            last_tx = cl.data
        elif cl.direction == "RX" and last_tx is not None:
            bus.setdefault(last_tx, cl.data)
            last_tx = None
    return bus


def test_parse_capture_line_formats():
    assert parse_capture_line("# comment") is None
    assert parse_capture_line("") is None
    cl = parse_capture_line("T+0001.250 RX 01 78 10 00")
    assert cl.t == 1.25 and cl.direction == "RX" and cl.data == bytes.fromhex("01781000")
    cl = parse_capture_line("[17:06:44.670][D][uart_debug:113]: >>> 01:78:10:00:10:A0:00:00:7F:B2")
    assert cl.direction == "TX" and cl.data == bytes.fromhex("0178100010a000007fb2")
    cl = parse_capture_line("0178100010a000007fb2")
    assert cl.direction == "" and len(cl.data) == 10
    cl = parse_capture_line("TX ~20014642E00201FD35\\r")
    assert cl.data == b"~20014642E00201FD35\r"
    assert parse_capture_line("hello world") is None


def test_up_request_round_trip_and_error_counting():
    bus = _bus_from_fixture("up16s020_two_devices_syssi.txt")
    req = upf.encode_read(1, upf.REG_PACK_STATUS)
    tr = MockTransport(bus)
    st = LinkStats()
    fr = up_request(tr, req, st, timeout=0.01)
    assert fr is not None and fr.length == 158 and st.frames_ok == 1
    # timeout: unknown request gets no bytes
    assert up_request(tr, upf.encode_read(9, upf.REG_PACK_STATUS), st, timeout=0.01) is None
    assert st.timeouts == 1
    # corrupted reply
    bad = bytearray(bus[req])
    bad[20] ^= 0xFF
    tr2 = MockTransport({req: bytes(bad)})
    assert up_request(tr2, req, st, timeout=0.01) is None
    assert st.crc_errors == 1
    # truncated reply
    tr3 = MockTransport({req: bus[req][:50]})
    assert up_request(tr3, req, st, timeout=0.01) is None
    assert st.short_frames == 1
    # garbage header is drained, not fatal
    tr4 = MockTransport({req: b"\xff" * 40})
    assert up_request(tr4, req, st, timeout=0.01) is None
    assert st.resyncs == 1
    # address mismatch
    tr5 = MockTransport({req: bus[upf.encode_read(2, upf.REG_PACK_STATUS)]})
    assert up_request(tr5, req, st, timeout=0.01) is None
    assert "address mismatch" in st.last_error


def test_poller_two_packs_with_identity_merge():
    bus = _bus_from_fixture("up16s020_two_devices_syssi.txt")
    tr = MockTransport(bus)
    poller = Poller(transport=tr, addresses=[1, 2, 3], timeout=0.01, identity_retries=2)
    packs = poller.poll_once()
    assert [p.address for p in packs] == [1, 2]
    assert poller.resolved_dialect is Dialect.UP_NATIVE
    master = packs[0]
    assert master.bms_serial.startswith("UP16S019")
    assert master.manufacturer_code == "ECO-LFP48100-3U-024EDD"
    assert master.soc == 60.95 and master.raw_unknown["soc_source"] == 0x2000
    slave = packs[1]
    assert slave.bms_serial is None  # slave identity unavailable via master, as documented
    assert slave.soc == 73.0
    # second cycle: identity cached, slave identity attempts capped, pack 3 keeps timing out quietly
    packs = poller.poll_once()
    assert len(packs) == 2
    assert poller.identities[1].fetched
    assert poller.identities[2].attempts == 2
    assert poller.stats.timeouts > 0
    writes_first_cycle = [w for w in tr.writes if w[0] == 1]
    assert upf.encode_read(1, upf.REG_PACK_PARAMS_1) in writes_first_cycle
    assert upf.encode_read(1, upf.REG_PRODUCT_INFO) in writes_first_cycle


def test_poller_falls_back_to_pylontech():
    bus = _bus_from_fixture("pylontech_analog_synthetic.txt")
    tr = MockTransport(bus)
    poller = Poller(transport=tr, addresses=[1], timeout=0.01)
    packs = poller.poll_once()
    assert len(packs) == 1 and packs[0].dialect is Dialect.PYLONTECH
    assert packs[0].voltage == 53.17 and packs[0].alarms.alarms == []
    assert poller.resolved_dialect is Dialect.PYLONTECH


def test_poller_dialect_forced_up_does_not_try_pylontech():
    bus = _bus_from_fixture("pylontech_analog_synthetic.txt")
    tr = MockTransport(bus)
    poller = Poller(transport=tr, addresses=[1], dialect="up", timeout=0.01)
    assert poller.poll_once() == []
    assert all(not w.startswith(b"~") for w in tr.writes)


def test_discover_up_native_then_fc45_then_pylontech_then_nothing():
    up_bus = _bus_from_fixture("up16s020_two_devices_syssi.txt")
    r = discover(MockTransport(up_bus), [1], timeout=0.01)
    assert r.dialect is Dialect.UP_NATIVE and r.address == 1 and r.pack.voltage == 53.17

    only45 = {k: v for k, v in up_bus.items() if k[1] == upf.FC_READ_SLAVE_DATA}
    r = discover(MockTransport(only45), [1], timeout=0.01)
    assert r.dialect is Dialect.UP_INTERPACK and r.pack.cycles == 12

    pyl_bus = _bus_from_fixture("pylontech_analog_synthetic.txt")
    r = discover(MockTransport(pyl_bus), [2, 1], timeout=0.01)
    assert r.dialect is Dialect.PYLONTECH and r.address == 1

    r = discover(MockTransport({}), [1, 2], timeout=0.01)
    assert r.dialect is None and "no reply" in r.detail


def test_pylontech_request_frames_are_what_poller_sends():
    tr = MockTransport({})
    Poller(transport=tr, addresses=[1], dialect="pylontech", timeout=0.01).poll_once()
    assert tr.writes == [pyl.encode_analog_request(1)]
