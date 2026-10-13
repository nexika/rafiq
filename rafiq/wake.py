"""Hearing the name "Rafiq" (or رفيق), or another name from `[wake] names`, in what a small Whisper
model transcribed.

Speech-to-text writes the name many ways (Rafik, Rafeeq, Rafique, رافيق), so a word within one edit
of a known spelling counts. Short everyday words are kept apart by the length check; another name
shorter than five letters must be heard exactly.
"""

from __future__ import annotations

import re
import unicodedata

from .lang import normalize_arabic

LATIN = ("rafiq", "rafik", "rafeeq", "rafeek", "rafique", "rafic", "raphik")
ARABIC = ("رفيق", "رافيق", "رفيج")


def _plain(word: str) -> str:
    word = unicodedata.normalize("NFKD", word.lower())
    return "".join(ch for ch in word if not unicodedata.combining(ch))


def _distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 1:
        return 2
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def names(cfg: dict) -> tuple[str, ...]:
    """The other names Rafiq answers to, from `[wake] names`; "Rafiq" always works."""
    found = cfg.get("wake", {}).get("names") or ()
    if isinstance(found, str):
        found = [found]
    return tuple(n.strip() for n in found if isinstance(n, str) and n.strip())


def prompt(extra=()) -> str:
    """What the small wake model is told to expect: the name, and any other names."""
    return ", ".join(["Rafiq", "رفيق", *extra]) + "."


def _clean(word: str) -> str:
    return normalize_arabic(_plain(word)).strip("'’-")


def _close(heard: str, name: str) -> bool:
    # Within one edit for a name of five letters or more; a shorter one would catch everyday words.
    return heard == name if len(name) < 5 else _distance(heard, name) <= 1


def _without_prefix(word: str):
    # Arabic often attaches "يا" or "و" to the word in transcripts: "يارفيق".
    yield word
    for prefix in ("يا", "و"):
        if word.startswith(prefix) and len(word) > len(prefix):
            yield word[len(prefix):]


def is_name(word: str) -> bool:
    w = _clean(word)
    if len(w) < 4:
        return False
    names = ARABIC if re.search(r"[؀-ۿ]", w) else LATIN
    return any(_distance(rest, normalize_arabic(n)) <= 1 for rest in _without_prefix(w) for n in names)


def _other_name_at(words: list[str], i: int, extra) -> int:
    """How many words another name takes at position i (0 when none starts there)."""
    for name in extra:
        parts = [_clean(p) for p in name.split()]
        heard = words[i:i + len(parts)]
        if len(heard) < len(parts) or not all(parts):
            continue
        if all(_close(h, p) for h, p in zip(heard[1:], parts[1:], strict=True)) and \
                any(_close(first, parts[0]) for first in _without_prefix(heard[0])):
            return len(parts)
    return 0


def find(text: str, extra=()) -> tuple[bool, str]:
    """Whether the name (or another name in `extra`) is in the text, and what was said after it (the
    command, if any). "Hey Rafiq, what time is it?" gives (True, "what time is it?")."""
    tokens = re.findall(r"\S+", text)
    words = [_clean(re.sub(r"[^\w'’-]", "", token)) for token in tokens]
    for i, token in enumerate(tokens):
        taken = 1 if is_name(re.sub(r"[^\w'’-]", "", token)) else _other_name_at(words, i, extra)
        if taken:
            rest = " ".join(tokens[i + taken:]).lstrip(" ,،.!?؟:;-")
            return True, rest
    return False, ""
