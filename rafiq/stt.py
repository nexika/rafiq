"""Speech to text with faster-whisper, on the CPU, limited to the languages Rafiq speaks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .lang import latinize, words

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
        text = " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.8).strip()
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
