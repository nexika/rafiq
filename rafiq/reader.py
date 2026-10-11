"""Reading aloud: the selected text, the clipboard, a web page or a PDF, sentence by sentence, each in
the voice for its language, with pause, resume, skip and stop.

Arabic sentences are read as Modern Standard Arabic ("ar"): written Arabic is فصحى, and the Azure
voices add the diacritics themselves. The text itself never goes to the model, only how long it is.

The reader is driven from two threads: the assistant's thread reads, the panel's pauses and skips.
Pausing or skipping stops the sentence being spoken; a paused reading starts that sentence again.
"""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import lang

MAX_CHARS = 100_000        # about an hour of listening

# The primary selection is what is highlighted; the clipboard is what was copied. Wayland first.
PRIMARY = [["wl-paste", "--primary", "--no-newline"], ["xclip", "-o", "-selection", "primary"],
           ["xsel", "--primary", "--output"]]
CLIPBOARD = [["wl-paste", "--no-newline"], ["xclip", "-o", "-selection", "clipboard"],
             ["xsel", "--clipboard", "--output"]]

_SENTENCE_END = re.compile(r"(?<=[.!?؟…])\s+|\n\s*\n")


@dataclass(frozen=True)
class Segment:
    text: str
    tag: str


def sentences(text: str) -> list[str]:
    parts = (re.sub(r"\s+", " ", p).strip() for p in _SENTENCE_END.split(text))
    return [p for p in parts if p]


def _tag(sentence: str, previous: str) -> str:
    """Arabic when most letters are Arabic; else the language its common words tell, else the one before."""
    letters = sum(ch.isalpha() for ch in sentence)
    if not letters:
        return previous
    if len(lang.ARABIC.findall(sentence)) / letters > 0.5:
        return "ar"
    return lang.guess(lang.ARABIC.sub("", sentence), previous)


def segments(text: str, fallback: str = "en") -> list[Segment]:
    found, previous = [], fallback
    for sentence in sentences(text[:MAX_CHARS]):
        previous = _tag(sentence, previous)
        found.append(Segment(sentence, previous))
    return found


# ---------------------------------------------------------------------- where the text comes from

def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)


def _first_output(commands: list[list[str]], run) -> str:
    for args in commands:
        try:
            result = run(args)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    return ""


def selection(run=_run) -> str:
    """The highlighted text, or "" when nothing is selected or no clipboard tool is installed."""
    return _first_output(PRIMARY, run)


def clipboard(run=_run) -> str:
    return _first_output(CLIPBOARD, run)


def pdf_text(path: str) -> str:
    file = Path(path).expanduser()
    if not file.is_file():
        raise ValueError(f"no file at {path}")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("reading PDFs needs pypdf: pip install 'rafiq[pdf]'") from exc
    try:
        pages = [page.extract_text() or "" for page in PdfReader(str(file)).pages]
    except Exception as exc:   # pypdf raises many kinds for a damaged or encrypted file
        raise ValueError(f"cannot read that PDF ({type(exc).__name__})") from exc
    return "\n\n".join(p.strip() for p in pages if p.strip())


def page_text(url: str) -> str:
    """The main text of a public web page, fetched like read_page does."""
    import httpx
    import trafilatura

    from .tools.web import fetch

    try:
        _final, html = fetch(url)
    except httpx.HTTPError as exc:
        raise ValueError(f"cannot fetch that page ({type(exc).__name__})") from exc
    return trafilatura.extract(html, include_comments=False, include_tables=False) or ""


# ---------------------------------------------------------------------- the reader

class Reader:
    """What is being read and where it is. `interrupt` stops the sentence being spoken."""

    def __init__(self, interrupt: Callable[[], None]):
        self.interrupt = interrupt
        self.segments: list[Segment] = []
        self.index = 0
        self.state = "idle"          # idle, reading or paused
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        """True when there is reading to do now."""
        with self._lock:
            return self.state == "reading" and self.index < len(self.segments)

    def load(self, segments: list[Segment]) -> None:
        with self._lock:
            self.segments, self.index = list(segments), 0
            self.state = "reading" if segments else "idle"

    def pause(self) -> bool:
        with self._lock:
            if self.state != "reading":
                return False
            self.state = "paused"
        self.interrupt()
        return True

    def resume(self) -> bool:
        with self._lock:
            if self.state != "paused" or self.index >= len(self.segments):
                return False
            self.state = "reading"
            return True

    def skip(self) -> bool:
        with self._lock:
            if self.state == "idle":
                return False
            self.index = min(self.index + 1, len(self.segments))
        self.interrupt()
        return True

    def stop(self) -> None:
        with self._lock:
            was = self.state
            self.segments, self.index, self.state = [], 0, "idle"
        if was != "idle":
            self.interrupt()

    def run(self, say: Callable[[str, str], object], on_progress: Callable[[int, int], None] | None = None) -> None:
        """Read until the end, a pause or a stop. Blocks: call it from the thread that speaks."""
        while True:
            with self._lock:
                if self.state != "reading":
                    return
                if self.index >= len(self.segments):
                    self.segments, self.index, self.state = [], 0, "idle"
                    return
                current, segment, total = self.index, self.segments[self.index], len(self.segments)
            if on_progress:
                on_progress(current, total)
            say(segment.text, segment.tag)
            with self._lock:   # a skip already moved on; a pause reads this sentence again
                if self.state == "reading" and self.index == current:
                    self.index += 1
