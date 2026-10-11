"""Voice ID: whether the yes to an action that changes something came from your voice.

`rafiq voiceid` turns your `rafiq enroll` recordings into a voice print: each recording becomes a
256-number speaker embedding (WeSpeaker's ResNet34 model, trained on VoxCeleb, run with onnxruntime),
and the voice print is their average direction. A spoken yes is compared with it by cosine
similarity; below the threshold, Rafiq is not sure it was you and asks for the panel's Yes button.

A recording or a cloned voice can fool it, so it is an extra check on top of the spoken yes, never the
only one. A spoken no is taken from anyone: cancelling is always safe.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config

MODEL_URL = ("https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/main/"
             "voxceleb_resnet34_LM.onnx")
MODEL_SHA256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"
RATE = 16000
MIN_FRAMES = 100   # 10 ms frames
MARGIN = 0.1     # how far below your least typical recording a yes may still score
FLOOR = 0.5      # never accept below this, however unlike each other your recordings are
CEILING = 0.65   # nor ask for more: a short "yes" scores lower than a full sentence


def folder() -> Path:
    path = config.data_dir() / "voiceid"
    path.mkdir(parents=True, exist_ok=True)
    return path


def model_path(download: bool = False) -> Path:
    path = config.data_dir() / "wespeaker" / "voxceleb_resnet34_LM.onnx"
    if not path.is_file() and download:
        import httpx

        path.parent.mkdir(parents=True, exist_ok=True)
        data = httpx.get(MODEL_URL, follow_redirects=True, timeout=120).content
        if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
            raise RuntimeError("the downloaded speaker model does not match its checksum")
        path.write_bytes(data)
    return path


def _mel_banks(bins: int = 80, nfft: int = 512, low: float = 20.0, high: float = RATE / 2) -> np.ndarray:
    """Kaldi's triangular mel filters, (bins, nfft // 2 + 1); the Nyquist bin gets no weight."""
    def mel(f):
        return 1127.0 * np.log(1.0 + np.asarray(f) / 700.0)

    edges = np.linspace(mel(low), mel(high), bins + 2)
    freqs = mel(np.arange(nfft // 2) * RATE / nfft)
    left, center, right = edges[:-2, None], edges[1:-1, None], edges[2:, None]
    up, down = (freqs - left) / (center - left), (right - freqs) / (right - center)
    banks = np.maximum(0.0, np.minimum(up, down))
    return np.pad(banks, ((0, 0), (0, 1)))


_BANKS = _mel_banks()


def fbank(audio: np.ndarray, cmn: bool = True) -> np.ndarray:
    """80 log-mel energies every 10 ms, as Kaldi computes them (25 ms Hamming windows, pre-emphasis,
    no dither), with each bin's mean removed: what WeSpeaker's models were trained on."""
    x = np.asarray(audio, dtype=np.float64) * 32768
    length, shift = 400, 160
    if len(x) < length:
        return np.zeros((0, 80), dtype="float32")
    count = 1 + (len(x) - length) // shift
    frames = x[np.arange(length)[None, :] + shift * np.arange(count)[:, None]]
    frames = frames - frames.mean(axis=1, keepdims=True)
    frames = np.concatenate([frames[:, :1] * (1 - 0.97), frames[:, 1:] - 0.97 * frames[:, :-1]], axis=1)
    frames = frames * np.hamming(length)
    power = np.abs(np.fft.rfft(frames, n=512)) ** 2
    feats = np.log(np.maximum(power @ _BANKS.T, np.finfo(np.float32).eps))
    if cmn:
        feats = feats - feats.mean(axis=0)
    return feats.astype("float32")


class Embedder:
    """A speaker embedding of a recording, of length one, from the WeSpeaker ONNX model."""

    def __init__(self, path: Path | None = None):
        import onnxruntime

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = onnxruntime.InferenceSession(str(path or model_path(download=True)),
                                                    providers=["CPUExecutionProvider"], sess_options=opts)

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        feats = fbank(audio)
        if len(feats) == 0:
            return np.zeros(256, dtype="float32")
        if len(feats) < MIN_FRAMES:     # a short "yes": repeated, as the model gives nothing useful under ~1 s
            feats = np.tile(feats, (-(-MIN_FRAMES // len(feats)), 1))
        emb = np.squeeze(self.session.run(None, {"feats": feats[None]})[0])
        return unit(emb) if np.all(np.isfinite(emb)) else np.zeros(256, dtype="float32")


def unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype="float32")
    return v / (np.linalg.norm(v) or 1.0)


@dataclass
class VoicePrint:
    centroid: np.ndarray
    threshold: float

    def score(self, embedding: np.ndarray) -> float:
        """Cosine similarity with the voice print: 1 the same direction, 0 unrelated."""
        return float(unit(embedding) @ self.centroid)

    def matches(self, score: float) -> bool:
        return score >= self.threshold

    def save(self, path: Path) -> None:
        np.save(path / "voiceprint.npy", self.centroid)
        (path / "voiceprint.json").write_text(json.dumps({"threshold": self.threshold}))

    @classmethod
    def load(cls, path: Path | None = None) -> VoicePrint | None:
        path = path or folder()
        try:
            centroid = np.load(path / "voiceprint.npy")
            threshold = json.loads((path / "voiceprint.json").read_text())["threshold"]
        except (OSError, ValueError, KeyError):
            return None
        return cls(unit(centroid), float(threshold))


def build(embeddings: list[np.ndarray]) -> tuple[VoicePrint, dict]:
    """A voice print from embeddings of your recordings. The threshold sits a little below how close
    each recording comes to the average of the others, kept between FLOOR and CEILING."""
    if len(embeddings) < 3:
        raise ValueError("a voice print needs at least three recordings")
    embs = [unit(e) for e in embeddings]
    same = [float(e @ unit(sum(embs[:i] + embs[i + 1:]))) for i, e in enumerate(embs)]
    threshold = min(CEILING, max(FLOOR, min(same) - MARGIN))
    return VoicePrint(unit(sum(embs)), threshold), {"recordings": len(embs), "least_typical": min(same),
                                                    "threshold": threshold}


def measure(mine: list[np.ndarray], others: list[np.ndarray]) -> dict:
    """False rejects: each of your recordings checked against a voice print built from the rest.
    False accepts: other people's recordings checked against the voice print built from all of yours."""
    rejects = sum(not vp.matches(vp.score(e))
                  for i, e in enumerate(mine)
                  for vp in [build(mine[:i] + mine[i + 1:])[0]])
    vp = build(mine)[0]
    accepts = sum(vp.matches(vp.score(e)) for e in others)
    return {"mine": len(mine), "false_rejects": rejects, "others": len(others), "false_accepts": accepts,
            "threshold": vp.threshold}


def from_recordings(others: Path | None = None, embedder=None) -> dict:
    """Build and save the voice print from the `rafiq enroll` recordings, and measure it: false rejects
    on your own recordings, false accepts on the .wav files in `others` (other people, a video...)."""
    from . import kws
    from .enroll import load_wav

    mine = sorted(kws.folder().glob("*.wav"))
    if len(mine) < 4:
        raise ValueError("record your voice first: rafiq enroll")
    embedder = embedder or Embedder()
    mine_embs = [embedder(load_wav(p)) for p in mine]
    other_embs = [embedder(load_wav(p)) for p in sorted(others.glob("*.wav"))] if others else []
    vp, report = build(mine_embs)
    vp.save(folder())
    return {**report, **measure(mine_embs, other_embs)}
