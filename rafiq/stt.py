"""Speech to text with faster-whisper, on the CPU, limited to the languages Rafiq speaks, and a
cloud service (Azure AI Speech or OpenAI) for chosen languages or when Whisper can't run."""

from __future__ import annotations

import io
import logging
import os
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import lang as lang_mod
from .hallucinations import is_hallucination, is_speech
from .lang import latinize, words

logger = logging.getLogger(__name__)

HOTWORDS = "Rafiq رفيق"
# For the small model that only listens for the name: in tests on six languages, a prompt found the
# name more often than hotwords (6 of 8 against 5 of 8) and as rarely by mistake.
WAKE_PROMPT = "Rafiq, رفيق."
WAKE_SECONDS = 2.5
# For Arabic: Egyptians say English words inside Arabic sentences, and Whisper, which picks one language
# per utterance, writes them in Arabic script. A prompt in that mixed style keeps them in Latin letters
# more often; lang.latinize puts back the common ones it still writes in Arabic.
MIXED_PROMPT = "يا رفيق، افتحلي الـ settings وشغّل الـ playlist بتاعتي على Spotify، وابعت email لسارة."


@dataclass
class Heard:
    text: str
    lang: str          # the Whisper language code: en, fr, es, de, ar
    confidence: float  # the language detection's probability


def pick_language(probs: list[tuple[str, float]], allowed: list[str]) -> tuple[str, float]:
    """The most likely language among the allowed ones: Whisper sometimes hears Egyptian Arabic as
    Persian or Urdu, or German as Dutch, and Rafiq only speaks its six."""
    best = max(((lang, p) for lang, p in probs if lang in allowed), key=lambda x: x[1], default=None)
    return best or (allowed[0], 0.0)


class Transcriber:
    def __init__(self, size: str = "small", languages: list[str] | None = None, mixed: bool = False):
        self.size = size
        self.languages = languages or ["en", "fr", "es", "de", "ar"]
        self.mixed = mixed  # Arabic with English words: the mixed prompt, and English words in Latin letters
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.size, device="cpu", compute_type="int8",
                                       cpu_threads=max(1, (os.cpu_count() or 4) - 2))
        return self._model

    def language(self, audio: np.ndarray) -> tuple[str, float]:
        _, _, probs = self.model.detect_language(audio=audio)
        return pick_language(probs, self.languages)

    def transcribe(self, audio: np.ndarray, lang: str | None = None, beam_size: int = 3,
                   hotwords: str | None = HOTWORDS, prompt: str | None = None) -> Heard:
        confidence = 1.0
        if lang is None:
            lang, confidence = self.language(audio)
        mixed = self.mixed and lang == "ar"
        if mixed and prompt is None:
            prompt = MIXED_PROMPT
        segments, _ = self.model.transcribe(audio, language=lang, beam_size=beam_size,
                                            condition_on_previous_text=False, vad_filter=False,
                                            hotwords=hotwords, initial_prompt=prompt, without_timestamps=True)
        # Silence and noise come back as stock phrases ("Thanks for watching!"): never a request.
        kept = [s.text.strip() for s in segments if is_speech(s) and not is_hallucination(s.text)]
        text = " ".join(kept).strip()
        if is_hallucination(text):
            text = ""
        if mixed:
            text = latinize(text)
        return Heard(text=text, lang=lang, confidence=confidence)


def word_error_rate(reference: str, heard: str) -> float:
    """Words substituted, dropped or added, over the words of the reference; spelling that varies in
    speech-to-text output (hamza, ta marbuta, case) does not count."""
    ref, hyp = words(reference), words(heard)
    if not ref:
        return float(bool(hyp))
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1] / len(ref)


@dataclass
class Scored:
    name: str
    said: str
    heard: str
    error: float  # the word error rate


def score(folder: Path, transcriber: Transcriber) -> list[Scored]:
    """Transcribe every recording in a folder that has its text next to it (name.wav and name.txt), to
    compare models and settings on your own voice, such as Egyptian Arabic mixed with English."""
    from .enroll import load_wav

    results = []
    for wav in sorted(folder.glob("*.wav")):
        txt = wav.with_suffix(".txt")
        if not txt.is_file():
            continue
        said = txt.read_text(encoding="utf-8").strip()
        heard = transcriber.transcribe(load_wav(wav)).text
        results.append(Scored(wav.stem, said, heard, word_error_rate(said, heard)))
    return results


# ---------------------------------------------------------------------- cloud speech to text
#
# Azure AI Speech (it has an Egyptian Arabic recogniser, ar-EG, and shares the voice key) and OpenAI,
# for a language Whisper hears badly or when Whisper can't run. Never with speech.offline.

LOCAL_FAILURES = (ImportError, OSError, RuntimeError)   # faster-whisper missing, the model can't load
AZURE_LOCALES = {"en": "en-US", "fr": "fr-FR", "es": "es-ES", "de": "de-DE", "ar": "ar-EG"}


class CloudSTTError(Exception):
    """A cloud service could not transcribe: no network, a bad key, a refused request."""


def wav_bytes(audio: np.ndarray, rate: int = 16000) -> bytes:
    """Float samples in [-1, 1] as a 16-bit mono WAV file."""
    samples = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())
    return out.getvalue()


class AzureSTT:
    """Azure's REST API for short audio (up to 60 seconds, one language per request)."""

    def __init__(self, key: str, region: str, locales: dict[str, str]):
        self.key, self.region = key, region
        self.locales = {**AZURE_LOCALES, **locales}

    def locale(self, lang: str | None) -> str:
        if lang and "-" in lang:          # already a locale, such as the conversation's "ar-EG"
            return lang
        return self.locales.get(lang or "en", "en-US")

    def transcribe(self, audio: np.ndarray, lang: str | None = None) -> str:
        import httpx

        try:
            resp = httpx.post(
                f"https://{self.region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1",
                params={"language": self.locale(lang), "format": "simple"},
                headers={"Ocp-Apim-Subscription-Key": self.key, "User-Agent": "rafiq",
                         "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000"},
                content=wav_bytes(audio), timeout=20)
            resp.raise_for_status()
            result = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CloudSTTError(f"Azure speech to text: {type(exc).__name__}") from exc
        status = result.get("RecognitionStatus")
        if status in ("NoMatch", "InitialSilenceTimeout", "BabbleTimeout"):
            return ""
        if status != "Success":
            raise CloudSTTError(f"Azure speech to text: {status}")
        return result.get("DisplayText", "").strip()


class OpenAISTT:
    """OpenAI's transcription API; without a language, the service finds it."""

    def __init__(self, key: str, model: str):
        self.key, self.model = key, model

    def transcribe(self, audio: np.ndarray, lang: str | None = None) -> str:
        import httpx

        data = {"model": self.model, "response_format": "json"}
        if lang:
            data["language"] = lang.split("-")[0]
        try:
            resp = httpx.post("https://api.openai.com/v1/audio/transcriptions",
                              headers={"Authorization": f"Bearer {self.key}"}, data=data,
                              files={"file": ("speech.wav", wav_bytes(audio), "audio/wav")}, timeout=30)
            resp.raise_for_status()
            return resp.json().get("text", "").strip()
        except (httpx.HTTPError, ValueError) as exc:
            raise CloudSTTError(f"OpenAI speech to text: {type(exc).__name__}") from exc


def cloud_engines(cfg: dict) -> dict:
    """The cloud services that have a key; none with speech.offline. Keys come from the environment
    first (AZURE_SPEECH_KEY and AZURE_SPEECH_REGION, as for the voice; OPENAI_API_KEY), then the file."""
    speech = cfg["speech"]
    if speech.get("offline"):
        return {}
    engines: dict = {}
    key = os.environ.get("AZURE_SPEECH_KEY") or cfg["voice"].get("azure_key", "")
    region = os.environ.get("AZURE_SPEECH_REGION") or cfg["voice"].get("azure_region", "")
    if key and region:
        engines["azure"] = AzureSTT(key, region, speech.get("azure_locales") or {})
    key = os.environ.get("OPENAI_API_KEY") or speech.get("openai_key", "")
    if key:
        engines["openai"] = OpenAISTT(key, speech.get("openai_model", "gpt-4o-mini-transcribe"))
    return engines


class SpeechToText:
    """Whisper on this computer, or a cloud service for the languages sent there (speech.stt_by_language,
    or every language with speech.stt_engine = "azure" or "openai"). Whisper still detects the language
    first, on this computer. When Whisper can't run, a cloud service hears everything; when the cloud
    fails, Whisper does."""

    def __init__(self, local, cloud: dict, speech: dict):
        self.local = local
        self.offline = bool(speech.get("offline")) or speech.get("stt_engine", "auto") == "local"
        self.cloud = {} if self.offline else cloud
        self.engine = speech.get("stt_engine", "auto")
        self.by_language = speech.get("stt_by_language") or {}
        self.local_failed = False

    @classmethod
    def from_config(cls, cfg: dict) -> SpeechToText:
        speech = cfg["speech"]
        return cls(Transcriber(speech["whisper_model"], speech["languages"], mixed=speech.get("mixed", False)),
                   cloud_engines(cfg), speech)

    def engine_for(self, lang: str) -> str:
        engine = self.by_language.get(lang) or self.engine
        return engine if engine in self.cloud else "local"

    def fallback(self) -> str | None:
        """The cloud service to use when Whisper can't run: the chosen one, else OpenAI, which finds
        the language itself."""
        for engine in (self.engine, "openai", "azure"):
            if engine in self.cloud:
                return engine
        return None

    def transcribe(self, audio: np.ndarray, hint: str | None = None) -> Heard:
        """What was said. hint: the conversation's language, for a cloud service when Whisper can't
        tell the language."""
        if not self.cloud:
            return self.local.transcribe(audio)
        if not self.local_failed:
            try:
                lang, confidence = self.local.language(audio)
            except LOCAL_FAILURES as exc:
                logger.warning("Whisper can't run (%s): using cloud speech to text", type(exc).__name__)
                self.local_failed = True
        if self.local_failed:
            engine = self.fallback()
            asked = None if engine == "openai" else hint
            text = self.cloud[engine].transcribe(audio, asked)
            return Heard(text=text, lang=lang_mod.guess(text, (hint or "en").split("-")[0]), confidence=0.0)
        engine = self.engine_for(lang)
        if engine != "local":
            try:
                return Heard(text=self.cloud[engine].transcribe(audio, lang), lang=lang, confidence=confidence)
            except CloudSTTError as exc:
                logger.warning("%s: using Whisper", exc)
        return self.local.transcribe(audio, lang=lang)
