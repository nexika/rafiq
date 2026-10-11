"""Language practice: a simple conversation at the learner's level, with short spoken corrections.

The model holds the conversation; this keeps the session (language, level, topic), the corrections
it notes (each shown in the panel in full), and a short local history of the mistakes made most often,
so the end of a session can say what to work on.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import lang as lang_mod

LEVELS = ("A1", "A2", "B1", "B2", "C1")
SLOW = {"A1": "-25%", "A2": "-15%"}     # beginners hear a slower voice
HISTORY_PER_LANGUAGE = 30
COMMON_SHOWN = 3

LEVEL_STYLE = {
    "A1": "very short sentences, the most common words, present tense, one simple question at a time",
    "A2": "short sentences and everyday words, simple past and future, one question at a time",
    "B1": "ordinary sentences about familiar topics, a few less common words",
    "B2": "natural speech with some idioms, at a normal pace",
    "C1": "fluent, natural speech with idioms and nuance",
}

NOTE = """Language practice: be a friendly conversation partner in {language} at CEFR level {level}{topic}.
Speak only {language}, in {style}. Keep each reply to one or two sentences and end with a question.
When the user makes a mistake, first say the correct form briefly (a few words, no grammar lecture),
then go on with the conversation; call practice_correct once for each mistake, with the explanation
there. Ignore speech-to-text slips that are not language mistakes. When the user wants to stop, call
practice_end and tell them, in {home}, the two or three things to work on."""


@dataclass
class Correction:
    said: str
    correct: str
    note: str = ""


@dataclass
class Practice:
    """The current practice session, if any, and the history file of common mistakes."""
    path: Path
    on_event: Callable[[str, dict], None] = lambda kind, data: None
    tag: str | None = None
    level: str = "A2"
    topic: str = ""
    home: str = "en"                 # the language the learner asked in; the summary is given in it
    corrections: list[Correction] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.tag is not None

    def start(self, tag: str, level: str, topic: str, home: str = "en") -> None:
        level = (level or "A2").upper()
        if tag not in lang_mod.TAGS:
            raise ValueError(f"unknown language {tag!r}")
        if level not in LEVELS:
            raise ValueError(f"the level is one of {', '.join(LEVELS)}")
        self.tag, self.level, self.topic, self.home = tag, level, topic.strip(), home
        self.corrections = []
        self.on_event("practice", {"state": "start", "tag": tag, "level": level, "topic": self.topic})

    def note(self) -> str:
        """Instructions added to each request while practising; empty otherwise."""
        if not self.active:
            return ""
        return NOTE.format(language=lang_mod.NAMES[self.tag], level=self.level, style=LEVEL_STYLE[self.level],
                           topic=f", talking about {self.topic}" if self.topic else "",
                           home=lang_mod.NAMES.get(self.home, "English"))

    def rate(self) -> str | None:
        """The voice rate for this session, or None for the usual one."""
        return SLOW.get(self.level) if self.active else None

    def correct(self, said: str, correct: str, note: str = "") -> Correction:
        item = Correction(said.strip(), correct.strip(), note.strip())
        self.corrections.append(item)
        self.on_event("correction", {"said": item.said, "correct": item.correct, "note": item.note,
                                     "tag": self.tag})
        return item

    def end(self) -> str:
        """Close the session, remember its mistakes, and say what to work on."""
        tag, corrections = self.tag, self.corrections
        self._remember(tag, corrections)
        lines = [f"Corrections this session ({len(corrections)}):"]
        lines += [f"- '{c.said}' -> '{c.correct}'" + (f" ({c.note})" if c.note else "") for c in corrections]
        if not corrections:
            lines[0] = "No corrections this session."
        summary = "\n".join(lines + [self.history_summary(tag)])
        self.tag, self.corrections = None, []
        self.on_event("practice", {"state": "end", "tag": tag, "summary": summary})
        return summary

    # ------------------------------------------------------------------ the history of mistakes

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def common(self, tag: str) -> list[dict]:
        """The mistakes made most often in this language, most frequent first."""
        items = self._load().get(tag)
        return items if isinstance(items, list) else []

    def history_summary(self, tag: str) -> str:
        common = self.common(tag)[:COMMON_SHOWN]
        if not common:
            return "No common mistakes recorded yet."
        return "Most common mistakes so far: " + "; ".join(
            f"'{m['said']}' -> '{m['correct']}' ({m['count']} times)" for m in common)

    def _remember(self, tag: str | None, corrections: list[Correction]) -> None:
        if not tag or not corrections:
            return
        data = self._load()
        items = {(m["said"].lower(), m["correct"].lower()): m for m in self.common(tag)}
        for c in corrections:
            key = (c.said.lower(), c.correct.lower())
            if key in items:
                items[key]["count"] += 1
                items[key]["note"] = c.note or items[key].get("note", "")
            else:
                items[key] = {"said": c.said, "correct": c.correct, "note": c.note, "count": 1}
        # The most frequent first; among equals the order they were first made in.
        data[tag] = sorted(items.values(), key=lambda m: -m["count"])[:HISTORY_PER_LANGUAGE]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
