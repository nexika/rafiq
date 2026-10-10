"""Microphone in, speech detection, and where an utterance starts and ends.

Audio is 16 kHz mono float32 in frames of 512 samples (32 ms), the size Silero VAD expects.
"""

from __future__ import annotations

import queue
from collections import deque
from dataclasses import dataclass, field

import numpy as np

RATE = 16000
FRAME = 512
FRAME_SECONDS = FRAME / RATE


def level(frame: np.ndarray) -> float:
    """How loud a frame is, from 0 (silence, or below -60 dBFS) to 1 (full scale), for the panel's meter."""
    rms = float(np.sqrt(np.mean(np.square(frame, dtype="float64")))) if len(frame) else 0.0
    if rms <= 0.001:
        return 0.0
    return round(min(1.0, 1 + 20 * np.log10(rms) / 60), 3)


class SileroVAD:
    """Silero VAD over a live stream: unlike faster-whisper's own wrapper, the model's state is kept
    from one frame to the next. Uses the ONNX model that ships with faster-whisper."""

    CONTEXT = 64

    def __init__(self, path: str | None = None):
        import onnxruntime
        from faster_whisper.vad import get_assets_path

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = onnxruntime.InferenceSession(
            path or f"{get_assets_path()}/silero_vad_v6.onnx", providers=["CPUExecutionProvider"],
            sess_options=opts)
        self.reset()

    def reset(self) -> None:
        self.h = np.zeros((1, 1, 128), dtype="float32")
        self.c = np.zeros((1, 1, 128), dtype="float32")
        self.context = np.zeros((1, self.CONTEXT), dtype="float32")

    def __call__(self, frame: np.ndarray) -> float:
        """The probability that this 512-sample frame is speech."""
        x = np.concatenate([self.context, frame.reshape(1, FRAME).astype("float32")], axis=1)
        out, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
        self.context = x[:, -self.CONTEXT:]
        return float(np.asarray(out).reshape(-1)[0])


@dataclass
class Endpointer:
    """Turns per-frame speech probabilities into utterances.

    Speech starts after `start_frames` speech frames in a row, and ends after `end_seconds` of silence.
    A little audio from before the start is kept, so the first syllable is not cut off.
    """

    threshold: float = 0.5
    start_frames: int = 3
    end_seconds: float = 0.7
    pre_seconds: float = 0.3
    max_seconds: float = 20.0
    _pre: deque = field(default_factory=deque)
    _speech: list = field(default_factory=list)
    _run: int = 0
    _silence: int = 0
    active: bool = False

    def __post_init__(self):
        self._pre = deque(maxlen=max(1, int(self.pre_seconds / FRAME_SECONDS)))

    def reset(self) -> None:
        self._pre.clear()
        self._speech, self._run, self._silence, self.active = [], 0, 0, False

    def push(self, frame: np.ndarray, prob: float) -> np.ndarray | None:
        """Feed one frame; returns the whole utterance when it ends, else None."""
        speech = prob >= self.threshold
        if not self.active:
            self._pre.append(frame)
            self._run = self._run + 1 if speech else 0
            if self._run >= self.start_frames:
                self.active, self._speech, self._silence = True, list(self._pre), 0
                self._pre.clear()
            return None
        self._speech.append(frame)
        self._silence = 0 if speech else self._silence + 1
        too_long = len(self._speech) * FRAME_SECONDS >= self.max_seconds
        if self._silence * FRAME_SECONDS >= self.end_seconds or too_long:
            utterance = np.concatenate(self._speech)
            self.reset()
            return utterance
        return None


class Microphone:
    """The default input device as a queue of 512-sample frames. Frames are dropped, never blocked on,
    while Rafiq is busy: an old frame is worth less than a responsive callback."""

    def __init__(self, device=None):
        self.frames: queue.Queue = queue.Queue(maxsize=int(30 / FRAME_SECONDS))
        self.device = device
        self.stream = None
        self.muted = False

    def _callback(self, indata, _frames, _time, _status):
        if self.muted:
            return
        try:
            self.frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def start(self) -> None:
        import sounddevice as sd

        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=FRAME,
                                     device=self.device, callback=self._callback)
        self.stream.start()

    def stop(self) -> None:
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def drain(self) -> None:
        """Forget what was heard so far, for example Rafiq's own voice while it spoke."""
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                return

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            return None
