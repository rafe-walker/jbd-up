# Fixtures

Every file here is parsed by `jbd_up.transport.read_capture` and decoded in
`tests/test_fixtures.py`. A `#` header in each file states whether it is a
**REAL** capture or **SYNTHETIC**, and where it came from.

| File | Kind | Device / firmware | Blocks | Source |
| --- | --- | --- | --- | --- |
| `up16s020_two_devices_syssi.txt` | REAL (verbatim ESPHome `uart_debug` log) | JBD-UP16S020 (BMS code `UP16S019…`), Eco-Worthy `ECO-LFP48100-3U`, fw 12.4.8, 2 packs daisy-chained, polled through the master | 0x1000 (master 158 B + slave 150 B), 0x1C00 (134 B), 0x1800 (208 B), 0x2000, 0x2400, 0x2800, 0x2900, **0x45 (116 B)** | <https://github.com/syssi/esphome-jbd-bms/blob/main/docs/pdus/JBD-UP16S020-two-devices.txt> (reported by @deltamelter in [discussions/222](https://github.com/syssi/esphome-jbd-bms/discussions/222)) |
| `up16s010_fw13.2.4_pack_status_gist.txt` | REAL (assembled from byte table; CRC verifies) | JBD UP16S010 fw 13.2.4, Eco-Worthy | 0x1000 (162 B) | <https://gist.github.com/PhracturedBlue/7ef619594eaa4c27f4ff068b461865b8>, extended at <https://gist.github.com/dmitrych5/e2fa4ef16b0b483808e4f4089846d0d0> |
| `up16s010_fw13.2.4_params_gist.txt` | REAL (assembled; CRCs verify) | same | 0x2000 (64 B), 0x2800 (12 B), 0x1800 (208 B) | same gist |
| `up16s_fc45_slave_data_marionw.txt` | REAL (assembled; CRC verifies) | JBD UP16S, Eco-Worthy, newer fw (124 B 0x45 layout) | 0x45 (124 B) | marionw, <https://diysolarforum.com/threads/eco-worthy-48v-100ah-5120wh-lifepo4-server-rack-battery.92299/post-1447436>, via the dmitrych5 gist |
| `pylontech_analog_synthetic.txt` | **SYNTHETIC** | n/a | Pylontech 0x42 analog + 0x44 alarm | built with `jbd_up.pylontech.encode()`; framing per <https://github.com/Frankkkkk/python-pylontech>. Request frames match the canonical `~20014642E00201FD35` example. |

Not yet in the tree (wanted, see README "Bench procedure"):

* A UP16S in **Pylontech RS485 mode** captured from the RS485-1 port.
* A **sniff** of the RS485-2 inter-pack bus (master polling slaves with 0x45) - the
  SRNE repo reports it at 19200 baud.
* Any pack **under load** so the 0x45 current field (offset 40, 4 bytes) can be
  confirmed; every public capture so far is at 0 A.
* A non-Eco-Worthy UP16S (Datou Boss, Vatrer, …) to see what differs.

Public captures are redistributed here for interoperability testing; each
original author retains their rights and is credited above.
