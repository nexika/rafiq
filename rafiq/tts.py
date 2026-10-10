"""Text to speech.

- azure: Azure AI Speech, Microsoft's official neural voices, including Egyptian Arabic (ar-EG). Needs
  AZURE_SPEECH_KEY and AZURE_SPEECH_REGION; the free tier covers 500,000 characters a month.
- piper: local voices, offline and free. There is no Egyptian Piper voice: Arabic uses ar_JO.
- edge: the same voices through Edge's read-aloud service. Unofficial: Microsoft can block it at
  any time (it answered 403 on 2026-10-10), so it is used only when chosen.

"auto" uses Azure when a key is set, else Piper. When nothing can speak, a desktop notification
shows the text instead.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from xml.sax.saxutils import escape

from . import config as config_mod

PIPER_FILES = {"en": "en_US-ryan-medium", "fr": "fr_FR-tom-medium", "es": "es_ES-davefx-medium",
               "de": "de_DE-thorsten-medium", "ar": "ar_JO-kareem-medium"}
LOCALES = {"en": "en-US", "fr": "fr-FR", "es": "es-ES", "de": "de-DE", "ar": "ar-SA", "ar-EG": "ar-EG"}


def for_speech(text: str) -> str:
    """What is read aloud: no Markdown marks, no URLs, no emoji."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`#>|]+", "", text)
    text = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def ssml(text: str, voice: str, tag: str, rate: str = "+0%") -> str:
    return (f'<speak version="1.0" xml:lang="{LOCALES.get(tag, "en-US")}">'
            f'<voice name="{escape(voice)}"><prosody rate="{escape(rate)}">{escape(text)}</prosody></voice>'
            "</speak>")


class Speaker:
    def __init__(self, cfg: dict, on_start=None, on_end=None):
        self.cfg = cfg["voice"]
        self.on_start, self.on_end = on_start, on_end
        self.rate: str | None = None      # overrides the setting while set (slower for language learners)
        self._proc: subprocess.Popen | None = None
        self._piper: dict = {}
        self.key = os.environ.get("AZURE_SPEECH_KEY") or self.cfg.get("azure_key", "")
        self.region = os.environ.get("AZURE_SPEECH_REGION") or self.cfg.get("azure_region", "")

    def engines(self) -> list[str]:
        engine = self.cfg.get("engine", "auto")
        if engine != "auto":
            return [engine, "piper"] if engine != "piper" else ["piper"]
        return (["azure"] if self.key and self.region else []) + ["piper"]

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

    # ------------------------------------------------------------------ playing

    def _play(self, path: Path) -> None:
        player = shutil.which("ffplay")
        if not player:
            raise RuntimeError("ffplay is not installed")
        self._proc = subprocess.Popen([player, "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)])
        self._proc.wait()
        self._proc = None

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def synthesize(self, text: str, tag: str, folder: Path) -> tuple[Path | None, list[str]]:
        """The first engine that works, and the errors of those that didn't (never the key)."""
        errors = []
        for engine in self.engines():
            try:
                make = {"azure": self._azure, "edge": self._edge, "piper": self._piper_say}[engine]
                return make(text, tag, folder / "say"), errors
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


def notify(text: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "Rafiq", text[:300]], check=False)
