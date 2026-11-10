"""Escape ends an operator run (#18). On Wayland a program never sees keys pressed in other windows, so Escape is
bound through the XDG GlobalShortcuts portal, only while a run goes: GNOME asks the user once to allow it, and
the binding is dropped when the run ends, so Escape works as usual the rest of the time. Without the portal (X11,
an older desktop), the run still ends with the Stop button in the widget and the dashboard. On Windows any program
may read the keyboard's state, so Escape is polled instead, only while a run goes (#242).
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable

from . import pointer

SHORTCUT = "stop"
BIND_SECONDS = 60          # the first time, GNOME asks the user whether Rafiq may have this shortcut
POLL_SECONDS = 0.05        # Windows: how often Escape is looked at; a press is longer than this


class EscapeWatch:
    """With start() and stop() around a run; `pressed` is set when the user presses Escape."""

    def __init__(self, bus=None, bus_factory: Callable | None = None):
        self.bus, self.bus_factory = bus, bus_factory
        self.pressed, self._stop = threading.Event(), threading.Event()
        self.session: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Bind Escape; False when it can't be (the run then relies on the Stop button)."""
        from . import desktop

        if desktop.system() == "windows":
            self._thread = threading.Thread(target=self._poll, name="rafiq-escape", daemon=True)
            self._thread.start()
            return True
        try:
            self.bus = self.bus or (self.bus_factory or pointer.JeepneyBus)()
            created = self.bus.request("GlobalShortcuts", "CreateSession", "a{sv}",
                                       ({"session_handle_token": ("s", "rafiq" + secrets.token_hex(8))},))
            handle = created.get("session_handle")
            if not (isinstance(handle, tuple) and len(handle) == 2 and isinstance(handle[1], str)):
                raise pointer.PointerError("the shortcuts portal gave no session")
            self.session = handle[1]
            shortcut = (SHORTCUT, {"description": ("s", "Stop Rafiq controlling the computer"),
                                   "preferred_trigger": ("s", "Escape")})
            bound = self.bus.request("GlobalShortcuts", "BindShortcuts", "oa(sa{sv})sa{sv}",
                                     (self.session, [shortcut], "", {}), timeout=BIND_SECONDS)
            # The user may leave it unassigned in GNOME's dialog: then Escape does nothing, and the run says Stop.
            given = (bound.get("shortcuts") or ("", []))[1]
            if not any(isinstance(item, tuple) and item and item[0] == SHORTCUT for item in given):
                raise pointer.PointerError("the shortcut wasn't bound")
        except (pointer.PointerError, OSError, KeyError, ValueError, TypeError):
            self._close()
            return False
        self._thread = threading.Thread(target=self.bus.listen, name="rafiq-escape", daemon=True,
                                        args=("GlobalShortcuts", "Activated", self._activated, self._stop))
        self._thread.start()
        return True

    def _poll(self) -> None:
        """Windows: a press that starts during the run. An Escape already held when it began doesn't count, since
        it may be the end of the user's last key press."""
        from . import desktop

        try:
            was = desktop.escape_down()
            while not self._stop.wait(POLL_SECONDS):
                now = desktop.escape_down()
                if now and not was:
                    self.pressed.set()
                    return
                was = now
        except OSError:                                     # the run still ends with the Stop button
            return

    def _activated(self, body) -> None:
        """Activated(session_handle, shortcut_id, timestamp, options): only this run's own shortcut counts."""
        if isinstance(body, tuple) and len(body) >= 2 and body[0] == self.session and body[1] == SHORTCUT:
            self.pressed.set()

    def _close(self) -> None:
        if self.bus is None:
            return
        try:
            if self.session is not None:
                self.bus.close_session(self.session)       # closing the session drops the binding
            else:
                self.bus.close()
        except Exception:                                   # best effort: the run is over either way
            pass
        self.session, self.bus = None, None

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._close()
