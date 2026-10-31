"""``jbd-up`` command line.

jbd-up dump     --port /dev/ttyUSB0 --seconds 30 [--out file]   active capture (requests + replies)
jbd-up sniff    --port /dev/ttyUSB0 --seconds 30 [--out file]   passive capture (never transmits)
jbd-up decode   <capture>                                       decode a capture / fixture file
jbd-up discover --port /dev/ttyUSB0                             which dialect answers?
jbd-up poll     --port /dev/ttyUSB0 --mqtt host:1883 ...        run the bridge
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__
from . import frame as upf
from . import pylontech as pyl
from .model import LinkStats
from .poll import Poller, discover, run_forever
from .transport import (
    DEFAULT_BAUD,
    CaptureWriter,
    SerialTransport,
    TransportError,
    pylon_request,
    read_capture,
    sniff,
    up_request,
)

log = logging.getLogger("jbd_up")

# Blocks the stock PC tool asks for, in the order the UP16S020 capture shows.
DUMP_BLOCKS = (
    ("pack_status", upf.REG_PACK_STATUS, upf.FC_READ),
    ("pack_params_1", upf.REG_PACK_PARAMS_1, upf.FC_READ),
    ("protection_params", upf.REG_PROTECTION_PARAMS, upf.FC_READ),
    ("pack_params_2", upf.REG_PACK_PARAMS_2, upf.FC_READ),
    ("product_info", upf.REG_PRODUCT_INFO, upf.FC_READ),
    ("rtc", upf.REG_RTC, upf.FC_READ),
    ("slave_data", upf.REG_SLAVE_DATA, upf.FC_READ_SLAVE_DATA),
)


def _parse_addresses(s: str) -> list[int]:
    out: list[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a, 0), int(b, 0) + 1))
        else:
            out.append(int(part, 0))
    for a in out:
        if not 0 <= a <= 255:
            raise argparse.ArgumentTypeError(f"address {a} out of range")
    return out


def _add_serial_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--port", required=True, help="serial device, e.g. /dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="baud rate (default 9600; try 19200 on RS485-2)")
    p.add_argument("--timeout", type=float, default=1.5, help="reply timeout seconds")


def _add_addr_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--addresses",
        type=_parse_addresses,
        default=[1],
        help="pack addresses to poll, e.g. '1' or '1,2,3' or '1-16' (default 1)",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="jbd-up", description="Reader for JBD UP16S-series BMS (Eco-Worthy 48 V rack packs)"
    )
    ap.add_argument("--version", action="version", version=f"jbd-up {__version__}")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="active capture: send the stock read requests, record everything")
    _add_serial_args(d)
    _add_addr_arg(d)
    d.add_argument("--seconds", type=float, default=30)
    d.add_argument("--out", type=Path, default=None, help="output file (default jbd-up-<timestamp>.capture.txt)")
    d.add_argument("--pylontech", action="store_true", help="also send Pylontech analog/alarm requests")
    d.add_argument("--no-decode", action="store_true", help="do not print decoded values while capturing")

    s = sub.add_parser("sniff", help="passive capture: read-only, never transmits")
    _add_serial_args(s)
    s.add_argument("--seconds", type=float, default=30, help="0 = until Ctrl-C")
    s.add_argument("--out", type=Path, default=None)

    de = sub.add_parser("decode", help="decode a capture or fixture file")
    de.add_argument("capture", type=Path)
    de.add_argument("--json", action="store_true", help="emit one JSON object per decoded frame")
    de.add_argument("--fields", action="store_true", help="print the field map with confidence tags")

    di = sub.add_parser("discover", help="probe which dialect answers")
    _add_serial_args(di)
    _add_addr_arg(di)

    po = sub.add_parser("poll", help="poll packs and publish to MQTT")
    _add_serial_args(po)
    _add_addr_arg(po)
    po.add_argument("--dialect", choices=["auto", "up", "pylontech"], default="auto")
    po.add_argument("--interval", type=float, default=5.0, help="seconds between poll cycles")
    po.add_argument("--mqtt", default=None, help="host[:port] (omit to print JSON to stdout)")
    po.add_argument("--user", default=None)
    po.add_argument("--pass", dest="password", default=None)
    po.add_argument("--base-topic", default="jbd_up")
    po.add_argument("--ha-prefix", default="homeassistant")
    po.add_argument("--no-discovery", action="store_true")
    po.add_argument("--no-high-res-soc", action="store_true", help="skip the 0x2000 read (older firmware)")
    po.add_argument("--once", action="store_true", help="one cycle then exit (for testing)")
    return ap


# ---------------------------------------------------------------------------
def _print_pack(pack, prefix: str = "") -> None:  # noqa: ANN001
    cells = " ".join(f"{v:.3f}" for v in pack.cell_voltages)
    print(
        f"{prefix}pack {pack.address} [{pack.dialect.name}] {pack.voltage} V {pack.current} A "
        f"SOC {pack.soc}% SOH {pack.soh}% cycles {pack.cycles} "
        f"cells min/max/delta {pack.cell_min}/{pack.cell_max}/{pack.cell_delta} "
        f"temps {pack.temperatures} mos {pack.mosfet_temperature} amb {pack.ambient_temperature} "
        f"fw {pack.firmware_version} sn {pack.pack_serial}"
    )
    if cells:
        print(f"{prefix}  cells: {cells}")
    if pack.alarms.faults or pack.alarms.alarms:
        print(f"{prefix}  FAULTS {pack.alarms.faults} ALARMS {pack.alarms.alarms}")


def cmd_dump(args: argparse.Namespace) -> int:
    out = args.out or Path(f"jbd-up-{time.strftime('%Y%m%d-%H%M%S')}.capture.txt")
    tr = SerialTransport(args.port, args.baud)
    meta = {"mode": "dump", "port": args.port, "baud": args.baud, "addresses": args.addresses, "version": __version__}
    cw = CaptureWriter(out, meta)
    stats = LinkStats()
    deadline = time.monotonic() + args.seconds
    print(f"capturing to {out} for {args.seconds:.0f}s ...")
    try:
        while time.monotonic() < deadline:
            for addr in args.addresses:
                for name, block, fc in DUMP_BLOCKS:
                    req = upf.encode_read(addr, block, fc)
                    fr = up_request(tr, req, stats, args.timeout, cw.write)
                    if fr is None:
                        print(f"addr {addr} {name}: no reply")
                        continue
                    dec = upf.decode_frame_payload(fr)
                    if args.no_decode:
                        print(f"addr {addr} {name}: {fr.length} bytes")
                    elif dec.pack is not None:
                        _print_pack(dec.pack, prefix=f"{name}: ")
                    else:
                        print(f"addr {addr} {name}: {json.dumps(dec.data, default=str)}")
                if args.pylontech:
                    for req in (pyl.encode_analog_request(addr), pyl.encode_alarm_request(addr)):
                        raw = pylon_request(tr, req, stats, args.timeout, cw.write)
                        print(
                            f"addr {addr} pylontech: "
                            f"{'reply ' + raw.decode(errors='replace').strip() if raw else 'no reply'}"
                        )
            if time.monotonic() < deadline:
                time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        cw.close()
        tr.close()
    print(f"done. stats: {stats.to_dict()}")
    print(
        f"capture written to {out} -- please attach it to a GitHub issue: https://github.com/rafe-walker/jbd-up/issues"
    )
    return 0


def cmd_sniff(args: argparse.Namespace) -> int:
    out = args.out or Path(f"jbd-up-sniff-{time.strftime('%Y%m%d-%H%M%S')}.capture.txt")
    tr = SerialTransport(args.port, args.baud, read_only=True)
    cw = CaptureWriter(out, {"mode": "sniff", "port": args.port, "baud": args.baud, "version": __version__})
    splitter = upf.FrameSplitter()
    psplit = pyl.PylonSplitter()
    n = 0
    print(f"sniffing {args.port} @ {args.baud} (read-only) to {out} ...")
    try:
        for t, chunk in sniff(tr, args.seconds):
            cw.write("RX", chunk, t)
            for fr in splitter.feed(chunk):
                n += 1
                dec = upf.decode_frame_payload(fr)
                if dec.pack is not None:
                    _print_pack(dec.pack, prefix=f"T+{t:.3f} {dec.kind}: ")
                else:
                    print(
                        f"T+{t:.3f} addr {fr.address} fc 0x{fr.function:02x} reg 0x{fr.start:04x} "
                        f"{dec.kind} len {fr.length}"
                    )
            for pf in psplit.feed(chunk):
                n += 1
                print(f"T+{t:.3f} pylontech adr {pf.address} cid2 0x{pf.cid2:02x} info {pf.info.hex(' ')}")
    except KeyboardInterrupt:
        pass
    finally:
        cw.close()
        tr.close()
    print(f"done. {n} frames, {splitter.crc_errors} UP crc errors, {psplit.errors} pylontech errors -> {out}")
    return 0


def decode_capture(path: Path) -> list[upf.Decoded | pyl.PylonDecoded]:
    """Decode every frame in a capture; RX bytes are concatenated per direction and re-split."""
    lines = read_capture(path)
    results: list[upf.Decoded | pyl.PylonDecoded] = []
    splitter = upf.FrameSplitter()
    psplit = pyl.PylonSplitter()
    pyl_ctx = pyl.PylonContext()
    for cl in lines:
        if cl.data.startswith(b"~"):
            for pf in psplit.feed(cl.data):
                results.append(pyl_ctx.decode(pf))
            continue
        for fr in splitter.feed(cl.data):
            results.append(upf.decode_frame_payload(fr))
    return results


def cmd_decode(args: argparse.Namespace) -> int:
    if args.fields:
        for title, table in (
            ("0x78/0x1000 pack status", upf.PACK_STATUS_FIELDS),
            ("0x45 slave data", upf.SLAVE_DATA_FIELDS),
            ("0x78/0x1C00 pack params 1", upf.PACK_PARAMS_1_FIELDS),
            ("0x78/0x2000 pack params 2", upf.PACK_PARAMS_2_FIELDS),
        ):
            print(f"== {title}")
            for name, off, size, unit, conf, src in table:
                print(f"  {name:28s} off {off:>4} size {size:>2}  {conf:9s} {unit}  [{src}]")
        return 0
    results = decode_capture(args.capture)
    if not results:
        print("no frames found", file=sys.stderr)
        return 1
    for r in results:
        if isinstance(r, pyl.PylonDecoded):
            if args.json:
                obj = {"dialect": "PYLONTECH", "kind": r.kind, "address": r.frame.address, "info": r.frame.info.hex()}
                if r.pack is not None:
                    obj["pack"] = r.pack.to_dict()
                if r.data:
                    obj["data"] = r.data
                print(json.dumps(obj, default=str))
            elif r.pack is not None:
                _print_pack(r.pack, prefix=f"{r.kind:12s} ")
            else:
                print(f"{r.kind:12s} pylontech adr {r.frame.address} info {r.frame.info.hex(' ')} {json.dumps(r.data)}")
            continue
        if args.json:
            obj = {
                "kind": r.kind,
                "address": r.frame.address,
                "function": r.frame.function,
                "start": r.frame.start,
                "length": r.frame.length,
            }
            if r.pack is not None:
                obj["pack"] = r.pack.to_dict()
            if r.data:
                obj["data"] = r.data
            print(json.dumps(obj, default=str))
            continue
        if r.kind == "request":
            print(
                f"request  addr {r.frame.address} fc 0x{r.frame.function:02x} "
                f"reg 0x{r.frame.start:04x}-0x{r.frame.end:04x}"
            )
        elif r.pack is not None:
            _print_pack(r.pack, prefix=f"{r.kind:12s} ")
        else:
            print(
                f"{r.kind:12s} addr {r.frame.address} fc 0x{r.frame.function:02x} reg 0x{r.frame.start:04x} "
                f"len {r.frame.length}: {json.dumps(r.data, default=str)}"
            )
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    tr = SerialTransport(args.port, args.baud)
    try:
        res = discover(tr, args.addresses, args.timeout)
    finally:
        tr.close()
    print(res.detail)
    if res.pack is not None:
        _print_pack(res.pack)
    if res.dialect is None:
        print(
            "Hints: check A/B polarity (swap them), confirm 9600 baud, confirm the pack DIP address "
            "(pack 1 = DIP1 ON), use RS485-1 not RS485-2, and make sure no inverter is polling the same port."
        )
        return 2
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    tr = SerialTransport(args.port, args.baud, retry_forever=not args.once)
    poller = Poller(
        transport=tr,
        addresses=args.addresses,
        dialect=args.dialect,
        timeout=args.timeout,
        high_res_soc=not args.no_high_res_soc,
    )
    publisher = None
    if args.mqtt:
        from .mqtt import MqttPublisher

        host, _, port = args.mqtt.partition(":")
        publisher = MqttPublisher(
            host,
            int(port or 1883),
            args.user,
            args.password,
            base=args.base_topic,
            ha_prefix=args.ha_prefix,
            discovery=not args.no_discovery,
        )

    def on_packs(packs, stats):  # noqa: ANN001
        if publisher is not None:
            publisher.publish_packs(packs, stats)
        else:
            for p in packs:
                print(json.dumps(p.to_dict(), default=str))
            print(json.dumps({"stats": stats.to_dict()}))
        if not packs:
            log.warning("no packs answered this cycle (stats %s)", stats.to_dict())

    try:
        if args.once:
            on_packs(poller.poll_once(), poller.stats)
            return 0
        run_forever(poller, args.interval, on_packs)
    except KeyboardInterrupt:
        pass
    finally:
        if publisher is not None:
            publisher.close()
        tr.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except BrokenPipeError:  # e.g. `jbd-up decode x | head`
        return 0
    except TransportError as exc:
        print(f"serial error: {exc}", file=sys.stderr)
        print("hint: ls /dev/serial/by-id/  (and check the user is in the dialout group)", file=sys.stderr)
        return 3


def _main(argv: list[str] | None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return {
        "dump": cmd_dump,
        "sniff": cmd_sniff,
        "decode": cmd_decode,
        "discover": cmd_discover,
        "poll": cmd_poll,
    }[args.cmd](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
