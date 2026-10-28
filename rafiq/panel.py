"""The local panel: a page on 127.0.0.1 that shows what Rafiq heard, said and did, with push-to-talk,
a text box, yes/no buttons for confirmations, the mode switch, the choice of style, a model picker and the controls
of a reading aloud, the history of requests (searchable, with delete) and whether anything leaves
this computer. A settings drawer switches the brain's mode and model and the voices; only the allowlist in
config.py can be written, and keys never reach the page.

Only this computer can reach it, and only the page it serves can drive it: every request must carry
the random token embedded in that page, and the Host header must be the panel's own address (so a
web site that rebinds its name to 127.0.0.1 is refused). The hand-gesture tracker posts labels to
/api/gesture with a token of its own, made each time it starts, which opens nothing else.

Besides the conversation, the page has three more: models (Ollama and Whisper), status (is each part
up) and permissions (each tool's level, remembered approvals). They read with GET and the token in
the X-Rafiq-Token header; changes are POSTs to the paths listed in WRITES, with names checked against
what is offered, so nothing else can be changed through the panel.
"""

from __future__ import annotations

import hmac
import json
import queue
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import lang, modes, notify, permissions, persona, status, subscriptions
from .brain import BrainError
from .tools import display

EVENTS_VERSION = 1           # docs/events.md; fields are only added, a removal raises it

MAX_BODY = 10_000
# How long `rafiq ask` waits for the running Rafiq: a request's own 120 s, a spoken yes, and one already
# being answered ahead of it.
ASK_SECONDS = 300

# The panel's changes besides talking: path -> what it does with the name sent.
WRITES = {
    "/api/models/install": lambda a, name: a.models.install(name),
    "/api/models/benchmark": lambda a, name: a.models.benchmark(name),
    "/api/models/delete": lambda a, name: a.models.delete(name),
    "/api/models/activate": lambda a, name: a.models.activate(name),
    "/api/whisper/activate": lambda a, name: a.models.activate_whisper(name),
    "/api/permissions/revoke": lambda a, name: a.approvals.revoke(name),
}


def page_data(assistant, path: str) -> dict | None:
    """What the models, status and permissions pages show; None for an unknown page."""
    if path == "/api/models":
        return {"ollama": assistant.models.ollama_page(), "whisper": assistant.models.whisper_page()}
    if path == "/api/status":
        return {"parts": status.check_all(assistant.cfg, assistant.models, assistant.speaker)}
    if path == "/api/permissions":
        return {"tools": permissions.levels(assistant.tools, assistant.approvals),
                "remembered": assistant.approvals.names()}
    return None


def make_handler(assistant, token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "Rafiq"

        def log_message(self, *_args):   # quiet: the panel shows what matters
            pass

        def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' "
                             "'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def _token_ok(self, given: str | None) -> bool:
            return bool(given) and hmac.compare_digest(given, token)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b'{"error":"wrong host"}')
            url = urlparse(self.path)
            pages = {"/": "panel.html", "/subscriptions": "subscriptions.html"}
            if url.path in pages:
                page = resources.files("rafiq").joinpath(pages[url.path]).read_text(encoding="utf-8")
                return self._send(200, page.replace("__TOKEN__", token).encode(), "text/html; charset=utf-8")
            query = parse_qs(url.query)
            if url.path in ("/events", "/api/subscriptions", "/api/history"):
                if not self._token_ok((query.get("token") or [None])[0]):
                    return self._send(403, b'{"error":"token"}')
                if url.path == "/api/history":
                    entries = assistant.history.entries((query.get("q") or [""])[0][:200])
                    body = {"entries": [asdict(e) for e in entries], "privacy": assistant.privacy()}
                    return self._send(200, json.dumps(body, ensure_ascii=False).encode())
                if url.path == "/api/subscriptions":
                    summary = subscriptions.summary(subscriptions.default_store())
                    return self._send(200, json.dumps(summary, ensure_ascii=False).encode())
                return self._stream()
            if url.path == "/api/settings":     # what the settings drawer shows
                if not self._token_ok(self.headers.get("X-Rafiq-Token")):
                    return self._send(403, b'{"error":"token"}')
                return self._send(200, json.dumps(assistant.status(), ensure_ascii=False).encode())
            if url.path.startswith("/api/"):
                if not self._token_ok(self.headers.get("X-Rafiq-Token")):
                    return self._send(403, b'{"error":"token"}')
                data = page_data(assistant, url.path)
                if data is not None:
                    return self._send(200, json.dumps(data, ensure_ascii=False).encode())
            return self._send(404, b'{"error":"not found"}')

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = assistant.events.subscribe()
            try:
                for event in list(assistant.events.history):
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                # Where things stand now, for a panel that just opened; versioned like every published event.
                now = [{"kind": "state", "state": assistant.state}, {"kind": "privacy", **assistant.privacy()},
                       {"kind": "mode", "mode": assistant.mode},
                       {"kind": "persona", "persona": persona.active(getattr(assistant, "cfg", {}))}]
                for event in now:
                    event = {**event, "version": EVENTS_VERSION}
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        event = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                assistant.events.unsubscribe(q)

        def do_POST(self):
            path = urlparse(self.path).path
            gestures = getattr(assistant, "gestures", None)
            # The gesture tracker has its own token, good for /api/gesture and nothing else.
            if path == "/api/gesture":
                allowed = gestures is not None and gestures.token_ok(self.headers.get("X-Rafiq-Gesture-Token"))
            else:
                allowed = self._token_ok(self.headers.get("X-Rafiq-Token"))
            if not self._host_ok() or not allowed:
                return self._send(403, b'{"error":"forbidden"}')
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, b'{"error":"too large"}')
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, b'{"error":"bad json"}')
            if not isinstance(data, dict):
                return self._send(400, b'{"error":"bad json"}')
            if path == "/api/gesture":
                gestures.handle(data.get("gesture"))
            elif path == "/api/gestures" and gestures is not None and isinstance(data.get("on"), bool):
                if data["on"]:
                    gestures.start()
                else:
                    gestures.stop()
            elif path == "/api/text" and isinstance(data.get("text"), str) and data["text"].strip():
                assistant.submit("text", text=data["text"][:2000])
            elif (path == "/api/ask" and isinstance(data.get("text"), str) and data["text"].strip()
                  and data.get("lang") in (None, *lang.TAGS)):
                answer = assistant.ask(data["text"][:2000], data.get("lang"), data.get("speak") is True, ASK_SECONDS)
                if answer is None:
                    return self._send(504, b'{"error":"no answer in time"}')
                return self._send(200, json.dumps({"answer": answer}, ensure_ascii=False).encode())
            elif path == "/api/display":          # `rafiq mcp`: a plan, an explanation or a card
                try:
                    shown = display.clean(data.get("kind"), data.get("data"))
                except ValueError as exc:
                    return self._send(400, json.dumps({"error": str(exc)}).encode())
                if not assistant.offer_show(data["kind"], shown):
                    return self._send(429, b'{"error":"too much waiting to be shown"}')
            elif path == "/api/listen":
                assistant.submit("listen")
            elif path == "/api/mute" and isinstance(data.get("muted", False), bool):
                assistant.set_muted(data.get("muted"))      # no value: a toggle
            elif path == "/api/confirm" and isinstance(data.get("yes"), bool):
                assistant.pending_answer.put("always" if data["yes"] and data.get("always") is True else data["yes"])
            elif path == "/api/dictate":
                assistant.submit("dictate")
            elif path == "/api/project" and isinstance(data.get("path"), str):
                folder = assistant.set_project(data["path"][:1000])
                if folder.startswith("error"):
                    return self._send(400, json.dumps({"error": folder}).encode())
            elif (path == "/api/notify" and isinstance(data.get("text"), str) and data["text"].strip()
                  and data.get("event", "") in notify.EVENTS and data.get("lang") in (None, *lang.TAGS)):
                assistant.submit("notify", text=data["text"][:500], event=data.get("event", ""),
                                 lang=data.get("lang"))
            elif path == "/api/show":
                assistant.submit("show_again")
            elif path == "/api/paper":
                assistant.submit("paper")
            elif path == "/api/mode" and data.get("mode") in modes.MODES:
                assistant.submit("mode", mode=data["mode"])
            elif path == "/api/persona" and data.get("name") in persona.PERSONAS:
                persona.choose(data["name"])
                assistant.events.publish("persona", {"persona": data["name"]})
            elif path == "/api/stop":
                assistant.reader.stop()
                assistant.speaker.stop()
            elif path == "/api/reading" and data.get("action") in ("pause", "skip", "stop"):
                getattr(assistant.reader, data["action"])()
            elif path == "/api/reading" and data.get("action") == "resume":
                if assistant.reader.resume():
                    assistant.submit("read")
            elif path == "/api/voice-test" and data.get("tag") in lang.TAGS:
                assistant.submit("say", text=lang.phrase("voice_test", data["tag"]), tag=data["tag"])
            elif path == "/api/settings" and isinstance(data.get("changes"), dict):
                try:
                    assistant.change_settings(data["changes"])
                except ValueError as exc:
                    return self._send(400, json.dumps({"error": str(exc)}).encode())
            elif path == "/api/subscriptions" and isinstance(data, dict):
                return self._subscriptions(data)
            elif path == "/api/brain":
                return self._send(200, json.dumps(assistant.brain.status()).encode())
            elif path == "/api/models" and isinstance(data.get("backend"), str):
                try:
                    models = assistant.brain.models(data["backend"])
                except BrainError as exc:
                    return self._send(400, json.dumps({"error": str(exc)}).encode())
                return self._send(200, json.dumps({"models": models}).encode())
            elif path == "/api/model" and isinstance(data.get("backend"), str) and isinstance(data.get("model"), str):
                try:   # the provider is asked first, so a model the key cannot use is never picked
                    assistant.brain.choose(data["backend"], data["model"])
                except BrainError as exc:
                    return self._send(400, json.dumps({"error": str(exc)}).encode())
                assistant.events.publish("brain", {"backend": data["backend"], "mode": data["backend"],
                                                   "model": data["model"]})
            elif path in WRITES and isinstance(data.get("name"), str):
                try:
                    WRITES[path](assistant, data["name"])
                except ValueError as exc:
                    return self._send(400, json.dumps({"error": str(exc)}).encode())
            elif path == "/api/history/delete" and isinstance(data.get("id"), str):
                assistant.forget(assistant.history.delete(data["id"]))
            elif path == "/api/history/clear":
                assistant.forget(assistant.history.clear())
            else:
                return self._send(400, b'{"error":"unknown request"}')
            if path in ("/api/stop", "/api/reading"):
                reader = assistant.reader
                assistant.events.publish("reading", {"index": reader.index, "total": len(reader.segments),
                                                     "state": reader.state})
            return self._send(200, b'{"ok":true}')

        def _subscriptions(self, data: dict) -> None:
            # Gmail is searched only when the user presses Scan, and only if Google is set up.
            google = getattr(assistant, "cfg", {}).get("google", {}).get("enabled")
            inbox = subscriptions.GmailReceipts() if google else None
            try:
                result = subscriptions.handle(data, subscriptions.default_store(), inbox)
            except ValueError as exc:
                return self._send(400, json.dumps({"error": str(exc)}).encode())
            except Exception as exc:   # Gmail or the network failed: say so on the page
                return self._send(502, json.dumps({"error": f"Gmail: {exc}"}).encode())
            return self._send(200, json.dumps(result).encode())

    return Handler


def serve(assistant, host: str, port: int, token: str) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the panel only listens on this computer (127.0.0.1)")
    server = ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)
    # The allowed Host names use the port actually bound (port 0 picks a free one).
    server.RequestHandlerClass = make_handler(assistant, token, server.server_address[1])
    server.daemon_threads = True
    return server
