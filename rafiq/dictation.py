"""Dictation: what you say is typed into the window that has the keyboard, with spoken punctuation
("comma", "virgule", "فاصلة"), until you say "stop dictating".

Typing goes through a command-line tool: wtype or ydotool on Wayland, xdotool on X11. ydotool sends
key codes and cannot type Arabic or accented letters, so such text (or text a tool failed to type) is
put on the clipboard and pasted. Commands run as argument lists, never through a shell.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable

from .lang import words

START = {"dictate", "dictation", "start dictating", "start dictation", "dicte", "dictée", "dicter",
         "commence la dictée", "dicta", "dictar", "dictado", "empieza a dictar", "modo dictado", "diktieren",
         "diktat", "diktat starten", "إملاء", "ابدأ الإملاء", "اكتب ورايا", "اكتب ورائي"}
STOP = {"stop dictating", "stop dictation", "end dictation", "arrête la dictée", "arrête de dicter",
        "fin de la dictée", "deja de dictar", "para de dictar", "fin del dictado", "diktat beenden",
        "diktat stoppen", "stopp diktat", "توقف عن الإملاء", "أوقف الإملاء", "إنهاء الإملاء", "بطل إملاء",
        "خلاص الإملاء"}

# What is said, and the mark it becomes. "\n" starts a new line.
PUNCTUATION = {
    "en": {"comma": ",", "full stop": ".", "period": ".", "question mark": "?", "exclamation mark": "!",
           "exclamation point": "!", "colon": ":", "semicolon": ";", "new line": "\n", "new paragraph": "\n\n"},
    "fr": {"virgule": ",", "point": ".", "point d'interrogation": "?", "point d'exclamation": "!",
           "deux points": ":", "deux-points": ":", "point-virgule": ";", "point virgule": ";",
           "à la ligne": "\n", "nouvelle ligne": "\n", "nouveau paragraphe": "\n\n"},
    "es": {"coma": ",", "punto": ".", "punto y seguido": ".", "punto final": ".", "signo de interrogación": "?",
           "signo de exclamación": "!", "dos puntos": ":", "punto y coma": ";", "nueva línea": "\n",
           "punto y aparte": ".\n", "nuevo párrafo": "\n\n"},
    "de": {"komma": ",", "punkt": ".", "fragezeichen": "?", "ausrufezeichen": "!", "doppelpunkt": ":",
           "semikolon": ";", "neue zeile": "\n", "neuer absatz": "\n\n"},
    "ar": {"فاصلة": "،", "فاصله": "،", "نقطة": ".", "علامة استفهام": "؟", "علامة تعجب": "!", "نقطتان": ":",
           "فاصلة منقوطة": "؛", "سطر جديد": "\n", "فقرة جديدة": "\n\n"},
}
MARKS = ",.?!:;،؟؛"

_VARIANTS = {"ا": "[اأإآ]", "أ": "[اأإآ]", "إ": "[اأإآ]", "آ": "[اأإآ]", "ة": "[ةه]", "ه": "[ةه]",
             "ى": "[ىي]", "ي": "[ىي]", "'": "['’]"}


def _spoken(phrase: str) -> str:
    """A pattern for a spoken phrase, as speech-to-text may write it: any case, any spacing, and the
    spellings of Arabic letters that vary."""
    parts = []
    for word in re.split(r"[\s-]+", phrase):
        parts.append("".join(_VARIANTS.get(ch, re.escape(ch)) for ch in word))
    return r"[\s-]+".join(parts)


def _compile(table: dict[str, str]) -> list[tuple[re.Pattern, str]]:
    found = []
    for phrase in sorted(table, key=len, reverse=True):   # "point d'interrogation" before "point"
        # Whisper often writes the mark too ("comma," or ", comma"): the phrase takes it along.
        pattern = rf"[\s{MARKS}]*(?<!\w){_spoken(phrase)}(?!\w)[{MARKS}]*"
        found.append((re.compile(pattern, re.IGNORECASE), table[phrase]))
    return found


_PATTERNS = {code: _compile(table) for code, table in PUNCTUATION.items()}


def _phrase_words(phrases: set[str]) -> list[list[str]]:
    return sorted((words(p) for p in phrases), key=len, reverse=True)


_START = {" ".join(words(p)) for p in START}
_STOP = _phrase_words(STOP)


def wants_dictation(text: str) -> bool:
    """Whether a request is to start dictating: the whole request is the phrase ("dictate", "إملاء"),
    so "dictate an email to Sara" still goes to the brain."""
    return " ".join(words(text)) in _START


def stop_command(text: str) -> tuple[bool, str]:
    """Whether the stop phrase was said, and what was said before it (still to be typed)."""
    tokens = list(re.finditer(r"[\w']+", text))
    said = [words(t.group())[0] if words(t.group()) else "" for t in tokens]
    for i in range(len(said)):
        for phrase in _STOP:
            if said[i:i + len(phrase)] == phrase:
                return True, text[:tokens[i].start()].rstrip()
    return False, text


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def punctuate(text: str, lang: str, sentence_start: bool = True) -> str:
    """Spoken punctuation in the language it was said in, turned into marks."""
    for pattern, mark in _PATTERNS.get(lang.split("-")[0], []):
        text = pattern.sub(lambda _m, mark=mark: mark if mark.endswith("\n") else mark + " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text).strip(" ")
    text = re.sub(r"([.?!؟] )(\w)", lambda m: m.group(1) + m.group(2).upper(), text)
    return _capitalize(text) if sentence_start else text


class Session:
    """One dictation: the chunks heard so far, so each new one is spaced and capitalized after the last."""

    def __init__(self):
        self.typed = ""

    def add(self, heard: str, lang: str) -> tuple[bool, str]:
        """Whether this chunk ended the dictation, and the text to type for it."""
        stopped, heard = stop_command(heard)
        sentence_start = not self.typed.strip(" ") or self.typed.rstrip(" ")[-1] in ".?!؟"
        text = punctuate(heard, lang, sentence_start=sentence_start and not self.typed.endswith("\n"))
        if not text:
            return stopped, ""
        if self.typed and not self.typed[-1].isspace() and text[0] not in MARKS + "\n":
            text = " " + text
        self.typed += text
        return stopped, text


class TypingFailed(RuntimeError):
    pass


class Keyboard:
    """Types into the focused window through the desktop's tools; the environment, the lookup of
    installed programs and the runner are passed in for tests."""

    def __init__(self, env: dict | None = None, which: Callable = shutil.which, run: Callable = subprocess.run):
        self.env = os.environ if env is None else env
        self.which, self.run = which, run

    @property
    def wayland(self) -> bool:
        return self.env.get("XDG_SESSION_TYPE") == "wayland" or bool(self.env.get("WAYLAND_DISPLAY"))

    def _ok(self, args: list[str], text: str | None = None) -> bool:
        if not self.which(args[0]):
            return False
        try:
            # No pipes: wl-copy and xclip stay in the background to serve the clipboard.
            result = self.run(args, input=text, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _paste(self, text: str) -> bool:
        if self.wayland:
            copy = ["wl-copy"]
            keys = [["wtype", "-M", "ctrl", "v", "-m", "ctrl"], ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"]]
        else:
            copy = ["xclip", "-selection", "clipboard"]
            keys = [["xdotool", "key", "--clearmodifiers", "ctrl+v"]]
        if not self._ok(copy, text):
            return False
        return any(self._ok(args) for args in keys)

    def type(self, text: str) -> str:
        """Type the text; the way it went in ("wtype", "ydotool", "xdotool" or "paste")."""
        if self.wayland:
            tries = [("wtype", ["wtype", "--", text])]
            if text.isascii():
                tries.append(("ydotool", ["ydotool", "type", "--", text]))
        else:
            tries = [("xdotool", ["xdotool", "type", "--clearmodifiers", "--", text])]
        for name, args in tries:
            if self._ok(args):
                return name
        if self._paste(text):
            return "paste"
        if self.wayland:
            raise TypingFailed("could not type: install wtype, or ydotool and wl-clipboard")
        raise TypingFailed("could not type: install xdotool and xclip")
