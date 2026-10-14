"""Video and stage lights over DMX, through an Art-Net node on the local network ("key light to 60%, 5600 K").

DMX is an open standard: Aputure, Godox, Nanlite and most stage lights take it in a DMX mode, through an
Art-Net node (a small box on the LAN) or a light with Art-Net built in. Each light is described once in
the settings: where its channels start and what each one does, from the "DMX profile" table in its
manual. Rafiq keeps what it last set in data/dmx.json and sends the whole frame each time, because a
DMX frame always carries every channel: one light changed, the others stay as they were.

Bluetooth control of amaran lights is not supported: it needs keys taken from the vendor's app (#31).
"""

from __future__ import annotations

import json
import re
import socket
import threading
from dataclasses import dataclass

from .. import config
from ..remote import _private_ip
from . import tool

PORT = 6454                   # Art-Net's UDP port
# What a channel does. The 16-bit kinds take two channels, coarse then fine.
KINDS = {"dimmer": 1, "dimmer16": 2, "cct": 1, "cct16": 2, "none": 1}
_FILLER = re.compile(r"\b(the|lights?)\b")
ALL = ("all", "every", "everything")
_lock = threading.Lock()      # the voice loop and the satellites' threads both change lights


@dataclass
class Light:
    name: str
    address: int                     # its first channel, 1 to 512
    channels: list[str]
    kelvin: tuple[int, int] = (2700, 6500)

    @property
    def footprint(self) -> int:
        return sum(KINDS[kind] for kind in self.channels)


def _norm(name: str) -> str:
    """A light's name as matched: "The Key Light" and "key" are the same light."""
    return " ".join(_FILLER.sub(" ", name.casefold()).split())


def _whole(value, what: str, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}: {what} must be a whole number, not {value!r}")
    return value


def _light(i: int, raw) -> Light:
    if not isinstance(raw, dict):
        raise ValueError(f"light {i} must be a [[dmx.lights]] block with a name, an address and channels")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError(f"light {i} has no name")
    if _norm(name) in ("", *ALL):
        raise ValueError(f"{name}: a light can't be called {name!r}, which means all of them")
    channels = raw.get("channels") or ["dimmer"]
    channels = [channels] if isinstance(channels, str) else channels
    if not isinstance(channels, list) or not all(isinstance(kind, str) for kind in channels):
        raise ValueError(f"{name}: channels must be a list such as [\"dimmer\", \"cct\"]")
    unknown = [kind for kind in channels if kind not in KINDS]
    if unknown:
        raise ValueError(f"{name}: unknown channel {', '.join(unknown)} (use {', '.join(KINDS)})")
    kelvin = raw.get("kelvin") or [2700, 6500]
    if not isinstance(kelvin, list | tuple) or len(kelvin) != 2:
        raise ValueError(f"{name}: kelvin must be [lowest, highest], such as [2700, 6500]")
    low, high = (_whole(k, "kelvin", name) for k in kelvin)
    if not 1000 <= low < high <= 20000:
        raise ValueError(f"{name}: kelvin must be [lowest, highest], such as [2700, 6500]")
    light = Light(name, _whole(raw.get("address", 0), "address", name), channels, (low, high))
    if not 1 <= light.address <= 513 - light.footprint:
        raise ValueError(f"{name}: address {light.address} does not fit its {light.footprint} channels in 1 to 512")
    return light


def lights(cfg: dict) -> list[Light]:
    """The lights in [[dmx.lights]]; a wrong one raises ValueError saying what is wrong."""
    found: list[Light] = []
    for i, raw in enumerate(cfg.get("dmx", {}).get("lights") or [], 1):
        light = _light(i, raw)
        for other in found:
            if _norm(light.name) == _norm(other.name):
                raise ValueError(f"{light.name} and {other.name} have the same name")
            if light.address < other.address + other.footprint and other.address < light.address + light.footprint:
                raise ValueError(f"{light.name} and {other.name} overlap on the same channels")
        found.append(light)
    return found


def node(cfg: dict) -> str:
    """The Art-Net node's address, checked with the universe; only one on this network, as the frames are
    not protected."""
    host = str(cfg.get("dmx", {}).get("host") or "")
    if not _private_ip(host):
        raise ValueError(f"[dmx] host must be the Art-Net node's address on your network, not {host!r}")
    universe = cfg.get("dmx", {}).get("universe", 0)
    if isinstance(universe, bool) or not isinstance(universe, int) or not 0 <= universe <= 0x7FFF:
        raise ValueError(f"[dmx] universe must be a whole number from 0 to 32767, not {universe!r}")
    return host


def artdmx(universe: int, data: bytes, sequence: int = 0) -> bytes:
    """An ArtDmx packet (Art-Net 4): the header, then the channels, padded to an even length."""
    if len(data) > 512:
        raise ValueError("a DMX universe has 512 channels")
    if len(data) % 2:
        data += b"\0"
    data = data or b"\0\0"
    return (b"Art-Net\0" + (0x5000).to_bytes(2, "little") + (14).to_bytes(2, "big")
            + bytes([sequence & 0xFF, 0]) + (universe & 0x7FFF).to_bytes(2, "little")
            + len(data).to_bytes(2, "big") + data)


def _send(host: str, packet: bytes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(1)
        sock.sendto(packet, (host, PORT))


def _load() -> dict:
    try:
        data = json.loads((config.data_dir() / "dmx.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(state: dict) -> None:
    path = config.data_dir() / "dmx.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def frame(found: list[Light], state: dict) -> bytes:
    """The channels of every light, as the state says; a light never set stays dark."""
    data = bytearray(max((light.address + light.footprint - 1 for light in found), default=0))
    for light in found:
        now = state.get(light.name) or {}
        level = min(1.0, max(0.0, now.get("brightness", 100) / 100)) if now.get("on") else 0.0
        low, high = light.kelvin
        warmth = min(1.0, max(0.0, (now.get("kelvin", (low + high) // 2) - low) / (high - low)))  # range changed
        at = light.address - 1
        for kind in light.channels:
            share = level if kind.startswith("dimmer") else warmth if kind.startswith("cct") else 0.0
            if KINDS[kind] == 2:
                value = round(share * 65535)
                data[at:at + 2] = bytes([value >> 8, value & 0xFF])
            else:
                data[at] = round(share * 255)
            at += KINDS[kind]
    return bytes(data)


def _match(found: list[Light], said: str) -> list[Light]:
    key = _norm(said)
    if key in ("", *ALL):
        return found
    return [light for light in found if _norm(light.name) == key]


def _describe(light: Light, now: dict) -> str:
    if not now.get("on"):
        return f"{light.name}: off"
    shown = f"{light.name}: {now.get('brightness', 100)}%"
    if any(kind.startswith("cct") for kind in light.channels):
        shown += f", {now.get('kelvin', sum(light.kelvin) // 2)} K"
    return shown


def apply(cfg: dict, light: str, on: bool | None = None, brightness: int | None = None,
          kelvin: int | None = None, send=_send) -> str:
    """video_light with the settings and the network passed in, for tests."""
    try:
        found, host = lights(cfg), node(cfg)
    except ValueError as exc:
        return f"error: {exc}"
    chosen = _match(found, light)
    if not chosen:
        return f"error: no light called {light!r}; the lights are {', '.join(f.name for f in found)}"
    try:
        brightness = None if brightness is None else int(brightness)
        kelvin = None if kelvin is None else int(kelvin)
    except (TypeError, ValueError):
        return "error: brightness and kelvin are whole numbers, such as 60 and 5600"
    with _lock:
        state = _load()
        for target in chosen:
            now = dict(state.get(target.name) or {})
            if on is not None:
                now["on"] = bool(on)
            if brightness is not None:
                level = max(0, min(100, brightness))
                if level:
                    now["brightness"] = level            # kept while off, for the next "on"
                now["on"] = level > 0 and on is not False   # 0 turns it off, whatever "on" says
            if kelvin is not None:
                now["kelvin"] = max(target.kelvin[0], min(target.kelvin[1], kelvin))
            state[target.name] = now
        try:
            send(host, artdmx(cfg["dmx"].get("universe", 0), frame(found, state)))
        except OSError as exc:
            return f"error: the Art-Net node at {host} could not be reached ({type(exc).__name__})"
        _save(state)
    return "; ".join(_describe(target, state[target.name]) for target in chosen)


@tool("video_light", "Video or stage lights over DMX (Art-Net): turn one on or off, set its brightness or its "
      "colour temperature (\"key light to 60%, 5600 K\").",
      {"light": {"type": "string", "description": "the light's name, such as key or fill; 'all' for every one"},
       "on": {"type": "boolean", "description": "true to turn on, false to turn off"},
       "brightness": {"type": "integer", "description": "percent, 0 to 100 (0 turns it off)"},
       "kelvin": {"type": "integer", "description": "colour temperature, such as 3200 or 5600"}},
      required=("light",), level="act")
def video_light(light: str, on: bool | None = None, brightness: int | None = None, kelvin: int | None = None,
                ctx=None) -> str:
    return apply(ctx.cfg if ctx else config.load(), light, on, brightness, kelvin)
