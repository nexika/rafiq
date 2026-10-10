"""Hearing the name "Rafiq" (or رفيق) in what a small Whisper model transcribed.

Speech-to-text writes the name many ways (Rafik, Rafeeq, Rafique, رافيق), so a word within one edit
of a known spelling counts. Short everyday words are kept apart by the length check.
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


def is_name(word: str) -> bool:
    w = normalize_arabic(_plain(word)).strip("'’-")
    if len(w) < 4:
        return False
    names = ARABIC if re.search(r"[؀-ۿ]", w) else LATIN
    # Arabic often attaches "يا" or "و" to the word in transcripts: "يارفيق".
    for prefix in ("", "يا", "و"):
        if prefix and not w.startswith(prefix):
            continue
        rest = w[len(prefix):]
        if any(_distance(rest, normalize_arabic(n)) <= 1 for n in names):
            return True
    return False


def find(text: str) -> tuple[bool, str]:
    """Whether the name is in the text, and what was said after it (the command, if any).
    "Hey Rafiq, what time is it?" gives (True, "what time is it?")."""
    tokens = re.findall(r"\S+", text)
    for i, token in enumerate(tokens):
        if is_name(re.sub(r"[^\w'’-]", "", token)):
            rest = " ".join(tokens[i + 1:]).lstrip(" ,،.!?؟:;-")
            return True, rest
    return False, ""
