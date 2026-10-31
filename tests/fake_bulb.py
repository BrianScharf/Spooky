"""A pretend LIFX bulb for tests and demos (speaks enough of the LAN protocol).

Bind several to 127.0.0.x:56700 and point the controller at them.
"""

from __future__ import annotations

import asyncio
import struct

from lifx_control import protocol as P


class FakeBulb(asyncio.DatagramProtocol):
    def __init__(self, mac: str, label: str, group: str, drop_first: int = 0):
        self.mac, self.label, self.group = mac, label, group
        self.power = False
        self.color = P.HSBK(0, 0, 32768, 3500)
        self.drop_first = drop_first  # simulate flaky Wi-Fi: ignore N packets
        self.offline = False
        self.received: list[int] = []
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def _reply(self, hdr, msg_type, payload, addr):
        pkt = P.encode(msg_type, payload, source=hdr.source, sequence=hdr.sequence, target=self.mac)
        self.transport.sendto(pkt, addr)

    def datagram_received(self, data, addr):
        hdr = P.decode_header(data)
        if not hdr.tagged and P.bytes_to_mac(hdr.target) != self.mac:
            return
        if self.offline:
            return
        if self.drop_first > 0:
            self.drop_first -= 1
            return
        self.received.append(hdr.type)
        payload = data[P.HEADER_SIZE:]
        t = hdr.type
        if t == P.GET_SERVICE:
            self._reply(hdr, P.STATE_SERVICE, struct.pack("<BI", 1, P.LIFX_PORT), addr)
            return
        if t == P.LIGHT_SET_POWER:
            (level, _dur) = struct.unpack_from("<HI", payload)
            self.power = level > 0
        elif t == P.LIGHT_SET_COLOR:
            _, h, s, b, k, _dur = struct.unpack_from("<BHHHHI", payload)
            self.color = P.HSBK(h, s, b, k)
        if hdr.ack_required:
            self._reply(hdr, P.ACKNOWLEDGEMENT, b"", addr)
        if t == P.LIGHT_GET:
            c = self.color
            body = struct.pack("<HHHHhH", c.hue, c.saturation, c.brightness, c.kelvin, 0,
                               65535 if self.power else 0)
            body += self.label.encode().ljust(32, b"\0") + b"\0" * 8
            self._reply(hdr, P.LIGHT_STATE, body, addr)
        elif t == P.GET_GROUP:
            body = bytes(16) + self.group.encode().ljust(32, b"\0") + b"\0" * 8
            self._reply(hdr, P.STATE_GROUP, body, addr)


async def start_fake(ip: str, mac: str, label: str, group: str, **kw) -> FakeBulb:
    loop = asyncio.get_running_loop()
    _, bulb = await loop.create_datagram_endpoint(
        lambda: FakeBulb(mac, label, group, **kw), local_addr=(ip, P.LIFX_PORT)
    )
    return bulb


if __name__ == "__main__":
    # Demo: python -m tests.fake_bulb, then run the app with
    #   python -m lifx_control --broadcast 127.0.0.2 --broadcast 127.0.0.3 --broadcast 127.0.0.4
    async def main():
        await start_fake("127.0.0.2", "d0:73:d5:00:00:01", "Porch", "Outside")
        await start_fake("127.0.0.3", "d0:73:d5:00:00:02", "Lamp", "Living Room")
        await start_fake("127.0.0.4", "d0:73:d5:00:00:03", "Ceiling", "Living Room")
        print("3 fake bulbs running on 127.0.0.2-4. Ctrl+C to stop.")
        await asyncio.Event().wait()

    asyncio.run(main())
