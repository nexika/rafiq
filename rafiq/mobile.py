"""The phone companion (#207): Rafiq's page on the phone, from anywhere, as a companion to Rafiq at home.

The phone sees the steps live, asks by voice or text, answers Yes or No to Rafiq's questions and hears the answer.
Rafiq itself stays on the computer; the phone is a remote for it.

How it is reached: a listener of its own, which `tailscale serve` publishes at https://<computer>.<tailnet>.ts.net
on the user's tailnet only. By default it is a socket, <settings>/run/mobile.sock, in a folder only this user can
open, so no other account can listen in its place while Rafiq is down and collect a phone's token (#238); with
`[mobile] listen = "port"`, 127.0.0.1:port (never the panel's port, never another address). Only this user's
programs and root (tailscaled) may connect: the kernel says who, on the socket, and /proc on Linux. tailscaled also
brings what another account here sends to this computer's ts.net name, so past that a phone's token is the barrier,
as it is for every node on the tailnet. HTTPS is what lets
a phone's browser use the microphone and install the page; Tailscale makes and renews the certificate, and its
Funnel (the public internet) is never used. Every request but the page and pairing carries a paired phone's token.

How a phone is paired: `rafiq mobile pair NAME` shows an eight-letter code, good for five minutes and five tries;
the phone types it once and gets a token of its own, kept on the phone. Only its SHA-256 is kept here, so
`rafiq mobile unpair NAME` ends that phone and no other.

What a phone may do: follow the event stream, ask, answer Yes or No (never "always") and stop. A request from
the phone is answered silently at home, and its questions are asked on screens only, so nobody at home can say
yes to it. Settings, permissions, history and tools are not offered here at all.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import queue
import secrets
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from . import config, sock
from .http_body import BodyError, body_length

PORT = 8768
PAIR_SECONDS = 300
PAIR_TRIES = 5
CODE_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no O/0, I/1/L: nothing to misread
CODE_LENGTH = 8
MAX_NAME = 40
MAX_BODY = 10_000
MAX_AUDIO = 3_000_000          # about three minutes of a phone's Opus or AAC
MAX_SECONDS = 60               # of speech kept from one recording
MAX_TEXT = 2000
ASK_SECONDS = 300
EVENTS_VERSION = 1
HOSTS_SUFFIX = ".ts.net"        # the name `tailscale serve` answers on, on the user's tailnet
STREAM_CHECK_SECONDS = 5        # how soon an open event stream notices its phone was unpaired, when nothing happens
MAX_CONNECTIONS = 32            # at once, on the listener
MAX_STREAMS = 2                 # open event streams per phone (two tabs)
MAX_DECODING = 2                # recordings being decoded at once


class PairingError(Exception):
    """The code can't pair a phone; the message says why."""


# ---------------------------------------------------------------------- files

def _save(path: Path, data: dict) -> None:
    """Owner-only, and atomic: `rafiq mobile pair` and `rafiq run` both write these files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False)
    os.replace(tmp, path)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class Devices:
    """The paired phones (name: the SHA-256 of its token) and the pairing under way, if any. Read from disk on
    every check, so `rafiq mobile pair` and `unpair` in another terminal work at once. Every change holds a file
    lock: `rafiq run` redeeming a code while `rafiq mobile unpair` runs must not bring the unpaired phone back."""

    _lock = threading.Lock()

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    @contextmanager
    def _changing(self) -> Iterator[None]:
        from . import desktop

        self.folder.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self.folder / ".lock", "a") as lock:
            desktop.lock_exclusive(lock)          # released when the file closes
            yield

    def _devices(self) -> dict:
        return _read(self.folder / "devices.json")

    def start_pairing(self, name: str) -> str:
        """A new code for the phone called `name`; any earlier code stops working."""
        name = name.strip()
        if not name or len(name) > MAX_NAME or not name.isprintable():
            raise ValueError(f"a phone's name is 1 to {MAX_NAME} characters on one line")
        code = "".join(secrets.choice(CODE_LETTERS) for _ in range(CODE_LENGTH))
        with self._changing():
            _save(self.folder / "pairing.json", {"name": name, "code": _hash(code),
                                                "until": time.time() + PAIR_SECONDS, "tries": 0})
        return code

    def redeem(self, code: str) -> tuple[str, str]:
        """(the phone's name, its new token) for a right code, which then stops working; PairingError otherwise.
        Five wrong codes end the pairing, so a code can't be guessed."""
        path = self.folder / "pairing.json"
        with self._changing():
            pairing = _read(path)
            if not pairing.get("code"):
                raise PairingError("no phone is being paired: run `rafiq mobile pair NAME` on the computer")
            if time.time() > float(pairing.get("until", 0)):
                path.unlink(missing_ok=True)
                raise PairingError("the code ran out: run `rafiq mobile pair NAME` again")
            given = "".join(c for c in str(code) if c.isalnum()).upper()   # "abcd-efgh" as shown is fine
            if not hmac.compare_digest(_hash(given), str(pairing["code"])):
                pairing["tries"] = int(pairing.get("tries", 0)) + 1
                if pairing["tries"] >= PAIR_TRIES:
                    path.unlink(missing_ok=True)
                    raise PairingError("too many wrong codes: run `rafiq mobile pair NAME` again")
                _save(path, pairing)
                raise PairingError("that code is wrong")
            path.unlink(missing_ok=True)
            token = secrets.token_urlsafe(32)
            devices = self._devices()
            devices[pairing["name"]] = {"token": _hash(token), "paired": time.time()}
            _save(self.folder / "devices.json", devices)
            return pairing["name"], token

    def check(self, token: str) -> str | None:
        """The name of the phone this token is for, or None."""
        if not token:
            return None
        given = _hash(token)
        found = None
        for name, device in self._devices().items():
            if isinstance(device, dict) and hmac.compare_digest(given, str(device.get("token", ""))):
                found = name
        return found

    def names(self) -> list[str]:
        return sorted(self._devices())

    def remove(self, name: str) -> bool:
        with self._changing():
            devices = self._devices()
            if name not in devices:
                return False
            del devices[name]
            _save(self.folder / "devices.json", devices)
            return True


# ---------------------------------------------------------------------- the phone's recording

# What a phone's browser records, by its first bytes, and the ffmpeg demuxer for each. Only these: left to guess,
# ffmpeg would also read a playlist (HLS, concat) that names other files on this computer.
FORMATS = ((b"\x1a\x45\xdf\xa3", 0, "matroska"),     # WebM: Opus from Android's Chrome and Firefox
           (b"ftyp", 4, "mov"),                      # MP4: AAC from an iPhone
           (b"OggS", 0, "ogg"),
           (b"RIFF", 0, "wav"))


def audio_format(data: bytes) -> str:
    """The demuxer for a recording, or ValueError when it isn't one a phone makes."""
    for magic, at, name in FORMATS:
        if data[at:at + len(magic)] == magic:
            return name
    raise ValueError("that isn't a recording from a phone (WebM, MP4, Ogg or WAV)")


def decode(data: bytes) -> np.ndarray:
    """A phone's recording (Opus in WebM from Android, AAC in MP4 from an iPhone) as 16 kHz mono floats, by
    ffmpeg. Written to a file first: an MP4 from a pipe often can't be read (its index is at the end)."""
    kind = audio_format(data)
    with tempfile.TemporaryDirectory(prefix="rafiq-phone-") as folder:
        source = Path(folder) / "recording"
        source.write_bytes(data)
        try:
            done = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file",
                                   "-f", kind, "-i", str(source), "-t", str(MAX_SECONDS), "-vn", "-ac", "1",
                                   "-ar", "16000", "-f", "s16le", "pipe:1"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"the recording can't be decoded ({type(exc).__name__})") from exc
    if done.returncode != 0 or not done.stdout:
        raise ValueError("the recording can't be decoded")
    return np.frombuffer(done.stdout[: len(done.stdout) // 2 * 2], dtype="<i2").astype("float32") / 32768.0


# ---------------------------------------------------------------------- the page and its icons

def manifest() -> dict:
    return {"name": "Rafiq", "short_name": "Rafiq", "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#101418", "theme_color": "#101418",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}


_icons: dict[int, bytes] = {}


def icon(size: int) -> bytes:
    """Rafiq's home-screen icon: a dot that listens, in the panel's colours. Drawn once per size."""
    if size not in _icons:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (size, size), "#101418")
        draw = ImageDraw.Draw(image)
        ring, dot = size * 0.30, size * 0.13
        mid = size / 2
        draw.ellipse((mid - ring, mid - ring, mid + ring, mid + ring), outline="#4fb3a9", width=max(2, size // 24))
        draw.ellipse((mid - dot, mid - dot, mid + dot, mid + dot), fill="#4fb3a9")
        out = io.BytesIO()
        image.save(out, "PNG")
        _icons[size] = out.getvalue()
    return _icons[size]


ICONS = {"/icon-192.png": 192, "/icon-512.png": 512, "/apple-touch-icon.png": 180}


# ---------------------------------------------------------------------- the listener

def host_ok(host: str, port: int, tailnet: str = "") -> bool:
    """The phone through `tailscale serve` (this computer's tailnet name, when Tailscale says it), or this computer
    itself. Another name is a web page that pointed its name at 127.0.0.1."""
    host = (host or "").strip().lower()
    if host in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return True
    name, _, given_port = host.partition(":")
    if given_port not in ("", "443"):
        return False
    if tailnet:
        return name == tailnet.lower().rstrip(".")
    labels = name.split(".")                  # computer.tailnet.ts.net
    return name.endswith(HOSTS_SUFFIX) and len(labels) >= 4 and all(labels)


SHOWN_ALWAYS = ("state",)
CARD_KINDS = ("heard", "typed", "said")


def for_phone(event: dict, device: str, runs: dict, shown: set) -> dict | None:
    """What a phone's event stream gets of one event: Rafiq's state, and the events of the requests this phone
    made, and nothing else. What others at home say, hear and read stays at home. `shown` gathers the words the
    phone was sent, so "forget that" at home takes them off the phone too, without telling it any other words."""
    kind = event.get("kind")
    if kind in SHOWN_ALWAYS:
        return event
    if kind == "forgotten":
        texts = [t for t in event.get("texts", []) if t in shown]
        return {**event, "texts": texts, "kinds": []} if texts else None
    if event.get("run_id") and runs.get(event["run_id"]) == device:
        if kind in CARD_KINDS and isinstance(event.get("text"), str):
            shown.add(event["text"])
        return event
    return None


def make_handler(assistant, devices: Devices, port: int, tailnet: str = ""):
    streams: dict[str, int] = {}             # open event streams, per phone
    streams_lock = threading.Lock()
    decoding = threading.BoundedSemaphore(MAX_DECODING)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Rafiq"
        timeout = 10                         # a client that connects and then says nothing, or sends slowly

        def log_message(self, *_args):
            pass

        def _send(self, code: int, body: bytes, kind: str = "application/json; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                             "connect-src 'self'; media-src 'self' data: blob:; img-src 'self' data:; "
                             "frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: dict) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode())

        def _token(self) -> str:
            auth = self.headers.get("Authorization") or ""
            return auth[7:].strip() if auth.lower().startswith("bearer ") else ""

        def _device(self) -> str | None:
            return devices.check(self._token())

        def _body(self, limit: int) -> bytes | None:
            try:
                length = body_length(self.headers.get("Content-Length"), limit)
            except BodyError as exc:
                self._json(exc.status, {"error": str(exc)})
                return None
            return self.rfile.read(length)

        def _peer_ok(self) -> bool:
            """This user's program, or tailscaled (root) bringing the phone: never another account's (#238)."""
            allowed = (os.getuid(), 0)
            if self.connection.family == getattr(socket, "AF_UNIX", None):
                return sock.peer_uid(self.connection) in allowed
            if owner_known():
                from . import panel

                return panel.connection_owner(self.client_address, self.connection.getsockname()) in allowed
            return True                      # a port elsewhere: nothing can say whose (listen = "socket" can)

        def do_GET(self):  # noqa: N802 (http.server's name)
            if not self._peer_ok():
                return self._json(403, {"error": "not this user's program"})
            if not host_ok(self.headers.get("Host", ""), port, tailnet):
                return self._json(403, {"error": "wrong host"})
            path = urlparse(self.path).path
            if path == "/":
                page = resources.files("rafiq").joinpath("mobile.html").read_bytes()
                return self._send(200, page, "text/html; charset=utf-8")
            if path == "/manifest.webmanifest":
                return self._send(200, json.dumps(manifest()).encode(), "application/manifest+json")
            if path in ICONS:
                return self._send(200, icon(ICONS[path]), "image/png")
            if path == "/events":
                device = self._device()
                if device is None:
                    return self._json(403, {"error": "pair this phone first"})
                with streams_lock:
                    if streams.get(device, 0) >= MAX_STREAMS:
                        return self._json(429, {"error": "this phone already follows Rafiq in other tabs"})
                    streams[device] = streams.get(device, 0) + 1
                try:
                    return self._stream(device, self._token())
                finally:
                    with streams_lock:
                        streams[device] -= 1
            return self._json(404, {"error": "not found"})

        def _stream(self, device: str, token: str) -> None:
            """Live from now (no history: what happened before belongs to whoever was at home), and only while
            the phone is paired: unpairing it ends a stream already open at its next event or keep-alive."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = assistant.events.subscribe()
            runs, shown = getattr(assistant, "phone_runs", {}), set()
            try:
                now = {"kind": "state", "state": getattr(assistant, "state", ""), "version": EVENTS_VERSION}
                self.wfile.write(f"data: {json.dumps(now)}\n\n".encode())
                self.wfile.flush()
                while devices.check(token) == device:
                    try:
                        event = for_phone(q.get(timeout=STREAM_CHECK_SECONDS), device, runs, shown)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                    else:
                        if event is not None:
                            self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                assistant.events.unsubscribe(q)

        def do_POST(self):  # noqa: N802
            if not self._peer_ok():
                return self._json(403, {"error": "not this user's program"})
            if not host_ok(self.headers.get("Host", ""), port, tailnet):
                return self._json(403, {"error": "wrong host"})
            path = urlparse(self.path).path
            if path == "/api/pair":
                return self._pair()
            if path not in ("/api/ask", "/api/voice", "/api/confirm", "/api/stop"):
                return self._json(404, {"error": "not found"})
            device = self._device()
            if device is None:
                return self._json(403, {"error": "pair this phone first"})
            if path == "/api/voice":
                return self._voice(device)
            raw = self._body(MAX_BODY)
            if raw is None:
                return None
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            if not isinstance(data, dict):
                return self._json(400, {"error": "bad json"})
            if path == "/api/ask":
                text = data.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._json(400, {"error": "say something"})
                return self._answer(assistant.ask_phone(device, text=text[:MAX_TEXT], timeout=ASK_SECONDS))
            if path == "/api/confirm":
                if not isinstance(data.get("yes"), bool) or not isinstance(data.get("id", ""), str):
                    return self._json(400, {"error": "yes or no"})
                # Yes or no only: "always" is for the panel at home, where approvals are managed. With the phone's
                # name: its yes counts only for a question about its own request (assistant._clicked).
                assistant.pending_answer.put((data.get("id", "")[:32], data["yes"], device))
                return self._json(200, {"ok": True})
            brain = getattr(assistant, "brain", None)       # /api/stop
            if brain is not None:
                brain.stop()
            if getattr(assistant, "confirming", False):
                assistant.pending_answer.put(False)
            return self._json(200, {"ok": True})

        def _pair(self) -> None:
            # JSON only: a web page in the user's browser can post a form or text to 127.0.0.1 without asking, and
            # five wrong codes would end a pairing under way.
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return self._json(415, {"error": "send JSON"})
            raw = self._body(MAX_BODY)
            if raw is None:
                return
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            code = data.get("code") if isinstance(data, dict) else None
            if not isinstance(code, str):
                return self._json(400, {"error": "the code"})
            try:
                name, token = devices.redeem(code[:40])
            except PairingError as exc:
                return self._json(403, {"error": str(exc)})
            assistant.events.publish("mobile", {"paired": name})
            return self._json(200, {"name": name, "token": token})

        def _voice(self, device: str) -> None:
            if not decoding.acquire(blocking=False):      # before reading 3 MB and starting ffmpeg
                return self._json(429, {"error": "Rafiq is still listening to your other recordings"})
            try:
                raw = self._body(MAX_AUDIO)
                if raw is None:
                    return None
                try:
                    audio = decode(raw)
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
            finally:
                decoding.release()
            return self._answer(assistant.ask_phone(device, audio=audio, timeout=ASK_SECONDS))

        def _answer(self, result: dict | None) -> None:
            if result is None:
                return self._json(504, {"error": "no answer in time"})
            if "error" in result:                           # other requests from phones still waiting
                return self._json(429, result)
            return self._json(200, result)

    return Handler


class _Slots:
    """At most MAX_CONNECTIONS at once: anyone who reaches the listener (a tailnet node, another user here) could
    otherwise hold a thread per slow connection until `rafiq run` itself runs out. The ones over are closed."""

    daemon_threads = True
    # socketserver's queue is 5. Linux holds a Unix connect when it is full; macOS refuses it, so a phone opening a
    # few requests at once would be turned away before the slots below are even asked.
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class PhoneServer(_Slots, ThreadingHTTPServer):
    pass


class PhoneSocketServer(_Slots, socketserver.ThreadingMixIn, sock.UnixServer):
    pass


def socket_path() -> Path:
    return config.home() / "run" / "mobile.sock"


def owner_known() -> bool:
    """Can a TCP connection's owner be found here? Only on Linux, in /proc."""
    return sys.platform.startswith("linux")


def serve_targets(run=subprocess.run) -> list[str] | None:
    """Every address `tailscale serve` forwards to, from `tailscale serve status --json`; None when Tailscale can't
    be asked (not installed, not running, or not this user's to ask)."""
    try:
        done = run(["tailscale", "serve", "status", "--json"], capture_output=True, text=True, timeout=5)
        data = json.loads(done.stdout or "{}") if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if data is None:
        return None
    found: list[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in ("Proxy", "TCPForward") and isinstance(inner, str):   # HTTP, or a raw TCP forward
                    found.append(inner)
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    walk(data)
    return found


def on_port(targets: list[str], port: int) -> list[str]:
    """The targets that forward to the phone page's port on this computer."""
    names = (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")
    return [t for t in targets if any(t.rstrip("/").endswith(n) or f"{n}/" in t for n in names)]


def check_start(cfg: dict, targets=None) -> None:
    """What would keep the phone page from starting safely, found before the speech models load (#238). ValueError
    with what to do; nothing when it can start."""
    settings = cfg["mobile"]
    port, listen = int(settings["port"]), settings.get("listen", "socket")
    if listen not in ("socket", "port"):
        raise ValueError(f'[mobile] listen is "socket" or "port", not {listen!r}')
    if listen == "socket":
        sock.prepare(socket_path())
        found = (targets or serve_targets)()      # looked up now, so a test's stand-in is the one asked
        if found is None:
            # Not knowing isn't "nothing on the old port": an old setup would stay open to whoever takes it.
            raise ValueError("Rafiq couldn't ask Tailscale where it sends the phone, so it can't tell whether an old "
                             "setup still points at a port another account could take. Check that "
                             "`tailscale serve status --json` works for you (tailscale installed, on PATH and running)")
        old = on_port(found, port)
        if old:
            # An install from before #238 forwards the phone to the port, which nothing here holds any more:
            # whichever program takes it next, another account's included, would get the phones' tokens.
            raise ValueError(f"`tailscale serve` still sends the phone to {old[0]}, which Rafiq no longer listens on "
                             f"and another account could take. Move it: tailscale serve --https=443 off, then "
                             f"tailscale serve --bg unix:{socket_path()}")
        return
    probe = socket.socket()
    # As the real server does: a connection the last Rafiq closed lingers in TIME_WAIT for about 60 s, and without
    # this a restart would read the port as taken. A program listening on it still fails the bind.
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise ValueError(f"127.0.0.1:{port} is taken ({exc.strerror}). If that isn't your program, your phone may be "
                         "talking to it: run `tailscale serve --https=443 off` now, then unpair and pair each phone "
                         "again (rafiq mobile unpair NAME, rafiq mobile pair NAME)") from exc
    finally:
        probe.close()


def serve_socket(assistant, port: int, devices: Devices, tailnet: str = "") -> PhoneSocketServer:
    """The phone's listener on its socket (#238), for `tailscale serve --bg unix:<path>`: in a folder only this user
    can open, so no other account can listen in its place. `port` is the one the page checks in the Host header
    when this computer opens it itself. ValueError when the socket can't be made safely."""
    where = socket_path()
    sock.prepare(where)
    server = PhoneSocketServer(str(where), make_handler(assistant, devices, port, tailnet))
    os.chmod(where, 0o600)
    return server


def serve(assistant, host: str, port: int, devices: Devices, tailnet: str = "") -> ThreadingHTTPServer:
    """The phone's listener: on this computer only, where `tailscale serve` reaches it. With `tailnet` (this
    computer's name there), only that name is a host it answers."""
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError(f"the phone page listens on 127.0.0.1 only, not {host}: `tailscale serve` brings the "
                         "phone to it over HTTPS")
    server = PhoneServer(("127.0.0.1", port), make_handler(assistant, devices, port, tailnet))
    server.RequestHandlerClass = make_handler(assistant, devices, server.server_address[1], tailnet)
    return server


def audio_reply(path: Path | None) -> tuple[str, str]:
    """(base64, MIME type) of the spoken answer's file, for the phone to play."""
    if path is None or not path.is_file():
        return "", ""
    mime = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg"}.get(path.suffix, "audio/mpeg")
    return base64.b64encode(path.read_bytes()).decode(), mime
