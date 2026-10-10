"""What Rafiq heard and answered, kept on this computer so you can look back and delete it, and what
leaves the computer right now.

Only requests are kept: what followed the name, a follow-up, or what was typed in the panel. The
listening for the name itself is never written down. Entries older than the retention (7 days by
default) are dropped; a retention of 0 keeps nothing.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

CLOUD_BRAINS = {"claude"}
CLOUD_VOICES = {"azure", "edge"}
DAY = 86400


@dataclass
class Entry:
    id: str
    time: float         # epoch seconds
    heard: str          # what the user said or typed
    said: str           # Rafiq's answer
    tag: str            # the language of the request
    source: str         # "voice" or "typed"


class History:
    def __init__(self, path: Path, retention_days: float = 7, clock=time.time):
        self.path, self.retention, self.clock = path, retention_days * DAY, clock
        self.lock = threading.Lock()
        self._entries: list[Entry] = []
        self._load()

    def _load(self) -> None:
        try:
            self._entries = [Entry(**item) for item in json.loads(self.path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError):
            self._entries = []
        with self.lock:
            if self._prune():
                self._save()

    def _prune(self) -> bool:
        cutoff = self.clock() - self.retention
        kept = [e for e in self._entries if e.time > cutoff] if self.retention > 0 else []
        changed = len(kept) != len(self._entries)
        self._entries = kept
        return changed

    def _save(self) -> None:
        """Written readable only by you, and replaced in one step so a crash never leaves half a file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump([asdict(e) for e in self._entries], f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def add(self, heard: str, said: str, tag: str, source: str) -> Entry:
        entry = Entry(uuid.uuid4().hex[:12], self.clock(), heard, said, tag, source)
        with self.lock:
            if self.retention <= 0:
                return entry
            self._entries.append(entry)
            self._prune()
            self._save()
        return entry

    def entries(self, query: str = "") -> list[Entry]:
        """Newest first; with a query, only those whose request or answer contains it (any case)."""
        q = query.strip().casefold()
        with self.lock:
            if self._prune():
                self._save()
            found = [e for e in self._entries if not q or q in e.heard.casefold() or q in e.said.casefold()]
        return found[::-1]          # kept in the order they happened

    def delete(self, entry_id: str) -> list[Entry]:
        with self.lock:
            gone = [e for e in self._entries if e.id == entry_id]
            if gone:
                self._entries = [e for e in self._entries if e.id != entry_id]
                self._save()
        return gone

    def forget_last(self) -> Entry | None:
        with self.lock:
            if not self._entries:
                return None
            last = self._entries.pop()
            self._save()
        return last

    def clear(self) -> list[Entry]:
        with self.lock:
            gone, self._entries = self._entries, []
            if self.path.exists():
                self._save()
        return gone


def privacy(brains: list[str], voices: list[str]) -> dict:
    """Whether a request can leave this computer, from the engines Rafiq would use, in order: a cloud
    engine anywhere in the list counts, since it is tried before the local fallback."""
    cloud = [b for b in brains if b in CLOUD_BRAINS] + [v for v in voices if v in CLOUD_VOICES]
    return {"brain": brains[0] if brains else "", "voice": voices[0] if voices else "", "cloud": cloud,
            "offline": not cloud}
