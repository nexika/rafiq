"""The phone bridge: send Rafiq a note, a shared link or a command from your phone.

An iOS Shortcut, Android's HTTP Shortcuts or Tasker POSTs JSON to /api/inbox with the phone token:

    {"text": "film mode"}                                   a command, answered in the reply
    {"kind": "note", "list": "shopping", "text": "milk"}    filed in ideas, shopping or tasks
    {"kind": "share", "text": "Read later", "url": "..."}   kept in a list (ideas by default)

A command from the phone may only run safe (level 1) tools. The model still sees the others, so it
can explain: calling one answers "do that at home by voice" and runs nothing, and nothing from here
ever asks for a confirmation, because nobody at home would hear the question.

The bridge listens on this computer only unless `[phone] host` says otherwise, and never on a public
address: from the street, the phone reaches it over Tailscale (`host = "tailscale"`), never through a
public tunnel. The token crosses the network in plain HTTP, so only a private network may carry it (#70).
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import subprocess
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import lang
from .brain import Brain
from .http_body import BodyError, body_length
from .remote import TAILSCALE, _private_ip, check_listen
from .tools import Tool, lists

MAX_BODY = 10_000
MAX_TEXT = 2000
KINDS = ("command", "note", "share")
# The safe (level 1) tools: they only read, or change nothing but Rafiq's own timers and notes.
PHONE_TOOLS = frozenset({"get_time", "set_timer", "set_reminder", "list_timers", "cancel_timer", "weather",
                         "calculate", "convert_units", "list_apps", "system_status", "web_search", "read_page",
                         "add_note", "list_notes", "remove_note"})


def remote_tools(tools: dict[str, Tool]) -> dict[str, Tool]:
    """The tools as seen from the phone: safe ones as they are, every other one replaced by a stand-in
    with the same name and inputs that refuses."""
    def refuse(ctx=None) -> str:
        return ("refused: not allowed on a request sent from the phone; nothing was done. Tell the user: "
                + lang.phrase("at_home", getattr(ctx, "tag", "en")))

    return {name: tool if name in PHONE_TOOLS and tool.confirm is None else
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
            name = data.get("list") if data.get("list") in lists.LISTS else "ideas"
            url = data.get("url")
            if isinstance(url, str) and urlparse(url).scheme in ("http", "https"):
                text = f"{text} {url.strip()}"
            result = lists.add_note(name, text, source="phone")
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
                length = body_length(self.headers.get("Content-Length"), MAX_BODY)
            except BodyError as exc:
                return self._send(exc.status, {"error": str(exc)})
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


def tailscale_ip(run=subprocess.run) -> str:
    """This computer's address on the tailnet (100.64.0.0/10), from `tailscale ip -4`."""
    fix = "install Tailscale on this computer and the phone, then run `sudo tailscale up`"
    try:
        done = run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise ValueError(f"Tailscale is not available: {fix}") from None
    for line in done.stdout.split() if done.returncode == 0 else []:
        try:
            if ipaddress.ip_address(line) in TAILSCALE:
                return line
        except ValueError:
            continue
    raise ValueError(f"this computer has no Tailscale address: {fix}")


def address(host: str, run=subprocess.run) -> str:
    """Where the bridge listens: "tailscale" is this computer's tailnet address; anything else must be this
    computer, the LAN or Tailscale. Every network (0.0.0.0) and public addresses raise ValueError."""
    if host == "tailscale":
        return tailscale_ip(run)
    try:
        check_listen(host, tls=False)
    except ValueError:
        raise ValueError(f'the phone bridge never listens on {host}: set [phone] host = "tailscale" '
                         "(or this computer's LAN address, such as 192.168.1.20)") from None
    return host


def serve(bridge: PhoneBridge, host: str, port: int, token: str) -> ThreadingHTTPServer:
    if len(token) < 32:
        raise ValueError("the phone token is too short")
    check_listen(host, tls=False)
    server = ThreadingHTTPServer((host, port), make_handler(bridge, token))
    if not _private_ip(server.server_address[0]):    # what was bound, whatever the name resolved to
        server.server_close()
        raise ValueError(f"the phone bridge bound {server.server_address[0]}, which is not a private address")
    server.daemon_threads = True
    return server
