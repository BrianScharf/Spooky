"""Minimal implementation of the LIFX LAN protocol.

Reference: https://lan.developer.lifx.com/docs/

Every packet has a 36-byte header (all fields little-endian) followed by an
optional payload. We only implement the messages needed to discover bulbs and
control power / colour, which keeps this dependency-free and easy to audit.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

LIFX_PORT = 56700
HEADER_SIZE = 36
PROTOCOL = 1024

# Message types -------------------------------------------------------------
GET_SERVICE = 2
STATE_SERVICE = 3
GET_POWER = 20
SET_POWER = 21
STATE_POWER = 22
GET_LABEL = 23
STATE_LABEL = 25
GET_VERSION = 32
STATE_VERSION = 33
ACKNOWLEDGEMENT = 45
GET_LOCATION = 48
STATE_LOCATION = 50
GET_GROUP = 51
STATE_GROUP = 53
ECHO_REQUEST = 58
ECHO_RESPONSE = 59
LIGHT_GET = 101
LIGHT_SET_COLOR = 102
LIGHT_STATE = 107
LIGHT_GET_POWER = 116
LIGHT_SET_POWER = 117
LIGHT_STATE_POWER = 118
STATE_UNHANDLED = 223
GET_DEVICE_CHAIN = 701  # only matrix devices (Tile, Candle, Ceiling...) answer
STATE_DEVICE_CHAIN = 702
SET_TILE_EFFECT = 719

# Built-in firmware animations on matrix devices
TILE_EFFECT_OFF = 0
TILE_EFFECT_MORPH = 2
TILE_EFFECT_FLAME = 3

_HEADER = struct.Struct("<HHI Q6sBB QHH".replace(" ", ""))
assert _HEADER.size == HEADER_SIZE


@dataclass
class Header:
    size: int
    tagged: bool
    source: int
    target: bytes  # 6-byte MAC (or zeros for broadcast)
    ack_required: bool
    res_required: bool
    sequence: int
    type: int


@dataclass
class HSBK:
    """Colour as LIFX stores it: every field 0..65535 except kelvin."""

    hue: int
    saturation: int
    brightness: int
    kelvin: int

    def to_friendly(self) -> dict:
        return {
            "hue": round(self.hue * 360 / 65535),
            "saturation": round(self.saturation * 100 / 65535),
            "brightness": round(self.brightness * 100 / 65535),
            "kelvin": self.kelvin,
        }

    @classmethod
    def from_friendly(
        cls, hue: float, saturation: float, brightness: float, kelvin: int
    ) -> "HSBK":
        """Build from hue 0-360, saturation/brightness 0-100, kelvin 1500-9000."""
        return cls(
            hue=_clamp(round((hue % 360) * 65535 / 360), 0, 65535),
            saturation=_clamp(round(saturation * 65535 / 100), 0, 65535),
            brightness=_clamp(round(brightness * 65535 / 100), 0, 65535),
            kelvin=_clamp(int(kelvin), 1500, 9000),
        )


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def mac_to_bytes(mac: str) -> bytes:
    return bytes.fromhex(mac.replace(":", ""))


def bytes_to_mac(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b[:6])


def encode(
    msg_type: int,
    payload: bytes = b"",
    *,
    source: int,
    sequence: int = 0,
    target: str | None = None,
    ack_required: bool = False,
    res_required: bool = False,
) -> bytes:
    """Build a complete packet. ``target=None`` means broadcast (tagged)."""
    tagged = target is None
    target_bytes = b"\x00" * 6 if tagged else mac_to_bytes(target)
    size = HEADER_SIZE + len(payload)
    proto_field = PROTOCOL | (1 << 12) | ((1 if tagged else 0) << 13)  # addressable=1
    flags = (1 if res_required else 0) | ((1 if ack_required else 0) << 1)
    header = _HEADER.pack(
        size,
        proto_field,
        source,
        int.from_bytes(target_bytes + b"\x00\x00", "little"),
        b"\x00" * 6,
        flags,
        sequence & 0xFF,
        0,
        msg_type,
        0,
    )
    return header + payload


def decode_header(data: bytes) -> Header:
    if len(data) < HEADER_SIZE:
        raise ValueError("packet too short")
    (size, proto_field, source, target, _r1, flags, seq, _r2, msg_type, _r3) = (
        _HEADER.unpack_from(data)
    )
    if proto_field & 0x0FFF != PROTOCOL:
        raise ValueError("not a LIFX packet")
    return Header(
        size=size,
        tagged=bool(proto_field & (1 << 13)),
        source=source,
        target=target.to_bytes(8, "little")[:6],
        ack_required=bool(flags & 0b10),
        res_required=bool(flags & 0b01),
        sequence=seq,
        type=msg_type,
    )


# Payload builders ------------------------------------------------------------


def payload_set_power(on: bool, duration_ms: int = 0) -> bytes:
    """Payload for LIGHT_SET_POWER (supports a fade duration)."""
    return struct.pack("<HI", 65535 if on else 0, max(0, int(duration_ms)))


def payload_set_color(color: HSBK, duration_ms: int = 0) -> bytes:
    return struct.pack(
        "<BHHHHI",
        0,
        color.hue,
        color.saturation,
        color.brightness,
        color.kelvin,
        max(0, int(duration_ms)),
    )


def payload_set_tile_effect(
    effect: int,
    speed_ms: int = 3000,
    palette: list[HSBK] | None = None,
    instance_id: int = 0,
) -> bytes:
    """Payload for SET_TILE_EFFECT (188 bytes). Runs until turned off."""
    palette = (palette or [])[:16]
    body = struct.pack(
        "<BBIBIQII", 0, 0, instance_id & 0xFFFFFFFF, effect, max(1, int(speed_ms)), 0, 0, 0
    )
    body += bytes(32)  # effect parameters (only used by the SKY effect)
    body += struct.pack("<B", len(palette))
    for c in palette:
        body += struct.pack("<HHHH", c.hue, c.saturation, c.brightness, c.kelvin)
    body += bytes(8 * (16 - len(palette)))
    return body


def payload_echo(data: bytes) -> bytes:
    return data[:64].ljust(64, b"\x00")


# Payload parsers -------------------------------------------------------------


def _label(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")


def parse_state_service(p: bytes) -> tuple[int, int]:
    service, port = struct.unpack_from("<BI", p)
    return service, port


def parse_light_state(p: bytes) -> tuple[HSBK, bool, str]:
    h, s, b, k, _r, power = struct.unpack_from("<HHHHhH", p)
    label = _label(p[12:44])
    return HSBK(h, s, b, k), power > 0, label


def parse_state_power(p: bytes) -> bool:
    (level,) = struct.unpack_from("<H", p)
    return level > 0


def parse_state_label(p: bytes) -> str:
    return _label(p[:32])


def parse_state_group(p: bytes) -> tuple[str, str]:
    """Returns (group_id_hex, label). Same layout as StateLocation."""
    return p[:16].hex(), _label(p[16:48])


def parse_state_version(p: bytes) -> tuple[int, int]:
    vendor, product = struct.unpack_from("<II", p)
    return vendor, product
