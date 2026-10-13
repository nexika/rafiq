"""`rafiq enroll`: record the name, the name before a request, and a few ordinary sentences, and fit the
wake-word spotter to them."""

from __future__ import annotations

import time
import wave
from pathlib import Path

import numpy as np

from . import config, kws, wake
from .audio import RATE, Endpointer, Microphone, SileroVAD

NAME_FORMS = ["Rafiq", "يا رفيق"]
TAKES = 3
# Said the way you would really call it: the name runs into the request. Any request will do.
REQUESTS = [
    "Rafiq, what time is it?",
    "يا رفيق، شغل الموسيقى",
    "Rafiq, set a timer for five minutes",
]
SENTENCES = [
    "What's the weather like today?",
    "أنا رايح الشغل دلوقتي",
    "Remind me to call my friend later.",
]


# A boosted laptop microphone (+50 dB) clipped every take: peaks at 1.0, speech RMS 0.65 to 0.86. The name
# and ordinary speech then looked alike (0.51 to 0.54 against 0.57) and Rafiq never woke. After lowering
# the boost, peaks were 0.24 to 0.44 and no sample reached full scale; the same takes with the boost back
# (+20 dB) had 7.6 to 26% of their samples there.
CLIP_LEVEL = 0.99
CLIP_SHARE = 0.001       # more than this share of samples at full scale is clipping, not one click
TOO_LOUD = ("  That was too loud: the sound hit the microphone's limit and is distorted, so I can't learn "
            "the name from it. Lower the microphone's input or its boost (the desktop's sound settings, or "
            "`alsamixer`, F4), then try again.")


def too_loud(clip: np.ndarray) -> bool:
    """Whether a recording clipped: distorted sound makes the name look like any other speech."""
    return len(clip) > 0 and float(np.mean(np.abs(clip) >= CLIP_LEVEL)) > CLIP_SHARE


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


def form_tag(form: str) -> str | None:
    """The language a form of the name says it in: "يا رفيق" is Arabic; "Rafiq" is said the same way in
    every language, so it keeps the conversation's."""
    return "ar" if any("\u0600" <= ch <= "\u06ff" for ch in form) else None


def head(clip: np.ndarray) -> np.ndarray:
    return clip[:int(kws.MAX_SECONDS * RATE)]


def fit(name_clips: list[np.ndarray], other_clips: list[np.ndarray], mel: kws.Mel | None = None,
        sensitivity: float = kws.SENSITIVITY, request_clips: list[np.ndarray] = (),
        name_tags: list[str | None] = ()):
    mel = mel or kws.Mel()
    return kws.calibrate([mel(c) for c in name_clips], [mel(head(c)) for c in other_clips], sensitivity,
                         request_frames=[mel(head(c)) for c in request_clips], name_tags=name_tags)


def run(forms: list[str] | None = None, takes: int = TAKES, say=print, ask=input) -> int:
    cfg = config.load()
    forms = forms or [*NAME_FORMS, *wake.names(cfg)]
    out = kws.folder()
    for old in out.glob("*.wav"):
        old.unlink()
    mic, vad = Microphone.from_config(cfg), SileroVAD()
    mel = kws.Mel()

    def record(prompt: str, timeout: float = 8.0, required: bool = True) -> np.ndarray | None:
        while True:
            ask(f"  Press Enter, then say {prompt} ")
            clip = record_one(mic, vad, timeout)
            if clip is not None and too_loud(clip):
                say(TOO_LOUD)
                continue
            if clip is not None or not required:
                return clip
            say("  I heard nothing; again, please.")

    say("Recording your voice for the wake word. Speak at your normal distance from the microphone.")
    names, tags, requests, others = [], [], [], []
    mic.start()
    try:
        for form in forms:
            for take in range(1, takes + 1):
                names.append(record(f'"{form}" ({take}/{takes})'))
                tags.append(form_tag(form))
                save_wav(out / f"name-{len(names)}.wav", names[-1])
        say("Now call me with a request, the way you normally would (any request is fine), for example:")
        for i, example in enumerate(REQUESTS, 1):
            requests.append(record(f'something like "{example}"', timeout=10))
            save_wav(out / f"request-{i}.wav", requests[-1])
        say("Last, a few ordinary sentences, so I learn what is not my name.")
        for i, sentence in enumerate(SENTENCES, 1):
            clip = record(f'"{sentence}"', timeout=10, required=False)
            if clip is not None:
                others.append(clip)
                save_wav(out / f"other-{i}.wav", clip)
    finally:
        mic.stop()
    spotter, report = fit(names, others, mel, request_clips=requests, name_tags=tags)
    spotter.save(out)
    say(f"Saved {len(names)} recordings of the name, {len(requests)} requests and {len(others)} sentences "
        f"in {out}.")
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
    requests = [load_wav(p) for p in sorted(out.glob("request-*.wav"))]
    others = [load_wav(p) for p in sorted(out.glob("other-*.wav"))]
    previous = kws.Spotter.load(out)       # the recordings don't say which form they are; the last fit does
    tags = previous.tags[:len(names)] if previous else []
    spotter, report = fit(names, others, sensitivity=sensitivity, request_clips=requests, name_tags=tags)
    spotter.save(out)
    return report
