"""Poll loop: walk pack addresses 1..16, decode, merge identity, keep stats."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import frame as upf
from . import pylontech as pyl
from .model import Dialect, LinkStats, Pack
from .transport import Transport, TransportError, pylon_request, up_request

log = logging.getLogger(__name__)

ByteHook = Callable[[str, bytes], None] | None


@dataclass
class PackIdentity:
    """Slowly-changing identity fields fetched once (0x1C00, 0x2810)."""

    bms_serial: str | None = None
    pack_serial: str | None = None
    manufacturer_code: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    bms_date: str | None = None
    pack_date: str | None = None
    fetched: bool = False
    attempts: int = 0


@dataclass
class Poller:
    transport: Transport
    addresses: list[int] = field(default_factory=lambda: [1])
    dialect: str = "auto"  # auto | up | pylontech
    timeout: float = 1.5
    high_res_soc: bool = True  # also read 0x2000 for 0.01 % SOC (fw >= ~12)
    identity_retries: int = 5  # dbus-serialbattery: slaves never answer identity via master
    on_bytes: ByteHook = None
    stats: LinkStats = field(default_factory=LinkStats)
    identities: dict[int, PackIdentity] = field(default_factory=dict)
    resolved_dialect: Dialect | None = None
    _pp2_unavailable: dict[int, int] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def poll_once(self) -> list[Pack]:
        """Poll every address once.  Returns the packs that answered."""
        packs: list[Pack] = []
        for addr in self.addresses:
            try:
                pack = self._poll_address(addr)
            except TransportError as exc:
                self.stats.last_error = str(exc)
                self.stats.reconnects += 1
                log.warning("transport error on address %d: %s; reopening", addr, exc)
                self.transport.reopen()
                continue
            if pack is not None:
                packs.append(pack)
        return packs

    def _poll_address(self, addr: int) -> Pack | None:
        if self.dialect in ("auto", "up"):
            pack = self._poll_up(addr)
            if pack is not None:
                self.resolved_dialect = Dialect.UP_NATIVE
                return pack
            if self.dialect == "up":
                return None
        if self.dialect in ("auto", "pylontech"):
            pack = self._poll_pylontech(addr)
            if pack is not None:
                self.resolved_dialect = Dialect.PYLONTECH
                return pack
        return None

    # ------------------------------------------------------------------
    def _poll_up(self, addr: int) -> Pack | None:
        fr = up_request(
            self.transport, upf.encode_read(addr, upf.REG_PACK_STATUS), self.stats, self.timeout, self.on_bytes
        )
        if fr is None or fr.function != upf.FC_READ:
            return None
        try:
            pack = upf.decode_pack_status(fr.payload, addr)
        except upf.FrameError as exc:
            self.stats.last_error = f"addr {addr}: {exc}"
            return None

        ident = self.identities.setdefault(addr, PackIdentity())
        if not ident.fetched and ident.attempts < self.identity_retries:
            self._fetch_identity(addr, ident)
        if ident.bms_serial:
            pack.bms_serial = ident.bms_serial
        if ident.model:
            pack.model = ident.model
        if ident.manufacturer_code:
            pack.manufacturer_code = ident.manufacturer_code
        if ident.firmware_version and (pack.firmware_version is None or "." not in pack.firmware_version):
            pack.firmware_version = ident.firmware_version
        if pack.pack_serial is None and ident.pack_serial:
            pack.pack_serial = ident.pack_serial

        if self.high_res_soc and self._pp2_unavailable.get(addr, 0) < self.identity_retries:
            fr2 = up_request(
                self.transport, upf.encode_read(addr, upf.REG_PACK_PARAMS_2), self.stats, self.timeout, self.on_bytes
            )
            if fr2 is not None and fr2.start == upf.REG_PACK_PARAMS_2[0]:
                try:
                    pp2 = upf.decode_pack_params_2(fr2.payload)
                    pack.soc = pp2["soc_high_res"]
                    pack.raw_unknown["soc_source"] = 0x2000
                    self._pp2_unavailable[addr] = 0
                except upf.FrameError:
                    pass
            else:
                self._pp2_unavailable[addr] = self._pp2_unavailable.get(addr, 0) + 1
        return pack

    def _fetch_identity(self, addr: int, ident: PackIdentity) -> None:
        ident.attempts += 1
        fr = up_request(
            self.transport, upf.encode_read(addr, upf.REG_PACK_PARAMS_1), self.stats, self.timeout, self.on_bytes
        )
        if fr is None or fr.start != upf.REG_PACK_PARAMS_1[0]:
            if ident.attempts >= self.identity_retries:
                log.info("address %d: identity block not available (normal for slaves via master)", addr)
            return
        try:
            pp1 = upf.decode_pack_params_1(fr.payload)
        except upf.FrameError as exc:
            self.stats.last_error = f"addr {addr} 0x1C00: {exc}"
            return
        ident.bms_serial = pp1["bms_serial"]
        ident.pack_serial = pp1["pack_serial"]
        ident.manufacturer_code = pp1["manufacturer_code"]
        ident.bms_date = pp1["bms_date"]
        ident.pack_date = pp1["pack_date"]
        fr2 = up_request(
            self.transport, upf.encode_read(addr, upf.REG_PRODUCT_INFO), self.stats, self.timeout, self.on_bytes
        )
        if fr2 is not None and fr2.start == upf.REG_PRODUCT_INFO[0]:
            try:
                pi = upf.decode_product_info(fr2.payload)
                ident.model = pi["model"]
                ident.firmware_version = pi["firmware_version"]
            except upf.FrameError:
                pass
        ident.fetched = True

    # ------------------------------------------------------------------
    def _poll_pylontech(self, addr: int) -> Pack | None:
        raw = pylon_request(self.transport, pyl.encode_analog_request(addr), self.stats, self.timeout, self.on_bytes)
        if raw is None:
            return None
        try:
            pf = pyl.decode(raw)
            if pf.cid2 != pyl.RTN_OK:
                self.stats.last_error = f"pylontech rtn {pf.rtn}"
                return None
            pack = pyl.decode_analog(pf.info, addr)
        except pyl.PylontechError as exc:
            self.stats.crc_errors += 1
            self.stats.last_error = str(exc)
            return None
        raw2 = pylon_request(self.transport, pyl.encode_alarm_request(addr), self.stats, self.timeout, self.on_bytes)
        if raw2:
            try:
                pf2 = pyl.decode(raw2)
                if pf2.cid2 == pyl.RTN_OK:
                    pack.alarms = pyl.alarms_from_alarm_info(pyl.decode_alarm(pf2.info))
            except pyl.PylontechError:
                pass
        return pack


# ----------------------------------------------------------------------
@dataclass
class DiscoveryResult:
    dialect: Dialect | None
    address: int | None
    detail: str
    pack: Pack | None = None


def discover(
    transport: Transport, addresses: list[int], timeout: float = 1.5, on_bytes: ByteHook = None
) -> DiscoveryResult:
    """Try UP native 0x1000, then UP 0x45, then Pylontech analog, on each address."""
    stats = LinkStats()
    for addr in addresses:
        fr = up_request(transport, upf.encode_read(addr, upf.REG_PACK_STATUS), stats, timeout, on_bytes)
        if fr is not None and fr.function == upf.FC_READ:
            try:
                pack = upf.decode_pack_status(fr.payload, addr)
                return DiscoveryResult(
                    Dialect.UP_NATIVE, addr, f"UP native (FC 0x78 reg 0x1000) answered on address {addr}", pack
                )
            except upf.FrameError as exc:
                return DiscoveryResult(Dialect.UP_NATIVE, addr, f"UP frame on address {addr} but decode failed: {exc}")
    for addr in addresses:
        fr = up_request(
            transport, upf.encode_read(addr, upf.REG_SLAVE_DATA, upf.FC_READ_SLAVE_DATA), stats, timeout, on_bytes
        )
        if fr is not None and fr.function == upf.FC_READ_SLAVE_DATA:
            try:
                pack = upf.decode_slave_data(fr.payload, addr)
                return DiscoveryResult(
                    Dialect.UP_INTERPACK, addr, f"UP inter-pack (FC 0x45) answered on address {addr}", pack
                )
            except upf.FrameError as exc:
                return DiscoveryResult(
                    Dialect.UP_INTERPACK, addr, f"0x45 frame on address {addr} but decode failed: {exc}"
                )
    for addr in addresses:
        raw = pylon_request(transport, pyl.encode_analog_request(addr), stats, timeout, on_bytes)
        if raw:
            try:
                pf = pyl.decode(raw)
                pack = pyl.decode_analog(pf.info, addr) if pf.cid2 == pyl.RTN_OK else None
                return DiscoveryResult(
                    Dialect.PYLONTECH, addr, f"Pylontech ASCII answered on address {addr} (rtn {pf.rtn})", pack
                )
            except pyl.PylontechError as exc:
                return DiscoveryResult(Dialect.PYLONTECH, addr, f"Pylontech-looking reply on {addr} but invalid: {exc}")
    return DiscoveryResult(
        None, None, f"no reply on addresses {addresses} (timeouts={stats.timeouts}, crc_errors={stats.crc_errors})"
    )


def run_forever(poller: Poller, interval: float, on_packs: Callable[[list[Pack], LinkStats], None]) -> None:
    """Blocking loop used by the CLI/add-on.  Never exits on bus errors."""
    while True:
        t0 = time.monotonic()
        try:
            packs = poller.poll_once()
            on_packs(packs, poller.stats)
        except Exception as exc:  # noqa: BLE001 - last line of defence, keep the daemon alive
            poller.stats.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("poll cycle failed")
        dt = time.monotonic() - t0
        time.sleep(max(0.2, interval - dt))
