"""The desktop, per platform (#192): which program opens a page, shows a notification, sets the volume, controls
the player, switches the output, locks, powers off, reads the clipboard, presses keys or types, on Linux and on
macOS. Callers ask here instead of naming a Linux program, and the Linux commands stay what they were.

On macOS most of it is AppleScript through osascript. Words the user said reach a script only as its arguments
(`item 1 of argv`), never inside the script's text, so nothing said can become a command. Pressing keys and
typing go through System Events, which needs the Accessibility permission (System Settings > Privacy &
Security > Accessibility); switching outputs needs SwitchAudioSource (brew install switchaudio-osx).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

OSASCRIPT_SECONDS = 10
MAC_APP_DIRS = ["/Applications", "/Applications/Utilities", "/System/Applications",
                "/System/Applications/Utilities", "~/Applications"]


def system() -> str:
    """"mac" or "linux": everything that isn't macOS is treated as Linux, as before."""
    return "mac" if sys.platform == "darwin" else "linux"


def run(args: list[str], timeout: float = OSASCRIPT_SECONDS, **kwargs) -> subprocess.CompletedProcess:
    """An argument list with a timeout, never a shell; output captured unless the caller says where it goes."""
    if "stdout" not in kwargs and "stderr" not in kwargs:
        kwargs["capture_output"] = True
    return subprocess.run(args, text=True, timeout=timeout, check=False, **kwargs)


def _runner(given: Callable | None) -> Callable:
    """The runner a caller gave, else this module's run (looked up now, so tests can replace it)."""
    return given or globals()["run"]


def _script(lines: list[str]) -> list[str]:
    """osascript with AppleScript lines inside `on run argv`. "--" ends osascript's own options, so an argument
    after it that starts with "-" is still an argument."""
    command = ["osascript"]
    for line in ["on run argv", *lines, "end run"]:
        command += ["-e", line]
    return command + ["--"]


def osascript(lines: list[str], *args: str, run: Callable | None = None,
              timeout: float = OSASCRIPT_SECONDS) -> subprocess.CompletedProcess:
    """Run AppleScript lines, with `args` as argv: the only way words reach a script."""
    return _runner(run)(_script(lines) + list(args), timeout=timeout)


def open_command(target: str) -> list[str]:
    """What opens a web address or a link (steam://) with the user's default app."""
    return ["open", target] if system() == "mac" else ["xdg-open", target]


def notify(text: str, run: Callable | None = None, which: Callable[[str], str | None] = shutil.which) -> None:
    run = _runner(run)
    if system() == "mac":
        osascript(['display notification (item 1 of argv) with title "Rafiq"'], text[:300], run=run)
    elif which("notify-send"):
        try:
            run(["notify-send", "Rafiq", text[:300]], timeout=5)
        except (OSError, subprocess.SubprocessError):     # a notification that can't be shown is no failure
            pass


# ---------------------------------------------------------------------- volume (macOS)

class NoVolume(Exception):
    """The output has no volume of its own (HDMI, DisplayPort, some USB): macOS says "missing value"."""


def mac_volume(run: Callable | None = None) -> tuple[int, bool]:
    """The output volume in percent, and whether it is muted."""
    done = osascript(["set s to get volume settings",
                      "return (output volume of s as text) & \",\" & (output muted of s as text)"], run=run)
    level, _, muted = (done.stdout or "").strip().partition(",")
    if not level.strip().isdigit():
        raise NoVolume("this output has no volume control of its own; change it on the device")
    return int(level), muted.strip() == "true"


def set_mac_volume(level: int | None = None, mute: bool | None = None, run: Callable | None = None) -> None:
    if mute is not None:
        osascript(["set volume output muted ((item 1 of argv) is \"true\")"], "true" if mute else "false", run=run)
    if level is not None:
        osascript(["set volume output volume ((item 1 of argv) as integer)"], str(int(level)), run=run)


# ---------------------------------------------------------------------- media (macOS)

# The players AppleScript can drive, and how each says each action. Only these names reach a script's text.
PLAYERS = ("Spotify", "Music")
MAC_MEDIA = {"play": "play", "pause": "pause", "toggle": "playpause", "next": "next track",
             "previous": "previous track", "stop": "pause"}
MEDIA_DONE = {"play": "playing", "pause": "paused", "toggle": "toggled", "next": "skipped to the next on",
              "previous": "went back on", "stop": "stopped"}


def _player_state(player: str, run: Callable | None) -> str:
    """"playing", "paused", "stopped", or "" when the app isn't running. pgrep looks first: a script that names
    an app which isn't installed could make macOS ask "Where is Spotify?"."""
    try:
        running = _runner(run)(["pgrep", "-x", player], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    if running.returncode != 0 or not (running.stdout or "").strip():
        return ""
    done = osascript([f'tell application "{player}" to return player state as text'], run=run)
    return (done.stdout or "").strip().lower()


def mac_media(action: str, run: Callable | None = None) -> str:
    """Play, pause, skip or say what plays in Spotify or Music: the one playing, else the one paused."""
    if action not in MAC_MEDIA and action != "status":
        return f"error: action is one of {', '.join([*MAC_MEDIA, 'status'])}"
    states = [(p, _player_state(p, run)) for p in PLAYERS]
    found = next(((p, s) for wanted in ("playing", "paused") for p, s in states if s == wanted), None)
    if found is None:
        return "error: nothing is playing music or video"
    player, state = found
    if action == "status":
        done = osascript([f'tell application "{player}" to return (artist of current track) & " - " & '
                          "(name of current track)"], run=run)
        title = (done.stdout or "").strip().strip(" -")
        return f"{player} is {state}" + (f": {title}" if title else "")
    done = osascript([f'tell application "{player}" to {MAC_MEDIA[action]}'], run=run)
    if done.returncode != 0:
        return f"error: {(done.stderr or '').strip()[:200]}"
    return f"{MEDIA_DONE[action]} {player}"


# ---------------------------------------------------------------------- outputs (macOS)

@dataclass
class Output:
    id: int
    name: str
    default: bool


def mac_outputs(run: Callable | None = None) -> list[Output]:
    run = _runner(run)
    names = [n.strip() for n in (run(["SwitchAudioSource", "-a", "-t", "output"]).stdout or "").splitlines()
             if n.strip()]
    current = (run(["SwitchAudioSource", "-c", "-t", "output"]).stdout or "").strip()
    return [Output(i, name, name == current) for i, name in enumerate(names)]


def set_mac_output(name: str, run: Callable | None = None) -> subprocess.CompletedProcess:
    """The name is one SwitchAudioSource listed, passed as one argument."""
    return _runner(run)(["SwitchAudioSource", "-t", "output", "-s", name])


# ---------------------------------------------------------------------- lock and power

def lock_command() -> list[str]:
    if system() == "mac":
        return ["pmset", "displaysleepnow"]        # locks when "require password after sleep" is on, as by default
    session = os.environ.get("XDG_SESSION_ID")
    return ["loginctl", "lock-session"] + ([session] if session else [])


_MAC_POWER = {"shutdown": "shut down", "restart": "restart", "suspend": "sleep"}
_LINUX_POWER = {"shutdown": "poweroff", "restart": "reboot", "suspend": "suspend"}


def power_command(action: str) -> list[str]:
    if system() == "mac":
        return _script([f'tell application "System Events" to {_MAC_POWER[action]}'])
    return ["systemctl", _LINUX_POWER[action]]


# ---------------------------------------------------------------------- clipboard

def clipboard_commands() -> tuple[list[str], list[str]] | None:
    """(read, write) for this desktop session, or None when there is none."""
    if system() == "mac":
        return ["pbpaste"], ["pbcopy"]
    if os.environ.get("WAYLAND_DISPLAY"):
        return ["wl-paste", "--no-newline", "--type", "text"], ["wl-copy"]
    if os.environ.get("DISPLAY"):
        return ["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard", "-i"]
    return None


# ---------------------------------------------------------------------- keys and typing (macOS)

# The gestures' key presses: macOS key codes (Tab 48, = 24, - 27), and the modifiers; zoom is Command there.
MAC_KEYS = {"next_tab": (48, "control down"), "previous_tab": (48, "control down, shift down"),
            "zoom_in": (24, "command down"), "zoom_out": (27, "command down")}


def key_command(name: str) -> list[str]:
    code, modifiers = MAC_KEYS[name]
    return _script([f'tell application "System Events" to key code {code} using {{{modifiers}}}'])


def type_command() -> list[str]:
    """Type the first argument, as if on the keyboard."""
    return _script(['tell application "System Events" to keystroke (item 1 of argv)'])


def paste_command() -> list[str]:
    # Key code 9 is the V key whatever the layout: keystroke "v" may not be Command-V with an Arabic layout.
    return _script(['tell application "System Events" to key code 9 using {command down}'])


# ---------------------------------------------------------------------- apps (macOS)

def mac_apps(dirs: list[str] | None = None) -> list[dict]:
    """Every .app bundle in the usual folders, as open_app's entries: its name, and `open -a` to start it."""
    found, seen = [], set()
    for folder in dirs or MAC_APP_DIRS:
        for path in sorted(Path(folder).expanduser().glob("*.app")):
            name = path.stem
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            found.append({"id": name, "name": name, "names": [name.lower()], "keywords": [],
                          "launch": ["open", "-a", str(path)]})
    return found
