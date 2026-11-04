"""The pointer and the keyboard: one left click for click_on_screen (#26), and a whole run of clicks, keys, typing
and scrolling for the operator (#18, PortalRemote and XdoRemote below).

On Wayland only the compositor may move the pointer, so the click goes through the XDG desktop portal's
RemoteDesktop interface: GNOME asks the user once ("allow remote interaction?") and, with persist_mode,
gives a restore token so it doesn't ask again; the token is kept in remote_desktop.json, readable only by
the user. Each click opens a session, moves the pointer to an absolute point of a monitor's stream,
presses and releases the left button, and closes the session. On X11, xdotool clicks.

Points come in pixels of the screenshot. The portal's streams are in logical pixels (a 4K screen at 200 %
is 1920 by 1080), so the point is scaled by the screenshot's size over the monitors' and found in the
monitor that holds it; a point on no monitor is never clicked. One scale for every monitor: when the monitors'
layout doesn't have the screenshot's shape (monitors at different scales), nothing is clicked, since the
point could land off the spot that was circled and agreed to.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from . import config

PORTAL_SECONDS = 60          # time for the user to answer GNOME's "allow remote interaction?" the first time
REQUEST_SECONDS = 10         # the other portal requests answer at once
XDOTOOL_SECONDS = 5
BTN_LEFT = 272               # Linux evdev code of the left button
KEYBOARD = 1                 # RemoteDesktop device types
POINTER = 2
MONITOR = 1                  # ScreenCast source type
PERSIST_UNTIL_REVOKED = 2
SHAPE_TOLERANCE = 0.01       # how far the monitors' layout may differ in shape from the screenshot (rounding)


class PointerError(Exception):
    pass


class NotReady(PointerError):
    """The screen changed (or Stop was pressed) between the yes and the click: nothing was clicked."""


@dataclass(frozen=True)
class Stream:
    node: int                # the PipeWire node the portal names the monitor by
    x: int                   # its place and size in logical pixels
    y: int
    width: int
    height: int


def to_stream(px: float, py: float, shot_width: int, shot_height: int,
              streams: list[Stream]) -> tuple[Stream, float, float] | None:
    """The monitor holding the screenshot's point (px, py), and the point in that monitor's logical pixels."""
    if not streams:
        return None
    left, top = min(s.x for s in streams), min(s.y for s in streams)
    right, bottom = max(s.x + s.width for s in streams), max(s.y + s.height for s in streams)
    if abs((right - left) * shot_height / ((bottom - top) * shot_width) - 1) > SHAPE_TOLERANCE:
        return None
    x = left + px * (right - left) / shot_width
    y = top + py * (bottom - top) / shot_height
    for s in streams:
        if s.x <= x < s.x + s.width and s.y <= y < s.y + s.height:
            return s, x - s.x, y - s.y
    return None


# ---------------------------------------------------------------------- the restore token

# click_on_screen's grant (the pointer) and the operator's (the pointer and the keyboard) are kept apart, so GNOME's
# first question for the operator says that it wants the keyboard too.
CLICK_TOKEN, OPERATOR_TOKEN = "remote_desktop.json", "remote_desktop_operator.json"


def _token_path(name: str = CLICK_TOKEN):
    return config.home() / name


def load_token(name: str = CLICK_TOKEN) -> str:
    try:
        token = json.loads(_token_path(name).read_text(encoding="utf-8")).get("restore_token", "")
    except (OSError, ValueError, AttributeError):
        return ""
    return token if isinstance(token, str) else ""


def save_token(token: str, name: str = CLICK_TOKEN) -> None:
    """Readable only by the user, and replaced in one step: it lets Rafiq click without asking GNOME again."""
    path = _token_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"restore_token": token}, f)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# ---------------------------------------------------------------------- the portal

PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_NAME = "org.freedesktop.portal.Desktop"


class JeepneyBus:
    """The session bus, for the portal's request-and-response calls (as tools/screen.py takes screenshots)."""

    def __init__(self):
        try:
            from jeepney.io.blocking import open_dbus_connection
        except ImportError as exc:
            raise PointerError("jeepney is not installed") from exc
        try:
            self.conn = open_dbus_connection(bus="SESSION")
        except (OSError, KeyError) as exc:
            raise PointerError(f"no session bus: {exc}") from exc
        self.sender = self.conn.unique_name.lstrip(":").replace(".", "_")

    def _address(self, interface: str, path: str = PORTAL_PATH):
        from jeepney import DBusAddress

        return DBusAddress(path, bus_name=PORTAL_NAME, interface=f"org.freedesktop.portal.{interface}")

    def request(self, interface: str, method: str, signature: str, args: tuple, timeout: float = REQUEST_SECONDS):
        """Call a method that answers with a Response signal, and return its results."""
        from jeepney import MatchRule, message_bus, new_method_call
        from jeepney.io.blocking import Proxy

        token = "rafiq" + secrets.token_hex(8)
        args[-1]["handle_token"] = ("s", token)
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response",
                         path=f"{PORTAL_PATH}/request/{self.sender}/{token}")
        Proxy(message_bus, self.conn).AddMatch(rule)
        try:
            with self.conn.filter(rule) as responses:
                reply = self.conn.send_and_get_reply(new_method_call(self._address(interface), method, signature, args))
                if reply.header.message_type.name == "error":
                    raise PointerError(f"the remote desktop portal failed ({method})")
                code, results = self.conn.recv_until_filtered(responses, timeout=timeout).body
        except TimeoutError as exc:
            raise PointerError("remote interaction was not allowed in time") from exc
        if code != 0:
            raise PointerError("remote interaction was refused" if code == 1 else f"the portal failed ({method})")
        return results

    def call(self, interface: str, method: str, signature: str, args: tuple) -> None:
        from jeepney import new_method_call

        reply = self.conn.send_and_get_reply(new_method_call(self._address(interface), method, signature, args))
        if reply.header.message_type.name == "error":
            raise PointerError(f"the remote desktop portal failed ({method})")

    def close(self) -> None:
        self.conn.close()

    def listen(self, interface: str, member: str, on_signal: Callable, stop) -> None:
        """Call on_signal(body) for each `member` signal of the portal's `interface` until `stop` is set."""
        from jeepney import MatchRule, message_bus
        from jeepney.io.blocking import Proxy

        rule = MatchRule(type="signal", interface=f"org.freedesktop.portal.{interface}", member=member)
        Proxy(message_bus, self.conn).AddMatch(rule)
        with self.conn.filter(rule) as signals:
            while not stop.is_set():
                try:
                    on_signal(self.conn.recv_until_filtered(signals, timeout=0.5).body)
                except TimeoutError:
                    continue

    def close_session(self, session: str) -> None:
        from jeepney import new_method_call

        try:
            self.conn.send_and_get_reply(new_method_call(self._address("Session", session), "Close"))
        except Exception:            # closing is best effort: the session ends with the connection anyway
            pass
        finally:
            self.conn.close()


def _streams(value) -> list[Stream]:
    """The portal's a(ua{sv}): each monitor's node with its position and size. With several monitors, one without
    a position (it is optional) can't be placed, so nothing is clicked rather than guessing it is at 0, 0."""
    items = value[1] if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], list) else []
    found = []
    for item in items:
        try:
            node, props = item
            if "position" not in props and len(items) > 1:
                raise PointerError("a monitor's position is unknown, so the point can't be placed")
            x, y = props.get("position", ("(ii)", (0, 0)))[1]
            width, height = props["size"][1]
        except (KeyError, TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            found.append(Stream(int(node), int(x), int(y), int(width), int(height)))
    return found


def portal_click(px: float, py: float, shot_width: int, shot_height: int, bus=None,
                 ready: Callable[[], bool] = lambda: True) -> None:
    """One left click at the screenshot's point through the RemoteDesktop portal. `ready` is asked after the portal
    started (GNOME may have asked the user first) and before the pointer moves. The bus is always closed."""
    bus = bus or JeepneyBus()
    session = None
    try:
        created = bus.request("RemoteDesktop", "CreateSession", "a{sv}",
                              ({"session_handle_token": ("s", "rafiq" + secrets.token_hex(8))},))
        handle = created.get("session_handle")
        if not (isinstance(handle, tuple) and len(handle) == 2 and isinstance(handle[1], str)):
            raise PointerError("the remote desktop portal gave no session")
        session = handle[1]
        devices = {"types": ("u", POINTER), "persist_mode": ("u", PERSIST_UNTIL_REVOKED)}
        token = load_token()
        if token:
            devices["restore_token"] = ("s", token)
        bus.request("RemoteDesktop", "SelectDevices", "oa{sv}", (session, devices))
        bus.request("ScreenCast", "SelectSources", "oa{sv}",
                    (session, {"types": ("u", MONITOR), "multiple": ("b", True)}))
        started = bus.request("RemoteDesktop", "Start", "osa{sv}", (session, "", {}), timeout=PORTAL_SECONDS)
        if "restore_token" in started:
            save_token(str(started["restore_token"][1]))
        if not int(started.get("devices", ("u", 0))[1]) & POINTER:
            raise PointerError("remote interaction was allowed without the pointer")
        spot = to_stream(px, py, shot_width, shot_height, _streams(started.get("streams")))
        if spot is None:
            raise PointerError("that point is on no monitor, or the monitors' scales differ")
        if not ready():
            raise NotReady("the screen changed before the click")
        stream, x, y = spot
        bus.call("RemoteDesktop", "NotifyPointerMotionAbsolute", "oa{sv}udd",
                 (session, {}, stream.node, float(x), float(y)))
        # The press follows at once: the portal keeps the order, and a wait would give a page time to swap what is
        # under the pointer on hover.
        bus.call("RemoteDesktop", "NotifyPointerButton", "oa{sv}iu", (session, {}, BTN_LEFT, 1))
        bus.call("RemoteDesktop", "NotifyPointerButton", "oa{sv}iu", (session, {}, BTN_LEFT, 0))
    finally:
        if session is not None:
            bus.close_session(session)
        else:
            bus.close()


# ---------------------------------------------------------------------- which way

def method(env: Mapping[str, str] | None = None, which: Callable[[str], str | None] = shutil.which) -> str | None:
    """How a click is made here: "portal" (Wayland), "xdotool" (X11), or None."""
    env = os.environ if env is None else env
    if env.get("WAYLAND_DISPLAY"):
        return "portal"
    if env.get("DISPLAY") and which("xdotool"):
        return "xdotool"
    return None


def click(px: float, py: float, shot_width: int, shot_height: int, env: Mapping[str, str] | None = None,
          which: Callable[[str], str | None] = shutil.which, portal=None, run=None,
          ready: Callable[[], bool] = lambda: True) -> None:
    """One left click at (px, py) in pixels of a screenshot of shot_width by shot_height, if `ready` says the screen
    is still as agreed just before. portal and run are looked up when called, so tests can replace them."""
    how = method(env, which)
    if how == "portal":
        (portal or portal_click)(px, py, shot_width, shot_height, ready=ready)
        return
    if how is None:
        raise PointerError("no way to click here: on X11 install xdotool; on Wayland, run in a desktop session")
    if not ready():
        raise NotReady("the screen changed before the click")
    try:
        done = (run or subprocess.run)(["xdotool", "mousemove", "--sync", str(round(px)), str(round(py)), "click", "1"],
                                       capture_output=True, text=True, timeout=XDOTOOL_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PointerError(f"xdotool failed ({type(exc).__name__})") from exc
    if done.returncode != 0:
        raise PointerError("xdotool failed")


# ---------------------------------------------------------------------- keys (#18)

# X keysyms by name, lower case without "_" (Page_Up, page_up and PageUp are the same key). OpenAI's names
# (ENTER, ARROWDOWN, CMD) and xdotool's both land here.
KEYSYMS = {
    "return": 0xFF0D, "enter": 0xFF0D, "escape": 0xFF1B, "esc": 0xFF1B, "tab": 0xFF09, "backspace": 0xFF08,
    "delete": 0xFFFF, "del": 0xFFFF, "insert": 0xFF63, "home": 0xFF50, "end": 0xFF57,
    "pageup": 0xFF55, "prior": 0xFF55, "pagedown": 0xFF56, "next": 0xFF56,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "arrowleft": 0xFF51, "arrowup": 0xFF52, "arrowright": 0xFF53, "arrowdown": 0xFF54,
    "space": 0x20, "minus": 0x2D, "plus": 0x2B, "equal": 0x3D, "comma": 0x2C, "period": 0x2E, "slash": 0x2F,
    "menu": 0xFF67, "print": 0xFF61, "capslock": 0xFFE5,
    **{f"f{n}": 0xFFBE + n - 1 for n in range(1, 13)},
}
MODIFIERS = {"ctrl": 0xFFE3, "control": 0xFFE3, "shift": 0xFFE1, "alt": 0xFFE9, "option": 0xFFE9,
             "super": 0xFFEB, "meta": 0xFFEB, "cmd": 0xFFEB, "win": 0xFFEB}
# xdotool's names for the same keys.
XDO_NAMES = {0xFF0D: "Return", 0xFF1B: "Escape", 0xFF09: "Tab", 0xFF08: "BackSpace", 0xFFFF: "Delete",
             0xFF63: "Insert", 0xFF50: "Home", 0xFF57: "End", 0xFF55: "Prior", 0xFF56: "Next", 0xFF51: "Left",
             0xFF52: "Up", 0xFF53: "Right", 0xFF54: "Down", 0x20: "space", 0xFF67: "Menu", 0xFF61: "Print",
             0xFFE5: "Caps_Lock", 0xFFE3: "ctrl", 0xFFE1: "shift", 0xFFE9: "alt", 0xFFEB: "super",
             **{0xFFBE + n - 1: f"F{n}" for n in range(1, 13)}}


def char_keysym(char: str) -> int:
    """The keysym that types one character: Latin-1 as itself, anything else (Arabic) as 0x01000000 + code point."""
    if char == "\n":
        return KEYSYMS["return"]
    if char == "\t":
        return KEYSYMS["tab"]
    code = ord(char)
    return code if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF else 0x01000000 + code


def _key(name: str) -> int:
    word = name.strip()
    if len(word) == 1:
        return char_keysym(word.lower())          # "L" in ctrl+L is the L key, not Shift+L
    found = MODIFIERS.get(word.lower()) or KEYSYMS.get(word.lower().replace("_", ""))
    if found is None:
        raise PointerError(f"unknown key {name!r}")
    return found


def combo_keysyms(combo: str) -> list[int]:
    """ "ctrl+shift+Tab" as keysyms, modifiers first, in the order they are pressed."""
    parts = combo.split("+") if combo not in ("+",) else ["plus"]
    if not combo or any(not p.strip() for p in parts):
        raise PointerError(f"unknown key {combo!r}")
    return [_key(p) for p in parts]


# ---------------------------------------------------------------------- one session for a run (#18)

BUTTONS = {"left": BTN_LEFT, "right": BTN_LEFT + 1, "middle": BTN_LEFT + 2}


class PortalRemote:
    """The pointer and the keyboard through one RemoteDesktop session, held for a whole operator run (with):
    GNOME asks once, the restore token is kept as for a click, and the session is always closed. Points are in
    pixels of a screenshot of shot_width by shot_height."""

    def __init__(self, shot_width: int, shot_height: int, bus=None, copy=None):
        self.shot = (shot_width, shot_height)
        self.bus, self.session, self.streams = bus, None, []
        self.copy = copy            # puts text on the clipboard, for text the keyboard layout lacks

    def __enter__(self) -> PortalRemote:
        self.bus = self.bus or JeepneyBus()
        try:
            created = self.bus.request("RemoteDesktop", "CreateSession", "a{sv}",
                                       ({"session_handle_token": ("s", "rafiq" + secrets.token_hex(8))},))
            handle = created.get("session_handle")
            if not (isinstance(handle, tuple) and len(handle) == 2 and isinstance(handle[1], str)):
                raise PointerError("the remote desktop portal gave no session")
            self.session = handle[1]
            devices = {"types": ("u", POINTER | KEYBOARD), "persist_mode": ("u", PERSIST_UNTIL_REVOKED)}
            token = load_token(OPERATOR_TOKEN)
            if token:
                devices["restore_token"] = ("s", token)
            self.bus.request("RemoteDesktop", "SelectDevices", "oa{sv}", (self.session, devices))
            self.bus.request("ScreenCast", "SelectSources", "oa{sv}",
                             (self.session, {"types": ("u", MONITOR), "multiple": ("b", True)}))
            started = self.bus.request("RemoteDesktop", "Start", "osa{sv}", (self.session, "", {}),
                                       timeout=PORTAL_SECONDS)
            granted = int(started.get("devices", ("u", 0))[1])
            if "restore_token" in started and granted & (POINTER | KEYBOARD) == POINTER | KEYBOARD:
                save_token(str(started["restore_token"][1]), OPERATOR_TOKEN)
            if granted & (POINTER | KEYBOARD) != POINTER | KEYBOARD:
                raise PointerError("remote interaction was allowed without the pointer and the keyboard")
            self.streams = _streams(started.get("streams"))
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self.bus is None:
            return
        if self.session is not None:
            self.bus.close_session(self.session)
        else:
            self.bus.close()
        self.session = None

    def _call(self, method: str, signature: str, *args) -> None:
        self.bus.call("RemoteDesktop", method, signature, (self.session, {}, *args))

    def move(self, x: float, y: float) -> None:
        spot = to_stream(x, y, *self.shot, self.streams)
        if spot is None:
            raise PointerError("that point is on no monitor, or the monitors' scales differ")
        stream, sx, sy = spot
        self._call("NotifyPointerMotionAbsolute", "oa{sv}udd", stream.node, float(sx), float(sy))

    def _button(self, button: str, down: bool) -> None:
        self._call("NotifyPointerButton", "oa{sv}iu", BUTTONS[button], 1 if down else 0)

    def _keysym(self, sym: int, down: bool) -> None:
        self._call("NotifyKeyboardKeysym", "oa{sv}iu", sym, 1 if down else 0)

    def _hold(self, mods) -> list[int]:
        unknown = [m for m in mods if m not in MODIFIERS]
        if unknown:
            raise PointerError(f"unknown modifier {unknown[0]!r}")
        syms = [MODIFIERS[m] for m in mods]
        for sym in syms:
            self._keysym(sym, True)
        return syms

    def _release(self, syms: list[int]) -> None:
        for sym in reversed(syms):
            self._keysym(sym, False)

    def click(self, x: float, y: float, button: str = "left", count: int = 1, mods=()) -> None:
        self.move(x, y)
        held = self._hold(mods)
        try:
            for _ in range(count):
                self._button(button, True)
                self._button(button, False)
        finally:
            self._release(held)

    def mouse(self, down: bool) -> None:
        self._button("left", down)

    def drag(self, path, mods=()) -> None:
        self.move(*path[0])
        held = self._hold(mods)
        self._button("left", True)
        try:
            for x, y in path[1:]:
                self.move(x, y)
        finally:
            self._button("left", False)
            self._release(held)

    def scroll(self, x: float, y: float, dx: int = 0, dy: int = 0) -> None:
        self.move(x, y)
        if dy:
            self._call("NotifyPointerAxisDiscrete", "oa{sv}ui", 0, int(dy))
        if dx:
            self._call("NotifyPointerAxisDiscrete", "oa{sv}ui", 1, int(dx))

    def key(self, combo: str, repeat: int = 1) -> None:
        syms = combo_keysyms(combo)
        for _ in range(repeat):
            for sym in syms:
                self._keysym(sym, True)
            for sym in reversed(syms):
                self._keysym(sym, False)

    def hold(self, combo: str, seconds: float, sleep=time.sleep) -> None:
        syms = combo_keysyms(combo)
        for sym in syms:
            self._keysym(sym, True)
        try:
            sleep(seconds)
        finally:
            for sym in reversed(syms):
                self._keysym(sym, False)

    def type(self, text: str) -> str | None:
        """Typed key by key. GNOME drops a keysym the active keyboard layout lacks (Arabic on a US layout), and
        says nothing, so text that isn't plain ASCII goes through the clipboard and Ctrl+V instead."""
        if not text.isascii():
            if (self.copy or wl_copy)(text):
                self.key("ctrl+v")
                return "OK (pasted through the clipboard)"
            note = "OK, but check the screen: letters the keyboard layout lacks may not have been typed"
        else:
            note = None
        for char in text:
            sym = char_keysym(char)
            self._keysym(sym, True)
            self._keysym(sym, False)
        return note


def wl_copy(text: str, which: Callable[[str], str | None] = shutil.which, run=None) -> bool:
    """The text on the Wayland clipboard (wl-clipboard); False when wl-copy isn't there or fails."""
    if not which("wl-copy"):
        return False
    try:
        done = (run or subprocess.run)(["wl-copy", "--", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=XDOTOOL_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


class XdoRemote:
    """The same on X11, through xdotool: argument lists with a timeout, never a shell."""

    def __init__(self, run=None):
        self.run = run

    def __enter__(self) -> XdoRemote:
        return self

    def __exit__(self, *exc) -> None:
        pass

    def _xdo(self, *args: str) -> None:
        try:
            done = (self.run or subprocess.run)(["xdotool", *args], capture_output=True, text=True,
                                                timeout=XDOTOOL_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PointerError(f"xdotool failed ({type(exc).__name__})") from exc
        if done.returncode != 0:
            raise PointerError("xdotool failed")

    @staticmethod
    def _names(combo: str) -> str:
        """The combo in xdotool's names, checked first: an unknown key never reaches xdotool."""
        names = []
        parts = combo.split("+") if combo != "+" else ["plus"]
        for part, sym in zip(parts, combo_keysyms(combo), strict=True):
            names.append(XDO_NAMES.get(sym) or (part.lower() if len(part.strip()) == 1 else part))
        return "+".join(names)

    @staticmethod
    def _mods(mods) -> list[str]:
        """xdotool's names for the modifiers, all checked before any is pressed."""
        unknown = [m for m in mods if m not in MODIFIERS]
        if unknown:
            raise PointerError(f"unknown modifier {unknown[0]!r}")
        return [XDO_NAMES[MODIFIERS[m]] for m in mods]

    def move(self, x: float, y: float) -> None:
        self._xdo("mousemove", "--sync", str(round(x)), str(round(y)))

    def click(self, x: float, y: float, button: str = "left", count: int = 1, mods=()) -> None:
        number = {"left": "1", "middle": "2", "right": "3"}.get(button)
        if number is None:
            raise PointerError(f"unknown button {button!r}")
        names = self._mods(mods)
        pressed = []
        try:
            for name in names:
                self._xdo("keydown", name)
                pressed.append(name)
            self._xdo("mousemove", "--sync", str(round(x)), str(round(y)), "click", "--repeat", str(count), number)
        finally:
            for name in reversed(pressed):
                self._xdo("keyup", name)

    def mouse(self, down: bool) -> None:
        self._xdo("mousedown" if down else "mouseup", "1")

    def drag(self, path, mods=()) -> None:
        self.move(*path[0])
        self._xdo("mousedown", "1")
        try:
            for x, y in path[1:]:
                self.move(x, y)
        finally:
            self._xdo("mouseup", "1")

    def scroll(self, x: float, y: float, dx: int = 0, dy: int = 0) -> None:
        for steps, back, forward in ((dy, "4", "5"), (dx, "6", "7")):
            if steps:
                self._xdo("mousemove", "--sync", str(round(x)), str(round(y)), "click", "--repeat",
                          str(abs(int(steps))), forward if steps > 0 else back)

    def key(self, combo: str, repeat: int = 1) -> None:
        self._xdo("key", "--clearmodifiers", *(["--repeat", str(repeat)] if repeat > 1 else []), self._names(combo))

    def hold(self, combo: str, seconds: float, sleep=time.sleep) -> None:
        names = self._names(combo)
        self._xdo("keydown", names)
        try:
            sleep(seconds)
        finally:
            self._xdo("keyup", names)

    def type(self, text: str) -> None:
        self._xdo("type", "--clearmodifiers", "--", text)      # xdotool types any character, layout or not


def open_remote(shot_width: int, shot_height: int, env: Mapping[str, str] | None = None,
                which: Callable[[str], str | None] = shutil.which):
    """The input for an operator run on this desktop session, to use with `with`."""
    how = method(env, which)
    if how == "portal":
        return PortalRemote(shot_width, shot_height)
    if how == "xdotool":
        return XdoRemote()
    raise PointerError("no way to control this desktop: on X11 install xdotool; on Wayland, run in a desktop session")
