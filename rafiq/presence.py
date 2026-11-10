"""Presence: when your phone has been off the home Wi-Fi for ten minutes, play the "away" scene; when
it comes back, the "welcome home" scene.

The phone is looked for by its fixed IP: a ping, and when that gets no answer, the kernel's neighbour
(ARP) table, because a sleeping phone often ignores pings but still answers ARP. On Windows, Get-NetNeighbor
reads that table with each entry's state (#242). A phone that drops
off for a few minutes while asleep is still home; only a long absence counts.

When Rafiq starts, the first checks only learn where the phone is: no scene plays for a change that
happened while Rafiq was off. Turning it off by voice is saved, so it stays off after a restart.
"""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

# Neighbour states that mean the phone answered just now. STALE only means it answered once, maybe
# hours ago.
_PRESENT_STATES = {"REACHABLE"}


def _run(args: list[str], timeout: float = 5) -> subprocess.CompletedProcess:
    # errors="replace": Windows' ping writes in the console's OEM code page, not the locale's.
    return subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout, check=False)


def ping_command(ip: str) -> list[str]:
    """One ping waiting a second: -W is in seconds on Linux, in milliseconds on macOS; Windows has -n and -w (ms)."""
    from . import desktop

    if desktop.system() == "windows":
        return ["ping", "-n", "1", "-w", "1000", ip]
    return ["ping", "-c", "1", "-W", "1000" if desktop.system() == "mac" else "1", ip]


def ping_answered(done: subprocess.CompletedProcess) -> bool:
    """Windows' ping exits with 0 when this computer itself replies that the phone can't be reached: only a reply
    with a TTL is the phone's. "TTL=" is the same in every language Windows speaks."""
    from . import desktop

    if desktop.system() == "windows":
        return done.returncode == 0 and "ttl=" in (done.stdout or "").lower()
    return done.returncode == 0


def neighbour_command(ip: str) -> list[str]:
    """On Windows, Get-NetNeighbor with each entry's state by its English name, whatever Windows' language. The IP is
    checked as an address first (ValueError otherwise), since it goes into a PowerShell command."""
    from . import desktop

    if desktop.system() == "windows":
        address = ipaddress.ip_address(ip.strip())
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                f"Get-NetNeighbor -IPAddress '{address}' -ErrorAction SilentlyContinue | "
                "ForEach-Object { $_.State.ToString() + ' ' + $_.LinkLayerAddress }"]
    return ["arp", "-n", ip] if desktop.system() == "mac" else ["ip", "neigh", "show", ip]


NEIGHBOUR_SECONDS = 3
WINDOWS_NEIGHBOUR_SECONDS = 10    # PowerShell itself can take a few seconds to start


_ARP_ADDRESS = re.compile(r"\bat\s+([0-9a-f]{1,2}(?::[0-9a-f]{1,2}){5})\b", re.IGNORECASE)


def _same_mac(found: str, wanted: str) -> bool:
    """macOS's arp writes 0a:0b as a:b."""
    return [p.zfill(2) for p in found.lower().split(":")] == [p.zfill(2) for p in wanted.lower().split(":")]


def neighbour_present(table: str, mac: str = "") -> bool:
    """Whether `ip neigh show <ip>` (on macOS `arp -n <ip>`) says the phone answered ARP just now (with this MAC,
    if given)."""
    from . import desktop

    if desktop.system() == "windows":             # "Reachable AA-BB-CC-DD-EE-FF", one line per entry
        for line in table.splitlines():
            words = line.split()
            if words and words[0] == "Reachable":
                found = words[1].replace("-", ":") if len(words) > 1 else ""
                if not mac or _same_mac(found, mac):
                    return True
        return False
    if desktop.system() == "mac":
        for line in table.splitlines():
            found = _ARP_ADDRESS.search(line)
            if found and (not mac or _same_mac(found.group(1), mac)):
                return True
        return False
    for line in table.splitlines():
        words = line.split()
        if not words or words[-1] not in _PRESENT_STATES:
            continue
        lladdr = words[words.index("lladdr") + 1].lower() if "lladdr" in words[:-1] else ""
        if not mac or lladdr == mac.lower():
            return True
    return False


def phone_seen(ip: str, mac: str = "", run=_run) -> bool:
    """Is the phone on the network now? A missing command or a timeout counts as not seen."""
    from . import desktop

    wait = WINDOWS_NEIGHBOUR_SECONDS if desktop.system() == "windows" else NEIGHBOUR_SECONDS
    try:
        if ping_answered(run(ping_command(ip), timeout=3)):
            return True
        # The ping made the kernel ask for the phone by ARP; a sleeping phone answers that.
        return neighbour_present(run(neighbour_command(ip), timeout=wait).stdout or "", mac)
    except (OSError, subprocess.SubprocessError, ValueError):     # ValueError: a phone_ip that isn't an address
        return False


def run_scene(cfg: dict, which: str, run=_run) -> str | None:
    """Play the "away" or "home" scene: the command set for it in the config, as an argument list.
    Returns what went wrong, if anything."""
    command = cfg.get("presence", {}).get(f"{which}_command") or []
    if not command:
        return None
    try:
        done = run([str(word) for word in command], timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{which} scene: {type(exc).__name__}: {exc}"
    return f"{which} scene: exit code {done.returncode}" if done.returncode else None


class Presence:
    def __init__(self, path: Path, probe: Callable[[], bool], on_away: Callable[[], None],
                 on_home: Callable[[], None], away_after: float = 600, clock=time.monotonic):
        self.path, self.probe, self.on_away, self.on_home = path, probe, on_away, on_home
        self.away_after, self.clock = away_after, clock
        self.lock = threading.Lock()
        self.enabled = self._load()
        self._reset()
        self._stop = threading.Event()

    def _load(self) -> bool:
        try:
            return bool(json.loads(self.path.read_text()).get("enabled", True))
        except (OSError, ValueError, AttributeError):
            return True

    def _reset(self) -> None:
        self.state = "unknown"                    # "unknown", "home" or "away"
        self.last_seen = self.clock()             # unknown: counts from now, so a start while away is silent

    def set_enabled(self, enabled: bool) -> None:
        with self.lock:
            if enabled and not self.enabled:
                self._reset()
            self.enabled = enabled
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"enabled": enabled}))
            tmp.replace(self.path)

    def check(self) -> str | None:
        """Look for the phone once; returns "away" or "home" when that scene was just played."""
        with self.lock:
            enabled = self._load()                # `rafiq ask "turn presence off"` from another process
            if enabled and not self.enabled:
                self._reset()
            self.enabled = enabled
            if not self.enabled:
                return None
            try:
                seen = bool(self.probe())
            except Exception:
                seen = False
            now, before = self.clock(), self.state
            if seen:
                self.last_seen, self.state = now, "home"
            elif before != "away" and now - self.last_seen >= self.away_after:
                self.state = "away"
            changed = self.state if self.state != before and before != "unknown" else None
        if changed == "away":
            self.on_away()
        elif changed == "home":
            self.on_home()
        return changed

    def status(self) -> str:
        if not self.enabled:
            return "presence is off"
        where = {"home": "the phone is home", "away": "the phone is away",
                 "unknown": "not sure yet where the phone is"}[self.state]
        return f"presence is on; {where}"

    def start(self, every: float = 30) -> None:
        def loop():
            while not self._stop.wait(every):
                try:
                    self.check()
                except Exception:
                    pass

        threading.Thread(target=loop, name="rafiq-presence", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
