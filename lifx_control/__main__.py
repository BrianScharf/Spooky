"""Command line entry point.

    python -m lifx_control                 # start the web app on port 8080
    python -m lifx_control list            # show bulbs
    python -m lifx_control on  [NAME ...]  # NAME = bulb label or room/group
    python -m lifx_control off [NAME ...]
    python -m lifx_control scene spooky [NAME ...]
    python -m lifx_control animate candle [NAME ...]   # Ctrl+C stops and restores
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from .controller import LifxController

DEFAULT_CACHE = Path(os.environ.get("LIFX_CACHE", Path.home() / ".lifx_control.json"))


def _split(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        out += [x.strip() for x in v.split(",") if x.strip()]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="lifx_control", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="serve",
                    choices=["serve", "list", "on", "off", "scene", "animate"])
    ap.add_argument("args", nargs="*", help="bulb/room names (or scene name first)")
    ap.add_argument("--host", default=os.environ.get("LIFX_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("LIFX_PORT", 8080)))
    ap.add_argument("--broadcast", action="append",
                    default=_split([os.environ.get("LIFX_BROADCAST", "")]) or None,
                    help="broadcast address(es), e.g. 192.168.1.255 (default 255.255.255.255)")
    ap.add_argument("--bulb", action="append", dest="bulbs",
                    default=_split([os.environ.get("LIFX_BULBS", "")]),
                    help="IP of a bulb to always check directly (repeatable)")
    ap.add_argument("--cache", default=str(DEFAULT_CACHE), help="where to remember bulbs")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    ctl = LifxController(cache_path=a.cache,
                         broadcast_addrs=_split(a.broadcast) or ["255.255.255.255"],
                         static_hosts=_split(a.bulbs))

    if a.command == "serve":
        from .server import serve
        serve(ctl, a.host, a.port)
        return
    try:
        asyncio.run(_oneshot(ctl, a.command, a.args))
    except KeyboardInterrupt:
        pass


async def _oneshot(ctl: LifxController, cmd: str, args: list[str]) -> None:
    await ctl.start()
    try:
        if cmd == "list":
            for d in ctl.snapshot():
                c = d["color"]
                state = ("ON " if d["power"] else "off") if d["online"] else "OFFLINE"
                print(f"{d['group']:<16} {d['label']:<24} {state:<8} "
                      f"{c['brightness']:>3}%  {c['kelvin']}K  hue {c['hue']}  {d['ip']}")
            if not ctl.devices:
                print("No bulbs found. Try --broadcast <your subnet>.255 or --bulb <ip>.")
            return

        scene = None
        if cmd in ("scene", "animate"):
            if not args:
                raise SystemExit(f"usage: {cmd} NAME [bulb/room ...]")
            scene, args = args[0].lower(), args[1:]

        ids = None
        if args:
            wanted = {n.lower() for n in args}
            ids = [d["id"] for d in ctl.snapshot()
                   if d["label"].lower() in wanted or d["group"].lower() in wanted]
            if not ids:
                raise SystemExit(f"No bulb or room named {', '.join(args)}")

        if cmd == "animate":
            from .animations import ANIMATIONS
            if scene not in ANIMATIONS:
                raise SystemExit(f"Unknown animation. Choose from: {', '.join(ANIMATIONS)}")
            await ctl.animator.start(scene, ids)
            print(f"{ANIMATIONS[scene].label} running. Press Ctrl+C to stop.")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
            return  # ctl.stop() below puts the lights back

        if scene:
            from .server import SCENES, _apply_scene
            if scene not in SCENES:
                raise SystemExit(f"Unknown scene. Choose from: {', '.join(SCENES)}")
            res = await _apply_scene(ctl, ids, scene, 800)
        else:
            res = await ctl.set_power(ids, cmd == "on")
        failed = [ctl.devices[m].label or m for m, ok in res.items() if not ok]
        print(f"{len(res) - len(failed)}/{len(res)} bulbs updated"
              + (f"; no reply from: {', '.join(failed)}" if failed else ""))
    finally:
        await ctl.stop()


if __name__ == "__main__":
    main()
