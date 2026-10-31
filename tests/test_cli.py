"""CLI: dump against a scripted serial port, decode on fixtures, discover, poll --once."""

import json

import pytest

from jbd_up import cli
from jbd_up.transport import MockTransport, read_capture

from .conftest import fixture
from .test_transport_poll import _bus_from_fixture


@pytest.fixture
def fake_serial(monkeypatch):
    """Replace SerialTransport with a MockTransport fed from the UP16S020 capture."""
    bus = _bus_from_fixture("up16s020_two_devices_syssi.txt")
    created = []

    def factory(port, baud=9600, read_only=False, retry_forever=False):
        tr = MockTransport(bus)
        tr.port, tr.baud, tr.read_only = port, baud, read_only
        created.append(tr)
        return tr

    monkeypatch.setattr(cli, "SerialTransport", factory)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return created


def test_decode_fixture_text(capsys):
    assert cli.main(["decode", str(fixture("up16s020_two_devices_syssi.txt"))]) == 0
    out = capsys.readouterr().out
    assert "pack_status  pack 1 [UP_NATIVE] 53.17 V" in out
    assert "slave_data   pack 1 [UP_INTERPACK]" in out
    assert "JBD48100000" in out


def test_decode_fixture_json(capsys):
    assert cli.main(["decode", "--json", str(fixture("up16s010_fw13.2.4_pack_status_gist.txt"))]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["kind"] == "request"
    assert lines[1]["kind"] == "pack_status" and lines[1]["pack"]["soc"] == 73.2


def test_decode_fields_listing(capsys):
    assert cli.main(["decode", "--fields", "x"]) == 0
    out = capsys.readouterr().out
    assert "CONFIRMED" in out and "INFERRED" in out and "pack_voltage" in out


def test_decode_empty_file_fails(tmp_path, capsys):
    f = tmp_path / "empty.txt"
    f.write_text("# nothing\n")
    assert cli.main(["decode", str(f)]) == 1


def test_dump_writes_capture_that_decodes(fake_serial, tmp_path, capsys):
    out = tmp_path / "cap.txt"
    rc = cli.main(["dump", "--port", "/dev/null", "--seconds", "0.01", "--addresses", "1,2", "--out", str(out)])
    assert rc == 0
    text = out.read_text()
    assert text.startswith("# jbd-up capture v1")
    assert "# mode: dump" in text
    lines = read_capture(out)
    assert any(cl.direction == "TX" for cl in lines) and any(cl.direction == "RX" for cl in lines)
    # the capture must round-trip through the decoder
    results = cli.decode_capture(out)
    kinds = [r.kind for r in results]
    assert "pack_status" in kinds and "slave_data" in kinds and "pack_params_1" in kinds
    printed = capsys.readouterr().out
    assert "pack_status: pack 1 [UP_NATIVE]" in printed
    assert "addr 2 pack_params_1: no reply" in printed  # slave identity via master: documented limitation


def test_dump_timestamps_monotonic(fake_serial, tmp_path):
    out = tmp_path / "cap.txt"
    cli.main(["dump", "--port", "/dev/null", "--seconds", "0.01", "--out", str(out)])
    ts = [cl.t for cl in read_capture(out)]
    assert ts == sorted(ts)


def test_discover_reports_up_native(fake_serial, capsys):
    assert cli.main(["discover", "--port", "/dev/null", "--addresses", "1-2"]) == 0
    assert "UP native (FC 0x78 reg 0x1000) answered on address 1" in capsys.readouterr().out


def test_discover_nothing_gives_hints(monkeypatch, capsys):
    monkeypatch.setattr(cli, "SerialTransport", lambda *a, **k: MockTransport({}))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["discover", "--port", "/dev/null"]) == 2
    out = capsys.readouterr().out
    assert "no reply" in out and "polarity" in out


def test_poll_once_stdout_json(fake_serial, capsys):
    assert cli.main(["poll", "--port", "/dev/null", "--addresses", "1,2", "--once"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    packs = [ln for ln in lines if "address" in ln]
    assert [p["address"] for p in packs] == [1, 2]
    assert packs[0]["bms_serial"].startswith("UP16S019")
    assert lines[-1]["stats"]["frames_ok"] > 0


def test_sniff_decodes_stream(monkeypatch, tmp_path, capsys):
    raw = b"".join(cl.data for cl in read_capture(fixture("up16s020_two_devices_syssi.txt")) if cl.direction == "RX")
    tr = MockTransport(stream=b"\x00\xff" + raw[:3000])
    monkeypatch.setattr(cli, "SerialTransport", lambda *a, **k: tr)
    out = tmp_path / "sniff.txt"
    assert cli.main(["sniff", "--port", "/dev/null", "--seconds", "0.05", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "pack_status: pack 1 [UP_NATIVE]" in printed
    assert "# mode: sniff" in out.read_text()
    assert cli.decode_capture(out)


def test_address_parsing():
    assert cli._parse_addresses("1") == [1]
    assert cli._parse_addresses("1,2, 0x0f") == [1, 2, 15]
    assert cli._parse_addresses("1-4") == [1, 2, 3, 4]
