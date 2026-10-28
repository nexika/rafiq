"""Tools the brain uses to put its work in the widget and the dashboard: a plan with its progress, an
explanation said aloud, and cards. Whichever brain runs them, Rafiq's own or a Claude or Codex app over MCP,
they only show and speak and change nothing outside Rafiq.

What the model sends is cut to a size here, and kept as text: the dashboard and the widget never run it as
HTML or script."""

from __future__ import annotations

from .. import lang as lang_mod
from . import tool

KINDS = ("plan", "explain", "card")
CARD_KINDS = ("music", "news", "weather", "timer")
TITLE_CHARS, MAX_STEPS, STEP_CHARS = 120, 12, 120
EXPLAIN_CHARS = 1500                 # about a minute said aloud
CARD_FIELDS, KEY_CHARS, FIELD_CHARS = 8, 40, 200
MAX_WAITING = 5                      # from other programs, queued and not yet shown: more is refused

NO_SCREEN = "error: there is no screen to show it on here; give it in your answer instead"
SAID = "Shown and said aloud; don't repeat it in your answer."
NOT_SAID = "Shown on screen only, not said aloud; give it in your answer too."
SENT = "Sent to the running Rafiq, which shows it and says it aloud unless it is muted."


def clean(kind: str, data) -> dict:
    """What may be shown, cut to size; ValueError for anything else. The panel's /api/display uses it too."""
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    if kind == "plan":
        title, steps = data.get("title"), data.get("steps")
        if not isinstance(title, str) or not isinstance(steps, list) or not steps:
            raise ValueError("a plan needs a title and a list of steps")
        steps = [s[:STEP_CHARS] for s in steps if isinstance(s, str)][:MAX_STEPS]
        if not steps:
            raise ValueError("a plan needs a title and a list of steps")
        done = data.get("done", 0)
        if isinstance(done, float) and done.is_integer():      # 2.0 from a model that writes numbers so
            done = int(done)
        done = done if isinstance(done, int) and not isinstance(done, bool) else 0
        return {"title": title[:TITLE_CHARS], "steps": steps, "done": max(0, min(done, len(steps)))}
    if kind == "explain":
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("nothing to explain")
        tag = data.get("tag")
        tag = tag if tag in lang_mod.TAGS else lang_mod.script_language(text, "en")
        return {"text": text[:EXPLAIN_CHARS], "tag": tag}
    if kind == "card":
        # "card", not "kind": the event's own kind is "card", and the two are one object on the stream.
        card, fields = data.get("card"), data.get("data")
        if card not in CARD_KINDS:
            raise ValueError(f"a card is one of {', '.join(CARD_KINDS)}")
        if not isinstance(fields, dict):
            raise ValueError("a card's data must be an object")
        kept = [(k, v) for k, v in fields.items() if isinstance(v, str | int | float) and not isinstance(v, bool)]
        return {"card": card, "data": {str(k)[:KEY_CHARS]: str(v)[:FIELD_CHARS] for k, v in kept[:CARD_FIELDS]}}
    raise ValueError(f"can only show {', '.join(KINDS)}")


def _show(ctx, kind: str, data: dict) -> tuple[str | None, bool | None]:
    """Show it: an error for the model when it can't be, and whether it was said aloud (None: not known here,
    the running Rafiq decides)."""
    show = getattr(ctx, "show", None)
    if show is None:
        return NO_SCREEN, None
    try:
        return None, show(kind, clean(kind, data))
    except (ValueError, RuntimeError) as exc:
        return f"error: {exc}", None


@tool("show_steps", "Show the user your plan for a task with several steps, and how far you got. Call it "
      "again with a higher done as each step finishes.",
      {"title": {"type": "string", "description": "the task, in a few words in the user's language"},
       "steps": {"type": "array", "items": {"type": "string"}, "description": "the steps, short, in order"},
       "done": {"type": "integer", "description": "how many steps are done, 0 at the start"}},
      required=("title", "steps"), level="safe", expose=True)
def show_steps(title: str, steps: list, done: int = 0, ctx=None) -> str:
    return _show(ctx, "plan", {"title": title, "steps": steps, "done": done})[0] or "shown"


@tool("explain", "Show an explanation on the screen and read it aloud with Rafiq's voice: a concept, what is "
      "on the screen, the steps of a solution. Keep it under a minute to say.",
      {"text": {"type": "string", "description": "the explanation, as plain sentences to be said aloud"},
       "lang": {"type": "string", "enum": list(lang_mod.TAGS),
                "description": "its language; the conversation's when not given"}},
      required=("text",), level="safe", expose=True)
def explain(text: str, lang: str = "", ctx=None) -> str:
    # Over MCP the context's language is always English: a missing or odd lang is read from the script.
    tag = lang if lang in lang_mod.TAGS else lang_mod.script_language(text, getattr(ctx, "tag", "en"))
    error, said = _show(ctx, "explain", {"text": text, "tag": tag})
    return error or {True: SAID, False: NOT_SAID, None: SENT}[said]


@tool("show_card", "Show a card with what you found: music playing, news, weather or a timer.",
      {"kind": {"type": "string", "enum": list(CARD_KINDS)},
       "data": {"type": "object", "description": "a few short fields, such as {\"city\": \"Cairo\", "
                                                 "\"temperature\": \"31°C\"}"}},
      required=("kind", "data"), level="safe", expose=True)
def show_card(kind: str, data: dict, ctx=None) -> str:
    return _show(ctx, "card", {"card": kind, "data": data})[0] or "shown"
