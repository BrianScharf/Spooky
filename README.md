# Spooky Lights — fast, local LIFX control

A replacement for the LIFX app that talks to your bulbs **directly over your
home Wi‑Fi**. No cloud, no account, no "reconnecting…" spinner. Run it on any
always-on computer (a Raspberry Pi, a home server or a spare laptop) and open
it from your phone's browser.

* **Instant:** each tap sends one UDP packet straight to the bulb (LIFX LAN protocol).
* **Self-healing:** bulbs are remembered by MAC address. The app re-checks
  every few seconds and picks a bulb back up as soon as it rejoins Wi‑Fi, even
  if its IP address changed. You never have to "reconnect" anything.
* **Retries:** every command waits for the bulb to acknowledge it and is
  re-sent automatically if the Wi‑Fi drops a packet.
* **Rooms and scenes:** bulbs are grouped by the rooms you already set up in
  LIFX. There are one-tap scenes (Bright, Relax, Night, Movie, Spooky 🎃).
* **Animations across every light:** Candle, Storm (lightning), Haunted,
  Pumpkin, Rainbow, Breathe, Ocean and Party. They run on the whole house or on
  one room, all in sync, with speed and brightness controls. Stop puts every
  light back exactly how it was.
* **No dependencies:** only Python 3.10+ is needed. Nothing to `pip install`.

## Run it

```bash
python3 -m lifx_control            # web app at http://<this-computer's-ip>:8080
```

On your phone, open `http://<computer-ip>:8080`, then use **Share → Add to
Home Screen** (iPhone) or **Install app** (Android) to get an app icon.

### Command line

```bash
python3 -m lifx_control list                 # every bulb, its room, state and IP
python3 -m lifx_control on  "Living Room"    # a room or a bulb name
python3 -m lifx_control off                  # everything
python3 -m lifx_control scene spooky Porch
python3 -m lifx_control animate lightning    # runs until Ctrl+C, then restores
```

### HTTP API (for Siri Shortcuts, Home Assistant, etc.)

| Method | Path            | Body (JSON)                                                               |
|--------|-----------------|---------------------------------------------------------------------------|
| GET    | `/api/lights`   | none                                                                      |
| POST   | `/api/power`    | `{"on": true, "group": "Kitchen"}` or `{"ids": ["d0:73:d5:..."]}`, or no target for all |
| POST   | `/api/color`    | `{"group": "Kitchen", "brightness": 40, "kelvin": 2700}` (also `hue`, `saturation`) |
| POST   | `/api/scene`    | `{"name": "relax", "group": "Bedroom"}`                                   |
| POST   | `/api/animate`  | `{"name": "haunted", "group": "Den", "speed": 1.5, "brightness": 70}`       |
| POST   | `/api/animate/stop` | `{"group": "Den"}` or `{}` for everything (`"restore": false` to leave as-is) |
| POST   | `/api/discover` | none (look for bulbs right now)                                           |

### Keep it running

Copy `lifx-control.service` to `/etc/systemd/system/` (edit the user/path),
then run `sudo systemctl enable --now lifx-control`.

## How animations work

* **Regular bulbs:** the server sends each bulb its next colour several times
  a second. Every bulb shares one clock, so rooms stay in step. This needs the
  server running; if it stops, the lights just hold their last colour.
* **Matrix lights (Ceiling, Tile, Candle):** these are detected automatically.
  For Candle, Pumpkin, Rainbow and Ocean the light runs its own built-in Flame
  or Morph effect, so every zone moves on its own instead of the whole fixture
  changing as one colour. The other animations are streamed to them like a
  normal bulb.
* Changing a light by hand (switch, slider, scene) stops the animation on that
  light only.

## If bulbs aren't found

* The computer must be on the **same network** as the bulbs. Guest networks
  and "client isolation" block it.
* Some routers drop `255.255.255.255` broadcasts. Use your subnet's broadcast
  instead: `--broadcast 192.168.1.255`.
* You can also list bulb IPs directly with `--bulb 192.168.1.40 --bulb 192.168.1.41`.
  Settings can be given as env vars too: `LIFX_BROADCAST`, `LIFX_BULBS`, `LIFX_PORT`.
* Bulbs that were found once are saved in `~/.lifx_control.json`, so they keep
  working even if discovery later fails.

## Making the bulbs themselves drop off less

This app recovers from drop-outs on its own, but these router settings help
the bulbs stay connected in the first place:

1. **Give each bulb a DHCP reservation** (a fixed IP) in your router.
2. LIFX bulbs are **2.4 GHz only**. If your router merges 2.4/5 GHz under one
   name ("band steering" / "smart connect"), a separate 2.4 GHz network for
   smart-home devices often fixes constant disconnects.
3. On mesh systems (Eero, Deco, Orbi, Google/Nest Wifi), turn off features
   such as "fast roaming / 802.11r" for the IoT network if you can.
4. Keep the bulb firmware up to date (this needs the official app one last time).

## Development

```bash
python3 -m unittest discover -s tests -t .   # tests run against simulated bulbs
python3 -m tests.fake_bulb                   # 3 fake bulbs on 127.0.0.2-4, for UI work
python3 -m lifx_control --broadcast 127.0.0.2,127.0.0.3,127.0.0.4
```
