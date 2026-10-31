import asyncio
import json
import struct
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from lifx_control import protocol as P
from lifx_control.controller import LifxController
from lifx_control.server import LoopThread, make_handler
from tests.fake_bulb import start_fake

MAC1, MAC2 = "d0:73:d5:00:00:01", "d0:73:d5:00:00:02"


class ProtocolTests(unittest.TestCase):
    def test_header_roundtrip(self):
        pkt = P.encode(P.LIGHT_SET_POWER, P.payload_set_power(True, 500), source=1234,
                       sequence=7, target=MAC1, ack_required=True)
        self.assertEqual(len(pkt), 36 + 6)
        h = P.decode_header(pkt)
        self.assertEqual((h.size, h.source, h.sequence, h.type), (42, 1234, 7, P.LIGHT_SET_POWER))
        self.assertEqual(P.bytes_to_mac(h.target), MAC1)
        self.assertTrue(h.ack_required)
        self.assertFalse(h.res_required)
        self.assertFalse(h.tagged)

    def test_broadcast_is_tagged(self):
        pkt = P.encode(P.GET_SERVICE, source=5)
        # Known-good GetService header bytes from the LIFX docs layout.
        self.assertEqual(pkt[:4], bytes([0x24, 0x00, 0x00, 0x34]))
        self.assertTrue(P.decode_header(pkt).tagged)

    def test_color_conversion(self):
        c = P.HSBK.from_friendly(180, 50, 100, 3500)
        self.assertEqual(c.to_friendly(), {"hue": 180, "saturation": 50, "brightness": 100, "kelvin": 3500})
        self.assertEqual(P.HSBK.from_friendly(0, 0, 0, 99999).kelvin, 9000)

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            P.decode_header(b"\x00" * 10)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache.json"
        self.b1 = await start_fake("127.0.0.2", MAC1, "Porch", "Outside")
        self.b2 = await start_fake("127.0.0.3", MAC2, "Lamp", "Living Room")
        self.ctl = LifxController(self.cache, broadcast_addrs=["127.0.0.2", "127.0.0.3"])
        await self.ctl.start()

    async def asyncTearDown(self):
        await self.ctl.stop()
        self.b1.transport.close()
        self.b2.transport.close()
        self.tmp.cleanup()

    async def test_discovers_and_reads_state(self):
        snap = {d["id"]: d for d in self.ctl.snapshot()}
        self.assertEqual(set(snap), {MAC1, MAC2})
        self.assertEqual(snap[MAC1]["label"], "Porch")
        self.assertEqual(snap[MAC2]["group"], "Living Room")
        self.assertTrue(all(d["online"] for d in snap.values()))

    async def test_power_and_color(self):
        res = await self.ctl.set_power(None, True)
        self.assertEqual(res, {MAC1: True, MAC2: True})
        self.assertTrue(self.b1.power and self.b2.power)
        await self.ctl.set_color([MAC1], brightness=25)
        self.assertEqual(self.b1.color.brightness, round(25 * 65535 / 100))
        self.assertEqual(self.b1.color.kelvin, 3500)  # untouched
        self.assertEqual(self.b2.color.brightness, 32768)  # other bulb untouched

    async def test_retries_through_packet_loss(self):
        self.b1.drop_first = 3
        res = await self.ctl.set_power([MAC1], True)
        self.assertTrue(res[MAC1])
        self.assertTrue(self.b1.power)

    async def test_unresponsive_bulb_reports_failure(self):
        self.b2.offline = True
        res = await self.ctl.set_power(None, True)
        self.assertEqual(res, {MAC1: True, MAC2: False})

    async def test_bulb_reconnects_at_new_ip(self):
        # Bulb drops off Wi-Fi, then comes back with a different DHCP address.
        self.b1.transport.close()
        self.ctl.broadcast_addrs.append("127.0.0.6")
        self.b1 = await start_fake("127.0.0.6", MAC1, "Porch", "Outside")
        await self.ctl.discover(wait=0.3)
        self.assertEqual(self.ctl.devices[MAC1].ip, "127.0.0.6")
        res = await self.ctl.set_power([MAC1], True)
        self.assertTrue(res[MAC1] and self.b1.power)

    async def test_cache_remembers_bulbs(self):
        await self.ctl.stop()
        data = json.loads(self.cache.read_text())
        self.assertEqual(data["devices"][MAC1]["ip"], "127.0.0.2")
        # A new controller with *no* broadcast reach still finds them via cache.
        ctl2 = LifxController(self.cache, broadcast_addrs=["127.0.0.99"])
        await ctl2.start()
        try:
            self.assertTrue(all(d["online"] for d in ctl2.snapshot()))
            self.assertEqual(len(ctl2.snapshot()), 2)
        finally:
            await ctl2.stop()


class ServerTests(unittest.TestCase):
    """Drive the HTTP API end to end against fake bulbs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctl = LifxController(Path(self.tmp.name) / "c.json", broadcast_addrs=["127.0.0.4", "127.0.0.5"])
        self.runner = LoopThread(self.ctl)
        # start fake bulbs on the controller's loop before it discovers
        self.bulbs = []

        async def boot():
            self.bulbs.append(await start_fake("127.0.0.4", MAC1, "Porch", "Outside"))
            self.bulbs.append(await start_fake("127.0.0.5", MAC2, "Lamp", "Living Room"))

        self.runner.loop.run_until_complete(boot())
        self.runner.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.runner))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.runner.stop()
        self.tmp.cleanup()

    def _req(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_api(self):
        lights = self._req("/api/lights")["lights"]
        self.assertEqual(len(lights), 2)
        self._req("/api/power", {"group": "living room", "on": True})
        self.assertTrue(self.bulbs[1].power)
        self.assertFalse(self.bulbs[0].power)
        self._req("/api/scene", {"name": "spooky"})
        self.assertTrue(self.bulbs[0].power and self.bulbs[1].power)
        self.assertNotEqual(self.bulbs[0].color.hue, self.bulbs[1].color.hue)
        data = self._req("/api/lights")
        self.assertIn("candle", [a["name"] for a in data["animations"]])
        self._req("/api/animate", {"name": "candle", "group": "Outside", "speed": 1.5})
        lights = {d["label"]: d for d in self._req("/api/lights")["lights"]}
        self.assertEqual(lights["Porch"]["animation"], "candle")
        self.assertIsNone(lights["Lamp"]["animation"])
        self._req("/api/animate/stop", {})
        self.assertIsNone(self._req("/api/lights")["lights"][0]["animation"])
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"<title>Lights</title>", r.read())


if __name__ == "__main__":
    unittest.main()
