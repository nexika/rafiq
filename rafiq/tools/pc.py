"""This computer: open apps, volume, how it is doing, lock it, power off. Linux with PipeWire (wpctl)
and a desktop that reads .desktop files (GNOME, KDE, ...).

Commands run as argument lists, never through a shell: no word the user says can become a command.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from configparser import ConfigParser
from pathlib import Path

from . import tool

APP_DIRS = ["/usr/share/applications", "/usr/local/share/applications", "~/.local/share/applications",
            "/var/lib/flatpak/exports/share/applications", "~/.local/share/flatpak/exports/share/applications",
            "/var/lib/snapd/desktop/applications"]


def _run(args: list[str], timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def desktop_entries(dirs: list[str] | None = None) -> list[dict]:
    """Every app the menu shows: its id, and the names it goes by in every language."""
    found, seen = [], set()
    for folder in dirs or APP_DIRS:
        for path in sorted(Path(folder).expanduser().glob("*.desktop")):
            if path.name in seen:
                continue
            parser = ConfigParser(interpolation=None, strict=False)
            parser.optionxform = str
            try:
                parser.read(path, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except Exception:
                continue
            if entry.get("NoDisplay", "").lower() == "true" or entry.get("Type", "Application") != "Application":
                continue
            names = [v for k, v in entry.items() if k.startswith(("Name", "GenericName"))]
            keywords = [w for k, v in entry.items() if k.startswith("Keywords") for w in v.split(";") if w]
            seen.add(path.name)
            found.append({"id": path.stem, "name": entry.get("Name", path.stem),
                          "names": [n.lower() for n in names], "keywords": [k.lower() for k in keywords]})
    return found


def find_app(query: str, entries: list[dict]) -> dict | None:
    """The best match for a spoken app name: exact name, then a name starting with it, then a name or
    keyword containing it."""
    q = query.lower().strip()
    if not q:
        return None
    for test in (lambda e: q in e["names"] or q == e["id"].lower(),
                 lambda e: any(n.startswith(q) for n in e["names"]),
                 lambda e: any(q in n for n in e["names"]) or q in e["id"].lower(),
                 lambda e: q in e["keywords"]):
        matches = [e for e in entries if test(e)]
        if matches:
            return min(matches, key=lambda e: len(e["name"]))
    return None


@tool("open_app", "Open an application by name, e.g. 'Firefox', 'Files', 'Calculator', 'Terminal'.",
      {"name": {"type": "string", "description": "the app's name, in English where possible"}},
      required=("name",), level="act")
def open_app(name: str) -> str:
    app = find_app(name, desktop_entries())
    if not app:
        return f"error: no installed app matches {name!r}"
    subprocess.Popen(["gtk-launch", app["id"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return f"opened {app['name']}"


@tool("list_apps", "Names of the installed applications, to find the right one to open.",
      {"filter": {"type": "string", "description": "only names containing this"}},
      level="safe", expose=True, card="list")
def list_apps(filter: str = "") -> str:  # noqa: A002
    names = sorted({e["name"] for e in desktop_entries() if filter.lower() in e["name"].lower()})
    return ", ".join(names[:80]) or "none"


# ---------------------------------------------------------------------- volume

SINK = "@DEFAULT_AUDIO_SINK@"


def parse_volume(text: str) -> tuple[int, bool]:
    m = re.search(r"Volume:\s*([\d.]+)", text)
    return (round(float(m.group(1)) * 100) if m else 0), "[MUTED]" in text


@tool("volume", "Get or change the speaker volume.",
      {"level": {"type": "integer", "description": "set the volume to this percent, 0 to 100"},
       "change": {"type": "integer", "description": "raise (positive) or lower (negative) by this many percent"},
       "mute": {"type": "boolean", "description": "true to mute, false to unmute"}}, level="act")
def volume(level: int | None = None, change: int | None = None, mute: bool | None = None) -> str:
    current, muted = parse_volume(_run(["wpctl", "get-volume", SINK]).stdout)
    if mute is not None:
        _run(["wpctl", "set-mute", SINK, "1" if mute else "0"])
        muted = bool(mute)
    target = None
    if level is not None:
        target = int(level)
    elif change is not None:
        target = current + int(change)
    if target is not None:
        target = max(0, min(100, target))
        _run(["wpctl", "set-volume", SINK, f"{target / 100:.2f}"])
        current = target
    return f"volume {current}%" + (", muted" if muted else "")


# ---------------------------------------------------------------------- status

@tool("system_status", "How the computer is doing: CPU, memory, disk, battery, temperature, uptime.",
      level="safe", expose=True, card="facts")
def system_status() -> str:
    import psutil

    parts = [f"CPU {psutil.cpu_percent(interval=0.3):.0f}%"]
    mem = psutil.virtual_memory()
    parts.append(f"memory {mem.percent:.0f}% used ({mem.available / 2**30:.1f} GB free)")
    disk = psutil.disk_usage("/")
    parts.append(f"disk {disk.percent:.0f}% used ({disk.free / 2**30:.0f} GB free)")
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery:
        plug = "charging" if battery.power_plugged else "on battery"
        parts.append(f"battery {battery.percent:.0f}% ({plug})")
    temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
    hottest = max((t.current for group in temps.values() for t in group if t.current), default=None)
    if hottest:
        parts.append(f"hottest sensor {hottest:.0f}°C")
    up = int(time.time() - psutil.boot_time())
    parts.append(f"up {up // 3600} h {up % 3600 // 60} min")
    return ", ".join(parts)


# ---------------------------------------------------------------------- lock and power

@tool("lock_screen", "Lock the screen.", level="act")
def lock_screen() -> str:
    session = os.environ.get("XDG_SESSION_ID")
    result = _run(["loginctl", "lock-session"] + ([session] if session else []))
    return "locked" if result.returncode == 0 else f"error: {result.stderr.strip()[:200]}"


POWER = {"shutdown": (["systemctl", "poweroff"], "shut down the computer"),
         "restart": (["systemctl", "reboot"], "restart the computer"),
         "suspend": (["systemctl", "suspend"], "put the computer to sleep")}


@tool("power", "Shut down, restart or suspend the computer. Rafiq asks the user first.",
      {"action": {"type": "string", "enum": list(POWER)}}, required=("action",),
      confirm=lambda args: POWER.get(args.get("action", ""), (None, "change the power state"))[1], level="critical")
def power(action: str) -> str:
    if action not in POWER:
        return f"error: action is one of {', '.join(POWER)}"
    result = _run(POWER[action][0])
    return "done" if result.returncode == 0 else f"error: {result.stderr.strip()[:200]}"
