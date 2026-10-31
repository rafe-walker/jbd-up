"""MQTT publishing + Home Assistant MQTT discovery.

Topics (``base`` defaults to ``jbd_up``)::

    jbd_up/status                      online | offline  (LWT)
    jbd_up/bridge/stats                JSON LinkStats
    jbd_up/pack_1/state                JSON Pack.to_dict()
    jbd_up/pack_1/<field>              flat scalar per field (voltage, soc, cell_1 ...)
    homeassistant/sensor/jbd_up_pack_1/<field>/config   discovery

Discovery payloads are built by :func:`discovery_messages` so they can be
unit-tested without a broker.  ``MqttPublisher`` is the only thing that
touches paho.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from .model import ALARM_BITS, FAULT_BITS, LinkStats, Pack

log = logging.getLogger(__name__)

__version__ = "0.1.0"


@dataclass(frozen=True)
class SensorSpec:
    key: str
    name: str
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = "measurement"
    icon: str | None = None
    entity_category: str | None = None
    precision: int | None = None


PACK_SENSORS: tuple[SensorSpec, ...] = (
    SensorSpec("voltage", "Voltage", unit="V", device_class="voltage", precision=2),
    SensorSpec("current", "Current", unit="A", device_class="current", precision=2),
    SensorSpec("power", "Power", unit="W", device_class="power", precision=0),
    SensorSpec("soc", "State of charge", unit="%", device_class="battery", precision=2),
    SensorSpec("soh", "State of health", unit="%", icon="mdi:battery-heart-variant", precision=0),
    SensorSpec("remaining_capacity_ah", "Remaining capacity", unit="Ah", icon="mdi:battery-50", precision=2),
    SensorSpec("full_capacity_ah", "Full capacity", unit="Ah", icon="mdi:battery", precision=2),
    SensorSpec(
        "rated_capacity_ah", "Rated capacity", unit="Ah", icon="mdi:battery", entity_category="diagnostic", precision=2
    ),
    SensorSpec("cycles", "Cycles", state_class="total_increasing", icon="mdi:counter", precision=0),
    SensorSpec("cell_min", "Cell voltage min", unit="V", device_class="voltage", precision=3),
    SensorSpec("cell_max", "Cell voltage max", unit="V", device_class="voltage", precision=3),
    SensorSpec("cell_delta", "Cell voltage delta", unit="V", device_class="voltage", precision=3),
    SensorSpec("cell_avg", "Cell voltage average", unit="V", device_class="voltage", precision=3),
    SensorSpec(
        "cell_min_index", "Lowest cell", state_class=None, icon="mdi:numeric", entity_category="diagnostic", precision=0
    ),
    SensorSpec(
        "cell_max_index",
        "Highest cell",
        state_class=None,
        icon="mdi:numeric",
        entity_category="diagnostic",
        precision=0,
    ),
    SensorSpec("mosfet_temperature", "MOSFET temperature", unit="°C", device_class="temperature", precision=1),
    SensorSpec("ambient_temperature", "Ambient temperature", unit="°C", device_class="temperature", precision=1),
    SensorSpec(
        "charge_voltage_limit",
        "Charge voltage limit",
        unit="V",
        device_class="voltage",
        state_class=None,
        entity_category="diagnostic",
        precision=1,
    ),
    SensorSpec(
        "charge_current_limit",
        "Charge current limit",
        unit="A",
        device_class="current",
        state_class=None,
        entity_category="diagnostic",
        precision=1,
    ),
    SensorSpec(
        "discharge_voltage_limit",
        "Discharge voltage limit",
        unit="V",
        device_class="voltage",
        state_class=None,
        entity_category="diagnostic",
        precision=1,
    ),
    SensorSpec(
        "discharge_current_limit",
        "Discharge current limit",
        unit="A",
        device_class="current",
        state_class=None,
        entity_category="diagnostic",
        precision=1,
    ),
    SensorSpec("operation_status", "Operation status", state_class=None, icon="mdi:battery-sync"),
    SensorSpec(
        "balancing_cells_text",
        "Balancing cells",
        state_class=None,
        icon="mdi:scale-balance",
        entity_category="diagnostic",
    ),
    SensorSpec("faults_text", "Protection faults", state_class=None, icon="mdi:alert-octagon"),
    SensorSpec("alarms_text", "Alarms", state_class=None, icon="mdi:alert"),
    SensorSpec("firmware_version", "Firmware", state_class=None, icon="mdi:chip", entity_category="diagnostic"),
    SensorSpec("dialect", "Protocol dialect", state_class=None, icon="mdi:protocol", entity_category="diagnostic"),
)

BINARY_SENSORS: tuple[tuple[str, str, str | None], ...] = (
    # key, name, device_class
    ("charge_mosfet_on", "Charge MOSFET", "power"),
    ("discharge_mosfet_on", "Discharge MOSFET", "power"),
    ("balancing", "Balancing", None),
    ("any_fault", "Protection active", "problem"),
    ("any_alarm", "Alarm active", "problem"),
)

# One binary_sensor per known fault / alarm bit (device_class problem)
FAULT_BINARY = tuple(f"fault_{n}" for n in FAULT_BITS.values())
ALARM_BINARY = tuple(f"alarm_{n}" for n in ALARM_BITS.values())

STATS_SENSORS: tuple[SensorSpec, ...] = (
    SensorSpec("frames_ok", "Frames OK", state_class="total_increasing", icon="mdi:check-network"),
    SensorSpec("crc_errors", "CRC errors", state_class="total_increasing", icon="mdi:alert-circle"),
    SensorSpec("timeouts", "Timeouts", state_class="total_increasing", icon="mdi:timer-alert"),
    SensorSpec("short_frames", "Short frames", state_class="total_increasing", icon="mdi:alert-circle"),
    SensorSpec("resyncs", "Resyncs", state_class="total_increasing", icon="mdi:sync-alert"),
    SensorSpec("reconnects", "Serial reconnects", state_class="total_increasing", icon="mdi:usb-port"),
)


def pack_topic(base: str, address: int) -> str:
    return f"{base}/pack_{address}"


def node_id(base: str, address: int) -> str:
    return f"{base}_pack_{address}"


def device_info(base: str, pack: Pack) -> dict:
    ids = [node_id(base, pack.address)]
    if pack.bms_serial:
        ids.append(f"{base}_{pack.bms_serial}")
    model = pack.model or pack.manufacturer_code or "JBD UP16S"
    return {
        "identifiers": ids,
        "name": f"Battery pack {pack.address}",
        "manufacturer": "JBD (Jiabaida)",
        "model": model,
        "sw_version": pack.firmware_version or "unknown",
        "serial_number": pack.bms_serial or pack.pack_serial or None,
        "via_device": f"{base}_bridge",
    }


def bridge_device(base: str) -> dict:
    return {
        "identifiers": [f"{base}_bridge"],
        "name": "jbd-up bridge",
        "manufacturer": "Stormhaven Enterprises LLC",
        "model": "jbd-up",
        "sw_version": __version__,
    }


def flat_state(pack: Pack) -> dict:
    """Flatten a Pack into the per-field scalars we publish."""
    d = pack.to_dict()
    out: dict[str, object] = {}
    for spec in PACK_SENSORS:
        if spec.key == "balancing_cells_text":
            out[spec.key] = ",".join(str(c) for c in pack.balancing_cells) or "none"
        elif spec.key == "faults_text":
            out[spec.key] = ",".join(pack.alarms.faults) or "none"
        elif spec.key == "alarms_text":
            out[spec.key] = ",".join(pack.alarms.alarms) or "none"
        else:
            out[spec.key] = d.get(spec.key)
    for i, v in enumerate(pack.cell_voltages, start=1):
        out[f"cell_{i}"] = v
    for i, v in enumerate(pack.temperatures, start=1):
        out[f"temperature_{i}"] = v
    out["charge_mosfet_on"] = pack.charge_mosfet_on
    out["discharge_mosfet_on"] = pack.discharge_mosfet_on
    out["balancing"] = bool(pack.balancing_bitmask)
    out["any_fault"] = pack.alarms.any_fault
    out["any_alarm"] = pack.alarms.any_alarm
    for name in FAULT_BITS.values():
        out[f"fault_{name}"] = name in pack.alarms.faults
    for name in ALARM_BITS.values():
        out[f"alarm_{name}"] = name in pack.alarms.alarms
    return out


def _onoff(v: object) -> str:
    return "ON" if v else "OFF"


def discovery_messages(base: str, pack: Pack, ha_prefix: str = "homeassistant") -> list[tuple[str, dict]]:
    """Return (topic, payload) pairs for HA MQTT discovery of one pack."""
    nid = node_id(base, pack.address)
    ptopic = pack_topic(base, pack.address)
    avail = [{"topic": f"{base}/status"}]
    dev = device_info(base, pack)
    msgs: list[tuple[str, dict]] = []

    def sensor(spec: SensorSpec, key: str, name: str) -> None:
        p: dict[str, object] = {
            "name": name,
            "unique_id": f"{nid}_{key}",
            "object_id": f"{nid}_{key}",
            "state_topic": f"{ptopic}/{key}",
            "availability": avail,
            "device": dev,
            "origin": {
                "name": "jbd-up",
                "sw_version": __version__,
                "support_url": "https://github.com/rafe-walker/jbd-up",
            },
        }
        if spec.unit:
            p["unit_of_measurement"] = spec.unit
        if spec.device_class:
            p["device_class"] = spec.device_class
        if spec.state_class:
            p["state_class"] = spec.state_class
        if spec.icon:
            p["icon"] = spec.icon
        if spec.entity_category:
            p["entity_category"] = spec.entity_category
        if spec.precision is not None:
            p["suggested_display_precision"] = spec.precision
        msgs.append((f"{ha_prefix}/sensor/{nid}/{key}/config", p))

    for spec in PACK_SENSORS:
        sensor(spec, spec.key, spec.name)
    cell_spec = SensorSpec("cell", "Cell", unit="V", device_class="voltage", precision=3)
    for i in range(1, len(pack.cell_voltages) + 1):
        sensor(cell_spec, f"cell_{i}", f"Cell {i} voltage")
    temp_spec = SensorSpec("temperature", "Temperature", unit="°C", device_class="temperature", precision=1)
    for i in range(1, len(pack.temperatures) + 1):
        sensor(temp_spec, f"temperature_{i}", f"Temperature {i}")

    def binary(key: str, name: str, device_class: str | None, category: str | None = None) -> None:
        p: dict[str, object] = {
            "name": name,
            "unique_id": f"{nid}_{key}",
            "object_id": f"{nid}_{key}",
            "state_topic": f"{ptopic}/{key}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability": avail,
            "device": dev,
        }
        if device_class:
            p["device_class"] = device_class
        if category:
            p["entity_category"] = category
        msgs.append((f"{ha_prefix}/binary_sensor/{nid}/{key}/config", p))

    for key, name, dc in BINARY_SENSORS:
        binary(key, name, dc)
    for name in FAULT_BITS.values():
        binary(f"fault_{name}", f"Fault: {name.replace('_', ' ')}", "problem", "diagnostic")
    for name in ALARM_BITS.values():
        binary(f"alarm_{name}", f"Alarm: {name.replace('_', ' ')}", "problem", "diagnostic")
    return msgs


def stats_discovery_messages(base: str, ha_prefix: str = "homeassistant") -> list[tuple[str, dict]]:
    dev = bridge_device(base)
    msgs = []
    for spec in STATS_SENSORS:
        p: dict[str, object] = {
            "name": spec.name,
            "unique_id": f"{base}_bridge_{spec.key}",
            "object_id": f"{base}_bridge_{spec.key}",
            "state_topic": f"{base}/bridge/stats",
            "value_template": "{{ value_json." + spec.key + " }}",
            "availability": [{"topic": f"{base}/status"}],
            "device": dev,
            "entity_category": "diagnostic",
        }
        if spec.state_class:
            p["state_class"] = spec.state_class
        if spec.icon:
            p["icon"] = spec.icon
        msgs.append((f"{ha_prefix}/sensor/{base}_bridge/{spec.key}/config", p))
    return msgs


def state_messages(base: str, pack: Pack) -> list[tuple[str, str]]:
    """(topic, payload-string) pairs for one pack's state."""
    ptopic = pack_topic(base, pack.address)
    msgs: list[tuple[str, str]] = [(f"{ptopic}/state", json.dumps(pack.to_dict(), default=str))]
    for key, val in flat_state(pack).items():
        if isinstance(val, bool):
            msgs.append((f"{ptopic}/{key}", _onoff(val)))
        elif val is None:
            continue
        else:
            msgs.append((f"{ptopic}/{key}", str(val)))
    return msgs


class MqttPublisher:
    """Thin paho wrapper.  All payload shaping happens in the pure functions above."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        base: str = "jbd_up",
        ha_prefix: str = "homeassistant",
        discovery: bool = True,
        client_id: str = "jbd-up",
    ) -> None:
        import paho.mqtt.client as mqtt

        self.base = base
        self.ha_prefix = ha_prefix
        self.discovery = discovery
        self._announced: set[tuple[int, int, int]] = set()
        self._stats_announced = False
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True)
        if username:
            self.client.username_pw_set(username, password)
        self.client.will_set(f"{base}/status", "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(1, 60)
        self.client.connect_async(host, port, keepalive=60)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):  # noqa: ANN001
        log.info("mqtt connected (%s)", reason_code)
        client.publish(f"{self.base}/status", "online", retain=True)
        # force re-announce after a reconnect so HA picks entities up again
        self._announced.clear()
        self._stats_announced = False

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):  # noqa: ANN001
        log.warning("mqtt disconnected (%s)", reason_code)

    def publish_packs(self, packs: list[Pack], stats: LinkStats) -> None:
        if self.discovery and not self._stats_announced:
            for topic, payload in stats_discovery_messages(self.base, self.ha_prefix):
                self.client.publish(topic, json.dumps(payload), retain=True)
            self._stats_announced = True
        for pack in packs:
            sig = (pack.address, len(pack.cell_voltages), len(pack.temperatures))
            if self.discovery and sig not in self._announced:
                for topic, payload in discovery_messages(self.base, pack, self.ha_prefix):
                    self.client.publish(topic, json.dumps(payload), retain=True)
                self._announced.add(sig)
            for topic, payload in state_messages(self.base, pack):
                self.client.publish(topic, payload, retain=False)
        stats_payload = stats.to_dict()
        stats_payload["timestamp"] = int(time.time())
        stats_payload["packs_online"] = len(packs)
        self.client.publish(f"{self.base}/bridge/stats", json.dumps(stats_payload), retain=False)

    def close(self) -> None:
        try:
            self.client.publish(f"{self.base}/status", "offline", retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
