"""Rafiq's skills. Each tool is a plain function with a description and a JSON schema for its inputs;
a tool that changes something outside Rafiq names the action, and Rafiq asks before running it. Its
permission level (see permissions.py) says whether "yes, always" may be remembered."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field

from ..permissions import SAFE, SENSITIVE


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
    # 1, 2 or 3 (permissions.py). Left out: 1 for a tool that doesn't ask, 3 for one that does, so a
    # new tool that asks is asked about every time until it is marked reversible.
    level: int | None = None

    def __post_init__(self) -> None:
        if self.level is None:
            self.level = SAFE if self.confirm is None else SENSITIVE

    def schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": list(self.required)}

    def action(self, args: dict) -> str | None:
        if self.confirm is None:
            return None
        return self.confirm(args) if callable(self.confirm) else self.confirm

    def run(self, args: dict, ctx) -> str:
        accepted = inspect.signature(self.fn).parameters
        kwargs = {k: v for k, v in args.items() if k in accepted}
        if "ctx" in accepted:
            kwargs["ctx"] = ctx
        result = self.fn(**kwargs)
        return result if isinstance(result, str) else str(result)


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, params: dict | None = None, required: tuple = (), confirm=None,
         level: int | None = None):
    def register(fn: Callable) -> Callable:
        REGISTRY[name] = Tool(name, description, fn, params or {}, tuple(required), confirm, level)
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
