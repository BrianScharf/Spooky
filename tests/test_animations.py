import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lifx_control import protocol as P
from lifx_control.animations import ANIMATIONS, Show
from lifx_control.controller import LifxController
from tests.fake_bulb import start_fake

MACS = ["d0:73:d5:00:01:01", "d0:73:d5:00:01:02", "d0:73:d5:00:01:03"]
IPS = ["127.0.1.2", "127.0.1.3", "127.0.1.4"]


class FrameTests(unittest.TestCase):
    def test_every_animation_makes_valid_frames(self):
        for anim in ANIMATIONS.values():
            show = Show(anim, MACS, speed=1.0, brightness=1.0)
            clock = [0.0]
            with mock.patch("lifx_control.animations.time.monotonic", lambda: clock[0]):
                show.started = 0.0
                for step in range(400):  # 20 simulated seconds
                    clock[0] = step * 0.05
                    if anim.tick:
                        anim.tick(show)
                    for i, m in enumerate(MACS):
                        frame = anim.frame(show, i, show.per_light.setdefault(m, {}))
                        if frame is None:
                            continue
                        c, fade = frame
                        self.assertIsInstance(c, P.HSBK, anim.name)
                        for v in (c.hue, c.saturation, c.brightness):
                            self.assertTrue(0 <= v <= 65535, anim.name)
                        self.assertTrue(1500 <= c.kelvin <= 9000, anim.name)
                        self.assertGreaterEqual(fade, 0)

    def test_lightning_actually_strikes(self):
        anim = ANIMATIONS["lightning"]
        show = Show(anim, MACS, 1.0, 1.0)
        clock = [0.0]
        flashes = 0
        with mock.patch("lifx_control.animations.time.monotonic", lambda: clock[0]):
            show.started = 0.0
            for step in range(600):
                clock[0] = step * 0.05
                anim.tick(show)
                c, _ = anim.frame(show, 0, {})
                flashes += c.brightness == 65535
        self.assertGreater(flashes, 0)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bulbs = [
            await start_fake(IPS[0], MACS[0], "Porch", "Outside"),
            await start_fake(IPS[1], MACS[1], "Lamp", "Den"),
            await start_fake(IPS[2], MACS[2], "Ceiling", "Den", matrix=True),
        ]
        self.ctl = LifxController(Path(self.tmp.name) / "c.json", broadcast_addrs=IPS)
        await self.ctl.start()

    async def asyncTearDown(self):
        await self.ctl.stop()
        for b in self.bulbs:
            b.transport.close()
        self.tmp.cleanup()

    async def test_detects_matrix_lights(self):
        self.assertTrue(self.ctl.devices[MACS[2]].is_matrix)
        self.assertFalse(self.ctl.devices[MACS[0]].is_matrix)

    async def test_streams_then_restores(self):
        porch, lamp, ceiling = self.bulbs
        await self.ctl.set_color([MACS[1]], hue=0, saturation=0, brightness=40, kelvin=2700)
        await self.ctl.set_power([MACS[1]], True)
        before = lamp.color

        res = await self.ctl.animator.start("candle")
        self.assertTrue(all(res.values()))
        await asyncio.sleep(0.8)
        # plain bulbs get a stream of colours and are switched on
        self.assertGreater(porch.color_changes, 3)
        self.assertTrue(porch.power)
        # the ceiling runs its own built-in Flame effect instead
        self.assertEqual(ceiling.tile_effect, P.TILE_EFFECT_FLAME)
        self.assertEqual({d["animation"] for d in self.ctl.snapshot()}, {"candle"})

        await self.ctl.animator.stop()
        self.assertEqual(ceiling.tile_effect, P.TILE_EFFECT_OFF)
        self.assertEqual(lamp.color, before)  # back how it was
        self.assertTrue(lamp.power)
        self.assertFalse(porch.power)  # was off before, so off again
        n = porch.color_changes
        await asyncio.sleep(0.3)
        self.assertEqual(porch.color_changes, n)  # stream really stopped

    async def test_room_only_and_manual_override(self):
        porch, lamp, ceiling = self.bulbs
        await self.ctl.animator.start("rainbow", [MACS[1], MACS[2]], speed=2)
        self.assertIsNone(self.ctl.animator.current(MACS[0]))
        self.assertEqual(ceiling.tile_effect, P.TILE_EFFECT_MORPH)
        # touching the ceiling by hand stops its animation but not the lamp's
        await self.ctl.set_power([MACS[2]], False)
        self.assertIsNone(self.ctl.animator.current(MACS[2]))
        self.assertEqual(ceiling.tile_effect, P.TILE_EFFECT_OFF)
        self.assertEqual(self.ctl.animator.current(MACS[1]), "rainbow")

    async def test_switching_animation_keeps_original_state(self):
        porch = self.bulbs[0]
        await self.ctl.animator.start("party", [MACS[0]])
        await self.ctl.animator.start("haunted", [MACS[0]])
        self.assertEqual(self.ctl.animator.current(MACS[0]), "haunted")
        await self.ctl.animator.stop([MACS[0]])
        self.assertFalse(porch.power)  # restored to the state before *party*

    async def test_unknown_animation(self):
        with self.assertRaises(ValueError):
            await self.ctl.animator.start("disco-inferno")


if __name__ == "__main__":
    unittest.main()
