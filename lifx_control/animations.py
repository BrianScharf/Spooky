"""Light animations, synchronised across every bulb in the house.

Two ways to animate a light:

* **Streamed:** the server works out each bulb's next colour several times a
  second and sends it straight to the bulb. This works on every colour LIFX
  light, and all the bulbs stay in step because they share one clock.
* **Built-in (matrix lights only):** Ceiling, Tile and Candle lights have many
  zones and their own firmware effects (Flame, Morph). Where an animation has
  a matching effect, we ask the light to run it so every zone moves, instead
  of the whole fixture changing as one colour.

Stopping an animation puts each light back the way it was. Changing a light
by hand (power, colour or scene) stops the animation on just that light.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable

from . import protocol as P

if TYPE_CHECKING:
    from .controller import Device, LifxController

log = logging.getLogger("lifx.animate")

TICK = 0.05  # engine resolution (seconds)
RESEND_AFTER = 2.0  # re-send an unchanged frame this often in case UDP lost it
FADE_IN_MS = 600  # how quickly lights glide into an animation


def hsbk(hue: float, sat: float, bri: float, kelvin: int = 3500) -> P.HSBK:
    return P.HSBK.from_friendly(hue, max(0, min(100, sat)), max(0, min(100, bri)), kelvin)


@dataclass
class Show:
    """One running animation over a set of lights."""

    anim: "Animation"
    macs: list[str]
    speed: float
    brightness: float  # 0..1 multiplier applied to every frame
    started: float = field(default_factory=time.monotonic)
    shared: dict = field(default_factory=dict)  # state shared by all lights
    per_light: dict = field(default_factory=dict)  # mac -> state dict
    rng: random.Random = field(default_factory=random.Random)

    def t(self) -> float:
        """Animation time in seconds, scaled by speed."""
        return (time.monotonic() - self.started) * self.speed


Frame = tuple[P.HSBK, int]  # colour, fade time in ms


@dataclass
class Animation:
    name: str
    label: str
    description: str
    interval: float  # seconds between frames at speed 1
    frame: Callable[[Show, int, dict], Frame | None]
    tick: Callable[[Show], None] | None = None
    # Built-in effect for matrix lights: (effect type, speed_ms, palette)
    native: tuple[int, int, list[P.HSBK]] | None = None


# --------------------------------------------------------------- animations


def _candle(show: Show, i: int, st: dict) -> Frame:
    rng = show.rng
    b = st.get("b", 55.0)
    target = rng.gauss(55, 12)
    if rng.random() < 0.06:  # a draught: brief deep dip
        target = rng.uniform(15, 30)
    b = max(12, min(80, b * 0.55 + target * 0.45))
    st["b"] = b
    return hsbk(rng.uniform(24, 32), rng.uniform(85, 100), b), int(110 / show.speed)


def _lightning_tick(show: Show) -> None:
    sh, t, rng = show.shared, show.t(), show.rng
    if "next" not in sh:
        sh["next"] = t + rng.uniform(1, 3)
        sh["flashes"], sh["hit"] = [], set()
    if t >= sh["next"]:
        n = len(show.macs)
        sh["hit"] = set(rng.sample(range(n), k=rng.randint(max(1, n // 3), n)))
        start, flashes = t, []
        for _ in range(rng.choice((1, 2, 2, 3))):
            length = rng.uniform(0.06, 0.14)
            flashes.append((start, start + length))
            start += length + rng.uniform(0.07, 0.2)
        sh["flashes"] = flashes
        sh["next"] = start + rng.uniform(2.5, 9)


def _lightning(show: Show, i: int, st: dict) -> Frame:
    t = show.t()
    if i in show.shared.get("hit", ()) and any(a <= t < b for a, b in show.shared["flashes"]):
        return hsbk(0, 0, 100, 6500), 0
    return hsbk(232, 75, 5), 250


def _haunted(show: Show, i: int, st: dict) -> Frame:
    rng, t = show.rng, show.t()
    base = hsbk(150, 25, 22, 5000)  # pale ghostly green-white
    if t >= st.get("until", 0):
        st["mode"] = rng.choices(["calm", "fade", "flicker", "possessed"], [5, 2, 2, 1])[0]
        st["since"] = t
        st["hue"] = rng.choice((275, 110, 0))  # purple, slime green or blood red
        st["until"] = t + {"calm": rng.uniform(3, 8), "fade": 4.0, "flicker": 1.5,
                           "possessed": 3.0}[st["mode"]]
    mode, age = st["mode"], t - st["since"]
    if mode == "fade":  # slowly dies, then creeps back
        return (hsbk(150, 25, 0) if age < 2 else base), 1800
    if mode == "flicker":
        return (hsbk(150, 25, rng.choice((0, 4, 30)))), 0
    if mode == "possessed":
        return hsbk(st["hue"], 100, 60), 300
    return base, 1200


def _rainbow(show: Show, i: int, st: dict) -> Frame:
    hue = (show.t() * 36 + i * 360 / max(1, len(show.macs))) % 360
    return hsbk(hue, 100, 70), int(500 / show.speed)


def _pumpkin(show: Show, i: int, st: dict) -> Frame:
    k = int(show.t() / 4)
    hue = (28, 275)[(i + k) % 2]  # orange <-> purple, alternating bulbs
    return hsbk(hue, 100, 65), int(2500 / show.speed)


def _breathe(show: Show, i: int, st: dict) -> Frame:
    phase = (math.sin(show.t() * 2 * math.pi / 5) + 1) / 2
    return hsbk(28, 100, 8 + phase * 72), int(250 / show.speed)


def _party(show: Show, i: int, st: dict) -> Frame:
    beat = int(show.t() / 0.5)
    if st.get("beat") != beat:
        st["beat"] = beat
        st["hue"] = (st.get("hue", show.rng.uniform(0, 360)) + show.rng.uniform(70, 290)) % 360
    return hsbk(st["hue"], 100, 80), 120


def _ocean(show: Show, i: int, st: dict) -> Frame:
    t = show.t()
    w = math.sin(t * 0.9 + i * 1.3) + math.sin(t * 0.37 + i * 0.7)
    return hsbk(195 + w * 12, 85, 45 + w * 15), int(400 / show.speed)


ANIMATIONS: dict[str, Animation] = {
    a.name: a
    for a in [
        Animation("candle", "Candle", "Warm, flickering firelight", 0.11, _candle,
                  native=(P.TILE_EFFECT_FLAME, 4000, [])),
        Animation("lightning", "Storm", "Dark blue sky with random lightning strikes",
                  0.05, _lightning, tick=_lightning_tick),
        Animation("haunted", "Haunted", "Lights fade, flicker and get possessed", 0.1, _haunted),
        Animation("pumpkin", "Pumpkin", "Orange and purple trading places", 0.25, _pumpkin,
                  native=(P.TILE_EFFECT_MORPH, 5000, [hsbk(28, 100, 70), hsbk(275, 100, 60),
                                                      hsbk(15, 100, 50)])),
        Animation("rainbow", "Rainbow", "Colours rolling from light to light", 0.5, _rainbow,
                  native=(P.TILE_EFFECT_MORPH, 3000, [hsbk(h, 100, 70) for h in range(0, 360, 45)])),
        Animation("breathe", "Breathe", "Slow orange glow, in and out", 0.25, _breathe),
        Animation("ocean", "Ocean", "Gentle rolling blues", 0.4, _ocean,
                  native=(P.TILE_EFFECT_MORPH, 6000, [hsbk(190, 90, 50), hsbk(210, 80, 35),
                                                      hsbk(175, 70, 55)])),
        Animation("party", "Party", "A new colour on every beat", 0.1, _party),
    ]
}


# ------------------------------------------------------------------- engine


@dataclass
class _Saved:
    color: P.HSBK
    power: bool


class Animator:
    def __init__(self, controller: "LifxController"):
        self.ctl = controller
        self.shows: list[Show] = []
        self.saved: dict[str, _Saved] = {}  # how each light looked before
        self.native: set[str] = set()  # lights running a built-in effect
        self._last: dict[str, tuple[P.HSBK, float]] = {}
        self._next_frame: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    def current(self, mac: str) -> str | None:
        for s in self.shows:
            if mac in s.macs:
                return s.anim.name
        return None

    def _detach(self, macs: set[str]) -> None:
        for s in self.shows:
            s.macs = [m for m in s.macs if m not in macs]
        self.shows = [s for s in self.shows if s.macs]

    async def start(
        self,
        name: str,
        ids: Iterable[str] | None = None,
        *,
        speed: float = 1.0,
        brightness: float = 1.0,
    ) -> dict[str, bool]:
        if name not in ANIMATIONS:
            raise ValueError(f"unknown animation {name!r}")
        anim = ANIMATIONS[name]
        devs = self.ctl._resolve(ids)
        macs = [d.mac for d in devs]
        speed = max(0.1, min(5.0, float(speed)))
        brightness = max(0.05, min(1.0, float(brightness)))

        # Lights already animating keep their original "before" state.
        for d in devs:
            self.saved.setdefault(d.mac, _Saved(d.color, d.power))
        self._detach(set(macs))
        # Any built-in effect from a previous animation must be switched off.
        await asyncio.gather(*(self._native_off(d) for d in devs if d.mac in self.native))

        results: dict[str, bool] = {}

        async def prepare(d: "Device") -> None:
            ok = True
            if not d.power:
                r = await self.ctl.set_power([d.mac], True, FADE_IN_MS, stop_animation=False)
                ok = r.get(d.mac, False)
            if anim.native and d.is_matrix:
                ok = await self._native_on(d, anim, speed, brightness) and ok
            results[d.mac] = ok

        await asyncio.gather(*(prepare(d) for d in devs))
        show = Show(anim, macs, speed, brightness)
        for m in macs:
            self._next_frame[m] = 0.0
            self._last.pop(m, None)
        self.shows.append(show)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        log.info("animation %s started on %d lights", name, len(macs))
        return results

    async def stop(self, ids: Iterable[str] | None = None, *, restore: bool = True) -> None:
        """Stop animating ``ids`` (or everything). Optionally restore their look."""
        animating = {m for s in self.shows for m in s.macs} | self.native
        macs = animating if ids is None else animating & set(ids)
        if not macs:
            return
        self._detach(macs)
        devs = [self.ctl.devices[m] for m in macs if m in self.ctl.devices]

        async def one(d: "Device") -> None:
            if d.mac in self.native:
                await self._native_off(d)
            saved = self.saved.pop(d.mac, None)
            if restore and saved:
                ok = await self.ctl.request(
                    d, P.LIGHT_SET_COLOR, P.payload_set_color(saved.color, 800),
                    expect=P.ACKNOWLEDGEMENT,
                )
                if ok is not None:
                    d.color = saved.color
                if not saved.power:
                    await self.ctl.set_power([d.mac], False, 800, stop_animation=False)
            self._last.pop(d.mac, None)

        await asyncio.gather(*(one(d) for d in devs))
        for m in macs:  # lights that vanished from the registry
            self.saved.pop(m, None)
            self.native.discard(m)

    async def _native_on(self, d: "Device", anim: Animation, speed: float, bri: float) -> bool:
        effect, speed_ms, palette = anim.native
        palette = [P.HSBK(c.hue, c.saturation, int(c.brightness * bri), c.kelvin) for c in palette]
        payload = P.payload_set_tile_effect(
            effect, int(speed_ms / speed), palette, instance_id=random.getrandbits(32)
        )
        ok = await self.ctl.request(d, P.SET_TILE_EFFECT, payload, expect=P.ACKNOWLEDGEMENT)
        if ok is not None:
            self.native.add(d.mac)
            return True
        return False

    async def _native_off(self, d: "Device") -> None:
        await self.ctl.request(
            d, P.SET_TILE_EFFECT, P.payload_set_tile_effect(P.TILE_EFFECT_OFF),
            expect=P.ACKNOWLEDGEMENT,
        )
        self.native.discard(d.mac)

    async def _run(self) -> None:
        try:
            while self.shows:
                now = time.monotonic()
                for show in list(self.shows):
                    if show.anim.tick:
                        show.anim.tick(show)
                    for i, mac in enumerate(list(show.macs)):
                        dev = self.ctl.devices.get(mac)
                        if dev is None or mac in self.native:
                            continue
                        if now < self._next_frame.get(mac, 0):
                            continue
                        self._next_frame[mac] = now + show.anim.interval / show.speed
                        frame = show.anim.frame(show, i, show.per_light.setdefault(mac, {}))
                        if frame is None:
                            continue
                        color, fade = frame
                        if show.brightness < 1:
                            color = P.HSBK(color.hue, color.saturation,
                                           int(color.brightness * show.brightness), color.kelvin)
                        last = self._last.get(mac)
                        if last and last[0] == color and now - last[1] < RESEND_AFTER:
                            continue
                        if last is None:
                            fade = max(fade, FADE_IN_MS)
                        self._last[mac] = (color, now)
                        self.ctl.send_nowait(dev, P.LIGHT_SET_COLOR,
                                             P.payload_set_color(color, fade))
                await asyncio.sleep(TICK)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("animation loop crashed")
