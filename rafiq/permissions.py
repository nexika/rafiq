"""Permission levels, and the approvals remembered when the user says "yes, always".

1  safe: runs at once, and may run when the request comes from elsewhere (remote access, MCP).
2  reversible: Rafiq asks first; "yes, always" remembers the approval until it is revoked in the panel.
3  sensitive (email, bookings, deleting, shutting down): asked every time, never remembered, never
   run remotely.

Approvals are kept per tool in a file on this computer, readable only by the user.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

SAFE, REVERSIBLE, SENSITIVE = 1, 2, 3
ALWAYS = "always"        # the answer "yes, always", next to True (yes) and False (no)


class Approvals:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.approved: dict[str, float] = {}
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                self.approved = {str(k): float(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.approved))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def has(self, name: str) -> bool:
        return name in self.approved

    def names(self) -> list[str]:
        return sorted(self.approved)

    def remember(self, name: str) -> None:
        with self.lock:
            self.approved[name] = time.time()
            self._save()

    def revoke(self, name: str) -> bool:
        with self.lock:
            if self.approved.pop(name, None) is None:
                return False
            self._save()
            return True
