"""Language practice: start a conversation at a level, note corrections, end with what to work on."""

from __future__ import annotations

from .. import lang as lang_mod
from ..practice import LEVELS
from . import tool

_BY_NAME = {name.lower(): tag for tag, name in lang_mod.NAMES.items()} | {
    "arabic": "ar", "egyptian": "ar-EG", "français": "fr", "español": "es", "deutsch": "de"}


def _session(ctx):
    practice = getattr(ctx, "practice", None)
    if practice is None:
        raise LookupError("language practice needs Rafiq running (rafiq run)")
    return practice


@tool("practice_start", "Start a spoken language practice conversation (\"let's practise French\").",
      {"language": {"type": "string", "description": "en, fr, es, de, ar or ar-EG"},
       "level": {"type": "string", "description": f"CEFR level, one of {', '.join(LEVELS)}; A2 if not said"},
       "topic": {"type": "string", "description": "what to talk about, if the user said"}},
      required=("language",), level="act")
def practice_start(language: str, level: str = "A2", topic: str = "", ctx=None) -> str:
    try:
        practice = _session(ctx)
        tag = language if language in lang_mod.TAGS else _BY_NAME.get(language.strip().lower(), language)
        practice.start(tag, level, topic, home=getattr(ctx, "tag", "en"))
    except (LookupError, ValueError) as exc:
        return f"error: {exc}"
    return (f"Practice started in {lang_mod.NAMES[practice.tag]} at level {practice.level}. Greet the user and "
            f"ask your first question in {lang_mod.NAMES[practice.tag]} now, whatever language this request "
            f"asked for.")


@tool("practice_correct", "Note one mistake the learner made during language practice, for the panel and the "
      "history of common mistakes.",
      {"said": {"type": "string", "description": "what the user said"},
       "correct": {"type": "string", "description": "the correct form"},
       "note": {"type": "string", "description": "a one-line explanation, in the user's own language"}},
      required=("said", "correct"), level="act")
def practice_correct(said: str, correct: str, note: str = "", ctx=None) -> str:
    try:
        practice = _session(ctx)
    except LookupError as exc:
        return f"error: {exc}"
    if not practice.active:
        return "error: no practice session is running"
    practice.correct(said, correct, note)
    return "noted"


@tool("practice_end", "End language practice and get the list of what the learner should work on.",
      level="act")
def practice_end(ctx=None) -> str:
    try:
        practice = _session(ctx)
    except LookupError as exc:
        return f"error: {exc}"
    if not practice.active:
        return "error: no practice session is running"
    home = lang_mod.NAMES.get(practice.home, "English")
    return practice.end() + f"\nTell the user, in {home}, the two or three things to work on."
