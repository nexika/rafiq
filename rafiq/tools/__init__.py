"""Rafiq's skills. Each tool is a plain function with a description and a JSON schema for its inputs;
a tool that changes something outside Rafiq names the action, and Rafiq asks before running it."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable
    params: dict = field(default_factory=dict)
    required: tuple = ()
    # None: runs without asking. A string or a function of the arguments (and of the context, if it
    # takes `ctx`): the action to confirm out loud first, in words the user hears ("shut down the
    # computer"); a function may return None to run without asking.
    confirm: str | Callable | None = None

    def schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": list(self.required)}

    def action(self, args: dict, ctx=None) -> str | None:
        if self.confirm is None or not callable(self.confirm):
            return self.confirm
        if "ctx" in inspect.signature(self.confirm).parameters:
            return self.confirm(args, ctx=ctx)
        return self.confirm(args)

    def run(self, args: dict, ctx) -> str:
        accepted = inspect.signature(self.fn).parameters
        kwargs = {k: v for k, v in args.items() if k in accepted}
        if "ctx" in accepted:
            kwargs["ctx"] = ctx
        result = self.fn(**kwargs)
        return result if isinstance(result, str) else str(result)


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, params: dict | None = None, required: tuple = (), confirm=None):
    def register(fn: Callable) -> Callable:
        REGISTRY[name] = Tool(name, description, fn, params or {}, tuple(required), confirm)
        return fn
    return register


def load_all(cfg: dict) -> dict[str, Tool]:
    """Import every skill module; Google only when it is set up."""
    from . import everyday, pc, screen, web  # noqa: F401  (registers the tools)

    if cfg.get("google", {}).get("enabled"):
        from . import google  # noqa: F401
    return dict(REGISTRY)


def claude_schemas(tools: dict[str, Tool]) -> list[dict]:
    return [{"name": t.name, "description": t.description, "input_schema": t.schema()} for t in tools.values()]


def ollama_schemas(tools: dict[str, Tool]) -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": t.schema()}} for t in tools.values()]
