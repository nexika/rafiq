"""`rafiq satellite`: a microphone and speaker in another room, for a Raspberry Pi 4 or 5 (or any Linux
computer) with a USB speakerphone. It hears "Rafiq" itself, with your recordings of the name (`rafiq
enroll` on the satellite, or the main computer's data/wake folder copied over), sends what was said to
the main computer, and plays the answer. It reconnects on its own and runs as a systemd service.

The protocol, over one WebSocket (protocol 1). Audio both ways is 16-bit little-endian mono PCM in
binary frames; everything else is a JSON text frame with a "type".

    satellite -> server                          server -> satellite
    hello {protocol, name, token, rate, format}  welcome {room}  or  refused {reason}
    wake {score, threshold, name_seconds}        listen {seconds}: send the next thing said, no name needed
      then PCM at 16 kHz, then end {}            play {rate}, then PCM, then play_end {}
    after listen: PCM then end {}, or silence {}

Speech that does not match the name never leaves the satellite. The token goes only to a wss:// server
or to one on the private network, since ws:// sends it in clear text.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlparse

import numpy as np

from .audio import RATE, Endpointer, Microphone, SileroVAD

logger = logging.getLogger(__name__)

PROTOCOL = 1
CHUNK = RATE // 4                 # PCM is sent a quarter of a second at a time
HELLO_SECONDS = 10.0
MAX_WAIT = 30
MIN_UTTERANCE_SECONDS = 0.35
LAN_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")
TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


class LinkClosed(Exception):
    """The connection to the main computer failed or ended: try again later."""


class Refused(Exception):
    """The main computer does not accept this satellite (wrong token, unknown name): retrying won't help."""


class Link(Protocol):
    def send(self, message: dict | bytes) -> None: ...

    def recv(self, timeout: float) -> dict | bytes | None:
        """The next message, or None when nothing came before the timeout. Raises LinkClosed."""

    def close(self) -> None: ...


class Ears(Protocol):
    def next_utterance(self, timeout: float) -> np.ndarray | None: ...

    def mute(self, muted: bool) -> None: ...

    def drain(self) -> None: ...


class Player(Protocol):
    def play(self, audio: np.ndarray, rate: int) -> None: ...


def backoff(attempt: int) -> int:
    """Seconds to wait before reconnecting: 1, 2, 4... up to 30."""
    return min(MAX_WAIT, 2 ** min(attempt, 10))


def _private_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith(LAN_SUFFIXES)
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip in TAILSCALE


def check_server(url: str) -> None:
    """Refuse an address the token should not be sent to."""
    parts = urlparse(url)
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        raise ValueError(f"the server must be a ws:// or wss:// address, not {url!r}")
    if parts.scheme == "ws" and not _private_host(parts.hostname):
        raise ValueError(f"{parts.hostname} is not on the private network: use wss:// so the token is encrypted")


def token(cfg: dict) -> str:
    return os.environ.get("RAFIQ_SATELLITE_TOKEN") or cfg["satellite"].get("token", "")


def pcm(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


class Satellite:
    def __init__(self, server: str, name: str, token: str, connect: Callable[[str], Link], ears: Ears,
                 player: Player, wake_score: Callable[[np.ndarray], float], threshold: float,
                 name_seconds: float = 0.0, sleep: Callable[[float], object] | None = None,
                 on_score: Callable[[float, float], None] | None = None):
        self.server, self.name, self.token = server, name, token
        self.connect, self.ears, self.player = connect, ears, player
        self.wake_score, self.threshold, self.name_seconds = wake_score, threshold, name_seconds
        self.on_score = on_score
        self._stop = threading.Event()
        self.sleep = sleep or self._stop.wait
        self.welcomed = False
        self._playing: int | None = None     # the sample rate of the answer being received
        self._answer: list[bytes] = []

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ one connection

    def session(self) -> None:
        """Connect, say hello, and serve until the connection ends (LinkClosed) or stop() is called."""
        self.welcomed, self._playing, self._answer = False, None, []
        link = self.connect(self.server)
        try:
            link.send({"type": "hello", "protocol": PROTOCOL, "name": self.name, "token": self.token,
                       "rate": RATE, "format": "s16le"})
            reply = link.recv(timeout=HELLO_SECONDS)
            if isinstance(reply, dict) and reply.get("type") == "refused":
                raise Refused(str(reply.get("reason", "refused")))
            if not (isinstance(reply, dict) and reply.get("type") == "welcome"):
                raise LinkClosed("the server did not answer the hello")
            self.welcomed = True
            logger.info("connected to %s as %s", self.server, reply.get("room") or self.name)
            while not self._stop.is_set():
                message = link.recv(timeout=0.05)
                if message is not None:
                    self.handle(link, message)
                if self._playing is None:
                    self.idle(link)
        finally:
            link.close()

    def handle(self, link: Link, message: dict | bytes) -> None:
        if isinstance(message, bytes):
            if self._playing is not None:
                self._answer.append(message)
            return
        kind = message.get("type")
        if kind == "listen":
            audio = self.ears.next_utterance(timeout=float(message.get("seconds", 8)))
            if audio is None:
                link.send({"type": "silence"})
            else:
                self.send_audio(link, audio)
        elif kind == "play":
            self._playing, self._answer = int(message.get("rate", RATE)), []
            self.ears.mute(True)
        elif kind == "play_end" and self._playing is not None:
            rate, data = self._playing, b"".join(self._answer)
            self._playing, self._answer = None, []
            try:
                self.player.play(np.frombuffer(data, dtype="<i2"), rate)
            finally:
                self.ears.mute(False)
                self.ears.drain()          # the answer's tail would be heard as speech

    def idle(self, link: Link) -> None:
        """Listen briefly; send what was said only when it starts with the name."""
        audio = self.ears.next_utterance(timeout=0.1)
        if audio is None:
            return
        score = self.wake_score(audio)
        if self.on_score:
            self.on_score(score, self.threshold)
        if score > self.threshold:
            return
        link.send({"type": "wake", "score": round(score, 3), "threshold": round(self.threshold, 3),
                   "name_seconds": round(self.name_seconds, 2)})
        self.send_audio(link, audio)

    def send_audio(self, link: Link, audio: np.ndarray) -> None:
        for start in range(0, len(audio), CHUNK):
            link.send(pcm(audio[start:start + CHUNK]))
        link.send({"type": "end"})

    # ------------------------------------------------------------------ forever

    def run(self) -> int:
        """Serve, reconnecting after every failure. 0 when stopped, 2 when the server refuses us."""
        attempt = 0
        while not self._stop.is_set():
            try:
                self.session()
            except Refused as exc:
                logger.error("the server refused this satellite: %s", exc)
                return 2
            except (LinkClosed, OSError) as exc:
                logger.warning("connection to %s lost: %s", self.server, exc)
            if self._stop.is_set():
                break
            if self.welcomed:
                attempt = 0
            self.sleep(backoff(attempt))
            attempt += 1
        return 0


# ---------------------------------------------------------------------- the real devices and network

class WebSocketLink:
    """A Link over the `websockets` package (pip install "rafiq[satellite]")."""

    def __init__(self, ws):
        self.ws = ws

    @classmethod
    def open(cls, url: str) -> WebSocketLink:
        from websockets.exceptions import WebSocketException
        from websockets.sync.client import connect

        try:
            return cls(connect(url, open_timeout=10, max_size=2 ** 22, ping_interval=20, ping_timeout=20))
        except WebSocketException as exc:
            raise LinkClosed(f"{type(exc).__name__}: {exc}") from exc

    def send(self, message: dict | bytes) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            self.ws.send(message if isinstance(message, bytes) else json.dumps(message))
        except ConnectionClosed as exc:
            raise LinkClosed(str(exc)) from exc

    def recv(self, timeout: float) -> dict | bytes | None:
        from websockets.exceptions import ConnectionClosed

        try:
            data = self.ws.recv(timeout=timeout)
        except TimeoutError:
            return None
        except ConnectionClosed as exc:
            raise LinkClosed(str(exc)) from exc
        if isinstance(data, bytes):
            return data
        try:
            message = json.loads(data)
        except ValueError:
            return None
        return message if isinstance(message, dict) else None

    def close(self) -> None:
        self.ws.close()


class MicrophoneEars:
    """The speakerphone's microphone, cut into utterances by speech detection."""

    def __init__(self, device=None, max_seconds: float = 20.0):
        self.mic = Microphone(device)
        self.vad = SileroVAD()
        self.endpointer = Endpointer(max_seconds=max_seconds)

    def start(self) -> None:
        self.mic.start()

    def stop(self) -> None:
        self.mic.stop()

    def next_utterance(self, timeout: float) -> np.ndarray | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline or self.endpointer.active:
            frame = self.mic.read(timeout=0.05)
            if frame is None:
                continue
            utterance = self.endpointer.push(frame, self.vad(frame))
            if utterance is not None and len(utterance) / RATE >= MIN_UTTERANCE_SECONDS:
                return utterance
        return None

    def mute(self, muted: bool) -> None:
        self.mic.muted = muted

    def drain(self) -> None:
        time.sleep(0.15)
        self.mic.drain()
        self.vad.reset()
        self.endpointer.reset()


class SpeakerPlayer:
    """Plays answers on the speakerphone (or any output device sounddevice knows)."""

    def __init__(self, device=None):
        self.device = device

    def play(self, audio: np.ndarray, rate: int) -> None:
        import sounddevice as sd

        sd.play(audio.astype("float32") / 32768, rate, device=self.device, blocking=True)


def device(value) -> int | str | None:
    """A device setting: "" for the default, a number, or part of the device's name."""
    value = str(value or "").strip()
    return int(value) if value.isdigit() else (value or None)


def default_name() -> str:
    return socket.gethostname().split(".")[0]


def service_unit(python: str) -> str:
    """A systemd user unit that starts the satellite at boot and restarts it, except when refused. A user
    unit (not a system one) shares the user's PipeWire sound server, so a Spotify receiver can play on
    the same speaker."""
    return f"""[Unit]
Description=Rafiq satellite (microphone and speaker for Rafiq)
After=pipewire.service pipewire-pulse.service

[Service]
ExecStart={python} -m rafiq satellite
Restart=always
RestartSec=5
# 2: the main computer refused this satellite; fix the settings instead of retrying forever.
RestartPreventExitStatus=2
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
