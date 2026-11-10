"""Recordings for Rafiq's own offline voice (#250). One speaker reads one sentence at a time; each take is kept in
the layout Piper's training reads: wavs/<id>.wav and metadata.csv with `<id>.wav|text` lines.

A sentence's id comes from its text, so editing or reordering a list never pairs a recording with other words,
and a session that stops resumes where it left off."""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import numpy as np

from . import config, lang

RATE = 22050                     # Piper's medium voices are trained at 22050 Hz
LISTS = Path(__file__).parent / "voice_sentences"
SHORTEST = 0.5                   # seconds: a take shorter than any sentence is a false start
LOUDEST = 0.99                   # a peak here means the take clipped
QUIETEST = 0.1                   # a peak below this is too quiet to train on (about -20 dBFS)
PAD = 0.2                        # seconds of silence kept before and after the speech


def sentences(tag: str, extra: Path | None = None) -> list[str]:
    """The list for `tag` (or `extra`), then Rafiq's own PHRASES in that language, since those are what it says most.
    Phrases with a {placeholder} are left out: the filled-in words would differ every time."""
    path = extra or LISTS / f"{tag}.txt"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    found = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    found += [p[tag] for p in lang.PHRASES.values() if tag in p and "{" not in p[tag]]
    return list(dict.fromkeys(" ".join(s.replace("|", " ").split()) for s in found))


def sentence_id(tag: str, text: str) -> str:
    return f"{tag}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:10]}"


def recorded(folder: Path) -> set[str]:
    meta = folder / "metadata.csv"
    if not meta.is_file():
        return set()
    return {line.split("|", 1)[0].removesuffix(".wav") for line in meta.read_text(encoding="utf-8").splitlines()
            if "|" in line}


def trim(audio: np.ndarray, rate: int = RATE, pad: float = PAD) -> np.ndarray:
    """Cut the silence before and after the speech, keeping `pad` seconds of it: the key presses that start and stop
    a take are not part of the voice. Speech is any 20 ms window louder than a twentieth of the loudest one."""
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    step = int(rate * 0.02)
    if len(audio) < step:
        return audio
    windows = audio[:len(audio) // step * step].reshape(-1, step)
    rms = np.sqrt((windows ** 2).mean(axis=1))
    loud = np.flatnonzero(rms > max(rms.max() / 20, 1e-4))
    if not len(loud):
        return audio[:0]
    keep = int(rate * pad)
    return audio[max(0, loud[0] * step - keep):min(len(audio), (loud[-1] + 1) * step + keep)]


def problem(audio: np.ndarray, rate: int = RATE) -> str | None:
    """Why a take should be recorded again, or None."""
    if len(audio) < SHORTEST * rate:
        return "nothing was heard"
    peak = float(np.abs(audio).max())
    if peak >= LOUDEST:
        return "too loud, it clipped: move back a little or turn the microphone down"
    if peak < QUIETEST:
        return "too quiet: move closer or turn the microphone up"
    return None


def save(folder: Path, sid: str, text: str, audio: np.ndarray, rate: int = RATE) -> Path:
    """The take as 16-bit mono, then its line in metadata.csv, so a line never names a file that isn't there."""
    (folder / "wavs").mkdir(parents=True, exist_ok=True)
    path = folder / "wavs" / f"{sid}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    with open(folder / "metadata.csv", "a", encoding="utf-8", newline="\n") as meta:
        meta.write(f"{sid}.wav|{text}\n")
    return path


def minutes(folder: Path) -> float:
    total = 0.0
    for path in (folder / "wavs").glob("*.wav"):
        with wave.open(str(path), "rb") as w:
            total += w.getnframes() / w.getframerate()
    return total / 60


def record_until_enter(ask=input) -> np.ndarray:
    """The microphone from now until Enter, at Piper's rate (PipeWire converts from the device's own)."""
    import sounddevice as sd

    blocks: list[np.ndarray] = []
    with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", callback=lambda d, *_: blocks.append(d.copy())):
        ask("  recording... Enter to stop ")
    return np.concatenate(blocks).reshape(-1) if blocks else np.zeros(0, dtype=np.float32)


def play(audio: np.ndarray) -> None:
    import sounddevice as sd

    sd.play(audio, RATE)
    sd.wait()


def run(tag: str, folder: Path | None = None, extra: Path | None = None, ask=input, record=record_until_enter,
        listen=play, say=print) -> int:
    """Read each sentence not yet recorded. Enter records, Enter stops; then keep, play or retry. s skips, q quits."""
    folder = folder or config.data_dir() / "voice" / tag
    todo = sentences(tag, extra)
    if not todo:
        say(f"No sentences for {tag}: add {LISTS / (tag + '.txt')} or pass --sentences FILE")
        return 1
    have = recorded(folder)
    left = [s for s in todo if sentence_id(tag, s) not in have]
    say(f"{len(todo) - len(left)} of {len(todo)} sentences recorded, in {folder}.")
    say("Read each one as you would say it to a friend, at an even distance from the microphone.")
    for n, text in enumerate(left, start=len(todo) - len(left) + 1):
        say(f"\n[{n}/{len(todo)}] {text}")
        while True:
            answer = ask("Enter to record, s to skip, q to quit: ").strip().lower()
            if answer == "q":
                say(f"Stopped. {minutes(folder):.1f} minutes recorded so far.")
                return 0
            if answer == "s":
                break
            take = trim(record(ask))
            why = problem(take)
            if why:
                say(f"  {why}. Again.")
                continue
            while (answer := ask("  Enter to keep, p to play, r to retry: ").strip().lower()) == "p":
                listen(take)
            if answer == "r":
                continue
            save(folder, sentence_id(tag, text), text, take)
            break
    say(f"Done: {minutes(folder):.1f} minutes in {folder}. Piper needs about 30 to 60 for a fine-tuned voice.")
    return 0
