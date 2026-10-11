"""How well the local model picks tools: a fixed list of requests, each sent once, scored on whether the
model called the expected tool (or none) and answered in the right language. Nothing is run; only the
model's first reply counts. `rafiq toolcheck` compares every tool against the smaller local set.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from . import lang as lang_mod
from .brain import system_prompt, with_note
from .tools import Tool, ollama_schemas


@dataclass(frozen=True)
class Request:
    text: str
    tag: str
    tool: str | None       # the tool the model should call first; None: it should just answer


REQUESTS = (
    Request("what time is it", "en", "get_time"),
    Request("set a timer for ten minutes", "en", "set_timer"),
    Request("remind me to call mum at six", "en", "set_reminder"),
    Request("what's the weather like tomorrow", "en", "weather"),
    Request("what is 15 percent of 240", "en", "calculate"),
    Request("how many kilometres is twenty miles", "en", "convert_units"),
    Request("who won the last world cup", "en", "web_search"),
    Request("open firefox", "en", "open_app"),
    Request("turn the volume down to thirty", "en", "volume"),
    Request("tell me a short joke", "en", None),
    Request("quelle heure est-il", "fr", "get_time"),
    Request("mets un minuteur de cinq minutes", "fr", "set_timer"),
    Request("¿qué tiempo hace hoy?", "es", "weather"),
    Request("wie viel Speicher ist noch frei", "de", "system_status"),
    Request("الساعة كام دلوقتي", "ar-EG", "get_time"),
    Request("اعمل منبه بعد ربع ساعة", "ar-EG", "set_timer"),
    Request("الجو عامل ايه النهارده", "ar-EG", "weather"),
    Request("إزيك عامل ايه", "ar-EG", None),
    Request("ما هي عاصمة أستراليا", "ar", None),
    Request("كم يساوي سبعة في ثمانية", "ar", "calculate"),
)


@dataclass
class Result:
    request: Request
    called: list[str] = field(default_factory=list)
    answer: str = ""

    @property
    def right_tool(self) -> bool:
        return (self.called[:1] == [self.request.tool]) if self.request.tool else not self.called

    @property
    def right_language(self) -> bool:
        """Arabic script for Arabic, and for the others no Arabic script and no sign of another
        language. A reply that only calls a tool has nothing to judge."""
        if not self.answer:
            return True
        tag = self.request.tag
        if tag.startswith("ar"):
            return lang_mod.script_language(self.answer, tag) == tag
        return lang_mod.script_language(self.answer, tag) == tag and lang_mod.guess(self.answer, tag) == tag


def run(cfg: dict, tools: dict[str, Tool], requests, ask: Callable[[dict], dict]) -> list[Result]:
    """Send each request with these tools; `ask` takes the chat request and returns the model's message."""
    schemas = ollama_schemas(tools)
    results = []
    for request in requests:
        message = ask({"messages": [{"role": "system", "content": system_prompt(cfg)},
                                    {"role": "user", "content": with_note(request.text, request.tag)}],
                       "tools": schemas})
        called = [(c.get("function") or {}).get("name", "") for c in message.get("tool_calls") or []]
        results.append(Result(request, called, (message.get("content") or "").strip()))
    return results


def score(results: list[Result]) -> tuple[int, int, int]:
    """(requests, right tool, right language)."""
    return len(results), sum(r.right_tool for r in results), sum(r.right_language for r in results)


def ollama_ask(cfg: dict) -> Callable[[dict], dict]:
    import httpx

    brain = cfg["brain"]

    def ask(payload: dict) -> dict:
        resp = httpx.post(f"{brain['ollama_url']}/api/chat", timeout=120, json={
            **payload, "model": brain["ollama_model"], "stream": False, "keep_alive": "30m",
            "options": {"num_predict": brain["max_tokens"], "num_ctx": 8192}})
        resp.raise_for_status()
        return resp.json().get("message") or {}

    return ask
