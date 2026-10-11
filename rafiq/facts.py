"""Long-term memory: facts the user asked Rafiq to remember ("my sister is called Sara").

They live in ~/.config/rafiq/data/memory.json, a plain list the user can read and edit by hand; the
file is read again on every use, so an edit counts at once. Forgetting removes the fact from the file.
A fact is something the user said about themselves: data for the model, never instructions to it.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

NOTE_FACTS = 8

# Words too common to say that a fact is about the request.
_COMMON = {"the", "and", "you", "your", "what", "who", "how", "are", "for", "about", "know", "that", "this",
           "with", "have", "has", "was", "his", "her", "its", "our", "they", "them", "can", "tell",
           "le", "la", "les", "des", "est", "que", "qui", "une", "pour", "der", "die", "das", "und", "ist",
           "los", "las", "del", "una", "في", "من", "على", "عن", "ايه", "اللي", "هو", "هي"}
# Arabic spellings that speech-to-text mixes up: أ إ آ → ا, ة → ه, ى → ي.
_ARABIC = str.maketrans("أإآةى", "اااهي")


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower().translate(_ARABIC)) if len(w) >= 3 and w not in _COMMON}


@dataclass
class Fact:
    id: str
    text: str
    saved: float = 0.0      # epoch seconds


class Facts:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = path, clock
        self.lock = threading.Lock()

    def _read(self) -> list[Fact]:
        """The facts in the file. Raises ValueError when the file is not valid JSON, so a hand edit
        with a typo is reported instead of being overwritten."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        data = json.loads(raw) if raw.strip() else []
        if not isinstance(data, list):
            raise ValueError(f"{self.path} should hold a list of facts")
        facts = []
        for i, item in enumerate(data):
            if isinstance(item, str):
                item = {"text": item}
            if isinstance(item, dict) and str(item.get("text") or "").strip():
                facts.append(Fact(str(item.get("id") or f"n{i + 1}"), str(item["text"]).strip(),
                                  float(item.get("saved") or 0)))
        return facts

    def _write(self, facts: list[Fact]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump([asdict(f) for f in facts], out, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def all(self) -> list[Fact]:
        """Every fact, oldest first; none when the file can't be read."""
        with self.lock:
            try:
                return self._read()
            except ValueError:
                return []

    def add(self, text: str) -> Fact:
        fact = Fact(uuid.uuid4().hex[:6], " ".join(text.split()), self.clock())
        with self.lock:
            self._write(self._read() + [fact])
        return fact

    def forget(self, fact_id: str | None = None, words: str | None = None) -> list[Fact]:
        """Delete the facts with this id or containing these words; returns them."""
        if not fact_id and not words:
            return []
        with self.lock:
            facts = self._read()
            gone = [f for f in facts if (fact_id and f.id == fact_id) or (words and words.lower() in f.text.lower())]
            if gone:
                self._write([f for f in facts if f not in gone])
        return gone

    def _ranked(self, text: str) -> list[tuple[int, Fact]]:
        asked = words(text)
        scored = [(len(asked & words(f.text)), f) for f in self.all()]
        return sorted(scored, key=lambda s: (s[0], s[1].saved), reverse=True)

    def search(self, text: str) -> list[Fact]:
        """The facts sharing a word with the text, best first."""
        return [f for score, f in self._ranked(text) if score]

    def for_note(self, text: str, limit: int = NOTE_FACTS) -> list[Fact]:
        """What goes with a request: the facts that match it first, then the newest, at most `limit`."""
        return [f for _, f in self._ranked(text)[:limit]]
