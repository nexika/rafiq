"""The permissions page: how freely each tool runs, and the approvals the user asked Rafiq to remember.

A tool that changes something asks first. Answering "Always" in the panel, or "yes, always", remembers the
approval for that tool, kept in approvals.json; from then on it runs without asking, until it is revoked.
A few tools (shutting down, sending or deleting mail, deleting an event) ask every time.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from . import config as config_mod
from .tools import Tool


class Approvals:
    def __init__(self, path: Path | None = None):
        self.path = path or config_mod.home() / "approvals.json"
        self.lock = threading.Lock()

    def names(self) -> list[str]:
        """The remembered tools; a missing or damaged file means none."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return sorted(n for n in data if isinstance(n, str)) if isinstance(data, list) else []

    def allowed(self, name: str) -> bool:
        return name in self.names()

    def _save(self, names: set[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(names)), encoding="utf-8")
        self.path.chmod(0o600)

    def remember(self, name: str) -> None:
        with self.lock:
            self._save(set(self.names()) | {name})

    def revoke(self, name: str) -> None:
        with self.lock:
            self._save(set(self.names()) - {name})


def levels(tools: dict[str, Tool], approvals: Approvals) -> list[dict]:
    """Each tool with its level: free (runs without asking), asks, remembered (asked once, always), or
    always_asks (asks every time; an approval saved for it is ignored)."""
    remembered = set(approvals.names())
    rows = []
    for t in sorted(tools.values(), key=lambda t: t.name):
        if t.confirm is None:
            level = "free"
        elif t.always_asks:
            level = "always_asks"
        else:
            level = "remembered" if t.name in remembered else "asks"
        rows.append({"name": t.name, "description": t.description, "level": level})
    return rows
