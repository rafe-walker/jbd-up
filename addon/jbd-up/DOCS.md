# jbd-up add-on

Polls one or more JBD UP16S-series battery packs (Eco-Worthy 48 V rack units and
other rebrands) over a USB serial adapter and publishes every value to MQTT with
Home Assistant discovery. Entities appear automatically under
**Settings → Devices → MQTT** as `Battery pack 1`, `Battery pack 2`, …

## Hardware

* **Port on the battery:** `RS485-1` (RJ45, "host computer / inverter") on the
  master pack. Pins 1+8 = B, 2+7 = A, 3+6 = GND. Or the 6P `RS232` jack
  (pin 3 TX, 4 RX, 5 GND) with a TTL-level USB adapter.
* **Adapter:** any FTDI/CH340 USB-RS485 dongle for RS485-1. 9600 8N1.
* **Addressing:** pack DIP switch 1 ON = address 1 (master). Slaves 2..15.
  With the packs daisy-chained via RS485-2 (ADD IN/OUT) the master relays
  pack-status for the slaves, so one cable to the master is enough.

## Options

| Option | Meaning |
| --- | --- |
| `port` | Serial device. Prefer the stable `/dev/serial/by-id/...` path. |
| `baud` | 9600 unless you changed the BMS RS485 baud DIP. |
| `addresses` | `1` or `1,2,3` or `1-4`. |
| `dialect` | `auto` tries the UP native protocol then Pylontech. Force `up` or `pylontech` once you know. |
| `interval` | Seconds between poll cycles (the master refreshes slaves only every ~50 s). |
| `mqtt_*` | Leave `mqtt_host` as `core-mosquitto` and user/password empty to use the Supervisor-provided Mosquitto credentials. |
| `high_res_soc` | Also read register 0x2000 for 0.01 % SOC (firmware ≥ ~12). Turn off on old firmware if you see timeouts. |

## First run

Watch the add-on log. You should see `mqtt connected` and then nothing (silence is
good). If every cycle logs `no packs answered`, run the CLI `discover` from the
SSH add-on or a laptop (see the project README "Bench procedure").

## Diagnostics

The bridge device publishes `frames_ok`, `crc_errors`, `timeouts`,
`short_frames`, `resyncs` and `reconnects` as sensors. Rising `crc_errors`
means wiring/termination; rising `timeouts` means wrong address, baud, A/B
polarity, or the inverter is also polling the same port.

## Developer note

The add-on image is built by the Supervisor from `addon/jbd-up/` and installs
`jbd_up` from GitHub at the ref in `build.yaml` (`JBD_UP_REF`, default the
release tag matching `config.yaml` `version`). Tag the release before publishing
the add-on, or point `JBD_UP_REF` at `main` while developing.
