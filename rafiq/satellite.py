"""Satellites: a microphone and speaker in another room.

A satellite (`rafiq satellite` on a small computer in the kitchen) hears "Rafiq" itself, with your
recordings of the name or a small Whisper model, then sends the request to the main computer (the
hub), which understands it, thinks, and sends back what to say. The satellite speaks it in its own
voice. The hub knows each satellite's room, so "turn on the light" means the kitchen's, and can hide
tools from a room: risky ones, or any by name.

The connection is TCP, optionally TLS, carrying frames: one byte for the kind ("J" for a JSON control
message, "A" for audio), four bytes of length, then the payload. Audio is 16 kHz mono 16-bit PCM.

    satellite -> hub   {"type": "hello", "name": "kitchen", "token": "..."}
    hub -> satellite   {"type": "welcome", "room": "kitchen"}   (or {"type": "error"} and the end)
    satellite -> hub   {"type": "start"}, audio frames, {"type": "end"}
    hub -> satellite   {"type": "heard", "text": ...}
                       {"type": "ask", "text": ..., "tag": ...}: say it, then send the reply the same
                       way as a request (or {"type": "answer", "yes": true/false})
                       {"type": "say", "text": ..., "tag": ...}, then {"type": "done"}

Each satellite has its own token, compared in constant time.
"""

from __future__ import annotations

import hmac
import json
import socket
import socketserver
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from . import lang, wake
from .audio import RATE

MAX_FRAME = 1 << 20                  # one frame: a JSON message or a piece of audio
MAX_REQUEST_SECONDS = 30
CHUNK = 4096                         # audio samples per frame sent
HELLO_SECONDS = 10                   # a connection that doesn't say who it is in time is closed
MIN_TOKEN = 16


class ProtocolError(Exception):
    pass


# ---------------------------------------------------------------------- frames

def to_pcm(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def from_pcm(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype("float32") / 32767


def _send(sock, kind: bytes, payload: bytes) -> None:
    sock.sendall(kind + len(payload).to_bytes(4, "big") + payload)


def send_json(sock, message: dict) -> None:
    _send(sock, b"J", json.dumps(message, ensure_ascii=False).encode())


def send_audio(sock, samples: np.ndarray) -> None:
    for i in range(0, max(len(samples), 1), CHUNK):
        _send(sock, b"A", to_pcm(samples[i:i + CHUNK]))


def _recv_exact(sock, n: int) -> bytes | None:
    data = b""
    while len(data) < n:
        part = sock.recv(n - len(data))
        if not part:
            if data:
                raise ProtocolError("the connection closed inside a frame")
            return None
        data += part
    return data


def recv(sock) -> tuple[str, dict | np.ndarray] | None:
    """The next frame as ("json", message) or ("audio", samples); None when the other side closed."""
    head = _recv_exact(sock, 5)
    if head is None:
        return None
    kind, size = head[:1], int.from_bytes(head[1:], "big")
    if size > MAX_FRAME:
        raise ProtocolError(f"frame too large ({size} bytes)")
    payload = _recv_exact(sock, size) if size else b""
    if payload is None:
        raise ProtocolError("the connection closed inside a frame")
    if kind == b"A":
        return "audio", from_pcm(payload)
    if kind == b"J":
        try:
            message = json.loads(payload)
        except ValueError as exc:
            raise ProtocolError("a control frame is not JSON") from exc
        if not isinstance(message, dict):
            raise ProtocolError("a control frame is not a JSON object")
        return "json", message
    raise ProtocolError(f"unknown frame kind {kind!r}")


def recv_json(sock) -> dict:
    frame = recv(sock)
    if frame is None:
        raise ProtocolError("the connection closed")
    kind, message = frame
    if kind != "json":
        raise ProtocolError("expected a control frame")
    return message


def recv_utterance(sock, first: dict | None = None) -> np.ndarray | bool:
    """Audio between "start" and "end", or the yes/no of an "answer" message."""
    message = first or recv_json(sock)
    if message.get("type") == "answer":
        return bool(message.get("yes"))
    if message.get("type") != "start":
        raise ProtocolError(f"expected start, got {message.get('type')}")
    pieces, total = [], 0
    while True:
        frame = recv(sock)
        if frame is None:
            raise ProtocolError("the connection closed during a request")
        kind, data = frame
        if kind == "json":
            if data.get("type") == "end":
                return np.concatenate(pieces) if pieces else np.zeros(0, dtype="float32")
            raise ProtocolError(f"expected audio or end, got {data.get('type')}")
        total += len(data)
        if total > MAX_REQUEST_SECONDS * RATE:
            raise ProtocolError("request too long")
        pieces.append(data)


# ---------------------------------------------------------------------- devices

@dataclass(frozen=True)
class Device:
    name: str
    token: str
    room: str
    blocked_tools: tuple = ()
    risky: str = "ask"               # "ask": confirm on the satellite; "block": risky tools are hidden


def devices(cfg: dict) -> dict[str, Device]:
    """The satellites listed in the settings. One with a missing or short token is left out."""
    out = {}
    for name, entry in (cfg.get("satellites", {}).get("devices") or {}).items():
        token = str(entry.get("token", ""))
        if len(token) < MIN_TOKEN:
            continue
        out[name] = Device(name, token, str(entry.get("room") or name), tuple(entry.get("blocked_tools") or ()),
                           "block" if entry.get("risky") == "block" else "ask")
    return out


def authenticate(known: dict[str, Device], name, token) -> Device | None:
    device = known.get(name) if isinstance(name, str) else None
    given = token.encode() if isinstance(token, str) else b""
    # Compared even for an unknown name, so the time taken says nothing about which names exist.
    expected = device.token.encode() if device else b"\0" * MIN_TOKEN
    ok = hmac.compare_digest(given, expected)
    return device if ok and device else None


def room_tools(tools: dict, device: Device) -> dict:
    """The tools a room may use: the blocked ones and, with risky = "block", any that asks first are
    not even shown to the model."""
    return {name: t for name, t in tools.items()
            if name not in device.blocked_tools and not (device.risky == "block" and t.confirm is not None)}


# ---------------------------------------------------------------------- the hub (main computer)

class Hub:
    """Serves satellites. `transcribe(audio) -> Heard` and `make_brain(tools, confirm)` are passed in, so
    the hub shares the main computer's models and the tests need neither."""

    def __init__(self, cfg: dict, transcribe: Callable, make_brain: Callable, tools: dict, scheduler,
                 on_event: Callable[[str, dict], None] | None = None):
        self.cfg, self.transcribe, self.make_brain = cfg, transcribe, make_brain
        self.tools, self.scheduler = tools, scheduler
        self.on_event = on_event or (lambda kind, data: None)
        self.devices = devices(cfg)

    def serve(self, host: str, port: int, tls: ssl.SSLContext | None = None) -> socketserver.ThreadingTCPServer:
        hub = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(HELLO_SECONDS)    # covers the TLS handshake too
                try:
                    sock = tls.wrap_socket(self.request, server_side=True) if tls else self.request
                    hub.session(sock)
                except (ProtocolError, OSError, ssl.SSLError) as exc:
                    hub.on_event("error", {"satellite": f"{type(exc).__name__}: {exc}"})

        server = socketserver.ThreadingTCPServer((host, port), Handler, bind_and_activate=False)
        server.daemon_threads = True
        server.allow_reuse_address = True
        server.server_bind()
        server.server_activate()
        return server

    def session(self, sock) -> None:
        from .assistant import Context

        sock.settimeout(HELLO_SECONDS)
        hello = recv_json(sock)
        device = authenticate(self.devices, hello.get("name"), hello.get("token")) \
            if hello.get("type") == "hello" else None
        if device is None:
            send_json(sock, {"type": "error", "error": "unknown satellite or wrong token"})
            return
        sock.settimeout(None)
        send_json(sock, {"type": "welcome", "room": device.room})
        self.on_event("satellite", {"name": device.name, "room": device.room, "connected": True})
        tools = room_tools(self.tools, device)

        def confirm(action, tag):
            return self._confirm(sock, action, tag)

        brain = self.make_brain(tools, confirm)
        ctx = Context(self.cfg, self.scheduler, room=device.room, confirm=confirm, tools=tools)
        try:
            while True:
                frame = recv(sock)
                if frame is None:
                    return
                kind, message = frame
                if kind == "json" and message.get("type") == "ping":
                    send_json(sock, {"type": "pong"})
                elif kind == "json" and message.get("type") == "start":
                    self._request(sock, recv_utterance(sock, message), device, brain, ctx)
                else:
                    raise ProtocolError("expected start or ping")
        finally:
            self.on_event("satellite", {"name": device.name, "room": device.room, "connected": False})

    def _hear(self, audio: np.ndarray, previous: str) -> tuple[str, str]:
        heard = self.transcribe(audio)
        return heard.text, lang.dialect(heard.lang, heard.text, previous)

    def _request(self, sock, audio, device: Device, brain, ctx) -> None:
        if isinstance(audio, bool):
            raise ProtocolError("an answer outside a question")
        text, tag = self._hear(audio, ctx.tag)
        found, rest = wake.find(text)
        text = rest if found else text
        send_json(sock, {"type": "heard", "text": text, "tag": tag})
        self.on_event("heard", {"text": text, "tag": tag, "room": device.room})
        if not text.strip():
            answer, answer_tag = lang.phrase("not_heard", tag), tag
        else:
            ctx.tag = tag
            answer = brain.think(text, tag, ctx)
            answer_tag = lang.script_language(answer, tag)
        send_json(sock, {"type": "say", "text": answer, "tag": answer_tag})
        send_json(sock, {"type": "done"})
        self.on_event("said", {"text": answer, "tag": answer_tag, "room": device.room})

    def _confirm(self, sock, action: str, tag: str) -> bool:
        """Ask on the satellite before an action that changes something; only a clear yes goes ahead."""
        send_json(sock, {"type": "ask", "text": lang.phrase("confirm", tag, action=action), "tag": tag})
        reply = recv_utterance(sock)
        if isinstance(reply, bool):
            answer = reply
        else:
            text, _ = self._hear(reply, tag)
            answer = lang.yes_or_no(text) is True
        if not answer:
            send_json(sock, {"type": "say", "text": lang.phrase("cancelled", tag), "tag": tag})
        return answer


def server_tls(cfg: dict) -> ssl.SSLContext | None:
    sats = cfg.get("satellites", {})
    if not (sats.get("tls_cert") and sats.get("tls_key")):
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(sats["tls_cert"], sats["tls_key"])
    return context


# ---------------------------------------------------------------------- the satellite side

@dataclass
class Client:
    """A satellite's connection to the hub."""
    host: str
    port: int
    name: str
    token: str
    tls: ssl.SSLContext | None = None
    timeout: float = 120.0
    room: str = ""
    sock: socket.socket | None = field(default=None, repr=False)

    def connect(self) -> Client:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.tls:
            sock = self.tls.wrap_socket(sock, server_hostname=self.host)
        send_json(sock, {"type": "hello", "name": self.name, "token": self.token})
        reply = recv_json(sock)
        if reply.get("type") != "welcome":
            sock.close()
            raise ProtocolError(f"the hub refused: {reply.get('error', 'wrong token')}")
        self.sock, self.room = sock, str(reply.get("room", ""))
        return self

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def __enter__(self) -> Client:
        return self.connect() if self.sock is None else self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _send_utterance(self, reply: np.ndarray | bool | None) -> None:
        if isinstance(reply, bool) or reply is None:
            send_json(self.sock, {"type": "answer", "yes": bool(reply)})
            return
        send_json(self.sock, {"type": "start"})
        send_audio(self.sock, reply)
        send_json(self.sock, {"type": "end"})

    def request(self, audio: np.ndarray, speak: Callable[[str, str], None],
                listen: Callable[[], np.ndarray | bool | None] = lambda: None,
                on_heard: Callable[[str], None] | None = None) -> list[str]:
        """Send what was said; speak what the hub answers. `listen` records the reply to a question
        (None or False: no). Returns everything said, the answer last."""
        self._send_utterance(audio)
        said = []
        while True:
            message = recv_json(self.sock)
            kind = message.get("type")
            if kind == "heard" and on_heard:
                on_heard(str(message.get("text", "")))
            elif kind == "ask":
                speak(str(message.get("text", "")), str(message.get("tag", "en")))
                self._send_utterance(listen())
            elif kind == "say":
                said.append(str(message.get("text", "")))
                speak(said[-1], str(message.get("tag", "en")))
            elif kind == "done":
                return said
            elif kind == "error":
                raise ProtocolError(str(message.get("error", "error")))


def client_tls(cfg: dict) -> ssl.SSLContext | None:
    sat = cfg.get("satellite", {})
    if not sat.get("tls"):
        return None
    return ssl.create_default_context(cafile=sat.get("tls_ca") or None)


@dataclass
class Simulated:
    room: str
    heard: str
    said: list[str]
    seconds: float                   # from the end of the request to the answer


def simulate(host: str, port: int, name: str, token: str, audio: np.ndarray, yes: bool = False,
             tls: ssl.SSLContext | None = None) -> Simulated:
    """Act as a satellite without a microphone or speaker: send a recording, collect the answer, and
    time the round trip (transcription and thinking included)."""
    heard = []
    with Client(host, port, name, token, tls) as client:
        started = time.monotonic()
        said = client.request(audio, speak=lambda text, tag: None, listen=lambda: yes, on_heard=heard.append)
        seconds = time.monotonic() - started
    return Simulated(client.room, heard[0] if heard else "", said, seconds)


def parse_hub(address: str, default_port: int = 8766) -> tuple[str, int]:
    """"kitchen-pi.lan:8766" -> ("kitchen-pi.lan", 8766); the port may be left out."""
    host, _, port = address.strip().rpartition(":")
    if not host or not port.isdigit():
        return address.strip(), default_port
    return host.strip("[]"), int(port)


def run_device(cfg: dict) -> int:
    """The loop on the satellite itself: wait for "Rafiq" with its own microphone, send the request to
    the hub, speak the answer. Uses the recordings from `rafiq enroll` on this device when there are
    some, else a small Whisper model listens for the name."""
    from . import kws
    from .audio import Endpointer, Microphone, SileroVAD
    from .stt import WAKE_PROMPT, WAKE_SECONDS, Transcriber
    from .tts import Speaker

    sat = cfg["satellite"]
    if not (sat.get("hub") and sat.get("name") and sat.get("token")):
        print("Set [satellite] hub, name and token in the config (the hub lists the same name and token).")
        return 1
    host, port = parse_hub(sat["hub"])
    mic, vad = Microphone(), SileroVAD()
    endpointer = Endpointer(max_seconds=cfg["speech"]["max_utterance_seconds"])

    def unmute():
        time.sleep(0.15)
        mic.drain()
        vad.reset()
        endpointer.reset()
        mic.muted = False

    def mute():
        mic.muted = True

    speaker = Speaker(cfg, on_start=mute, on_end=unmute)
    spotter = kws.Spotter.load()
    mel = kws.Mel() if spotter else None
    wake_stt = None if spotter else Transcriber(cfg["speech"]["wake_model"], cfg["speech"]["languages"])

    def utterance(timeout: float | None) -> np.ndarray | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while deadline is None or time.monotonic() < deadline or endpointer.active:
            frame = mic.read(timeout=0.1)
            if frame is None:
                continue
            audio = endpointer.push(frame, vad(frame))
            if audio is not None and len(audio) / RATE >= 0.35:
                return audio
        return None

    def woken(audio: np.ndarray) -> bool:
        if spotter:
            return spotter.score(mel(audio[:int(kws.MAX_SECONDS * RATE)])) <= spotter.threshold
        spotted = wake_stt.transcribe(audio[:int(WAKE_SECONDS * RATE)], beam_size=1, hotwords=None,
                                      prompt=WAKE_PROMPT)
        return wake.find(spotted.text)[0]

    client = Client(host, port, sat["name"], sat["token"], client_tls(cfg))
    mic.start()
    print(f'Satellite "{sat["name"]}" of {host}:{port}. Say "Rafiq". Ctrl+C stops.', flush=True)
    try:
        while True:
            audio = utterance(None)
            if audio is None or not woken(audio):
                continue
            if len(audio) / RATE < WAKE_SECONDS:           # the name alone: the request comes next
                speaker.say(lang.phrase("listening", "en"), "en")
                audio = utterance(8.0)
                if audio is None:
                    continue
            try:
                if client.sock is None:
                    client.connect()
                    print(f"Connected; this is the {client.room}.", flush=True)
                client.request(audio, speak=speaker.say, listen=lambda: utterance(10.0))
            except (OSError, ProtocolError) as exc:
                client.close()
                print(f"The hub did not answer ({type(exc).__name__}); trying again next time.", flush=True)
                speaker.say(lang.phrase("brain_down", "en"), "en")
    except KeyboardInterrupt:
        return 0
    finally:
        mic.stop()
        client.close()
