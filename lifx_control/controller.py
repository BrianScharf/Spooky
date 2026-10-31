"""Local LIFX controller.

Talks to bulbs directly over your Wi-Fi using the LIFX LAN protocol (UDP port
56700). There is no cloud, no account and no "reconnecting" spinner: every
command is a single UDP packet sent straight to the bulb, retried
automatically until the bulb acknowledges it.

Reliability features:
  * Bulbs are remembered (MAC -> IP) in a cache file, so they are reachable
    immediately on start-up even if broadcast discovery is flaky.
  * Discovery runs continuously in the background (broadcast + direct
    unicast to every known IP), so a bulb that drops off Wi-Fi is picked up
    again automatically within seconds of it coming back, even if its IP
    address changed.
  * Every command asks the bulb for an acknowledgement and is retried with
    back-off if none arrives.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import protocol as P

log = logging.getLogger("lifx")

POLL_INTERVAL = 5.0  # seconds between state refreshes
DISCOVERY_INTERVAL = 30.0  # seconds between discoveries when all is well
DISCOVERY_INTERVAL_DEGRADED = 8.0  # ...when at least one bulb is offline
OFFLINE_AFTER = 16.0  # seconds without a reply before a bulb shows offline
GROUP_REFRESH = 300.0  # how often to re-read a bulb's room/group


@dataclass
class Device:
    mac: str
    ip: str
    port: int = P.LIFX_PORT
    label: str = ""
    group: str = ""
    group_id: str = ""
    power: bool = False
    color: P.HSBK = field(default_factory=lambda: P.HSBK(0, 0, 65535, 3500))
    last_seen: float = 0.0
    group_checked: float = 0.0
    has_state: bool = False
    is_matrix: bool | None = None  # None = not checked yet

    @property
    def online(self) -> bool:
        return (time.monotonic() - self.last_seen) < OFFLINE_AFTER

    def to_dict(self) -> dict:
        return {
            "id": self.mac,
            "ip": self.ip,
            "label": self.label or self.mac,
            "group": self.group or "Ungrouped",
            "online": self.online,
            "power": self.power,
            "color": self.color.to_friendly(),
            "known_state": self.has_state,
            "matrix": bool(self.is_matrix),
        }


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, controller: "LifxController"):
        self.controller = controller

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            self.controller._on_packet(data, addr)
        except Exception:  # never let a bad packet kill the socket
            log.exception("error handling packet from %s", addr)

    def error_received(self, exc: Exception) -> None:
        log.debug("socket error: %s", exc)


class LifxController:
    def __init__(
        self,
        cache_path: str | os.PathLike | None = None,
        broadcast_addrs: Iterable[str] = ("255.255.255.255",),
        static_hosts: Iterable[str] = (),
    ):
        self.cache_path = Path(cache_path) if cache_path else None
        self.broadcast_addrs = list(broadcast_addrs) or ["255.255.255.255"]
        self.static_hosts = list(static_hosts)
        self.devices: dict[str, Device] = {}
        self.source = random.randint(2, 0xFFFFFFFF)
        self._seq = 0
        self._pending: dict[int, tuple[asyncio.Future, set[int], str | None]] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._tasks: list[asyncio.Task] = []
        self._cache_dirty = False
        self._discover_now = asyncio.Event()
        from .animations import Animator  # local import avoids a cycle

        self.animator = Animator(self)

    # ------------------------------------------------------------------ setup

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", 0))
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self), sock=sock
        )
        self._load_cache()
        await self.discover()
        await self.refresh_all()
        self._tasks = [
            asyncio.create_task(self._poll_loop()),
            asyncio.create_task(self._discovery_loop()),
        ]

    async def stop(self) -> None:
        try:
            await self.animator.stop(None, restore=True)
        except Exception:
            log.exception("could not stop animations cleanly")
        for t in self._tasks:
            t.cancel()
        self._save_cache()
        if self._transport:
            self._transport.close()

    # ------------------------------------------------------------- persistence

    def _load_cache(self) -> None:
        if not self.cache_path or not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            log.warning("could not read cache %s; starting fresh", self.cache_path)
            return
        for mac, d in data.get("devices", {}).items():
            self.devices[mac] = Device(
                mac=mac,
                ip=d["ip"],
                port=d.get("port", P.LIFX_PORT),
                label=d.get("label", ""),
                group=d.get("group", ""),
                group_id=d.get("group_id", ""),
                is_matrix=d.get("matrix"),
            )
        log.info("loaded %d bulbs from cache", len(self.devices))

    def _save_cache(self) -> None:
        if not self.cache_path or not self._cache_dirty:
            return
        data = {
            "devices": {
                d.mac: {
                    "ip": d.ip,
                    "port": d.port,
                    "label": d.label,
                    "group": d.group,
                    "group_id": d.group_id,
                    "matrix": d.is_matrix,
                }
                for d in self.devices.values()
            }
        }
        tmp = self.cache_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.cache_path)
            self._cache_dirty = False
        except OSError:
            log.exception("could not write cache %s", self.cache_path)

    # ---------------------------------------------------------------- network

    def _next_seq(self) -> int:
        for _ in range(256):
            self._seq = (self._seq + 1) & 0xFF
            if self._seq not in self._pending:
                return self._seq
        raise RuntimeError("too many requests in flight")

    def _send(self, packet: bytes, addr: tuple[str, int]) -> None:
        if self._transport is not None:
            self._transport.sendto(packet, addr)

    def _on_packet(self, data: bytes, addr) -> None:
        try:
            hdr = P.decode_header(data)
        except ValueError:
            return
        if hdr.source != self.source:
            return  # a reply to someone else (e.g. the LIFX app)
        payload = data[P.HEADER_SIZE : hdr.size]
        mac = P.bytes_to_mac(hdr.target)
        ip = addr[0]

        dev = self.devices.get(mac)
        if hdr.type == P.STATE_SERVICE:
            service, port = P.parse_state_service(payload)
            if service != 1:  # 1 == UDP
                return
            if dev is None:
                dev = Device(mac=mac, ip=ip, port=port)
                self.devices[mac] = dev
                self._cache_dirty = True
                log.info("found new bulb %s at %s", mac, ip)
            elif dev.ip != ip or dev.port != port:
                log.info("bulb %s moved %s -> %s", dev.label or mac, dev.ip, ip)
                dev.ip, dev.port = ip, port
                self._cache_dirty = True
        if dev is None:
            return
        dev.last_seen = time.monotonic()
        if dev.ip != ip:
            dev.ip = ip
            self._cache_dirty = True

        if hdr.type == P.LIGHT_STATE:
            color, power, label = P.parse_light_state(payload)
            dev.color, dev.power, dev.has_state = color, power, True
            if label and label != dev.label:
                dev.label = label
                self._cache_dirty = True
        elif hdr.type in (P.STATE_POWER, P.LIGHT_STATE_POWER):
            dev.power = P.parse_state_power(payload)
        elif hdr.type == P.STATE_LABEL:
            label = P.parse_state_label(payload)
            if label != dev.label:
                dev.label = label
                self._cache_dirty = True
        elif hdr.type == P.STATE_GROUP:
            gid, glabel = P.parse_state_group(payload)
            dev.group_checked = time.monotonic()
            if (gid, glabel) != (dev.group_id, dev.group):
                dev.group_id, dev.group = gid, glabel
                self._cache_dirty = True

        pending = self._pending.get(hdr.sequence)
        if pending:
            fut, expected, want_mac = pending
            if hdr.type in expected and (want_mac is None or want_mac == mac):
                if not fut.done():
                    fut.set_result((hdr.type, payload))

    async def request(
        self,
        dev: Device,
        msg_type: int,
        payload: bytes = b"",
        *,
        expect: int | tuple[int, ...],
        retries: int = 4,
        timeout: float = 0.35,
    ) -> bytes | None:
        """Send to one bulb and wait for ``expect``, retrying with back-off.

        Returns the response payload, or ``None`` if the bulb never answered.
        """
        res = await self.request_raw(
            dev, msg_type, payload, expect=expect, retries=retries, timeout=timeout
        )
        return None if res is None else res[1]

    async def request_raw(
        self,
        dev: Device,
        msg_type: int,
        payload: bytes = b"",
        *,
        expect: int | tuple[int, ...],
        retries: int = 4,
        timeout: float = 0.35,
    ) -> tuple[int, bytes] | None:
        """Like :meth:`request` but returns ``(reply_type, payload)``."""
        expected = {expect} if isinstance(expect, int) else set(expect)
        loop = asyncio.get_running_loop()
        seq = self._next_seq()
        fut: asyncio.Future = loop.create_future()
        self._pending[seq] = (fut, expected, dev.mac)
        packet = P.encode(
            msg_type,
            payload,
            source=self.source,
            sequence=seq,
            target=dev.mac,
            ack_required=expected == {P.ACKNOWLEDGEMENT},
            res_required=expected != {P.ACKNOWLEDGEMENT},
        )
        try:
            wait = timeout
            for _attempt in range(retries + 1):
                self._send(packet, (dev.ip, dev.port))
                try:
                    return await asyncio.wait_for(asyncio.shield(fut), wait)
                except asyncio.TimeoutError:
                    wait = min(wait * 1.6, 2.0)
            return None
        finally:
            self._pending.pop(seq, None)
            if not fut.done():
                fut.cancel()

    def send_nowait(self, dev: Device, msg_type: int, payload: bytes = b"") -> None:
        """Fire-and-forget (used for animation frames, where speed beats certainty)."""
        packet = P.encode(
            msg_type, payload, source=self.source, sequence=0, target=dev.mac
        )
        try:
            self._send(packet, (dev.ip, dev.port))
        except OSError as e:
            log.debug("send to %s failed: %s", dev.ip, e)

    # -------------------------------------------------------------- discovery

    async def discover(self, wait: float = 1.0) -> None:
        """Find bulbs: broadcast, plus unicast to every IP we already know."""
        pkt = P.encode(P.GET_SERVICE, source=self.source, res_required=True)
        targets = set(self.broadcast_addrs)
        targets.update(d.ip for d in self.devices.values())
        targets.update(self.static_hosts)
        for _ in range(3):  # UDP can drop packets; say it three times
            for host in targets:
                try:
                    self._send(pkt, (host, P.LIFX_PORT))
                except OSError as e:
                    log.debug("discovery send to %s failed: %s", host, e)
            await asyncio.sleep(wait / 3)
        self._save_cache()

    def request_discovery(self) -> None:
        self._discover_now.set()

    async def _discovery_loop(self) -> None:
        while True:
            degraded = not self.devices or any(
                not d.online for d in self.devices.values()
            )
            interval = DISCOVERY_INTERVAL_DEGRADED if degraded else DISCOVERY_INTERVAL
            try:
                await asyncio.wait_for(self._discover_now.wait(), interval)
            except asyncio.TimeoutError:
                pass
            self._discover_now.clear()
            try:
                await self.discover()
            except Exception:
                log.exception("discovery failed")

    # ---------------------------------------------------------------- polling

    async def refresh(self, dev: Device) -> None:
        await self.request(dev, P.LIGHT_GET, expect=P.LIGHT_STATE, retries=2)
        if dev.online and time.monotonic() - dev.group_checked > GROUP_REFRESH:
            await self.request(dev, P.GET_GROUP, expect=P.STATE_GROUP, retries=2)
        if dev.online and dev.is_matrix is None:
            # Matrix lights (Ceiling, Tile, Candle) can run built-in animations.
            res = await self.request_raw(
                dev,
                P.GET_DEVICE_CHAIN,
                expect=(P.STATE_DEVICE_CHAIN, P.STATE_UNHANDLED),
                retries=2,
            )
            if res is not None:
                dev.is_matrix = res[0] == P.STATE_DEVICE_CHAIN
                self._cache_dirty = True

    async def refresh_all(self) -> None:
        await asyncio.gather(*(self.refresh(d) for d in list(self.devices.values())))
        self._save_cache()

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                await self.refresh_all()
            except Exception:
                log.exception("poll failed")

    # --------------------------------------------------------------- commands

    def _resolve(self, ids: Iterable[str] | None) -> list[Device]:
        if ids is None:
            return list(self.devices.values())
        return [self.devices[i] for i in ids if i in self.devices]

    async def set_power(
        self,
        ids: Iterable[str] | None,
        on: bool,
        duration_ms: int = 300,
        *,
        stop_animation: bool = True,
    ) -> dict[str, bool]:
        devs = self._resolve(ids)
        if stop_animation:  # touching a light by hand ends its animation
            await self.animator.stop([d.mac for d in devs], restore=False)
        payload = P.payload_set_power(on, duration_ms)

        async def one(d: Device) -> bool:
            ok = await self.request(d, P.LIGHT_SET_POWER, payload, expect=P.ACKNOWLEDGEMENT)
            if ok is not None:
                d.power = on
                return True
            self.request_discovery()  # maybe it changed IP; go look
            return False

        results = await asyncio.gather(*(one(d) for d in devs))
        return {d.mac: r for d, r in zip(devs, results)}

    async def set_color(
        self,
        ids: Iterable[str] | None,
        *,
        hue: float | None = None,
        saturation: float | None = None,
        brightness: float | None = None,
        kelvin: int | None = None,
        duration_ms: int = 300,
        power_on: bool = False,
        stop_animation: bool = True,
    ) -> dict[str, bool]:
        """Change colour. Omitted fields keep each bulb's current value."""
        devs = self._resolve(ids)
        if stop_animation:
            await self.animator.stop([d.mac for d in devs], restore=False)

        async def one(d: Device) -> bool:
            cur = d.color.to_friendly()
            new = P.HSBK.from_friendly(
                hue if hue is not None else cur["hue"],
                saturation if saturation is not None else cur["saturation"],
                brightness if brightness is not None else cur["brightness"],
                kelvin if kelvin is not None else cur["kelvin"],
            )
            # When only brightness changes keep the bulb's exact raw values.
            if hue is None:
                new.hue = d.color.hue
            if saturation is None:
                new.saturation = d.color.saturation
            if brightness is None:
                new.brightness = d.color.brightness
            ok = await self.request(
                d,
                P.LIGHT_SET_COLOR,
                P.payload_set_color(new, duration_ms),
                expect=P.ACKNOWLEDGEMENT,
            )
            if ok is None:
                self.request_discovery()
                return False
            d.color = new
            if power_on and not d.power:
                return await self._power_one(d, True, duration_ms)
            return True

        results = await asyncio.gather(*(one(d) for d in devs))
        return {d.mac: r for d, r in zip(devs, results)}

    async def _power_one(self, d: Device, on: bool, duration_ms: int) -> bool:
        res = await self.set_power([d.mac], on, duration_ms, stop_animation=False)
        return res.get(d.mac, False)

    def snapshot(self) -> list[dict]:
        devs = sorted(
            self.devices.values(), key=lambda d: ((d.group or "~").lower(), (d.label or d.mac).lower())
        )
        out = []
        for d in devs:
            item = d.to_dict()
            item["animation"] = self.animator.current(d.mac)
            out.append(item)
        return out
