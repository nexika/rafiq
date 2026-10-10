"""The phone bridge: send Rafiq a note, a shared link or a command from your phone.

An iOS Shortcut, Android's HTTP Shortcuts or Tasker POSTs JSON to /api/inbox with the phone token:

    {"text": "film mode"}                                   a command, answered in the reply
    {"kind": "note", "list": "shopping", "text": "milk"}    filed in ideas, shopping or tasks
    {"kind": "share", "text": "Read later", "url": "..."}   kept in a list (ideas by default)

A command from the phone may only run safe (level 1) tools. The model still sees the others, so it
can explain: calling one answers "do that at home by voice" and runs nothing, and nothing from here
ever asks for a confirmation, because nobody at home would hear the question.

The bridge listens on this computer only unless `[phone] host` says otherwise; reaching it from the
street needs a tunnel or a VPN in front of it.
"""

from __future__ import annotations

import hmac
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import lang
from .brain import Brain
from .tools import Tool, notes

MAX_BODY = 10_000
MAX_TEXT = 2000
KINDS = ("command", "note", "share")


def remote_tools(tools: dict[str, Tool]) -> dict[str, Tool]:
    """The tools as seen from the phone: safe ones as they are, every other one replaced by a stand-in
    with the same name and inputs that refuses."""
    def refuse(ctx=None) -> str:
        return ("refused: not allowed on a request sent from the phone; nothing was done. Tell the user: "
                + lang.phrase("at_home", getattr(ctx, "tag", "en")))

    return {name: tool if tool.safe and tool.confirm is None else
            Tool(name, tool.description + " (Not available from the phone.)", refuse, tool.params, tool.required)
            for name, tool in tools.items()}


class PhoneBridge:
    def __init__(self, cfg: dict, tools: dict[str, Tool], scheduler,
                 on_event: Callable[[str, dict], None] | None = None):
        self.cfg, self.scheduler = cfg, scheduler
        self.on_event = on_event or (lambda kind, data: None)
        # Its own conversation, so a phone request never lands in the middle of one at home.
        self.brain = Brain(cfg, remote_tools(tools), lambda action, tag: False, on_event=self.on_event)
        self.lock = threading.Lock()

    def inbox(self, data) -> tuple[int, dict]:
        """(HTTP status, reply) for one request from the phone."""
        if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not data["text"].strip():
            return 400, {"error": "send JSON with a text"}
        kind = data.get("kind") or "command"
        if kind not in KINDS:
            return 400, {"error": f"kind is one of {', '.join(KINDS)}"}
        text = data["text"].strip()[:MAX_TEXT]
        self.on_event("phone", {"kind": kind, "text": text})
        if kind in ("note", "share"):
            name = data.get("list") if data.get("list") in notes.LISTS else "ideas"
            url = data.get("url")
            if isinstance(url, str) and urlparse(url).scheme in ("http", "https"):
                text = f"{text} {url.strip()}"
            result = notes.add_note(name, text, source="phone")
            if result.startswith("error"):
                return 400, {"error": result}
            return 200, {"ok": True, "list": name, "answer": result}
        from .assistant import Context

        tag = lang.dialect(lang.guess(text), text)
        with self.lock:
            answer = self.brain.think(text, tag, Context(self.cfg, self.scheduler, tag))
        return 200, {"ok": True, "answer": answer}


def make_handler(bridge: PhoneBridge, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Rafiq"

        def log_message(self, *_args):
            pass

        def _send(self, code: int, reply: dict) -> None:
            body = json.dumps(reply, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _token_ok(self) -> bool:
            given = self.headers.get("X-Rafiq-Token") or ""
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                given = auth[7:].strip()
            return bool(given) and hmac.compare_digest(given.encode(), token.encode())

        def do_GET(self):
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._token_ok():
                return self._send(403, {"error": "forbidden"})
            if urlparse(self.path).path != "/api/inbox":
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "bad length"})
            if length > MAX_BODY:
                return self._send(413, {"error": "too large"})
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            try:
                code, reply = bridge.inbox(data)
            except Exception as exc:
                bridge.on_event("error", {"error": f"{type(exc).__name__}: {exc}"})
                code, reply = 500, {"error": "something went wrong"}
            return self._send(code, reply)

    return Handler


def serve(bridge: PhoneBridge, host: str, port: int, token: str) -> ThreadingHTTPServer:
    if len(token) < 32:
        raise ValueError("the phone token is too short")
    server = ThreadingHTTPServer((host, port), make_handler(bridge, token))
    server.daemon_threads = True
    return server
