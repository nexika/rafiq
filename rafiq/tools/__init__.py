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
    # None: runs without asking. A string or a function of the arguments: the action to confirm out
    # loud first, in words the user hears ("shut down the computer").
    confirm: str | Callable | None = None

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


def tool(name: str, description: str, params: dict | None = None, required: tuple = (), confirm=None):
    def register(fn: Callable) -> Callable:
        REGISTRY[name] = Tool(name, description, fn, params or {}, tuple(required), confirm)
        return fn
    return register


def load_all(cfg: dict) -> dict[str, Tool]:
    """Import every skill module; Google only when it is set up."""
    from . import everyday, pc, web  # noqa: F401  (registers the tools)

    if cfg.get("google", {}).get("enabled"):
        from . import google  # noqa: F401
    return dict(REGISTRY)


# What a small local model gets by default. Every extra tool is one more wrong choice for a 7B model,
# and ours skipped tools when it had all of them; these cover what people ask a voice assistant most.
# Left out: list_apps, lock_screen, open_in_browser, read_page, list_timers, cancel_timer, calendar_add,
# email_read and email_send. [brain] ollama_tools = "all" sends everything, or a list names the set.
LOCAL_TOOLS = ("get_time", "set_timer", "set_reminder", "weather", "calculate", "convert_units", "web_search",
               "open_app", "volume", "system_status", "power", "calendar_events", "email_list")


def local_tools(tools: dict[str, Tool], choice: str | list = "local") -> dict[str, Tool]:
    """The tools to send a local model: "all", "local" (the set above) or a list of names. Names of
    tools that are not loaded (Google switched off, a typo) are skipped."""
    if choice == "all":
        return dict(tools)
    names = LOCAL_TOOLS if choice == "local" else choice
    return {name: tools[name] for name in names if name in tools}


def claude_schemas(tools: dict[str, Tool]) -> list[dict]:
    return [{"name": t.name, "description": t.description, "input_schema": t.schema()} for t in tools.values()]


def ollama_schemas(tools: dict[str, Tool]) -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": t.schema()}} for t in tools.values()]
