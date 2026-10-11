"""Text to speech.

- azure: Azure AI Speech, Microsoft's official neural voices, including Egyptian Arabic (ar-EG). Needs
  AZURE_SPEECH_KEY and AZURE_SPEECH_REGION; the free tier covers 500,000 characters a month.
- elevenlabs: ElevenLabs' cloud voices (model eleven_flash_v2_5), the most natural. Needs
  ELEVENLABS_API_KEY. One multilingual voice reads every language; elevenlabs_voices picks others,
  such as an Egyptian voice from the Voice Library for ar-EG.
- kokoro: local voices through kokoro-onnx, offline and free, for English, French and Spanish only
  (Kokoro has no Arabic or German, so those fall through to Piper).
- piper: local voices, offline and free. There is no Egyptian Piper voice: Arabic uses ar_JO.
- edge: the same voices through Edge's read-aloud service. Unofficial: Microsoft can block it at
  any time (it answered 403 on 2026-10-10), so it is used only when chosen.

"auto" uses Azure when a key is set, then ElevenLabs when its key is set, then Kokoro when its files
are there, then Piper; offline mode uses Piper only. When nothing can speak, a desktop notification
shows the text instead.

An answer can be spoken while it is still being written: a SpeechStream takes it sentence by sentence,
makes the next sentence's sound while the current one plays, and plays them in order.
"""

from __future__ import annotations

import asyncio
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np

from . import config as config_mod
from . import modes, timing

PIPER_FILES = {"en": "en_US-ryan-medium", "fr": "fr_FR-tom-medium", "es": "es_ES-davefx-medium",
               "de": "de_DE-thorsten-medium", "ar": "ar_JO-kareem-medium"}
# Kokoro's languages, each with one voice and kokoro-onnx's name for the language
KOKORO_VOICES = {"en": ("am_michael", "en-us"), "fr": ("ff_siwis", "fr-fr"), "es": ("em_alex", "es")}
KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
ELEVENLABS_MODEL = "eleven_flash_v2_5"
ELEVENLABS_VOICE = "JBFqnCBsd6RMkjVDRZzb"   # "George", a premade multilingual voice
LOCALES = {"en": "en-US", "fr": "fr-FR", "es": "es-ES", "de": "de-DE", "ar": "ar-SA", "ar-EG": "ar-EG"}


def for_speech(text: str) -> str:
    """What is read aloud: no Markdown marks, no URLs, no emoji."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`#>|]+", "", text)
    text = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", text)
    return re.sub(r"\s+", " ", text).strip()


# The end of a sentence: its mark, then a space. "3.5" and "www.x.org" have none after the dot.
_SENTENCE_END = re.compile(r"[.!?؟…]+[\"')»]*\s+|\n+")


class Sentences:
    """Cuts text that arrives in pieces into whole sentences, each as soon as it ends."""

    def __init__(self):
        self.buffer = ""

    def push(self, piece: str) -> list[str]:
        self.buffer += piece
        found = []
        while match := _SENTENCE_END.search(self.buffer):
            sentence = self.buffer[:match.end()].strip()
            self.buffer = self.buffer[match.end():]
            if sentence:
                found.append(sentence)
        return found

    def flush(self) -> str:
        rest, self.buffer = self.buffer.strip(), ""
        return rest


def ssml(text: str, voice: str, tag: str, rate: str = "+0%") -> str:
    return (f'<speak version="1.0" xml:lang="{LOCALES.get(tag, "en-US")}">'
            f'<voice name="{escape(voice)}"><prosody rate="{escape(rate)}">{escape(text)}</prosody></voice>'
            "</speak>")


class Speaker:
    def __init__(self, cfg: dict, on_start=None, on_end=None, budget=None):
        self.settings = cfg                  # shared: offline mode can be switched on while running
        self.cfg = cfg["voice"]
        self.budget = budget                 # charged for Azure's characters; at its cap, Piper only
        self.on_start, self.on_end = on_start, on_end
        self.rate: str | None = None      # overrides the setting while set (slower for language learners)
        self._proc: subprocess.Popen | None = None
        self._piper: dict = {}
        self._stream: SpeechStream | None = None
        self._kokoro = None
        self.key = os.environ.get("AZURE_SPEECH_KEY") or self.cfg.get("azure_key", "")
        self.region = os.environ.get("AZURE_SPEECH_REGION") or self.cfg.get("azure_region", "")
        self.eleven_key = os.environ.get("ELEVENLABS_API_KEY") or self.cfg.get("elevenlabs_key", "")

    def engines(self) -> list[str]:
        if modes.offline(self.settings):
            return ["piper"]
        engine = self.cfg.get("engine", "auto")
        if engine != "auto":
            engines = [engine, "piper"] if engine != "piper" else ["piper"]
        else:
            engines = ((["azure"] if self.key and self.region else []) + (["elevenlabs"] if self.eleven_key else [])
                + (["kokoro"] if self.kokoro_paths() else []) + ["piper"])
        if self.budget and not self.budget.allows_paid():
            engines = [e for e in engines if e != "azure"]
        return engines

    def _rate(self) -> str:
        return self.rate or self.cfg.get("rate", "+0%")

    def voice(self, tag: str) -> str:
        voices = self.cfg["voices"]
        return voices.get(tag) or voices.get(tag.split("-")[0]) or voices["en"]

    # ------------------------------------------------------------------ engines

    def _azure(self, text: str, tag: str, out: Path) -> Path:
        import httpx

        resp = httpx.post(
            f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1",
            headers={"Ocp-Apim-Subscription-Key": self.key, "Content-Type": "application/ssml+xml",
                     "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3", "User-Agent": "rafiq"},
            content=ssml(text, self.voice(tag), tag, self._rate()).encode(), timeout=20)
        resp.raise_for_status()
        if self.budget:
            self.budget.charge_characters("azure", len(text))
        out = out.with_suffix(".mp3")
        out.write_bytes(resp.content)
        return out

    def eleven_voice(self, tag: str) -> str:
        voices = self.cfg.get("elevenlabs_voices") or {}
        return voices.get(tag) or voices.get(tag.split("-")[0]) or voices.get("default") or ELEVENLABS_VOICE

    def _elevenlabs(self, text: str, tag: str, out: Path) -> Path:
        import httpx

        resp = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.eleven_voice(tag)}",
            headers={"xi-api-key": self.eleven_key, "Accept": "audio/mpeg", "User-Agent": "rafiq"},
            json={"text": text, "model_id": self.cfg.get("elevenlabs_model") or ELEVENLABS_MODEL,
                  "language_code": tag.split("-")[0]},
            timeout=20)
        resp.raise_for_status()
        out = out.with_suffix(".mp3")
        out.write_bytes(resp.content)
        return out

    def _edge(self, text: str, tag: str, out: Path) -> Path:
        import edge_tts

        out = out.with_suffix(".mp3")

        async def run():
            await edge_tts.Communicate(text, self.voice(tag), rate=self._rate()).save(str(out))

        asyncio.run(run())
        return out

    def piper_path(self, tag: str) -> Path | None:
        custom = self.cfg.get("piper_voices") or {}
        path = custom.get(tag) or custom.get(tag.split("-")[0])
        if path:
            return Path(path).expanduser()
        name = PIPER_FILES.get(tag.split("-")[0])
        candidate = config_mod.data_dir() / "piper" / f"{name}.onnx" if name else None
        return candidate if candidate and candidate.is_file() else None

    def _piper_voice(self, tag: str):
        path = self.piper_path(tag)
        if path is None:
            raise RuntimeError(f"no Piper voice for {tag}")
        if path not in self._piper:
            from piper import PiperVoice

            self._piper[path] = PiperVoice.load(str(path))
        return self._piper[path]

    def _piper_say(self, text: str, tag: str, out: Path) -> Path:
        voice = self._piper_voice(tag)
        out = out.with_suffix(".wav")
        with wave.open(str(out), "wb") as wav:
            voice.synthesize_wav(text, wav)
        return out

    def kokoro_paths(self) -> tuple[Path, Path] | None:
        """The Kokoro model and voices in data/kokoro/, or None unless both are there."""
        model, voices = (config_mod.data_dir() / "kokoro" / name for name in KOKORO_FILES)
        return (model, voices) if model.is_file() and voices.is_file() else None

    def _kokoro_say(self, text: str, tag: str, out: Path) -> Path:
        lang = tag.split("-")[0]
        if lang not in KOKORO_VOICES:
            raise RuntimeError(f"Kokoro has no voice for {tag}")
        if self._kokoro is None:
            paths = self.kokoro_paths()
            if paths is None:
                raise RuntimeError("the Kokoro files are missing")
            from kokoro_onnx import Kokoro

            self._kokoro = Kokoro(str(paths[0]), str(paths[1]))
        voice, code = KOKORO_VOICES[lang]
        samples, rate = self._kokoro.create(text, voice=voice, speed=1.0, lang=code)
        out = out.with_suffix(".wav")
        with wave.open(str(out), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes((np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes())
        return out

    # ------------------------------------------------------------------ playing

    def _play(self, path: Path) -> None:
        player = shutil.which("ffplay")
        if not player:
            raise RuntimeError("ffplay is not installed")
        self._proc = subprocess.Popen([player, "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)])
        self._proc.wait()
        self._proc = None

    def stop(self) -> None:
        """Stop talking, including the rest of an answer that is still coming."""
        if self._stream:
            self._stream.cancel()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def synthesize(self, text: str, tag: str, folder: Path, name: str = "say") -> tuple[Path | None, list[str]]:
        """The first engine that works, and the errors of those that didn't (never the key)."""
        errors = []
        for engine in self.engines():
            try:
                make = {"azure": self._azure, "elevenlabs": self._elevenlabs, "kokoro": self._kokoro_say,
                        "edge": self._edge, "piper": self._piper_say}[engine]
                with timing.Timer("voice", engine=engine):
                    return make(text, tag, folder / name), errors
            except Exception as exc:
                errors.append(f"{engine}: {type(exc).__name__}")
        return None, errors

    def say(self, text: str, tag: str) -> bool:
        """Speak text in the voice for tag (en, fr, es, de, ar, ar-EG). False if nothing could speak."""
        text = for_speech(text)
        if not text:
            return True
        with tempfile.TemporaryDirectory(prefix="rafiq-") as tmp:
            path, _errors = self.synthesize(text, tag, Path(tmp))
            if path is None:
                notify(text)
                return False
            if self.on_start:
                self.on_start()
            try:
                self._play(path)
            finally:
                if self.on_end:
                    self.on_end()
        return True

    def stream(self) -> SpeechStream:
        """A SpeechStream for one answer; close it when the answer is complete."""
        self._stream = SpeechStream(self)
        return self._stream


class SpeechStream:
    """Sentences spoken in order as they arrive. One thread makes the sound of each sentence, another
    plays them, so the next sentence is ready when the current one ends. on_start runs before the
    first sound, on_end once the speaker is free again (in wait or close)."""

    def __init__(self, speaker: Speaker):
        self.speaker = speaker
        self.ok = True
        self._created = time.perf_counter()
        self._texts: queue.Queue = queue.Queue()
        self._sounds: queue.Queue = queue.Queue()
        self._pending = 0
        self._done = threading.Condition()
        self._playing = self._heard = self._cancelled = False
        self._folder = tempfile.TemporaryDirectory(prefix="rafiq-")
        self._threads = [threading.Thread(target=self._make, name="rafiq-voice", daemon=True),
                         threading.Thread(target=self._play, name="rafiq-player", daemon=True)]
        for thread in self._threads:
            thread.start()

    def put(self, text: str, tag: str) -> None:
        text = for_speech(text)
        if not text or self._cancelled:
            return
        with self._done:
            self._pending += 1
        self._texts.put((text, tag))

    def _make(self) -> None:
        count = 0
        while (item := self._texts.get()) is not None:
            text, tag = item
            count += 1
            path = None
            if not self._cancelled:
                path, _errors = self.speaker.synthesize(text, tag, Path(self._folder.name), f"say{count}")
            self._sounds.put((path, text))
        self._sounds.put(None)

    def _play(self) -> None:
        while (item := self._sounds.get()) is not None:
            path, text = item
            try:
                if self._cancelled:
                    continue
                if path is None:
                    notify(text)
                    self.ok = False
                    continue
                if not self._playing:
                    if not self._heard:
                        timing.record("first_sound", time.perf_counter() - self._created)
                        self._heard = True
                    if self.speaker.on_start:
                        self.speaker.on_start()
                    self._playing = True
                self.speaker._play(path)
            finally:
                with self._done:
                    self._pending -= 1
                    self._done.notify_all()

    def _release(self) -> None:
        if self._playing:
            self._playing = False
            if self.speaker.on_end:
                self.speaker.on_end()

    def wait(self) -> None:
        """Until everything put so far has been said (before asking the user something)."""
        with self._done:
            self._done.wait_for(lambda: self._pending == 0)
        self._release()

    def cancel(self) -> None:
        self._cancelled = True

    def close(self) -> bool:
        """Say what is left and free the speaker. False if a sentence could not be spoken."""
        self._texts.put(None)
        for thread in self._threads:
            thread.join()
        self._release()
        self._folder.cleanup()
        if self.speaker._stream is self:
            self.speaker._stream = None
        return self.ok


def notify(text: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "Rafiq", text[:300]], check=False)
