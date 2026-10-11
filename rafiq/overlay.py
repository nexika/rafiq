"""The answer on screen: a small box with what Rafiq said, and cards for the weather, timers and music
that the answer used. "Show it again" brings back the last one.

On GNOME Wayland an app cannot place a window of its own above the others, so the box is a desktop
notification: the desktop draws it, it never takes the focus, and each answer replaces the last one
instead of piling up. Other ways to draw it (a GNOME Shell extension, a layer-shell window on other
Wayland desktops) can be added as more backends with the same show(text, cards) method.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

NOTIFY_TIMEOUT = 3          # seconds to wait for the notification service


@dataclass(frozen=True)
class Card:
    kind: str               # "weather", "timer" or "music"
    text: str


# Tools whose result is worth a card. Music tools don't exist yet; any tool named like one gets a card.
CARD_TOOLS = {"weather": "weather", "set_timer": "timer", "set_reminder": "timer"}
MUSIC_PREFIXES = ("music", "media", "play_", "spotify")
TITLES = {"weather": "Weather", "timer": "Timer", "music": "Music"}


def card_for(name: str, result: str) -> Card | None:
    """The card for one tool call, or None: errors, declined actions and other tools have none."""
    kind = CARD_TOOLS.get(name) or ("music" if name.startswith(MUSIC_PREFIXES) else None)
    if kind is None or result.startswith(("error", "declined", "The user said no")):
        return None
    first = result.strip().splitlines()[0] if result.strip() else ""
    return Card(kind, first.removeprefix("set: ")) if first else None


class Backend(Protocol):
    def show(self, text: str, cards: list[Card]) -> None: ...


def _gvariant_string(text: str) -> str:
    """A GVariant string literal, as gdbus reads its arguments."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


class NotifyBackend:
    """Desktop notifications (org.freedesktop.Notifications) through gdbus, which comes with GLib."""

    def __init__(self, seconds: float = 8, run: Callable = subprocess.run):
        self.seconds, self.run = seconds, run
        self.last_id = 0                 # the notification to replace, so one box updates in place
        self.lock = threading.Lock()

    def show(self, text: str, cards: list[Card]) -> None:
        body = html.escape(text, quote=False)
        for card in cards:
            body += f"\n<b>{TITLES.get(card.kind, card.kind)}</b>: {html.escape(card.text, quote=False)}"
        with self.lock:
            argv = ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                    "--object-path", "/org/freedesktop/Notifications",
                    "--method", "org.freedesktop.Notifications.Notify",
                    "'Rafiq'", str(self.last_id), "'audio-input-microphone'", "'Rafiq'", _gvariant_string(body),
                    "@as []", "{'transient': <true>, 'urgency': <byte 0>}", str(int(self.seconds * 1000))]
            try:
                done = self.run(argv, capture_output=True, text=True, timeout=NOTIFY_TIMEOUT, check=True)
            except (OSError, subprocess.SubprocessError):
                return                   # no notification service: the panel still shows the answer
            found = re.search(r"uint32 (\d+)", done.stdout or "")
            if found:
                self.last_id = int(found.group(1))


def pick_backend(cfg: dict, which: Callable = shutil.which) -> Backend | None:
    """How to show answers on this desktop: [overlay] display = "auto", "notify" or "off"."""
    settings = cfg.get("overlay", {})
    display = settings.get("display", "auto")
    if display == "off":
        return None
    if display in ("auto", "notify") and which("gdbus"):
        return NotifyBackend(seconds=float(settings.get("seconds", 8)))
    return None


def _in_thread(fn: Callable, *args) -> None:
    threading.Thread(target=fn, args=args, name="rafiq-overlay", daemon=True).start()


class Overlay:
    """Watches Rafiq's events: tool results become cards, and each thing Rafiq says is shown with the
    cards gathered since the last one. Showing happens off the listening thread."""

    def __init__(self, backend: Backend | None, spawn: Callable = _in_thread):
        self.backend, self.spawn = backend, spawn
        self.pending: list[Card] = []
        self.last: tuple[str, list[Card]] | None = None

    def feed(self, kind: str, data: dict) -> None:
        if kind == "tool":
            card = card_for(data.get("name", ""), str(data.get("result", "")))
            if card:
                self.pending.append(card)
        elif kind == "said":
            cards = self.pending + [Card(c["kind"], c["text"]) for c in data.get("cards") or []]
            self.pending = []
            self.last = (data["text"], cards)
            self._show(*self.last)

    def show_again(self) -> bool:
        if self.last is None:
            return False
        self._show(*self.last)
        return True

    def _show(self, text: str, cards: list[Card]) -> None:
        if self.backend is not None:
            self.spawn(self.backend.show, text, list(cards))
