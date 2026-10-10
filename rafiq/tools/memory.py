"""Long-term memory: remember, recall and forget facts about the user (see rafiq/facts.py)."""

from __future__ import annotations

from . import tool

NO_MEMORY = "error: long-term memory is not available"
BROKEN = "error: the memory file can't be read ({}); the user can fix it by hand"


def _facts(ctx):
    return getattr(ctx, "facts", None)


@tool("remember", "Remember a fact about the user for later conversations, when they ask you to remember it.",
      {"text": {"type": "string", "description": "the fact as a short sentence in the user's language, "
                                                 "such as 'The user's sister is called Sara'"}},
      required=("text",), level="act")
def remember(text: str, ctx=None) -> str:
    facts = _facts(ctx)
    if facts is None:
        return NO_MEMORY
    if not text.strip():
        return "error: nothing to remember"
    try:
        fact = facts.add(text)
    except (OSError, ValueError) as exc:
        return BROKEN.format(exc)
    return f"remembered {fact.id}: {fact.text}"


@tool("recall", "What you remember about the user: facts matching some words, or everything.",
      {"query": {"type": "string", "description": "words to look for; empty for everything"}}, level="private")
def recall(query: str = "", ctx=None) -> str:
    facts = _facts(ctx)
    if facts is None:
        return NO_MEMORY
    found = facts.search(query) if query.strip() else facts.all()
    if not found:
        return f"nothing remembered about {query!r}" if query.strip() else "nothing remembered"
    return "\n".join(f"{f.id}: {f.text}" for f in found[:50])


@tool("forget", "Delete remembered facts for good, by id (from recall) or by words in them.",
      {"id": {"type": "string"}, "words": {"type": "string", "description": "words the facts contain"}},
      level="act")
def forget(id: str = "", words: str = "", ctx=None) -> str:  # noqa: A002 (the model's word)
    facts = _facts(ctx)
    if facts is None:
        return NO_MEMORY
    if not id and not words.strip():
        return "error: give the id or words of what to forget"
    try:
        gone = facts.forget(id or None, words.strip() or None)
    except (OSError, ValueError) as exc:
        return BROKEN.format(exc)
    return f"forgot {len(gone)}: " + "; ".join(f.text for f in gone) if gone else "nothing matched"
