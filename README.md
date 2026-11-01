# jbd-up

**Open-source reader for the JBD "UP16S"-series BMS** — the board inside
Eco-Worthy 48 V server-rack LiFePO4 batteries (and Datou Boss, Vatrer and
other rebrands). Polls the pack over RS485/RS232, decodes per-cell voltages,
SOC/SOH, current, temperatures, limits and every protection/alarm bit, and
publishes it to MQTT with Home Assistant discovery. Ships as a Python package,
a CLI and a Home Assistant add-on.

```
jbd-up discover --port /dev/ttyUSB0
UP native (FC 0x78 reg 0x1000) answered on address 1
pack 1 [UP_NATIVE] 53.17 V 0.0 A SOC 60.95% SOH 100.0% cycles 12 cells min/max/delta 3.317/3.331/0.014 …
```

## Why

Eco-Worthy's rack packs talk to inverters over RS485/CAN (Pylontech, Growatt,
Voltronic, SRNE, Deye…), but if your inverters run **open-loop** (EG4
3000EHV-48 in parallel, for instance) that bus is idle and your monitoring
software (Solar Assistant "use inverter values") is *guessing* SOC from
voltage. The BMS knows the real SOC, every cell voltage, every temperature and
every alarm. This reads it.

The UP16S speaks a Modbus-RTU-like dialect (`[addr][0x78][start][end][len]…[crc16]`),
**not** the classic JBD `DD A5 … 77` protocol every other JBD tool expects —
which is why the usual tools come back empty or half-right.

### Honest status

Everything in `docs/PROTOCOL.md` and `src/jbd_up/frame.py` is tagged
**CONFIRMED** (two or more independent public sources *and* a real capture
decodes with a cross-check) or **INFERRED** (one source / plausible). The
decoder is validated against real captures from an **UP16S020** (two packs,
Eco-Worthy `ECO-LFP48100-3U`, fw 12.4.8) and an **UP16S010** (fw 13.2.4) that
other people published — see `tests/fixtures/README.md`. **No byte in this repo
has yet been measured on my own bench.** The first thing to do after installing
is the capture below; please file it as an issue so it becomes a fixture.

## Install

```sh
pip install git+https://github.com/rafe-walker/jbd-up          # Python 3.11+
# or, for development
git clone https://github.com/rafe-walker/jbd-up && cd jbd-up && pip install -e .[dev] && pytest
```

## Bench procedure: first real capture

You need the **master pack** (DIP 1 ON, others OFF = address 1) and one cable.

### Which port, which cable

| Option | Port on the battery | Cable | Notes |
| --- | --- | --- | --- |
| **A (recommended)** | `RS485-1` RJ45 — the one labelled for "host computer / inverter", *not* the `RS485-2` / ADD IN/OUT pair used to daisy-chain packs | Generic USB-RS485 dongle (FTDI/CH340, ~$10) + a cut Ethernet patch cable | RJ45 **pins 1+8 = B(−), pins 2+7 = A(+), pins 3+6 = GND** (official Eco-Worthy manual p.12; RAR wiring guide agrees). T568B colours: pin 1 orange/white, 2 orange, 3 green/white, 6 green, 7 brown/white, 8 brown. Connect B→B, A→A, GND→GND. If nothing answers, **swap A and B** — every vendor labels them differently. |
| B | `RS232` 6P6C jack | USB-RS232 adapter with a 6P6C plug | Pin 3 = TX (battery → you), 4 = RX, 5 = GND (manual p.12). dbus-serialbattery lists this port as *untested* and the public manual does not say whether it is true ±12 V RS232 or 3.3 V TTL, so use a real RS232 adapter (tolerates either) rather than a bare TTL dongle. The JBD/EG4 "USB read cable" sold for the PC tool is this port. Prefer option A. |
| C (passive) | `RS485-2` (the daisy-chain bus between packs) | Same USB-RS485 dongle, **RX only** | `jbd-up sniff --baud 19200`. Should see the master polling every slave with FC 0x45. Nobody has published this capture yet. |

Settings: **9600 8N1** (A/B). Make sure no inverter is wired to RS485-1 while
you test — two masters on one half-duplex bus = garbage.

### Commands

```sh
ls /dev/serial/by-id/                         # find the adapter; use that stable path
jbd-up discover --port /dev/ttyUSB0           # which dialect answers? (UP native → 0x45 → Pylontech)
jbd-up dump --port /dev/ttyUSB0 --seconds 30 --addresses 1,2 --out ecoworthy-$(date +%F).capture.txt
jbd-up decode ecoworthy-*.capture.txt         # should print pack voltage, 16 cells, temps, SN
```

`dump` sends exactly the read requests the stock JBD PC tool sends (0x1000,
0x1C00, 0x1800, 0x2000, 0x2810, 0x2800, 0x45) and records TX/RX with
timestamps. It never writes to the BMS. Then **open an issue titled
`capture: <brand> <model> fw <version>`** and attach the file (plus a photo of
the BMS label if you can see it). Add `--pylontech` to also probe the Pylontech
dialect, and `--addresses 1-4` for a daisy-chained bank.

If `discover` finds nothing: swap A/B; confirm the pack is on (press the button;
the BMS sleeps); confirm DIP address; try `--baud 19200`; try the RS232 jack.

### What you should see

With the stock Eco-Worthy firmware the master answers all seven blocks; a
daisy-chained slave answers **only** `0x1000` through the master (whole-percent
SOC, refreshed every ~50 s). That is a BMS limitation, not a bug — dbus-serialbattery
documents the same behaviour.

## Running the bridge

```sh
jbd-up poll --port /dev/serial/by-id/usb-FTDI_… --addresses 1,2 \
            --mqtt 192.168.1.10:1883 --user ha --pass secret --interval 5
```

Topics: `jbd_up/pack_1/state` (JSON), `jbd_up/pack_1/<field>` (flat), and
`homeassistant/{sensor,binary_sensor}/jbd_up_pack_1/<field>/config` discovery.
Entities per pack: voltage, current, power, SOC (0.01 % when register 0x2000 is
readable), SOH, remaining/full/rated Ah, cycles, cell 1..16, cell min/max/delta/
average + indices, 4 probe temperatures, MOSFET and ambient temperature, CVL/
CCL/DVL/DCL, operation status, balancing cells, firmware, and one
`binary_sensor` per protection fault / alarm bit plus charge/discharge MOSFET
state. A `jbd-up bridge` device exposes `frames_ok`, `crc_errors`, `timeouts`,
`short_frames`, `resyncs`, `reconnects`.

Without `--mqtt` it prints JSON lines to stdout. `--dialect pylontech` forces
the fallback; `--once` does a single cycle.

### Home Assistant add-on (HA Yellow / HAOS)

[![Open your Home Assistant instance and show the add app repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Frafe-walker%2Fjbd-up)

One click adds the repository (or by hand: **Settings → Apps → App store → ⋮ →
Repositories**, paste this repo's URL). Install **jbd-up**, pick the USB adapter under `port`, start.
It uses the Supervisor-provided Mosquitto credentials when you leave
`mqtt_host` as `core-mosquitto`. Details: `addon/jbd-up/DOCS.md`.

## Pylontech fallback

If the native command set ever proves unreadable on your firmware, the RS485-1
port still speaks the inverter protocol selected by DIP/PC tool. With the
factory default (Pylon) `jbd-up` can request `~20014642E00201FD35\r` (analog
data) and `0x44` (alarms) and decode the ASCII reply — fewer fields (no SOH,
no per-bit protection), but real SOC and cells. `discover` tries it
automatically. See `docs/PROTOCOL.md` §5.

## Comparison

| | jbd-up | syssi/esphome-jbd-bms `jbd_bms_up` | dbus-serialbattery `LltJbd_Up16s` | patman15/BMS_BLE-HA | RAR/esphome-ecoworthy-bms | R-j-walker Node-RED flow |
| --- | --- | --- | --- | --- | --- | --- |
| Runs on | Linux / HA add-on (Python) | ESP32 | Venus OS (Victron GX) | HA (BLE only) | ESP32 | Node-RED + RS485-TCP |
| UP native 0x78 | yes | yes | yes | no | yes | yes |
| 0x45 inter-pack decode / passive sniff | yes / yes | no | 0x45 for CCL/DCL | no | 0x45 | no |
| Pylontech fallback | yes | no | no | no | no | no |
| Raw capture tooling + fixtures | yes | debug yaml | no | no | no | no |
| Write (MOSFET, SOC reset, balance mode) | **no (v0.1)** | MOSFET | SOC reset | – | MOSFET/trip | balance mode, CVL |
| Bluetooth | no | no | no | yes (JBD classic, not UP) | no | no |

If you already run ESPHome or a Cerbo GX, those are mature choices; `jbd-up`
exists for the plain-Linux / HA-Yellow-with-a-USB-dongle case and as a place to
collect captures and reconcile the protocol in one document.

## Limitations

* Read-only. Writes (0x79) are deliberately not implemented in v0.1.
* Slaves via the master: whole-% SOC, no identity block, no balancing bits (BMS
  behaviour). One adapter per pack, or the BMS's internal UART header, gets
  everything.
* 0x45 current scale is unverified (every public capture is at 0 A).
* Pylontech-mode field mapping on an UP16S is inferred from real Pylontech packs.
* No CAN. No BLE.

## Roadmap

1. **Your capture** → fixture → confirm the INFERRED fields.
2. RS485-2 passive sniff mode validated on a daisy-chained bank.
3. Opt-in writes: charge/discharge MOSFET, SOC reset (`0x2006`), RTC set.
4. CAN (Victron / Pylon LV dialects) via python-can.
5. BLE via the JBD UART module (same frames over GATT, per dmitrych5).
6. HA add-on in the community store.

## Credits

Protocol knowledge is community work: dmitrych5 / PhracturedBlue (gist +
dbus-serialbattery driver), syssi (ESPHome component + UP16S020 capture by
@deltamelter), marionw and the diysolarforum thread, RAR, R-j-walker,
daniel-sullivan. Full list with links in `docs/PROTOCOL.md` §6. Thank you.

## License

Apache-2.0 — see `LICENSE` and `NOTICE`. Contributions under the DCO
(`git commit -s`), see `CONTRIBUTING.md`.

---

Built off-grid in Cochise County, AZ — https://theexiledentrepreneur.com
