"""Hearing "Rafiq" by comparing what was said with your own recordings of it.

`rafiq enroll` records you saying the name in the forms you use ("Rafiq", "يا رفيق") and a few ordinary
sentences. Each recording becomes a sequence of log-mel frames (20 ms each, from openWakeWord's small
mel-spectrogram model). Live speech is matched against the name recordings with subsequence dynamic
time warping, which finds the name at any speed near the start of what was said. The threshold sits
between how close your recordings of the name are to each other and how close your ordinary sentences
come to them, so it fits your voice and microphone.

Tested on synthetic voices: with mixed forms recorded, every wake phrase was caught in English and in
Arabic, and look-alikes ("Rafael", "terrific", "رفعت", "ربيع", "فريق") stayed below the threshold except
one in English. Real voices in a real room need their own test: `rafiq enroll` reports one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config

MEL_URL = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx"
MEL_SHA256 = "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f"
HOP = 2                 # mel frames are 10 ms; two are averaged into one 20 ms frame
START_FRAMES = 50       # the name must start within the first second of speech
MAX_SECONDS = 2.5       # only the start of what was said is compared
SENSITIVITY = 0.45      # where the threshold sits between the name and ordinary speech (0 strict, 1 loose)


def folder() -> Path:
    path = config.data_dir() / "wake"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mel_model_path(download: bool = False) -> Path:
    path = config.data_dir() / "oww" / "melspectrogram.onnx"
    if not path.is_file() and download:
        import httpx

        path.parent.mkdir(parents=True, exist_ok=True)
        data = httpx.get(MEL_URL, follow_redirects=True, timeout=60).content
        if hashlib.sha256(data).hexdigest() != MEL_SHA256:
            raise RuntimeError("the downloaded mel-spectrogram model does not match its checksum")
        path.write_bytes(data)
    return path


class Mel:
    """Log-mel frames from openWakeWord's ONNX model, run directly with onnxruntime."""

    def __init__(self, path: Path | None = None):
        import onnxruntime

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = onnxruntime.InferenceSession(str(path or mel_model_path(download=True)),
                                                    providers=["CPUExecutionProvider"], sess_options=opts)

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        """(frames, 32) for 16 kHz float audio, 20 ms a frame."""
        audio = np.concatenate([audio.astype("float32"), np.zeros((-len(audio)) % 1280, "float32")])
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).astype(np.float32)[None, :]
        spec = np.squeeze(self.session.run(None, {"input": pcm})[0]) / 10 + 2
        spec = spec.reshape(-1, 32)
        n = len(spec) // HOP * HOP
        return spec[:n].reshape(-1, HOP, 32).mean(axis=1)


def sdtw(template: np.ndarray, seq: np.ndarray, max_start: int = START_FRAMES) -> float:
    """The lowest average frame distance of the template aligned to any stretch of seq that starts in
    its first max_start frames. Steps: diagonal, or one side waits; the cost is normalised by the path
    length so long and short matches compare fairly."""
    if len(template) == 0 or len(seq) == 0:
        return float("inf")
    cost = np.sqrt(((template[:, None, :] - seq[None, :, :]) ** 2).sum(-1) / template.shape[1])
    m, n = cost.shape
    acc = np.full(n, np.inf)
    length = np.ones(n)
    acc[:max_start] = cost[0, :max_start]
    for i in range(1, m):
        diag = np.concatenate([[np.inf], acc[:-1]])
        diag_len = np.concatenate([[0.0], length[:-1]])
        take_diag = diag < acc
        cand = np.where(take_diag, diag, acc)
        cand_len = np.where(take_diag, diag_len, length)
        row, row_len = np.empty(n), np.empty(n)
        prev, prev_len = np.inf, 0.0
        for j in range(n):           # the left step depends on this row, so it is sequential
            best, best_len = cand[j], cand_len[j]
            if prev < best:
                best, best_len = prev, prev_len
            prev, prev_len = cost[i, j] + best, best_len + 1
            row[j], row_len[j] = prev, prev_len
        acc, length = row, row_len
    return float(np.min(acc / length))


@dataclass
class Spotter:
    templates: list[np.ndarray]
    mean: np.ndarray
    std: np.ndarray
    threshold: float

    def normalize(self, frames: np.ndarray) -> np.ndarray:
        return (frames - self.mean) / self.std

    def score(self, frames: np.ndarray) -> float:
        seq = self.normalize(frames)
        return min(sdtw(t, seq) for t in self.templates)

    def save(self, path: Path) -> None:
        np.savez(path / "templates.npz", *self.templates, mean=self.mean, std=self.std)
        (path / "calibration.json").write_text(json.dumps({"threshold": self.threshold}))

    @classmethod
    def load(cls, path: Path | None = None) -> Spotter | None:
        path = path or folder()
        try:
            data = np.load(path / "templates.npz")
            threshold = json.loads((path / "calibration.json").read_text())["threshold"]
        except (OSError, ValueError, KeyError):
            return None
        templates = [data[k] for k in sorted(data.files, key=lambda k: (len(k), k)) if k.startswith("arr_")]
        return cls(templates, data["mean"], data["std"], float(threshold)) if templates else None


def calibrate(name_frames: list[np.ndarray], other_frames: list[np.ndarray],
              sensitivity: float = SENSITIVITY) -> tuple[Spotter, dict]:
    """A spotter from recordings of the name and of ordinary speech, both raw mel frames.

    The threshold lies between the name recordings' distance to each other (each one against the rest)
    and the ordinary sentences' distance to the name, at `sensitivity` of the way."""
    if len(name_frames) < 3:
        raise ValueError("record the name at least three times")
    stacked = np.concatenate(name_frames)
    mean, std = stacked.mean(axis=0), stacked.std(axis=0) + 1e-6
    templates = [(f - mean) / std for f in name_frames]
    same = [min(sdtw(t, templates[i]) for k, t in enumerate(templates) if k != i) for i in range(len(templates))]
    spotter = Spotter(templates, mean, std, threshold=0.0)
    others = [spotter.score(f) for f in other_frames]
    near, far = max(same), (min(others) if others else max(same) * 1.6)
    if far <= near:
        far = near * 1.15      # ordinary speech came as close as the name: stay strict
    spotter.threshold = near + sensitivity * (far - near)
    return spotter, {"name_spread": near, "ordinary_closest": far if others else None,
                     "threshold": spotter.threshold}
