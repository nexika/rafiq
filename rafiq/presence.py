"""Presence: when your phone has been off the home Wi-Fi for ten minutes, play the "away" scene; when
it comes back, the "welcome home" scene.

The phone is looked for by its fixed IP: a ping, and when that gets no answer, the kernel's neighbour
(ARP) table, because a sleeping phone often ignores pings but still answers ARP. A phone that drops
off for a few minutes while asleep is still home; only a long absence counts.

When Rafiq starts, the first checks only learn where the phone is: no scene plays for a change that
happened while Rafiq was off. Turning it off by voice is saved, so it stays off after a restart.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

# Neighbour states that mean the phone answered just now. STALE only means it answered once, maybe
# hours ago.
_PRESENT_STATES = {"REACHABLE"}


def _run(args: list[str], timeout: float = 5) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def neighbour_present(table: str, mac: str = "") -> bool:
    """Whether `ip neigh show <ip>` says the phone answered ARP just now (with this MAC, if given)."""
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
    try:
        if run(["ping", "-c", "1", "-W", "1", ip], timeout=3).returncode == 0:
            return True
        # The ping made the kernel ask for the phone by ARP; a sleeping phone answers that.
        return neighbour_present(run(["ip", "neigh", "show", ip], timeout=3).stdout, mac)
    except (OSError, subprocess.SubprocessError):
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
