"""Hand gestures, Rafiq's side: the camera tracker runs in its own process (rafiq/tracker.py) and sends
only labels ("open_palm", "swipe_left") to the panel's /api/gesture; this turns a label into an action.

- Open palm pauses what is playing, thumbs-up plays, a fist cuts Rafiq off mid-sentence.
- One finger arms tab mode and two fingers audio mode for a few seconds; a swipe then changes the tab
  or the volume.
- Two hands moving apart or together zoom in or out.

Only the actions in ACTIONS can happen, none of them risky; any other label is ignored. The tracker
proves it is the one Rafiq started with a token made for that start, passed in its environment (not
on its command line, which other users can read), and no image ever leaves it.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Protocol

from . import config

MODE_SECONDS = 5.0           # how long a finger count keeps its mode armed
VOLUME_STEP = 10

DIRECT = {"open_palm": "pause", "thumbs_up": "play", "fist": "stop_speech",
          "zoom_in": "zoom_in", "zoom_out": "zoom_out"}
MODES = {"fingers_1": "tabs", "fingers_2": "audio"}
IN_MODE = {("tabs", "swipe_right"): "next_tab", ("tabs", "swipe_left"): "previous_tab",
           ("audio", "swipe_right"): "volume_up", ("audio", "swipe_left"): "volume_down"}
ACTIONS = frozenset(DIRECT.values()) | frozenset(IN_MODE.values())

# Key presses, by action. xdotool takes key names; ydotool (Wayland) takes Linux key codes.
KEYS = {"next_tab": ("ctrl", "Tab"), "previous_tab": ("ctrl", "shift", "Tab"),
        "zoom_in": ("ctrl", "plus"), "zoom_out": ("ctrl", "minus")}
KEYCODES = {"ctrl": 29, "shift": 42, "Tab": 15, "plus": 13, "minus": 12}   # plus is the = key


def settings(cfg: dict) -> dict:
    """[gestures] from the config, with the timing from `rafiq gestures calibrate` on top."""
    out = dict(cfg.get("gestures") or config.DEFAULTS["gestures"])
    try:
        saved = json.loads((config.data_dir() / "gestures.json").read_text())
        out.update({k: float(saved[k]) for k in ("hold_seconds", "cooldown_seconds") if k in saved})
    except (OSError, ValueError, TypeError):
        pass
    return out


def save_calibration(hold_seconds: float, cooldown_seconds: float) -> None:
    path = config.data_dir() / "gestures.json"
    timing = {"hold_seconds": round(hold_seconds, 2), "cooldown_seconds": round(cooldown_seconds, 2)}
    path.write_text(json.dumps(timing))


class Desktop(Protocol):
    def media(self, command: str) -> None: ...
    def keys(self, name: str) -> None: ...
    def volume(self, change: int) -> None: ...


def _run(args: list[str]) -> None:
    subprocess.run(args, capture_output=True, timeout=5, check=False)


class SystemDesktop:
    """The real computer: media players through MPRIS (playerctl, which works on Wayland too), key
    presses through ydotool on Wayland or xdotool on X11, the volume through PipeWire."""

    def __init__(self, run: Callable[[list[str]], object] = _run):
        self.run = run

    def media(self, command: str) -> None:
        self.run(["playerctl", command])

    def keys(self, name: str) -> None:
        combo = KEYS[name]
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("ydotool"):
            codes = [KEYCODES[k] for k in combo]
            self.run(["ydotool", "key", *[f"{c}:1" for c in codes], *[f"{c}:0" for c in reversed(codes)]])
        else:
            self.run(["xdotool", "key", "+".join(combo)])

    def volume(self, change: int) -> None:
        from .tools.pc import volume

        volume(change=change)


class GestureControl:
    def __init__(self, desktop: Desktop, stop_speech: Callable[[], None], publish: Callable[[str, dict], None],
                 url: str | None = None, options: dict | None = None, spawn: Callable = subprocess.Popen,
                 clock: Callable[[], float] = time.monotonic):
        self.desktop, self.stop_speech, self.publish = desktop, stop_speech, publish
        self.url = url                       # the panel's /api/gesture, once the panel is serving
        self.options = options or dict(config.DEFAULTS["gestures"])
        self.spawn, self.clock = spawn, clock
        self.token: str | None = None
        self.process = None
        self.mode: str | None = None
        self.mode_until = 0.0

    @property
    def enabled(self) -> bool:
        return self.process is not None and self.process.poll() is None   # (a tracker that died is off)

    def token_ok(self, given: str | None) -> bool:
        return bool(given) and self.token is not None and secrets.compare_digest(given, self.token)

    def start(self) -> str:
        if self.enabled:
            return "hand gestures are on"
        if not self.url:
            return "error: hand gestures need the panel (start Rafiq without --no-panel)"
        self.token = secrets.token_urlsafe(32)
        args = [sys.executable, "-m", "rafiq.tracker", "--post", self.url,
                "--hold", str(self.options["hold_seconds"]), "--cooldown", str(self.options["cooldown_seconds"]),
                "--camera", str(self.options["camera"])]
        self.process = self.spawn(args, env={**os.environ, "RAFIQ_GESTURE_TOKEN": self.token},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.publish("gestures", {"on": True})
        return "hand gestures are on"

    def stop(self) -> str:
        process, self.process, self.token, self.mode = self.process, None, None, None
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
            self.publish("gestures", {"on": False})
        return "hand gestures are off"

    def handle(self, label) -> str | None:
        """Do what a label asks, if it is allowed; the action done, or None."""
        if not self.enabled or not isinstance(label, str):
            return None
        now = self.clock()
        if self.mode and now > self.mode_until:
            self.mode = None
        if label in MODES:
            self.mode, self.mode_until = MODES[label], now + MODE_SECONDS
            action = f"mode:{self.mode}"
        else:
            action = DIRECT.get(label) or IN_MODE.get((self.mode, label))
            if (self.mode, label) in IN_MODE:   # each swipe keeps the mode armed a little longer
                self.mode_until = now + MODE_SECONDS
            if action not in ACTIONS:
                return None
            self._do(action)
        self.publish("gesture", {"gesture": label, "action": action})
        return action

    def _do(self, action: str) -> None:
        if action == "stop_speech":
            self.stop_speech()
        elif action in ("pause", "play"):
            self.desktop.media(action)
        elif action in ("volume_up", "volume_down"):
            self.desktop.volume(VOLUME_STEP if action == "volume_up" else -VOLUME_STEP)
        else:
            self.desktop.keys(action)
