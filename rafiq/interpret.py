"""Interpreter mode: Rafiq stands between two people who speak two languages and says what each one
says in the other's language, turn by turn, until someone says stop.

"Interpret between me and her, Egyptian and German" starts it. No name is needed between turns: the
language of each utterance decides the direction. A stop word in any language, or a long silence,
ends it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import lang

QUIET_SECONDS = 90.0         # nobody speaks this long: the conversation is over
MAX_STOP_WORDS = 3           # "stop" ends it only on its own or nearly, not inside a sentence

# The six languages' names, as said in each of the six languages (Arabic forms without "ال").
LANGUAGE_NAMES = {
    "en": {"english", "anglais", "inglés", "ingles", "englisch", "انجليزي", "انجليزيه", "انكليزي", "انجلش"},
    "fr": {"french", "français", "francais", "francés", "frances", "französisch", "franzosisch", "فرنساوي",
           "فرنسي", "فرنسيه"},
    "es": {"spanish", "espagnol", "español", "espanol", "spanisch", "اسباني", "اسبانيه", "اسبانيولي"},
    "de": {"german", "allemand", "alemán", "aleman", "deutsch", "deutsche", "الماني", "المانيه"},
    "ar": {"arabic", "arabe", "árabe", "arabisch", "عربي", "عربيه", "فصحى", "فصحي"},
    "ar-EG": {"egyptian", "égyptien", "egyptien", "egipcio", "ägyptisch", "agyptisch", "مصري", "مصريه"},
}
# Words that ask for an interpreter on their own; "translate" asks only with "between".
_INTERPRET = ("interpret", "dolmetsch", "مترجم")
_TRANSLATE = ("translat", "tradu", "übersetz", "ubersetz", "ترجم")
_BETWEEN = {"between", "entre", "zwischen"}
STOP = {"stop", "enough", "quit", "finish", "arrête", "arrete", "arrêter", "assez", "fini", "basta", "detente",
        "termina", "terminar", "stopp", "schluss", "aufhören", "aufhoren", "genug", "beenden", "وقف", "توقف",
        "كفايه", "خلاص", "اسكت"}

_NAME_TAG = {lang.normalize_arabic(name).lower(): tag for tag, names in LANGUAGE_NAMES.items() for name in names}
_STOP = frozenset(lang.normalize_arabic(w).lower() for w in STOP)


def _bare(word: str) -> str:
    """An Arabic word without "و" (and), "ب"/"لل" (in, to) and "ال" (the): "بالالماني" is "الماني"."""
    for prefix in ("وبال", "بال", "وال", "لل", "ال", "و"):
        if word.startswith(prefix) and len(word) - len(prefix) >= 3:
            return word[len(prefix):]
    return word


def base(tag: str) -> str:
    """The Whisper language of a tag: Egyptian is heard as Arabic."""
    return "ar" if tag.startswith("ar") else tag


@dataclass(frozen=True)
class Pair:
    """The two languages, in the order they were named."""
    first: str
    second: str

    def other(self, tag: str) -> str:
        return self.second if tag == self.first else self.first

    def side(self, tag: str) -> str | None:
        """Which of the two an utterance's language is; Egyptian and standard Arabic count as the same."""
        for side in (self.first, self.second):
            if base(side) == base(tag):
                return side
        return None

    def whisper_languages(self) -> list[str]:
        return [base(self.first), base(self.second)]


def parse_request(text: str) -> Pair | None:
    """The two languages of a request for an interpreter, or None when it is not one. "Egyptian
    Arabic" names one language, Egyptian."""
    found = lang.words(text)
    interpret = any(w.startswith(_INTERPRET) for w in found)
    translate = any(w.startswith(_TRANSLATE) for w in found)
    between = any(w in _BETWEEN or w.startswith("بين") for w in found)
    if not (interpret or (translate and between)):
        return None
    tags: list[str] = []
    for word in found:
        tag = _NAME_TAG.get(word) or _NAME_TAG.get(_bare(word))
        if tag and tag not in tags:
            tags.append(tag)
    if "ar-EG" in tags and "ar" in tags:
        tags.remove("ar")
    if len(tags) != 2 or base(tags[0]) == base(tags[1]):
        return None
    return Pair(tags[0], tags[1])


def is_stop(text: str) -> bool:
    """True for "stop", "Rafiq, stop", "arrête", "كفاية"...: a stop word with at most a couple of
    others, so a sentence that happens to contain one is still translated."""
    found = lang.words(text)
    return 0 < len(found) <= MAX_STOP_WORDS and any(w in _STOP for w in found)


@dataclass
class Session:
    """One interpreted conversation. Everything it needs comes in as functions, so the assistant
    gives it the microphone, the speaker and the brain, and tests give it lists."""
    pair: Pair
    listen: Callable[[float], object | None]          # the next utterance, or None after a silence
    hear: Callable[[object, list[str]], tuple[str, str]]   # audio and its possible languages -> text, tag
    translate: Callable[[str, str, str], str]          # text, from, to -> translation
    say: Callable[[str, str], None]
    publish: Callable[[str, dict], None] = lambda kind, data: None
    quiet_seconds: float = QUIET_SECONDS
    last_source: str | None = None

    def direction(self, tag: str) -> tuple[str, str]:
        """From which language to which. A language that is neither (a mishearing) is taken as the
        side that did not speak last: people take turns."""
        source = self.pair.side(tag)
        if source is None:
            source = self.pair.first if self.last_source is None else self.pair.other(self.last_source)
        return source, self.pair.other(source)

    def turn(self, audio) -> bool:
        """Interpret one utterance. False when it was a stop."""
        text, tag = self.hear(audio, self.pair.whisper_languages())
        if is_stop(text):
            return False
        if not text.strip():
            return True
        source, target = self.direction(tag)
        self.last_source = source
        translation = self.translate(text, source, target)
        self.publish("interpreted", {"text": text, "source": source, "translation": translation, "target": target})
        self.say(translation, target)
        return True

    def run(self) -> None:
        first, second = self.pair.first, self.pair.second
        self.publish("interpret", {"on": True, "languages": [first, second]})
        for tag in (first, second):      # each person hears how it works, in their own language
            self.say(lang.phrase("interpret_start", tag), tag)
        try:
            while True:
                audio = self.listen(self.quiet_seconds)
                if audio is None or not self.turn(audio):
                    break
        finally:
            self.publish("interpret", {"on": False, "languages": [first, second]})
        last = self.last_source or first
        self.say(lang.phrase("interpret_end", last), last)
