"""The brain on an always-on machine, this PC as its body.

`rafiq run --server` runs the brain (models, keys, confirmations, alarms) without a microphone or a
speaker. `rafiq agent` runs on the PC and lends it the screen, the speakers and the apps. The agent
only connects outward: it asks the server for the next action (a long poll over HTTP), runs it and
posts the result back, so the PC opens no port.

Each agent has its own token, and the server keeps only a hash of it. The agent runs only the actions
in its allowlist, with the arguments each one takes and of the right types; there is no shell, and no
action can become one. The token travels over TLS or a private network (the LAN, Tailscale): the
agent refuses a plain http address on the internet, and the server refuses to listen on one.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import itertools
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import lang
from .tools import Tool

# The tools that act on the PC: on the server they run on the agent. Thinking (calculate, weather,
# search, email...) stays on the brain.
ACTIONS = ("open_app", "list_apps", "volume", "system_status", "lock_screen", "power", "open_in_browser")
# What only the brain asks of the agent: say something, and ask the user yes or no.
AGENT_ONLY = {"say": ({"text": {"type": "string"}, "tag": {"type": "string"}}, ("text",)),
              "confirm": ({"question": {"type": "string"}, "tag": {"type": "string"}}, ("question",))}
WAIT_SECONDS = 25            # how long a poll waits for an action before the agent asks again
CALL_SECONDS = 30
CONFIRM_SECONDS = 60
MAX_BODY = 20_000
TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
TYPES = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,)}


# ---------------------------------------------------------------------- where the token may travel

def _private_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if ip.is_unspecified:     # 0.0.0.0 and :: listen on every network, the internet included
        return False
    return ip.is_loopback or ip.is_private or (ip.version == 4 and ip in TAILSCALE)


def private_or_tls(url: str) -> bool:
    """True when the agent may send its token to this address: https, or http on this computer, the
    local network or Tailscale."""
    parts = urlparse(url)
    if parts.scheme == "https":
        return True
    if parts.scheme != "http" or not parts.hostname:
        return False
    host = parts.hostname.lower()
    return host == "localhost" or host.endswith(".ts.net") or _private_ip(host)


def check_listen(host: str, tls: bool) -> None:
    """Without TLS, the server listens only on a private address (raises ValueError otherwise)."""
    if not tls and host != "localhost" and not _private_ip(host):
        raise ValueError(f"without a TLS certificate the server only listens on a private address "
                         f"(this computer, the LAN or Tailscale), not {host}")


# ---------------------------------------------------------------------- tokens

class AgentTokens:
    """One token per agent, created on the server (`rafiq agent-token NAME`). The file holds only
    their SHA-256, and is read again on each check, so a new agent works without a restart."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        self.path.chmod(0o600)

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def add(self, name: str) -> str:
        if not NAME.fullmatch(name):
            raise ValueError("an agent's name is 1 to 32 letters, digits, - or _")
        token = secrets.token_urlsafe(32)
        data = self._load()
        data[name] = self._hash(token)
        self._save(data)
        return token

    def remove(self, name: str) -> bool:
        data = self._load()
        found = data.pop(name, None) is not None
        self._save(data)
        return found

    def names(self) -> list[str]:
        return sorted(self._load())

    def check(self, name: str, token: str) -> bool:
        stored = self._load().get(name)
        return bool(stored and token) and hmac.compare_digest(stored, self._hash(token))


# ---------------------------------------------------------------------- the brain's side

class Hub:
    """Actions waiting for the agents, and their results. A tool call on the brain's thread waits
    here until the agent has run it."""

    def __init__(self, wait: float = WAIT_SECONDS):
        self.wait = wait
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)
        self._pending: dict[str, list[dict]] = {}
        self._seen: dict[str, float] = {}
        self._waiting: dict[str, tuple[str, threading.Event, list]] = {}   # id -> agent, done, result
        self._ids = itertools.count(1)

    def message(self, action: str, args: dict) -> dict:
        return {"id": f"{next(self._ids)}-{secrets.token_hex(4)}", "action": action, "args": args}

    def agents(self) -> list[str]:
        """The agents that asked for work recently, the most recent first."""
        stale = time.monotonic() - (self.wait + 15)
        with self._lock:
            live = [(seen, name) for name, seen in self._seen.items() if seen > stale]
        return [name for _, name in sorted(live, reverse=True)]

    def call(self, action: str, args: dict, timeout: float | None = None, agent: str | None = None) -> str:
        """Run an action on an agent (the most recent one by default) and return its result."""
        names = self.agents()
        agent = agent or (names[0] if names else None)
        if agent not in names:
            return "error: no desktop agent is connected (start `rafiq agent` on the PC)"
        message = self.message(action, args)
        done, result = threading.Event(), []
        with self._ready:
            self._waiting[message["id"]] = (agent, done, result)
            self._pending.setdefault(agent, []).append(message)
            self._ready.notify_all()
        finished = done.wait(timeout or CALL_SECONDS)
        with self._lock:
            self._waiting.pop(message["id"], None)
            if not finished and message in self._pending.get(agent, []):
                self._pending[agent].remove(message)
        return result[0] if finished else f"error: the desktop agent {agent} did not answer"

    def next(self, agent: str) -> dict | None:
        """The next action for this agent, waiting up to `wait` seconds for one."""
        deadline = time.monotonic() + self.wait
        with self._ready:
            self._seen[agent] = time.monotonic()
            while not self._pending.get(agent):
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._ready.wait(left)
            self._seen[agent] = time.monotonic()
            return self._pending[agent].pop(0)

    def deliver(self, agent: str, message_id: str, result: str) -> bool:
        """A result from an agent; only for an action that was sent to that agent."""
        with self._lock:
            entry = self._waiting.get(message_id)
            if entry is None or entry[0] != agent:
                return False
            entry[2].append(str(result)[:8000])
            entry[1].set()
            return True


@dataclass
class RemoteTool(Tool):
    """A PC tool the brain runs on the agent; its description and confirmation stay the same."""
    hub: Hub | None = None

    def run(self, args: dict, ctx) -> str:
        return self.hub.call(self.name, args)


def remote_tools(tools: dict[str, Tool], hub: Hub) -> dict[str, Tool]:
    return {name: RemoteTool(**{f.name: getattr(t, f.name) for f in fields(Tool)}, hub=hub)
            if name in ACTIONS else t for name, t in tools.items()}


def agent_confirm(hub: Hub) -> Callable[[str, str], bool]:
    """The brain's confirmation, asked on the PC where the user is; no answer is a no."""
    def confirm(action: str, tag: str) -> bool:
        question = lang.phrase("confirm", tag, action=action)
        answer = hub.call("confirm", {"question": question, "tag": tag}, timeout=CONFIRM_SECONDS)
        return answer.strip().lower() == "yes"
    return confirm


def make_handler(hub: Hub, tokens: AgentTokens, ask: Callable[[str, str], str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Rafiq"

        def log_message(self, *_args):
            pass

        def _send(self, code: int, data: dict | None = None) -> None:
            body = json.dumps(data, ensure_ascii=False).encode() if data is not None else b""
            self.send_response(code)
            if body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            name = self.headers.get("X-Rafiq-Agent", "")
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            if not tokens.check(name, token):
                return self._send(403, {"error": "forbidden"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "too large"})
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if not isinstance(data, dict):
                return self._send(400, {"error": "bad json"})
            path = urlparse(self.path).path
            if path == "/agent/poll":
                message = hub.next(name)
                return self._send(200, message) if message else self._send(204)
            if path == "/agent/result" and isinstance(data.get("id"), str) and isinstance(data.get("result"), str):
                return self._send(200 if hub.deliver(name, data["id"], data["result"]) else 404, {})
            if path == "/agent/ask" and isinstance(data.get("text"), str) and data["text"].strip():
                text = data["text"][:2000]
                tag = data.get("lang") if data.get("lang") in lang.TAGS else lang.dialect(lang.guess(text), text)
                return self._send(200, {"answer": ask(text, tag), "tag": tag})
            return self._send(400, {"error": "unknown request"})

    return Handler


def serve(hub: Hub, tokens: AgentTokens, ask: Callable[[str, str], str], host: str, port: int,
          tls_cert: str = "", tls_key: str = "") -> ThreadingHTTPServer:
    check_listen(host, tls=bool(tls_cert))
    server = ThreadingHTTPServer((host, port), make_handler(hub, tokens, ask))
    server.daemon_threads = True
    if tls_cert:
        import ssl

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(os.path.expanduser(tls_cert), os.path.expanduser(tls_key) if tls_key else None)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


# ---------------------------------------------------------------------- the PC's side

def action_specs() -> dict[str, tuple[dict, tuple]]:
    """Each allowed action and the arguments it takes, from the tools' own schemas."""
    from .tools import load_all

    tools = load_all({})
    return {**{name: (tools[name].params, tools[name].required) for name in ACTIONS}, **AGENT_ONLY}


def check_args(args: dict, params: dict, required: tuple) -> dict:
    """The arguments an action takes, of the type its schema names; raises ValueError otherwise."""
    kept = {}
    for key, value in args.items():
        spec = params.get(key)
        if spec is None:
            continue
        types = TYPES.get(spec.get("type", "string"), (str,))
        if not isinstance(value, types) or (isinstance(value, bool) and bool not in types):
            raise ValueError(f"{key} must be a {spec.get('type', 'string')}")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"{key} is one of {', '.join(map(str, spec['enum']))}")
        kept[key] = value
    missing = [key for key in required if key not in kept]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    return kept


class Agent:
    """Asks the server for actions and runs the allowed ones on this PC."""

    def __init__(self, server: str, name: str, token: str, actions: dict[str, Callable],
                 wait: float = WAIT_SECONDS, log: Callable[[str], None] = lambda text: None):
        if not private_or_tls(server):
            raise ValueError(f"{server}: use https, or http on the LAN or Tailscale, so the token is not "
                             f"sent in clear over the internet")
        specs = action_specs()
        self.server, self.name, self.wait, self.log = server.rstrip("/"), name, wait, log
        self.actions = {k: fn for k, fn in actions.items() if k in specs}    # the allowlist, nothing else
        self.specs = specs
        self.headers = {"Authorization": f"Bearer {token}", "X-Rafiq-Agent": name}
        self._stop = threading.Event()

    def handle(self, message: dict) -> str:
        action, args = message.get("action"), message.get("args", {})
        if not isinstance(action, str) or action not in self.actions:
            return f"error: {action!r} is not an action this PC runs"
        if not isinstance(args, dict):
            return "error: args must be an object"
        try:
            kwargs = check_args(args, *self.specs[action])
        except ValueError as exc:
            return f"error: {exc}"
        try:
            result = self.actions[action](**kwargs)
        except Exception as exc:
            return f"error: {type(exc).__name__}: {exc}"
        return str(result)[:8000]

    def ask(self, text: str, tag: str | None = None) -> str:
        """Send a typed request to the brain; returns its answer."""
        import httpx

        resp = httpx.post(f"{self.server}/agent/ask", headers=self.headers, json={"text": text, "lang": tag},
                          timeout=300)
        resp.raise_for_status()
        return resp.json()["answer"]

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        """Poll, run, report, until stopped. A lost connection is retried, waiting longer each time; a
        refused token stops the agent."""
        import httpx

        backoff = 1.0
        with httpx.Client(headers=self.headers, timeout=self.wait + 15) as client:
            while not self._stop.is_set():
                try:
                    resp = client.post(f"{self.server}/agent/poll", json={})
                    if resp.status_code == 403:
                        self.log("the server refused this agent's name or token")
                        return
                    resp.raise_for_status()
                    backoff = 1.0
                    if resp.status_code == 204:
                        continue
                    message = resp.json()
                    result = self.handle(message)
                    self.log(f"{message.get('action')} -> {result[:200]}")
                    client.post(f"{self.server}/agent/result", json={"id": message.get("id"), "result": result})
                except (httpx.HTTPError, ValueError) as exc:
                    self.log(f"server not reachable ({type(exc).__name__}); trying again in {backoff:.0f} s")
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 30)
