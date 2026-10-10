"""Personalities: how Rafiq talks, chosen by voice, in the panel or in the config file.

The style goes in the note before each request, not in the system prompt, so switching it does not
make a local model read the system prompt and tools again. Each style is described by tone rather
than by jokes or idioms, so it carries over to all six languages.
"""

from __future__ import annotations

from . import config

DEFAULT = "neutral"

PERSONAS = {
    "neutral": "",
    "concise": "Style: concise. Answer in one short sentence, or a few words when that is enough. No "
               "greetings, no filler, no follow-up questions unless you must.",
    "butler": "Style: a dry, witty butler. Unflappably polite and a little formal, with understated, "
              "deadpan humour now and then: wit from tone, understatement and gentle irony, never puns or "
              "idioms that only work in one language. In Egyptian Arabic, the warm, teasing wit (خفة دم) "
              "of a polite Egyptian, still respectful. The wit never gets in the way of the answer.",
}


def _saved_path():
    return config.home() / "persona"


def active(cfg: dict) -> str:
    """The style in use: the one last chosen by voice or in the panel, else the config's, else neutral."""
    path = _saved_path()
    chosen = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    if chosen not in PERSONAS:
        chosen = cfg.get("brain", {}).get("persona", DEFAULT)
    return chosen if chosen in PERSONAS else DEFAULT


def choose(name: str) -> str:
    """Switch style and remember it across restarts."""
    if name not in PERSONAS:
        raise ValueError(f"unknown style {name!r}; choose one of {', '.join(PERSONAS)}")
    path = _saved_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name, encoding="utf-8")
    return name


def instruction(name: str) -> str:
    return PERSONAS.get(name, "")
