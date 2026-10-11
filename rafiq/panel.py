"""The local panel: a page on 127.0.0.1 that shows what Rafiq heard, said and did, with push-to-talk,
a text box, yes/no buttons for confirmations, the mode switch, the choice of style and a model picker.

Only this computer can reach it, and only the page it serves can drive it: every request must carry
the random token embedded in that page, and the Host header must be the panel's own address (so a
web site that rebinds its name to 127.0.0.1 is refused).
"""

from __future__ import annotations

import hmac
import json
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import modes, persona
from .brain import BrainError

MAX_BODY = 10_000


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
            if url.path == "/":
                page = resources.files("rafiq").joinpath("panel.html").read_text(encoding="utf-8")
                return self._send(200, page.replace("__TOKEN__", token).encode(), "text/html; charset=utf-8")
            if url.path == "/events":
                if not self._token_ok((parse_qs(url.query).get("token") or [None])[0]):
                    return self._send(403, b'{"error":"token"}')
                return self._stream()
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
                self.wfile.write(f"data: {json.dumps({'kind': 'state', 'state': assistant.state})}\n\n".encode())
                self.wfile.write(f"data: {json.dumps({'kind': 'mode', 'mode': assistant.mode})}\n\n".encode())
                style = persona.active(getattr(assistant, "cfg", {}))
                self.wfile.write(f"data: {json.dumps({'kind': 'persona', 'persona': style})}\n\n".encode())
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
            if not self._host_ok() or not self._token_ok(self.headers.get("X-Rafiq-Token")):
                return self._send(403, b'{"error":"forbidden"}')
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, b'{"error":"too large"}')
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, b'{"error":"bad json"}')
            path = urlparse(self.path).path
            if path == "/api/text" and isinstance(data.get("text"), str) and data["text"].strip():
                assistant.submit("text", text=data["text"][:2000])
            elif path == "/api/listen":
                assistant.submit("listen")
            elif path == "/api/mute" and isinstance(data.get("muted", False), bool):
                assistant.set_muted(data.get("muted"))      # no value: a toggle
            elif path == "/api/confirm" and isinstance(data.get("yes"), bool):
                assistant.pending_answer.put(data["yes"])
            elif path == "/api/mode" and data.get("mode") in modes.MODES:
                assistant.submit("mode", mode=data["mode"])
            elif path == "/api/persona" and data.get("name") in persona.PERSONAS:
                persona.choose(data["name"])
                assistant.events.publish("persona", {"persona": data["name"]})
            elif path == "/api/stop":
                assistant.speaker.stop()
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
            else:
                return self._send(400, b'{"error":"unknown request"}')
            return self._send(200, b'{"ok":true}')

    return Handler


def serve(assistant, host: str, port: int, token: str) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the panel only listens on this computer (127.0.0.1)")
    server = ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)
    # The allowed Host names use the port actually bound (port 0 picks a free one).
    server.RequestHandlerClass = make_handler(assistant, token, server.server_address[1])
    server.daemon_threads = True
    return server
