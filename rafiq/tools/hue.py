"""Philips Hue lights, through the bridge's local REST API: no cloud account, nothing leaves the house.

Setup, once: `rafiq hue-setup` (or `rafiq hue-setup --host 192.168.1.20`), then press the round link
button on the bridge. The bridge's user key is stored in ~/.config/rafiq/hue.json, readable only by you.

Rooms are the Hue app's rooms and zones, matched loosely ("the living room", "kitchn"). Names in another
language than the Hue app's come from [hue.rooms] in the config ("الصالة" = "Living room"); an unknown
name lists the rooms, so the model can try again with the right one.
"""

from __future__ import annotations

import difflib
import json
import re
import socket
import time
import unicodedata

from .. import config
from ..lang import normalize_arabic
from . import tool

DISCOVERY_URL = "https://discovery.meethue.com/"


def _request(method: str, url: str, body: dict | None = None):
    import httpx

    resp = httpx.request(method, url, json=body, timeout=5)
    resp.raise_for_status()
    return resp.json()


class Bridge:
    """The few calls Rafiq makes to the bridge. `request(method, url, body)` returns the decoded JSON."""

    def __init__(self, host: str, username: str, request=_request):
        self.host, self.username, self.request = host, username, request

    def _url(self, path: str) -> str:
        return f"http://{self.host}/api/{self.username}/{path}"

    def rooms(self) -> dict[str, str]:
        """Room and zone names -> group ids."""
        groups = self.request("GET", self._url("groups")) or {}
        return {g["name"]: gid for gid, g in groups.items() if g.get("type") in ("Room", "Zone")}

    def set_group(self, group: str, state: dict) -> list[str]:
        """Apply a state to a group (0 is every light); returns the bridge's error messages."""
        answer = self.request("PUT", self._url(f"groups/{group}/action"), state) or []
        return [item["error"].get("description", "") for item in answer if isinstance(item, dict) and "error" in item]


def _hue_file():
    return config.home() / "hue.json"


def save(host: str, username: str) -> None:
    path = _hue_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text(json.dumps({"host": host, "username": username}))


def load_bridge() -> Bridge | None:
    path = _hue_file()
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    return Bridge(data["host"], data["username"])


def setup(host: str = "", request=_request, sleep=time.sleep, say=print, tries: int = 30) -> str:
    """Pair with the bridge: find it if no address is given, then wait for its link button."""
    if not host:
        # The only call outside the house: Philips' discovery page, which answers with the bridge's local address.
        try:
            found = request("GET", DISCOVERY_URL) or []
        except Exception as exc:  # noqa: BLE001 (any network failure means the same thing here)
            found = []
            say(f"Could not look for the bridge ({type(exc).__name__}).")
        if not found:
            return "No Hue bridge found: give its address with `rafiq hue-setup --host 192.168.1.20`."
        host = found[0]["internalipaddress"]
    say(f"Hue bridge at {host}: press the round link button on it now.")
    body = {"devicetype": f"rafiq#{socket.gethostname()[:19]}"}
    for _ in range(tries):
        answer = request("POST", f"http://{host}/api", body) or [{}]
        item = answer[0]
        if "success" in item:
            save(host, item["success"]["username"])
            return f"Hue bridge at {host} paired; key saved at {_hue_file()}."
        error = item.get("error", {})
        if error.get("type") != 101:  # 101: link button not pressed yet
            return f"The Hue bridge said: {error.get('description', answer)}"
        sleep(2)
    return "The link button was not pressed in time: run `rafiq hue-setup` again."


# ---------------------------------------------------------------------- rooms

_ARTICLES = {"the", "le", "la", "les", "l", "el", "los", "las", "der", "die", "das", "den", "im", "in", "room"}
EVERY_ROOM = {"", "all", "everything", "everywhere", "house", "home", "tout", "toutes", "tous", "maison",
              "todo", "todas", "todos", "casa", "alle", "alles", "haus", "كل", "الكل", "البيت", "كله"}


def _key(name: str) -> str:
    text = unicodedata.normalize("NFKD", normalize_arabic(name).casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    words = [w for w in re.findall(r"\w+", text) if w not in _ARTICLES]
    return " ".join(words) or text.strip()


def match_room(spoken: str, names: list[str], aliases: dict | None = None) -> str | None:
    """The Hue room meant by `spoken`: an alias or name exactly, then a part of one, then a near spelling."""
    candidates = {_key(n): n for n in names}
    for alias, target in (aliases or {}).items():
        if target in names:
            candidates[_key(alias)] = target
    key = _key(spoken)
    if key in candidates:
        return candidates[key]
    if len(key) >= 3:
        for cand, name in candidates.items():
            if key in cand or (len(cand) >= 3 and cand in key):
                return name
    close = difflib.get_close_matches(key, list(candidates), n=1, cutoff=0.75)
    return candidates[close[0]] if close else None


# ---------------------------------------------------------------------- colours

_RGB = {
    "red": (255, 0, 0), "orange": (255, 120, 0), "yellow": (255, 220, 0), "green": (0, 255, 0),
    "blue": (0, 0, 255), "purple": (140, 0, 255), "pink": (255, 80, 160), "cyan": (0, 255, 255),
}
_NAMES = {
    "red": {"red", "rouge", "rojo", "roja", "rot", "rote", "احمر", "حمرا"},
    "orange": {"orange", "naranja", "برتقالي", "برتقاني"},
    "yellow": {"yellow", "jaune", "amarillo", "amarilla", "gelb", "اصفر", "صفرا"},
    "green": {"green", "vert", "verte", "verde", "grun", "gruen", "اخضر", "خضرا"},
    "blue": {"blue", "bleu", "bleue", "azul", "blau", "ازرق", "زرقا", "لبني"},
    "purple": {"purple", "violet", "violette", "morado", "lila", "violett", "بنفسجي", "موف"},
    "pink": {"pink", "rose", "rosa", "وردي", "بمبي"},
    "cyan": {"cyan", "turquoise", "turquesa", "turkis", "تركواز", "سماوي"},
}
# White in mireds: warm 2700 K, plain 4000 K, cold 6500 K.
_WHITES = {
    370: {"warm", "chaud", "chaude", "calido", "calida", "warmes", "warmweiss", "دافي", "دافئ"},
    154: {"cold", "cool", "daylight", "froid", "froide", "frio", "fria", "kalt", "kaltes", "kaltweiss", "بارد"},
}
_WHITE_WORDS = {"white", "blanc", "blanche", "blanco", "blanca", "weiss", "weisses", "ابيض", "بيضا"}


def _xy(r: int, g: int, b: int) -> list[float]:
    """sRGB -> the CIE xy the bridge takes (Philips' published conversion)."""
    def linear(c: float) -> float:
        c /= 255
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r, g, b = linear(r), linear(g), linear(b)
    x = r * 0.664511 + g * 0.154324 + b * 0.162028
    y = r * 0.283881 + g * 0.668433 + b * 0.047685
    z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = x + y + z
    return [round(x / total, 4), round(y / total, 4)] if total else [0.3227, 0.329]


def color_state(color: str) -> dict:
    text = color.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        return {"xy": _xy(int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))}
    words = set(_key(text).split())
    for name, spoken in _NAMES.items():
        if words & spoken:
            return {"xy": _xy(*_RGB[name])}
    for mireds, spoken in _WHITES.items():
        if words & spoken:
            return {"ct": mireds}
    if words & _WHITE_WORDS:
        return {"ct": 250}
    raise ValueError(f"unknown colour {color!r}")


# ---------------------------------------------------------------------- the tool

@tool("lights", "Philips Hue lights: turn a room on or off, set its brightness or its colour.",
      {"room": {"type": "string", "description": "the room as the user said it (or its Hue name if you know it); "
                                                 "empty or 'all' for every light"},
       "on": {"type": "boolean", "description": "true to turn on, false to turn off"},
       "brightness": {"type": "integer", "description": "percent, 0 to 100 (0 turns it off)"},
       "color": {"type": "string", "description": "a colour name such as blue, or warm white, or #rrggbb"}},
      level="act")
def lights(room: str = "", on: bool | None = None, brightness: int | None = None, color: str = "",
           ctx=None, bridge: Bridge | None = None) -> str:
    bridge = bridge or load_bridge()
    if bridge is None:
        return "error: the Hue bridge is not paired: run `rafiq hue-setup`"
    state: dict = {}
    if on is not None:
        state["on"] = bool(on)
    if brightness is not None:
        percent = max(0, min(int(brightness), 100))
        state.update({"on": False} if percent == 0 else {"on": True, "bri": max(1, round(percent * 254 / 100))})
    if color:
        try:
            state.update(color_state(color))
        except ValueError as exc:
            return f"error: {exc}"
        state.setdefault("on", True)
    if not state:
        return "error: say what to change: on, off, a brightness or a colour"
    import httpx

    try:
        if _key(room) in EVERY_ROOM or room.strip().casefold() in EVERY_ROOM:
            name, group = "every light", "0"
        else:
            rooms = bridge.rooms()
            aliases = (ctx.cfg.get("hue", {}).get("rooms") if ctx else None) or {}
            name = match_room(room, list(rooms), aliases)
            if name is None:
                return f"error: no Hue room called {room!r}; the rooms are: {', '.join(rooms) or 'none'}"
            group = rooms[name]
        errors = bridge.set_group(group, state)
    except httpx.HTTPError as exc:
        return f"error: the Hue bridge at {bridge.host} did not answer ({type(exc).__name__})"
    if errors:
        return "error: the Hue bridge said: " + "; ".join(errors)
    done = ", ".join(f"{k} {v}" for k, v in state.items())
    return f"done: {name}: {done}"
