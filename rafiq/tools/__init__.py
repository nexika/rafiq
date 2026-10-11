"""Rafiq's skills. Each tool is a plain function with a description and a JSON schema for its inputs;
a tool that changes something outside Rafiq names the action, and Rafiq asks before running it.

Each tool also declares what it may do (its level), whether it takes long enough that Rafiq should say
something meanwhile, whether it may be offered to other programs over MCP, and how the panel shows
its result. The rules are checked when the tool is registered, so a tool that breaks them never loads.
"""

from __future__ import annotations

import dataclasses
import importlib
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
    # None: runs without asking. A string or a function of the arguments (and of the context, if it
    # takes `ctx`): the action to confirm out loud first, in words the user hears ("shut down the
    # computer"); a function may return None to run without asking.
    confirm: str | Callable | None = None
    level: str = "safe"
    slow: bool = False
    # What Rafiq says while a slow tool runs, by language tag (all six).
    waiting_phrase: dict = field(default_factory=dict)
    expose: bool = False             # may be offered to other programs over MCP
    card: str = "text"

    def schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": list(self.required)}

    def action(self, args: dict, ctx=None) -> str | None:
        if self.confirm is None or not callable(self.confirm):
            return self.confirm
        if "ctx" in inspect.signature(self.confirm).parameters:
            return self.confirm(args, ctx=ctx)
        return self.confirm(args)

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


# Skills that need an account or another app, loaded only when enabled in their config section.
OPTIONAL = ("google", "discord", "instagram", "obs", "spotify")


def load_all(cfg: dict) -> dict[str, Tool]:
    """Import every skill module; the optional ones, Home Assistant, Hue, the smart plugs and presence only
    when they are set up. The scene tool names the scenes in the config, so the brain knows which exist."""
    from . import background, everyday, lists, media, memory, notes, pc, scenes, screen, style, web  # noqa: F401  (registers the tools)

    off = set()
    for name in OPTIONAL:
        if cfg.get(name, {}).get("enabled"):
            importlib.import_module(f"{__name__}.{name}")
        else:
            off.add(f"{__name__}.{name}")
    if cfg.get("google", {}).get("enabled"):
        from . import parcels  # noqa: F401
    else:
        off.add(f"{__name__}.parcels")
    if cfg.get("home_assistant", {}).get("url") and cfg.get("home_assistant", {}).get("token"):
        from . import home  # noqa: F401
    else:
        off.add(f"{__name__}.home")
    if cfg.get("hue", {}).get("enabled"):
        from . import hue  # noqa: F401
    else:
        off.add(f"{__name__}.hue")
    if cfg.get("plugs"):
        from . import plugs

        plugs.configure(cfg)
    else:
        off.add(f"{__name__}.plugs")
    if cfg.get("presence", {}).get("phone_ip"):
        from . import presence  # noqa: F401
    else:
        off.add(f"{__name__}.presence")
    if cfg.get("deadlines", {}).get("feeds"):
        from . import agenda  # noqa: F401
    else:
        off.add(f"{__name__}.agenda")
    from . import brief  # noqa: F401  (last: it gathers the others)
    # A module imported earlier (by a setup command, or a test) stays registered: leave out the ones turned off.
    tools = {name: t for name, t in REGISTRY.items() if t.fn.__module__ not in off}
    found = scenes.scenes_from(cfg)
    if found:
        run = tools["run_scene"]
        tools["run_scene"] = dataclasses.replace(run, description=f"{run.description} The scenes: "
                                                 f"{scenes.describe(found)}.")
    return tools


# What a small local model gets by default. Every extra tool is one more wrong choice for a 7B model,
# and ours skipped tools when it had all of them; these cover what people ask a voice assistant most.
# Left out: list_apps, lock_screen, open_in_browser, read_page, list_timers, cancel_timer, calendar_add,
# email_read and email_send. [brain] ollama_tools = "all" sends everything, or a list names the set.
LOCAL_TOOLS = ("get_time", "set_timer", "set_reminder", "weather", "calculate", "convert_units", "web_search",
               "open_app", "volume", "media", "system_status", "power", "calendar_events", "email_list",
               "remember", "recall", "forget")


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


# OpenAI's chat completions take tools in the same shape as Ollama.
openai_schemas = ollama_schemas
