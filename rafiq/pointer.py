"""The pointer: one left click at a point of the screen, for click_on_screen (#26) and the operator (#18).

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from . import config

PORTAL_SECONDS = 60          # time for the user to answer GNOME's "allow remote interaction?" the first time
REQUEST_SECONDS = 10         # the other portal requests answer at once
XDOTOOL_SECONDS = 5
BTN_LEFT = 272               # Linux evdev code of the left button
POINTER = 2                  # RemoteDesktop device type
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

def _token_path():
    return config.home() / "remote_desktop.json"


def load_token() -> str:
    try:
        token = json.loads(_token_path().read_text(encoding="utf-8")).get("restore_token", "")
    except (OSError, ValueError, AttributeError):
        return ""
    return token if isinstance(token, str) else ""


def save_token(token: str) -> None:
    """Readable only by the user, and replaced in one step: it lets Rafiq click without asking GNOME again."""
    path = _token_path()
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
