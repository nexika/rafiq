"""`rafiq enroll`: record the name and a few ordinary sentences, and fit the wake-word spotter to them."""

from __future__ import annotations

import time
import wave
from pathlib import Path

import numpy as np

from . import kws
from .audio import RATE, Endpointer, Microphone, SileroVAD

NAME_FORMS = ["Rafiq", "يا رفيق"]
TAKES = 3
SENTENCES = [
    "What's the weather like today?",
    "أنا رايح الشغل دلوقتي",
    "Remind me to call my friend later.",
]


def save_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype("float32") / 32768


def record_one(mic: Microphone, vad: SileroVAD, timeout: float = 8.0) -> np.ndarray | None:
    endpointer = Endpointer(end_seconds=0.5, max_seconds=6.0)
    vad.reset()
    mic.drain()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline or endpointer.active:
        frame = mic.read(timeout=0.1)
        if frame is None:
            continue
        utterance = endpointer.push(frame, vad(frame))
        if utterance is not None and len(utterance) / RATE >= 0.25:
            return trim(utterance, vad)
    return None


def trim(audio: np.ndarray, vad: SileroVAD, frame: int = 512) -> np.ndarray:
    """The speech in a recording, without the silence around it (a little margin kept)."""
    vad.reset()
    probs = [vad(audio[i:i + frame]) for i in range(0, len(audio) - frame + 1, frame)]
    speech = [i for i, p in enumerate(probs) if p >= 0.5]
    if not speech:
        return audio
    return audio[max(0, (speech[0] - 2) * frame):min(len(audio), (speech[-1] + 3) * frame)]


def fit(name_clips: list[np.ndarray], other_clips: list[np.ndarray], mel: kws.Mel | None = None,
        sensitivity: float = kws.SENSITIVITY):
    mel = mel or kws.Mel()
    return kws.calibrate([mel(c) for c in name_clips], [mel(c[:int(kws.MAX_SECONDS * RATE)]) for c in other_clips],
                         sensitivity)


def run(forms: list[str] | None = None, takes: int = TAKES, say=print, ask=input) -> int:
    forms = forms or NAME_FORMS
    out = kws.folder()
    for old in out.glob("*.wav"):
        old.unlink()
    mic, vad = Microphone(), SileroVAD()
    mel = kws.Mel()
    say("Recording your voice for the wake word. Speak at your normal distance from the microphone.")
    names, others = [], []
    mic.start()
    try:
        for form in forms:
            for take in range(1, takes + 1):
                while True:
                    ask(f'  Press Enter, then say "{form}" ({take}/{takes}) ')
                    clip = record_one(mic, vad)
                    if clip is not None:
                        break
                    say("  I heard nothing; again, please.")
                names.append(clip)
                save_wav(out / f"name-{len(names)}.wav", clip)
        say("Now a few ordinary sentences, so I learn what is not my name.")
        for i, sentence in enumerate(SENTENCES, 1):
            ask(f'  Press Enter, then say: "{sentence}" ')
            clip = record_one(mic, vad, timeout=10)
            if clip is not None:
                others.append(clip)
                save_wav(out / f"other-{i}.wav", clip)
    finally:
        mic.stop()
    spotter, report = fit(names, others, mel)
    spotter.save(out)
    say(f"Saved {len(names)} recordings of the name and {len(others)} sentences in {out}.")
    say(f"  your recordings of the name differ by up to {report['name_spread']:.2f}; "
        + (f"ordinary speech comes no closer than {report['ordinary_closest']:.2f}; "
           if report["ordinary_closest"] is not None else "")
        + f"Rafiq wakes below {report['threshold']:.2f}.")
    if report["ordinary_closest"] is not None and report["ordinary_closest"] - report["name_spread"] < 0.08:
        say("  Your name recordings and ordinary speech are close: run `rafiq enroll` again in a quieter "
            "moment, or say the name more clearly.")
    return 0


def recalibrate(sensitivity: float) -> dict:
    """Fit the threshold again from the saved recordings, with another sensitivity."""
    out = kws.folder()
    names = [load_wav(p) for p in sorted(out.glob("name-*.wav"))]
    others = [load_wav(p) for p in sorted(out.glob("other-*.wav"))]
    spotter, report = fit(names, others, sensitivity=sensitivity)
    spotter.save(out)
    return report
