"""Rafiq's skills. Each tool is a plain function with a description and a JSON schema for its inputs;
a tool that changes something outside Rafiq names the action, and Rafiq asks before running it.

Each tool also declares what it may do (its level), whether it takes long enough that Rafiq should say
something meanwhile, whether it may be offered to other programs over MCP, and how the panel shows
its result. The rules are checked when the tool is registered, so a tool that breaks them never loads.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field

from ..lang import TAGS

# safe: no personal data and no change. private: reads the user's own data (mail, calendar, reminders).
# act: changes something that is easy to undo (a timer, the volume, an open app).
# critical: hard to undo, or speaks for the user (power, sending mail, adding an event); always asked first.
LEVELS = ("safe", "private", "act", "critical")
# text: shown as it is. list: one item per line. facts: "name: value" lines.
CARDS = ("text", "list", "facts")


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable
    params: dict = field(default_factory=dict)
    required: tuple = ()
    # None: runs without asking. A string or a function of the arguments: the action to confirm out
    # loud first, in words the user hears ("shut down the computer").
    confirm: str | Callable | None = None
    level: str = "safe"
    slow: bool = False
    # What Rafiq says while a slow tool runs, by language tag (all six).
    waiting_phrase: dict = field(default_factory=dict)
    expose: bool = False             # may be offered to other programs over MCP
    card: str = "text"

    def schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": list(self.required)}

    def action(self, args: dict) -> str | None:
        if self.confirm is None:
            return None
        return self.confirm(args) if callable(self.confirm) else self.confirm

    def waiting(self, tag: str) -> str | None:
        if not self.slow:
            return None
        return self.waiting_phrase.get(tag, self.waiting_phrase.get("en"))

    def run(self, args: dict, ctx) -> str:
        accepted = inspect.signature(self.fn).parameters
        kwargs = {k: v for k, v in args.items() if k in accepted}
        if "ctx" in accepted:
            kwargs["ctx"] = ctx
        result = self.fn(**kwargs)
        return result if isinstance(result, str) else str(result)


REGISTRY: dict[str, Tool] = {}


def check(t: Tool) -> None:
    """Refuse a tool whose declaration breaks the rules above."""
    if t.level not in LEVELS:
        raise ValueError(f"{t.name}: level must be one of {LEVELS}")
    if t.card not in CARDS:
        raise ValueError(f"{t.name}: card must be one of {CARDS}")
    if (t.confirm is not None) != (t.level == "critical"):
        raise ValueError(f"{t.name}: a critical tool asks first, and only a critical tool does")
    if t.expose and t.level != "safe":
        raise ValueError(f"{t.name}: only a safe tool may be exposed over MCP")
    if t.slow and (set(t.waiting_phrase) != set(TAGS) or not all(t.waiting_phrase.values())):
        raise ValueError(f"{t.name}: a slow tool needs a waiting phrase in each of {TAGS}")
    if not t.slow and t.waiting_phrase:
        raise ValueError(f"{t.name}: only a slow tool has a waiting phrase")


def tool(name: str, description: str, params: dict | None = None, required: tuple = (), confirm=None, *,
         level: str, slow: bool = False, waiting_phrase: dict | None = None, expose: bool = False,
         card: str = "text"):
    def register(fn: Callable) -> Callable:
        t = Tool(name, description, fn, params or {}, tuple(required), confirm, level, slow,
                 dict(waiting_phrase or {}), expose, card)
        check(t)
        REGISTRY[name] = t
        return fn
    return register


def load_all(cfg: dict) -> dict[str, Tool]:
    """Import every skill module; Google only when it is set up."""
    from . import everyday, pc, web  # noqa: F401  (registers the tools)

    if cfg.get("google", {}).get("enabled"):
        from . import google  # noqa: F401
    return dict(REGISTRY)


def claude_schemas(tools: dict[str, Tool]) -> list[dict]:
    return [{"name": t.name, "description": t.description, "input_schema": t.schema()} for t in tools.values()]


def ollama_schemas(tools: dict[str, Tool]) -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": t.schema()}} for t in tools.values()]
