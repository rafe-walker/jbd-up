# JBD UP16S-series serial protocol — reconciled public knowledge

Status: **v0.1, 2026-10-31.** Compiled from every public description and capture
we could find. Nothing here was measured on our own bench yet; see
[README → Bench procedure](../README.md#bench-procedure-first-real-capture).

Tags: **CONFIRMED** = at least two independent sources agree *and* a real
capture in `tests/fixtures/` decodes to a value that cross-checks against another
field or the author's annotation. **INFERRED** = single source, or plausible but
not cross-checked. **CONFLICT** = sources disagree; we say which we follow.

---

## 1. What hardware this is

| Brand label | BMS inside | Evidence |
| --- | --- | --- |
| ECO-WORTHY 48 V 100 Ah server rack (`ECO-LFP48100-3U`), pack code `JBD48100000` | **JBD UP16S010 / UP16S019 / UP16S020** (model text `UP16S019.16S100A` in the 0x2810 block; BMS serial `UP16S019…` in 0x1C00) | [S2], [S3], [S5] capture |
| Datou Boss DT-48314S | JBD UP16S | [S6] |
| Generic "JBD 48V 16S 100A/200A ESS smart BMS" | UP16S010/015/020 | [S1] README, [S4] |

The **UP** dialect is **not** the classic JBD/Xiaoxiang `DD A5 … 77` UART
protocol. UP packs *do* answer the classic protocol on their UART (some fields
wrong, no multi-pack), but the Eco-Worthy / JBD PC tool ("JBD-ES-UP") speaks the
Modbus-RTU-like dialect below. [S2], [S6]

## 2. Physical layer

### 2.1 Ports on the Eco-Worthy pack (official manual, CONFIRMED [S8] p.12)

```
 RS485-1 (RJ45)  "host computer / inverter"   CAN1 (RJ45)           RS232 (RJ11 6P)      RS485-2 (RJ45) "parallel"
 pin 1,8  RS485-B1                            pin 1,8  NC           pin 1,2,6 NC          pin 1,8  RS485-B2
 pin 2,7  RS485-A1                            pin 2,7  NC           pin 3     TX          pin 2,7  RS485-A2
 pin 4,5  NC                                  pin 4    CANH1        pin 4     RX          pin 4,5  NC
 pin 3,6  GND                                 pin 5    CANL1        pin 5     GND         pin 6    IN/OUT (address)
                                              pin 3,6  GND                                pin 3    (unlabelled in manual)
```

Pins are counted the standard RJ45 way (clip down, pin 1 on the left looking
into the jack). [S7] (RAR wiring guide) independently gives 1/8 = B-, 2/7 = A+,
3/6 = GND for the RS485 port. **CONFIRMED.**

The same JBD UP16S board has (per [S3] docs and the JBD UART module) an internal
"Bluetooth/Wi-Fi UART" header; that header is the only port where the master
forwards *every* 0x78/0x79 command to the slaves. [S2], [S3]

### 2.2 Line settings

* RS485-1 and RS232: **9600 baud, 8N1** (CONFIRMED [S2] [S3] [S5] [S6] [S7]).
  Eco-Worthy's manual says the DIP block also sets "RS485 baud rate"; [S6]
  reports some firmware at 19200. `jbd-up` defaults to 9600, `--baud` to override.
* RS485-2 inter-pack daisy chain: **19200 8N1** per [S9] (ten UP16S015 packs on
  one SRNE system). INFERRED for Eco-Worthy — nobody has sniffed theirs yet.
* Half-duplex RS485. A reply takes 0.3–0.6 s (dbus-serialbattery uses 1.5 s
  timeout). [S3]
* The inverter-facing protocol on RS485-1/CAN1 is selectable (Pylon default,
  Growatt, Voltronic, LXP, Deye, INVT, SRNE, …) via the PC tool or DIP switches
  5–6 ("ADD: used to set the RS485 baud rate and inverter protocol choosing",
  [S8] p.14). The exact DIP table for protocol selection is **not** in the
  public manual; the 0x1000 tail reports the current selection (§4.1 tail).

### 2.3 Addressing (CONFIRMED [S8] p.11)

DIP 1–4 binary, DIP1 = LSB: `0001` = pack 1 (master), `0010` = pack 2 … `1111` =
pack 15. `0000` = single battery (answers address 0? untested; use 1).
Address byte on the wire = DIP value. `0xFF` = broadcast; only the master
answers. [S2]

## 3. Framing (CONFIRMED — every public frame verifies)

```
offset  size  field
0       1     address (1..15 from DIP; 0xFF broadcast)
1       1     function code
2       2     start register, big-endian
4       2     end register, big-endian   (not start+count: e.g. 0x1000..0x10A0)
6       2     payload length N, big-endian (0 in read requests)
8       N     payload
8+N     2     CRC-16/MODBUS (poly 0xA001 reflected, init 0xFFFF) over bytes 0..8+N-1, **low byte first**
```

Reference request: `01 78 10 00 10 A0 00 00 7F B2`. Reference reply header:
`01 78 10 00 10 A0 00 9E` + 158 bytes + `6A 2E`.

Function codes ([S2] "per marionw", used by [S3] [S5] [S9]):

| FC | Name | Direction | Status |
| --- | --- | --- | --- |
| 0x78 | ReadBMSInsideInformation | host → pack | CONFIRMED |
| 0x79 | SetBMSInsideInformation | host → pack (write) | CONFIRMED framing; **not implemented in v0.1** |
| 0x45 | ReadSlaveData | master → slave (and host → pack) | CONFIRMED |
| 0x50 | ReadHostData (aggregated) | host → master | INFERRED, untested |
| 0x51 | aggregated push | master → BT/Wi-Fi UART only | INFERRED |
| 0x42 / 0x43 / 0x44 / 0x46 / 0x47 | host startup / status dump / slave reset / slave charging instruction | — | names only [S2] |

Write requests (0x79) carry a 6-byte payload `XX 4A 42 44 hi lo` where `4A 42 44`
= "JBD" and the leading byte is reported as `0x11` [S2][S5], `0x18` [S6] or
`0x26` [S7] — **CONFLICT**, unresolved; v0.1 is read-only so it does not matter.

## 4. Register blocks (0x78)

Payload offsets below are relative to the payload (frame byte 8 = offset 0).
Sources per field are in `src/jbd_up/frame.py` (`*_FIELDS` tables; print with
`jbd-up decode --fields`).

### 4.1 `0x1000–0x10A0` Pack status — the block you poll

Seen payload sizes: 162 (UP16S010 fw 13.2.4 [S2]), 158 (UP16S020 master [S5]),
150 (UP16S020 slave relayed by master [S5]).

| off | size | field | scale | tag |
| --- | --- | --- | --- | --- |
| 0 | 2 | pack voltage | V = raw/100 | CONFIRMED |
| 2 | 2 | pack voltage #2 | 0 on master, = pack voltage on relayed slaves | INFERRED |
| 4 | 4 | current | A = (raw − 300000)/100, + = charge | CONFIRMED (0x000493E0 = 0.00 A everywhere) |
| 8 | 2 | SOC | % = raw/100 (whole % for relayed slaves) | CONFIRMED |
| 10 | 2 | remaining capacity | Ah = raw/100 | CONFIRMED |
| 12 | 2 | full capacity | Ah = raw/100 | CONFIRMED |
| 14 | 2 | rated capacity | Ah = raw/100 | CONFIRMED |
| 16 | 2 | MOSFET temperature | °C = (raw − 500)/10 | CONFIRMED |
| 18 | 2 | ambient temperature | °C = (raw − 500)/10 | CONFIRMED |
| 20 | 2 | operation status | 0 idle, 1 charging, 2 discharging | CONFIRMED |
| 22 | 2 | SOH | % | CONFIRMED |
| 24 | 4 | level-2 **fault** bitmask | see §4.1.1 | CONFIRMED (bits 0–26) |
| 28 | 4 | level-1 **alarm** bitmask | see §4.1.1 | CONFIRMED (bits 0–18) |
| 32 | 2 | MOSFET/actuator state | b0 DSG, b1 CHG, b2 precharge, b3 heater, b4 fan, b5/b6 dry contacts, b7 limiting | CONFIRMED |
| 34 | 2 | external device state | b0 charger, b1 load, b2 switch | INFERRED |
| 36 | 2 | cycles | count | CONFIRMED (== 0x45 cycles) |
| 38/42 | 2 | index of max / min cell | 1-based | CONFIRMED |
| 40/44/46 | 2 | max / min / avg cell voltage | mV | CONFIRMED |
| 48/52 | 2 | index of max / min temp sensor | 1-based | CONFIRMED |
| 50/54/56 | 2 | max / min / avg cell temperature | (raw − 500)/10 | CONFIRMED |
| 58 | 2 | charge voltage limit (CVL) | V = raw/10 | CONFIRMED |
| 60 | 2 | charge current limit (CCL) | A = raw/10, **aggregated on the master** | CONFIRMED |
| 62 | 2 | discharge voltage limit (DVL) | V = raw/10 | CONFIRMED |
| 64 | 2 | discharge current limit (DCL) | A = raw/10, aggregated on master | CONFIRMED |
| 66 | 2 | cell count N | | CONFIRMED |
| 68 | 2N | cell voltages | mV | CONFIRMED |
| T=68+2N | 2 | temp sensor count M | | CONFIRMED |
| T+2 | 2M | temperatures | (raw − 500)/10 | CONFIRMED |
| T+2+2M | 2 | unknown (always 0) | | INFERRED |
| T+4+2M | 2 | balancing bitmask | cell 1 = bit 0; may be 0 via master | INFERRED |
| T+6+2M | 2 | firmware | hi.lo (0x0C04 = 12.4); master relays only `0x0004` for slaves | CONFIRMED |
| T+8+2M | 30 | pack SN code | ASCII `JBD48100000` | CONFIRMED |
| T+38+2M | 2 | parallel pack count (master) | | INFERRED |
| T+40+2M | 2 | bitmask of online packs (master) | | INFERRED |
| T+42+2M | 2 | constant `0x5AA6` | | INFERRED |
| T+44+2M | 2 | CAN protocol | 0 Pylon, 1 Growatt, 2 Goodwe, 3 Sofar, 4 Victron, 5 Voltronic, 6 LXP, 7 Deye, 8 Ginlong, 9 SMA, 10 VMII, 11 SRNE, 12 INVT, 13 Soroups, 14 Must, 15 Aiswei | INFERRED [S2] |
| T+46+2M | 2 | RS485 protocol | 0 Pylon, 1 Growatt, 2 Voltronic, 3 LXP, 4 Deye, 5 INVT, 6 SRNE, 7 IY-Power, 8 SMK, 9 PACE, 10 HNJD, 11 SAKO, 12–15 EXT-06..09 | INFERRED [S2] |

#### 4.1.1 Bit tables (0x1000)

Level-2 fault (off 24, u32): 0 cell OV, 1 cell UV, 2 pack OV, 3 pack UV,
4 charge OC1, 5 charge OC2, 6 discharge OC1, 7 discharge OC2, 8 charge OT,
9 charge UT, 10 discharge OT, 11 discharge UT, 12 MOSFET OT, 13 ambient OT,
14 ambient UT, 15 cell ΔV, 16 ΔT, 17 SOC low, 18 short circuit, 19 cell offline,
20 temp sensor failure, 21 charge MOS fault, 22 discharge MOS fault,
23 current-limiting anomaly, 24 aerosol, 25 full-charge protection,
26 AFE comm abnormal, 27 reverse protection ([S3] only).

Level-1 alarm (off 28, u32): 0 cell OV, 1 cell UV, 2 pack OV, 3 pack UV,
4 charge OC, 5 discharge OC, 6 charge OT, 7 charge UT, 8 discharge OT,
9 discharge UT, 10 MOSFET OT, 11 ambient OT, 12 ambient UT, 13 cell ΔV, 14 ΔT,
15 SOC low, 16 EEPROM fault, 17 RTC abnormal, 18 full-charge protection.

[S2], [S3], [S5] and [S7] agree on these; [S2]'s older revision presented them
as two u16 pairs with the high word first — same bits.

### 4.2 `0x45` ReadSlaveData (what the master polls slaves with)

Request `01 45 00 00 00 54 00 00 D4 D3`. Payload 116 bytes (fw 12.x, [S5] [S9])
or 124 bytes (fw ≥ 13.3, [S2]). Fixed layout — **16 cell slots and 4 temp
slots regardless of counts**.

| off | size | field | scale | tag |
| --- | --- | --- | --- | --- |
| 0 | 2 | cell count | | CONFIRMED |
| 2 | 32 | 16 × cell voltage | mV | CONFIRMED |
| 34/36 | 2 | max / min cell voltage | mV | CONFIRMED |
| 38 | 2 | pack voltage | V = raw/100 | CONFIRMED |
| 40 | 4 | current | signed; 0 in every public capture → **scale INFERRED** | INFERRED |
| 44/46 | 2 | max / min cell temperature | **°C = (raw − 2731)/10** | CONFIRMED by cross-check† |
| 48/50 | 2 | full / remaining capacity | Ah = raw/100 | CONFIRMED |
| 52 | 2 | cycles | | CONFIRMED (12 == 0x1000 cycles 12) ‡ |
| 54/56 | 2 | SOC / SOH | whole % | CONFIRMED |
| 58 | 4 | level-2 fault bitmask | **different bit order** from 0x1000, see `SLAVE_FAULT_BITS` | INFERRED [S2] |
| 62 | 2 | MOSFET state | b0 DSG, b1 CHG, b2 discharging, b3 charging, b4 limiting, b5/6 dry, b7 fan | INFERRED |
| 64 | 2 | level-1 alarm bitmask | different order, see `SLAVE_ALARM_BITS` | INFERRED |
| 66 | 30 | pack SN code, **byte-swapped per u16** (`BJ4D18…` → `JBD481…`) | | CONFIRMED |
| 96/98 | 2 | CCL / DCL, **non-aggregated** even on master | A = raw/10 | CONFIRMED [S2][S3][S7] |
| 100 | 2 | firmware patch | | INFERRED |
| 102/104 | 2 | MOSFET / ambient temperature | (raw − 2731)/10 | CONFIRMED† |
| 106 | 1 | temp sensor count | | INFERRED |
| 107 | 1 | firmware minor | | INFERRED (4 for fw 12.4.8) |
| 108 | 8 | 4 × temperature | (raw − 2731)/10 | CONFIRMED† |
| 116 | 2 | balancing bitmask | fw ≥ 13.3 only | INFERRED |
| 118 | 2 | rated capacity | Ah = raw/100, fw ≥ 13.3 | INFERRED |

† In the UP16S020 capture the 0x45 MOSFET temperature `0x0B7D` (2941) → 21.0 °C
exactly equals the 0x1000 MOSFET temperature `0x02C6` (710) → 21.0 °C, and all
four probe temps match to 0.1 °C. The gist's reading of these as 28.5 °C is
wrong. ‡ Settles the gist's "current 0.02 A" vs SRNE's "cycle count" at offset 52.

**Why this block matters for us:** the master polls every slave with 0x45 on
the RS485-2 daisy chain. A read-only tap on RS485-2 (`jbd-up sniff --baud 19200`)
should therefore see every pack's cells without transmitting anything.
INFERRED until someone sniffs it.

### 4.3 `0x1C00–0x1CA0` Pack parameters 1 (identity, balance) — master/direct only

Payload 134 [S5][S9] or 136 [S2]. Key fields: `+4` balance start mV, `+6`
balance ΔmV, `+8/+10` heater start/stop (raw−500)/10, `+12` full-charge V/100,
`+14` full-charge A/100, `+16` BMS code (30 B ASCII `UP16S019…`), `+46/48/50`
BMS Y/M/D, `+52` pack SN (30 B), `+82/84/86` pack Y/M/D, `+88` manufacturer code
(30 B, `ECO-LFP48100-3U-024EDD`), `+120` sleep mV, `+122` sleep delay,
`+126` RS485 type, `+128` CAN baud, `+130` RS485 baud, `+132` sleep current A/10.
CONFIRMED for identity/balance/full-charge fields (4 sources); rest INFERRED.
[S7] notes Eco-Worthy offsets are shifted 4 bytes vs some EG4 documents.

### 4.4 `0x2000–0x2050` Pack parameters 2 (capacity bookkeeping)

64 B. `+0` rated Ah/100, `+2` full Ah/100, `+4` remaining Ah/100, `+6` **SOC in
0.01 % — true high-res even for slaves via master** [S3], `+8` SOH, `+10`
cycles, `+12` cycle falloff factor, `+14` cycle factor %, `+16` **total charge
Ah/10 (u32)**, `+20` **total discharge Ah/10 (u32)** ([S2] new revision + [S3];
UP16S020 shows 1050.7/973.3 Ah at 12 cycles — plausible → CONFIRMED), `+24` self
power factor, `+26..+40` OCV-adjust table, `+42/44/46/48` configured CVL/CCL/DVL/
DCL (V/10, A/10 — static, unlike 0x1000's dynamic ones), `+50` cell count, `+52`
Rsns µΩ, `+54` temp sensor count, `+56/58` short-circuit A / µs, `+60` capacity
algorithm flags, `+62` function config bits. [S2]

### 4.5 Others

* `0x1800–0x1900` protection thresholds, 208 B — fully mapped in [S7]
  `REGISTER_MAP.md` and [S6]; `jbd-up` decodes the 26 main thresholds. CONFIRMED.
* `0x2800–0x280F` RTC: Y, M, D, h, m, s as u16. CONFIRMED.
* `0x2810–0x283C` product info, 44 B: model id, hw rev, fw major/minor/patch,
  16 B model `UP16S019.16S100A`, 16 B project code. INFERRED [S2][S3].
* `0x2400` Wi-Fi module info (zeros on V2 packs), `0x2900` debug, `0x3000–0x3102`
  raw status registers, `0x4000` history — see [S2]; not decoded in v0.1.
* `0x50` aggregated status (master only, untested) — layout in [S2].

### 4.6 Which commands work through the master (CONFIRMED [S3] docs, [S5] capture)

| Connection | 0x1000 for slaves | 0x1C00/0x2000/0x2810/0x45 for slaves |
| --- | --- | --- |
| RS485-1 or RS232 on the master, slaves daisy-chained on RS485-2 | yes (relayed, whole-% SOC, ~50 s refresh) | **no** — timeouts (the UP16S020 log shows exactly this) |
| Internal BT/Wi-Fi UART header on the master | yes | yes |
| One adapter per pack | yes | yes |

`jbd-up` tries identity blocks 5× per address then stops asking (same policy as
dbus-serialbattery).

## 5. Pylontech RS485 ASCII (fallback / inverter-facing)

With the RS485 protocol selector at 0 (Pylon, factory default) RS485-1 speaks
the Pylontech low-voltage protocol. Frame (printable ASCII):

```
~  VER  ADR  CID1  CID2  LENGTH  INFO...  CHKSUM  \r
~  20   02   46    42    E002    02       FD33    \r     ← request analog data, pack 2
```

* `LENGTH` = `LCHKSUM`(1 nibble) + `LENID`(12 bits = INFO length in ASCII chars).
  `LCHKSUM = (~(sum of LENID nibbles) + 1) & 0xF`.
* `CHKSUM` = `(~sum(ASCII bytes VER..INFO) + 1) & 0xFFFF`, 4 hex chars.
* Response `CID2` is a return code (00 OK, 01 VER error, 02 CHKSUM, 03 LCHKSUM,
  04 CID2 invalid, 05 format, 06 data, 90 ADR, 91 comm).
* `CID2 0x42` analog INFO (binary after hex-decoding): `INFOFLAG, PACK#, NCELL,
  cell[N] u16 mV, NTEMP, temp[M] u16 0.1 K, current i16 0.1 A, voltage u16 mV,
  remaining u16 mAh, USER, total u16 mAh, cycles u16, [USER>2: remaining u24,
  total u24 mAh]`. Temps are `(raw − 2731)/10 °C` — same convention as the
  UP 0x45 block, which is probably not a coincidence.
* `0x44` alarm, `0x47` system parameters, `0x4F` protocol version, `0x51`
  manufacturer, `0x92` charge/discharge management (CVL/DVL mV, CCL/DCL 0.1 A,
  status bits), `0x93` serial.

CONFIRMED framing ([S10] reproduces the canonical frames). Whether an UP16S in
Pylon mode fills the analog INFO identically to a real Pylontech is INFERRED —
Pylontech-mode third-party packs usually do, at 9600 (Pylontech's own is 115200).
`jbd-up discover` tries UP native first, then 0x45, then Pylontech on each address.

## 6. Sources

* [S1] syssi/esphome-jbd-bms README & components `jbd_bms_up`, `jbd_bms_up_pack`
  — <https://github.com/syssi/esphome-jbd-bms>
* [S2] dmitrych5 gist "Info related to Eco Worthy JBD UP16S010" (fw 10.2.1,
  12.1.7, 13.2.4, 13.3.2) — <https://gist.github.com/dmitrych5/e2fa4ef16b0b483808e4f4089846d0d0>;
  earlier revision by PhracturedBlue — <https://gist.github.com/PhracturedBlue/7ef619594eaa4c27f4ff068b461865b8>
* [S3] mr-manuel/venus-os_dbus-serialbattery `bms/lltjbd_up16s.py` (driver by
  dmitrych5, tested UP16S015 fw 10.2.1/12.1.7) and docs `connect.md` —
  <https://github.com/mr-manuel/venus-os_dbus-serialbattery>,
  <https://github.com/mr-manuel/venus-os_dbus-serialbattery_docs>
* [S4] diysolarforum thread "Eco-Worthy 48V 100Ah 5120Wh LiFePO4 server rack
  battery" (marionw's 0x45 decode, JBD-ES-UP software resource) —
  <https://diysolarforum.com/threads/eco-worthy-48v-100ah-5120wh-lifepo4-server-rack-battery.92299/>
* [S5] syssi capture `docs/pdus/JBD-UP16S020-two-devices.txt` (reported by
  @deltamelter, discussions/222) — in `tests/fixtures/up16s020_two_devices_syssi.txt`
* [S6] R-j-walker/jbd-up-bms-nodered `PROTOCOL.md` (Datou Boss) —
  <https://github.com/R-j-walker/jbd-up-bms-nodered>
* [S7] RAR/esphome-ecoworthy-bms `REGISTER_MAP.md`, `WIRING.md` —
  <https://github.com/RAR/esphome-ecoworthy-bms>
* [S8] ECO-WORTHY 48V 100Ah rack battery user manual (24-08-21), pp. 11–13 —
  <https://cdn.shopify.com/s/files/1/0253/9752/6580/files/48V100AH_-24-08-21-min.pdf>
* [S9] daniel-sullivan/srne-solar-controller `bms/interpack` (Go; 0x45 at 19200
  on the inter-pack bus, 10 × UP16S015) —
  <https://github.com/daniel-sullivan/srne-solar-controller>
* [S10] Frankkkkk/python-pylontech — <https://github.com/Frankkkkk/python-pylontech>
* Also consulted: Eco-Worthy blog "Decoding Multi-Dimensional Communication
  Capabilities…" (<https://ca.eco-worthy.com/blogs/blog/article-page-may-27-09-33-17>),
  patman15/BMS_BLE-HA (BLE only; lists Eco-Worthy under JBD/other BLE adapters,
  no UP16S serial support), shining-man/bsc_fw (`ID_SERIAL_DEVICE_JBD_BMS_UP16S`).

## 7. Open questions (please capture!)

1. 0x45 current field scale/offset (offset 40) — need a capture under load.
2. RS485-2 inter-pack sniff: baud (19200?), who polls, cadence.
3. 0x79 write "key" byte (0x11 / 0x18 / 0x26) — firmware dependent?
4. DIP 5–6 protocol table for Eco-Worthy.
5. Does an UP16S in Pylon mode answer `~20014642E00201FD35\r` on address 1 at 9600?
