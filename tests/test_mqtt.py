import json

from jbd_up import frame as upf
from jbd_up import mqtt
from jbd_up.cli import decode_capture
from jbd_up.model import LinkStats

from .conftest import fixture


def _master_pack():
    res = decode_capture(fixture("up16s020_two_devices_syssi.txt"))
    return next(d.pack for d in res if d.kind == "pack_status" and d.frame.address == 1)


def test_discovery_payload_shapes():
    pack = _master_pack()
    msgs = mqtt.discovery_messages("jbd_up", pack)
    topics = [t for t, _ in msgs]
    assert len(topics) == len(set(topics)), "duplicate discovery topics"
    sensors = [t for t in topics if t.startswith("homeassistant/sensor/jbd_up_pack_1/")]
    binaries = [t for t in topics if t.startswith("homeassistant/binary_sensor/jbd_up_pack_1/")]
    assert "homeassistant/sensor/jbd_up_pack_1/soc/config" in sensors
    assert "homeassistant/sensor/jbd_up_pack_1/cell_16/config" in sensors
    assert "homeassistant/sensor/jbd_up_pack_1/cell_17/config" not in sensors
    assert "homeassistant/sensor/jbd_up_pack_1/temperature_4/config" in sensors
    assert "homeassistant/sensor/jbd_up_pack_1/cell_delta/config" in sensors
    assert "homeassistant/binary_sensor/jbd_up_pack_1/fault_cell_overvoltage/config" in binaries
    assert "homeassistant/binary_sensor/jbd_up_pack_1/alarm_soc_too_low/config" in binaries
    assert "homeassistant/binary_sensor/jbd_up_pack_1/charge_mosfet_on/config" in binaries
    for topic, payload in msgs:
        json.dumps(payload)  # serialisable
        assert payload["unique_id"].startswith("jbd_up_pack_1_")
        assert payload["state_topic"].startswith("jbd_up/pack_1/")
        assert payload["device"]["identifiers"][0] == "jbd_up_pack_1"
        assert payload["availability"] == [{"topic": "jbd_up/status"}]
        if "/sensor/" in topic and payload.get("device_class") == "voltage":
            assert payload["unit_of_measurement"] == "V"
        if "/binary_sensor/" in topic:
            assert payload["payload_on"] == "ON" and payload["payload_off"] == "OFF"
    soc = dict(msgs)["homeassistant/sensor/jbd_up_pack_1/soc/config"]
    assert soc["device_class"] == "battery" and soc["unit_of_measurement"] == "%"
    assert soc["state_class"] == "measurement"
    cycles = dict(msgs)["homeassistant/sensor/jbd_up_pack_1/cycles/config"]
    assert cycles["state_class"] == "total_increasing"


def test_device_info_uses_bms_serial_when_known():
    pack = _master_pack()
    pack.bms_serial = "UP16S019000123525032200332"
    pack.model = "UP16S019.16S100A"
    dev = mqtt.device_info("jbd_up", pack)
    assert "jbd_up_UP16S019000123525032200332" in dev["identifiers"]
    assert dev["model"] == "UP16S019.16S100A" and dev["sw_version"] == "12.4"


def test_state_messages_cover_every_discovered_entity():
    pack = _master_pack()
    disc = {t.split("/")[3] for t, _ in mqtt.discovery_messages("jbd_up", pack)}
    state = dict(mqtt.state_messages("jbd_up", pack))
    published = {t.split("/")[-1] for t in state}
    missing = disc - published
    # firmware_version/dialect etc. are strings; None-valued fields are skipped on purpose
    assert not missing, missing
    assert state["jbd_up/pack_1/voltage"] == "53.17"
    assert state["jbd_up/pack_1/soc"] == "60.95"
    assert state["jbd_up/pack_1/power"] == "0.0"
    assert state["jbd_up/pack_1/cell_delta"] == "0.014"
    assert state["jbd_up/pack_1/charge_mosfet_on"] == "ON"
    assert state["jbd_up/pack_1/any_fault"] == "OFF"
    assert state["jbd_up/pack_1/faults_text"] == "none"
    assert state["jbd_up/pack_1/balancing_cells_text"] == "none"
    full = json.loads(state["jbd_up/pack_1/state"])
    assert full["dialect"] == "UP_NATIVE" and full["cell_voltages"][2] == 3.331


def test_fault_bits_turn_into_on_binary_sensors():
    pack = _master_pack()
    pack.alarms = upf.Alarms.from_bitmasks(1 << 18, 1 << 16)  # short circuit fault, EEPROM alarm
    state = dict(mqtt.state_messages("jbd_up", pack))
    assert state["jbd_up/pack_1/fault_short_circuit"] == "ON"
    assert state["jbd_up/pack_1/fault_cell_overvoltage"] == "OFF"
    assert state["jbd_up/pack_1/alarm_eeprom_fault"] == "ON"
    assert state["jbd_up/pack_1/any_fault"] == "ON" and state["jbd_up/pack_1/any_alarm"] == "ON"
    assert state["jbd_up/pack_1/faults_text"] == "short_circuit"


def test_stats_discovery_and_payload():
    msgs = mqtt.stats_discovery_messages("jbd_up")
    keys = {t.split("/")[3] for t, _ in msgs}
    assert {"crc_errors", "timeouts", "frames_ok", "reconnects"} <= keys
    for _, p in msgs:
        assert p["state_topic"] == "jbd_up/bridge/stats"
        assert p["value_template"].startswith("{{ value_json.")
    st = LinkStats(crc_errors=3)
    assert st.to_dict()["crc_errors"] == 3


def test_publisher_announces_once_per_shape(monkeypatch):
    """Drive MqttPublisher with a fake paho client; discovery must not be re-sent every cycle."""
    published: list[tuple[str, str, bool]] = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def username_pw_set(self, *a):
            pass

        def will_set(self, *a, **k):
            pass

        def reconnect_delay_set(self, *a):
            pass

        def connect_async(self, *a, **k):
            pass

        def loop_start(self):
            pass

        def loop_stop(self):
            pass

        def disconnect(self):
            pass

        def publish(self, topic, payload, retain=False):
            published.append((topic, payload, retain))

    import paho.mqtt.client as paho

    monkeypatch.setattr(paho, "Client", FakeClient)
    pub = mqtt.MqttPublisher("localhost")
    pack = _master_pack()
    pub.publish_packs([pack], LinkStats())
    n_disc_1 = sum(1 for t, _, _ in published if t.startswith("homeassistant/"))
    pub.publish_packs([pack], LinkStats())
    n_disc_2 = sum(1 for t, _, _ in published if t.startswith("homeassistant/"))
    assert n_disc_1 > 0 and n_disc_2 == n_disc_1
    assert any(t == "jbd_up/bridge/stats" for t, _, _ in published)
    assert all(retain for t, _, retain in published if t.startswith("homeassistant/"))
    pub.close()
    assert published[-1][0] == "jbd_up/status" and published[-1][1] == "offline"
