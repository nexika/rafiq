"""Speech to text with faster-whisper, on the CPU, limited to the languages Rafiq speaks."""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np

from .hallucinations import is_hallucination, is_speech

HOTWORDS = "Rafiq رفيق"
# For the small model that only listens for the name: in tests on six languages, a prompt found the
# name more often than hotwords (6 of 8 against 5 of 8) and as rarely by mistake.
WAKE_PROMPT = "Rafiq, رفيق."
WAKE_SECONDS = 2.5


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
    def __init__(self, size: str = "small", languages: list[str] | None = None):
        self.size = size
        self.languages = languages or ["en", "fr", "es", "de", "ar"]
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
        segments, _ = self.model.transcribe(audio, language=lang, beam_size=beam_size,
                                            condition_on_previous_text=False, vad_filter=False,
                                            hotwords=hotwords, initial_prompt=prompt, without_timestamps=True)
        # Silence and noise come back as stock phrases ("Thanks for watching!"): never a request.
        kept = [s.text.strip() for s in segments if is_speech(s) and not is_hallucination(s.text)]
        text = " ".join(kept).strip()
        if is_hallucination(text):
            text = ""
        return Heard(text=text, lang=lang, confidence=confidence)
