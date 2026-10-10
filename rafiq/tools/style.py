"""Rafiq's own settings that the user can change by voice."""

from __future__ import annotations

from .. import persona
from . import tool


@tool("set_personality", "Change how Rafiq talks from now on: neutral (plain and friendly), concise (as few "
      "words as possible) or butler (a dry, witty, polite butler). Use it when the user asks for another "
      "style, to be shorter, or to drop the jokes.",
      {"style": {"type": "string", "enum": list(persona.PERSONAS)}}, required=("style",))
def set_personality(style: str) -> str:
    try:
        persona.choose(style)
    except ValueError as exc:
        return f"error: {exc}"
    return f"style is now {style}; answer in that style from now on, starting with this answer"
