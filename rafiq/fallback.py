"""When the main computer is off: notice it, wake it with Wake-on-LAN, and answer the little a satellite can
answer on its own (the time and the date) instead of staying silent.

This is the satellite side of the fallback and knows nothing of how a satellite talks to the main computer:
the probe that says whether the computer answers and the socket that sends the packet are passed in.
"""

from __future__ import annotations

import logging
import re
import socket
from collections.abc import Callable
from datetime import datetime

from . import lang

logger = logging.getLogger(__name__)

Send = Callable[[bytes, tuple[str, int]], None]


def magic_packet(mac: str) -> bytes:
    """Six 0xFF bytes, then the network card's address sixteen times."""
    digits = re.sub(r"[:\-.]", "", mac.strip())
    if not re.fullmatch(r"[0-9A-Fa-f]{12}", digits):
        raise ValueError(f"not a network card address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(digits) * 16


def _broadcast(data: bytes, addr: tuple[str, int]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(data, addr)


def wake_pc(mac: str, broadcast: str = "255.255.255.255", port: int = 9, send: Send | None = None) -> None:
    """Wake a computer that has Wake-on-LAN turned on in its BIOS and network settings."""
    (send or _broadcast)(magic_packet(mac), (broadcast, port))


class HubWatch:
    """Whether the main computer answers. It is called down only after `misses` probes in a row fail, so a
    single slow answer does not switch a satellite to its fallback; one answer brings it back."""

    def __init__(self, probe: Callable[[], bool], misses: int = 3, on_change: Callable[[bool], None] | None = None):
        self.probe, self.misses, self.on_change = probe, max(1, misses), on_change
        self.up = True
        self._missed = 0

    def check(self) -> bool:
        try:
            ok = bool(self.probe())
        except OSError as exc:
            logger.debug("hub probe failed: %s", exc)
            ok = False
        self._missed = 0 if ok else self._missed + 1
        up = ok or (self.up and self._missed < self.misses)
        if up != self.up:
            self.up = up
            if self.on_change:
                self.on_change(up)
        return self.up


# Words after lang.words(): lower case, Arabic spelling normalised (ة -> ه, أ -> ا).
_START = {"wake", "start", "turn", "switch", "boot", "allume", "démarre", "réveille", "enciende", "arranca",
          "despierta", "prende", "schalte", "starte", "wecke", "شغل", "شغّل", "افتح", "صحي", "ولع"}
_PC = {"computer", "pc", "ordinateur", "ordi", "ordenador", "computadora", "rechner", "كمبيوتر", "الكمبيوتر",
       "جهاز", "الجهاز", "الحاسوب", "حاسوب"}
_DATE = {"date", "fecha", "datum", "تاريخ", "التاريخ"}
_TIME = {"time", "heure", "hora", "uhr", "spät", "ساعه", "الساعه"}


class Fallback:
    """What a satellite says when the main computer does not answer."""

    PHRASES = {
        "off": {
            "en": "The main computer is off. Say \"turn on the computer\" to start it.",
            "fr": "L'ordinateur principal est éteint. Dites « allume l'ordinateur » pour le démarrer.",
            "es": "El ordenador principal está apagado. Di «enciende el ordenador» para arrancarlo.",
            "de": "Der Hauptcomputer ist aus. Sag „schalte den Computer ein“, um ihn zu starten.",
            "ar": "الحاسوب الرئيسي مطفأ. قل «شغل الحاسوب» لتشغيله.",
            "ar-EG": "الكمبيوتر الرئيسي مقفول. قول «شغل الكمبيوتر» عشان أشغله.",
        },
        "waking": {
            "en": "Starting the computer. Give it a minute.",
            "fr": "Je démarre l'ordinateur. Laissez-lui une minute.",
            "es": "Arrancando el ordenador. Dale un minuto.",
            "de": "Ich starte den Computer. Gib ihm eine Minute.",
            "ar": "أشغّل الحاسوب الآن. امنحه دقيقة.",
            "ar-EG": "بشغّل الكمبيوتر. استنى دقيقة.",
        },
        "no_mac": {
            "en": "I can't start the computer: its network address is not set.",
            "fr": "Je ne peux pas démarrer l'ordinateur : son adresse réseau n'est pas réglée.",
            "es": "No puedo arrancar el ordenador: su dirección de red no está configurada.",
            "de": "Ich kann den Computer nicht starten: seine Netzwerkadresse ist nicht eingestellt.",
            "ar": "لا أستطيع تشغيل الحاسوب: عنوانه على الشبكة غير مضبوط.",
            "ar-EG": "مش قادر أشغل الكمبيوتر: عنوانه على الشبكة مش متظبط.",
        },
        "wake_failed": {
            "en": "I couldn't reach the computer to start it.",
            "fr": "Je n'ai pas pu joindre l'ordinateur pour le démarrer.",
            "es": "No pude contactar con el ordenador para arrancarlo.",
            "de": "Ich konnte den Computer nicht erreichen, um ihn zu starten.",
            "ar": "تعذّر الوصول إلى الحاسوب لتشغيله.",
            "ar-EG": "مقدرتش أوصل للكمبيوتر عشان أشغله.",
        },
        "time": {tag: "{time}" for tag in lang.TAGS},
        "date": {tag: "{date}" for tag in lang.TAGS},
    }

    def __init__(self, mac: str = "", broadcast: str = "255.255.255.255", port: int = 9, send: Send | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self.mac, self.broadcast, self.port, self.send, self.now = mac, broadcast, port, send, now

    def _say(self, key: str, tag: str, **values: str) -> str:
        table = self.PHRASES[key]
        return table.get(tag, table["en"]).format(**values)

    def answer(self, text: str, tag: str) -> str:
        said = {part for word in lang.words(text) for part in word.split("'")}   # l'ordinateur
        if said & _START and said & _PC:
            return self._wake(tag)
        when = self.now()
        if said & _DATE:
            return self._say("date", tag, date=when.strftime("%Y-%m-%d"))
        if said & _TIME:
            return self._say("time", tag, time=when.strftime("%H:%M"))
        return self._say("off", tag)

    def _wake(self, tag: str) -> str:
        if not self.mac:
            return self._say("no_mac", tag)
        try:
            wake_pc(self.mac, self.broadcast, self.port, self.send)
        except (OSError, ValueError) as exc:
            logger.warning("Wake-on-LAN failed: %s", exc)
            return self._say("wake_failed", tag)
        return self._say("waking", tag)
