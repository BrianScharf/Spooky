"""Tiny web server + JSON API in front of LifxController.

The controller runs on an asyncio loop in a background thread; HTTP requests
are handled by the standard-library threaded server and forwarded to it.
Nothing outside the Python standard library is needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .animations import ANIMATIONS
from .controller import LifxController

log = logging.getLogger("lifx.server")
STATIC = Path(__file__).parent / "static"

# name -> list of colours applied round-robin across the selected bulbs
SCENES: dict[str, list[dict]] = {
    "bright": [dict(hue=0, saturation=0, brightness=100, kelvin=4000)],
    "relax": [dict(hue=0, saturation=0, brightness=55, kelvin=2700)],
    "night": [dict(hue=0, saturation=0, brightness=8, kelvin=2000)],
    "movie": [dict(hue=240, saturation=60, brightness=12, kelvin=3500)],
    "spooky": [
        dict(hue=28, saturation=100, brightness=70, kelvin=3500),  # pumpkin
        dict(hue=275, saturation=100, brightness=55, kelvin=3500),  # purple
        dict(hue=110, saturation=100, brightness=45, kelvin=3500),  # slime
    ],
}


class LoopThread:
    """Runs the controller's event loop in a daemon thread."""

    def __init__(self, controller: LifxController):
        self.controller = controller
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="lifx")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.controller.start())
        self._ready.set()
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait()

    def call(self, coro, timeout: float = 15.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def call_sync(self, fn, *args):
        async def wrap():
            return fn(*args)

        return self.call(wrap())

    def stop(self) -> None:
        try:
            self.call(self.controller.stop(), timeout=5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)


def _select(snapshot: list[dict], body: dict) -> list[str] | None:
    """Turn {ids: [...]} / {group: "Kitchen"} / {} into a list of bulb ids."""
    if body.get("ids"):
        return list(body["ids"])
    if body.get("group"):
        g = body["group"].lower()
        return [d["id"] for d in snapshot if d["group"].lower() == g]
    return None  # everything


def make_handler(runner: LoopThread):
    ctl = runner.controller

    class Handler(BaseHTTPRequestHandler):
        server_version = "LifxControl/1.0"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        # -- helpers
        def _json(self, obj, status=HTTPStatus.OK):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return {}
            return body if isinstance(body, dict) else {}

        def _static(self, name: str, ctype: str):
            path = STATIC / name
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # -- routes
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if path == "/manifest.webmanifest":
                return self._static("manifest.webmanifest", "application/manifest+json")
            if path == "/icon.svg":
                return self._static("icon.svg", "image/svg+xml")
            if path == "/api/lights":
                return self._json(
                    {
                        "lights": runner.call_sync(ctl.snapshot),
                        "scenes": list(SCENES),
                        "animations": [
                            {"name": a.name, "label": a.label, "description": a.description}
                            for a in ANIMATIONS.values()
                        ],
                    }
                )
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            body = self._body()
            try:
                if path == "/api/discover":
                    runner.call(ctl.discover())
                    runner.call(ctl.refresh_all())
                    return self._json({"lights": runner.call_sync(ctl.snapshot)})

                ids = _select(runner.call_sync(ctl.snapshot), body)
                duration = int(body.get("duration", 300))

                if path == "/api/power":
                    res = runner.call(ctl.set_power(ids, bool(body.get("on")), duration))
                    return self._json({"results": res})

                if path == "/api/color":
                    kwargs = {
                        k: float(body[k])
                        for k in ("hue", "saturation", "brightness")
                        if body.get(k) is not None
                    }
                    if body.get("kelvin") is not None:
                        kwargs["kelvin"] = int(body["kelvin"])
                    res = runner.call(
                        ctl.set_color(
                            ids,
                            duration_ms=duration,
                            power_on=bool(body.get("power_on", True)),
                            **kwargs,
                        )
                    )
                    return self._json({"results": res})

                if path == "/api/animate":
                    name = str(body.get("name", "")).lower()
                    if name not in ANIMATIONS:
                        return self._json({"error": "unknown animation"}, HTTPStatus.NOT_FOUND)
                    res = runner.call(
                        ctl.animator.start(
                            name,
                            ids,
                            speed=float(body.get("speed", 1.0)),
                            brightness=float(body.get("brightness", 100)) / 100,
                        )
                    )
                    return self._json({"results": res})

                if path == "/api/animate/stop":
                    runner.call(ctl.animator.stop(ids, restore=bool(body.get("restore", True))))
                    return self._json({"ok": True})

                if path == "/api/scene":
                    name = str(body.get("name", "")).lower()
                    if name not in SCENES:
                        return self._json({"error": "unknown scene"}, HTTPStatus.NOT_FOUND)
                    return self._json({"results": runner.call(_apply_scene(ctl, ids, name, duration))})
            except (TypeError, ValueError) as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


async def _apply_scene(ctl: LifxController, ids, name: str, duration: int) -> dict:
    colors = SCENES[name]
    targets = ids if ids is not None else [d["id"] for d in ctl.snapshot()]
    jobs = [
        ctl.set_color([mac], duration_ms=max(duration, 800), power_on=True, **colors[i % len(colors)])
        for i, mac in enumerate(targets)
    ]
    results: dict[str, bool] = {}
    for r in await asyncio.gather(*jobs):
        results.update(r)
    return results


def serve(controller: LifxController, host: str = "0.0.0.0", port: int = 8080) -> None:
    runner = LoopThread(controller)
    runner.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(runner))
    shown = "localhost" if host in ("0.0.0.0", "") else host
    log.info("LIFX control running at http://%s:%d  (Ctrl+C to quit)", shown, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        runner.stop()
