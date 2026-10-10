"""Microphone in, speech detection, and where an utterance starts and ends.

Audio is 16 kHz mono float32 in frames of 512 samples (32 ms), the size Silero VAD expects. A
microphone that refuses 16 kHz is opened at a rate it takes, and resampled.
"""

from __future__ import annotations

import math
import queue
import subprocess
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

RATE = 16000
FRAME = 512
FRAME_SECONDS = FRAME / RATE
# Tried in this order when the microphone refuses 16 kHz.
RATES = (RATE, 48000, 44100, 32000, 22050, 96000, 8000)
# Calibration: speech must be this much more likely than the room's noisiest moments, and louder than
# the room by this factor. The caps keep Rafiq from going deaf if someone talked while it listened.
VAD_MARGIN = 0.15
MAX_THRESHOLD = 0.9
RMS_FACTOR = 2.0
MAX_RMS_GATE = 0.1
NOISY_EXTRA_END = 0.2


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(frame, dtype="float64"))))


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


@dataclass(frozen=True)
class Calibration:
    """Thresholds fitted to the room: the speech probability and loudness a frame needs to count as
    speech, and how long a silence ends an utterance."""
    threshold: float
    end_seconds: float
    rms_gate: float
    noise_rms: float


def calibrate(frames: Sequence[np.ndarray], probs: Sequence[float], threshold: float = 0.5,
              end_seconds: float = 0.7) -> Calibration:
    """Fit the thresholds to a second or so of the room's background noise and the VAD's view of it.

    A quiet room keeps the usual threshold. A room where the fan or the TV sometimes looks like speech
    raises it, and waits a little longer for the end, since soft syllables then fall below it.
    """
    if not frames or not probs:
        return Calibration(threshold, end_seconds, 0.0, 0.0)
    noise_rms = float(np.median([rms(f) for f in frames]))       # the median: a cough is not the room
    noisy = float(np.percentile(probs, 90)) + VAD_MARGIN
    fitted = min(MAX_THRESHOLD, max(threshold, noisy))
    end = end_seconds + (NOISY_EXTRA_END if fitted > threshold else 0.0)
    return Calibration(round(fitted, 3), round(end, 3), min(MAX_RMS_GATE, noise_rms * RMS_FACTOR), noise_rms)


def measure_noise(mic, vad, seconds: float = 1.0, timeout: float = 0.5) -> tuple[list[np.ndarray], list[float]]:
    """About `seconds` of what the microphone hears, and the VAD's speech probability for each frame.
    Stops early if the microphone goes silent."""
    mic.drain()
    vad.reset()
    frames, probs = [], []
    while len(frames) < round(seconds / FRAME_SECONDS):
        frame = mic.read(timeout=timeout)
        if frame is None:
            break
        frames.append(frame)
        probs.append(vad(frame))
    vad.reset()
    return frames, probs


@dataclass
class Endpointer:
    """Turns per-frame speech probabilities into utterances.

    Speech starts after `start_frames` speech frames in a row, and ends after `end_seconds` of silence.
    A little audio from before the start is kept, so the first syllable is not cut off. Frames quieter
    than `rms_gate` (set by calibration) are never speech.
    """

    threshold: float = 0.5
    start_frames: int = 3
    end_seconds: float = 0.7
    pre_seconds: float = 0.3
    max_seconds: float = 20.0
    rms_gate: float = 0.0
    _pre: deque = field(default_factory=deque)
    _speech: list = field(default_factory=list)
    _run: int = 0
    _silence: int = 0
    active: bool = False

    def __post_init__(self):
        self._pre = deque(maxlen=max(1, int(self.pre_seconds / FRAME_SECONDS)))

    def apply(self, cal: Calibration) -> None:
        self.threshold, self.end_seconds, self.rms_gate = cal.threshold, cal.end_seconds, cal.rms_gate

    def reset(self) -> None:
        self._pre.clear()
        self._speech, self._run, self._silence, self.active = [], 0, 0, False

    def push(self, frame: np.ndarray, prob: float) -> np.ndarray | None:
        """Feed one frame; returns the whole utterance when it ends, else None."""
        speech = prob >= self.threshold and (self.rms_gate <= 0 or rms(frame) >= self.rms_gate)
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


def pick_rate(supported: Callable[[int], bool], preferred: int = 0, rates: Sequence[int] = RATES) -> int:
    """The first sample rate the device takes: the configured one, then 16 kHz, then the usual ones."""
    for rate in dict.fromkeys(([preferred] if preferred else []) + list(rates)):
        if supported(rate):
            return rate
    raise RuntimeError("the microphone takes none of the usual sample rates")


class EchoGate:
    """While Rafiq speaks, the microphone hears its voice too. A frame counts as you only when it is
    `factor` times louder than that echo (the median level of the last 1.5 s) and above `min_level`.
    Nothing counts during the first quarter second, while the echo level is not known yet."""

    WINDOW_SECONDS = 1.5
    WARM_SECONDS = 0.25

    def __init__(self, factor: float = 1.8, min_level: float = 0.01):
        self.factor, self.min_level = factor, min_level
        self.levels: deque = deque(maxlen=int(self.WINDOW_SECONDS / FRAME_SECONDS))
        self.warm = int(self.WARM_SECONDS / FRAME_SECONDS)

    def reset(self) -> None:
        self.levels.clear()

    def loud(self, frame: np.ndarray) -> bool:
        level = rms(frame)
        ready = len(self.levels) >= self.warm
        echo = float(np.median(self.levels)) if ready else 0.0
        self.levels.append(level)
        return ready and level >= max(self.min_level, self.factor * echo)


def echo_cancel_source(run=subprocess.run) -> str | None:
    """The microphone with PipeWire's (or PulseAudio's) echo cancellation applied, when the
    echo-cancel module is loaded (`pactl load-module module-echo-cancel`); else None."""
    try:
        out = run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        cols = line.split("\t")
        name = cols[1] if len(cols) > 1 else ""
        if "echo" in name and "cancel" in name and not name.endswith(".monitor"):
            return name
    return None


class Resampler:
    """Device audio at any rate in, 16 kHz frames of 512 samples out (scipy's resample_poly).

    Each block is resampled with a little of the audio on both sides, so the filter sees continuous
    sound and block edges do not click. That costs a delay of `pad` samples, about 20 ms at most.
    """

    def __init__(self, rate: int):
        self.rate = rate
        g = math.gcd(RATE, rate)
        self.up, self.down = RATE // g, rate // g
        self.blocksize = self.down * math.ceil(FRAME / self.up)       # gives a whole number of output samples
        reach = 10 * max(self.up, self.down) / self.up                 # resample_poly's filter, in input samples
        self.pad = self.down * (math.ceil(reach / self.down) + 1) if rate != RATE else 0
        self._in = np.zeros(self.pad, dtype="float32")
        self._out = np.zeros(0, dtype="float32")

    def push(self, block: np.ndarray) -> list[np.ndarray]:
        block = np.asarray(block, dtype="float32").reshape(-1)
        if self.rate == RATE:
            self._out = np.concatenate([self._out, block])
        else:
            from scipy.signal import resample_poly

            self._in = np.concatenate([self._in, block])
            window = 2 * self.pad + self.blocksize
            while len(self._in) >= window:
                y = resample_poly(self._in[:window], self.up, self.down)
                start = self.pad * self.up // self.down
                self._out = np.concatenate([self._out, y[start:start + self.blocksize * self.up // self.down]])
                self._in = self._in[self.blocksize:]
        frames = []
        while len(self._out) >= FRAME:
            frames.append(self._out[:FRAME].astype("float32"))
            self._out = self._out[FRAME:]
        return frames


def resolve_device(setting, devices: Sequence[dict]) -> int | None:
    """The input device the settings name, by index or by part of its name; None is the system default."""
    if setting is None or str(setting).strip() == "":
        return None
    text = str(setting).strip()
    if text.isdigit():
        index = int(text)
        if index < len(devices) and devices[index].get("max_input_channels", 0) > 0:
            return index
        raise ValueError(f"device {index} is not a microphone")
    for index, device in enumerate(devices):
        if text.lower() in device["name"].lower() and device.get("max_input_channels", 0) > 0:
            return index
    raise ValueError(f"no microphone named {text!r}")


def describe_inputs(devices: Sequence[dict], chosen: int | None) -> list[str]:
    """One line per microphone for `rafiq doctor`, the one Rafiq uses marked with a star."""
    return [f"{'*' if index == chosen else ' '} [{index}] {device['name']} "
            f"({int(device.get('default_samplerate', 0))} Hz)"
            for index, device in enumerate(devices) if device.get("max_input_channels", 0) > 0]


def rate_supported(sd, device: int | None, rate: int) -> bool:
    try:
        sd.check_input_settings(device=device, samplerate=rate, channels=1, dtype="float32")
        return True
    except Exception:          # PortAudio reports a refused rate with several exception types
        return False


class Microphone:
    """An input device as a queue of 16 kHz frames of 512 samples. Frames are dropped, never blocked on,
    while Rafiq is busy: an old frame is worth less than a responsive callback.

    `device` is an index or part of a name (None: the system default); `rate` is the rate to try first
    (0: 16 kHz). If the device refuses it, another rate is picked and resampled."""

    def __init__(self, device=None, rate: int = 0):
        self.frames: queue.Queue = queue.Queue(maxsize=int(30 / FRAME_SECONDS))
        self.device = device
        self.rate = rate
        self.resampler = Resampler(rate or RATE)
        self.stream = None
        self.muted = False
        self.fallback = False         # if the device can't open, use the system default

    @classmethod
    def from_config(cls, cfg: dict) -> Microphone:
        audio = cfg.get("audio", {})
        return cls(audio.get("device") or None, int(audio.get("rate") or 0))

    def _callback(self, indata, _frames, _time, _status):
        if self.muted:
            return
        for frame in self.resampler.push(indata[:, 0]):
            try:
                self.frames.put_nowait(frame)
            except queue.Full:
                pass

    def start(self) -> None:
        import sounddevice as sd

        try:
            index = resolve_device(self.device, sd.query_devices())
            self.rate = pick_rate(lambda rate: rate_supported(sd, index, rate), preferred=self.rate)
            self.resampler = Resampler(self.rate)
            self.stream = sd.InputStream(samplerate=self.rate, channels=1, dtype="float32",
                                         blocksize=self.resampler.blocksize, device=index, callback=self._callback)
        except (ValueError, RuntimeError, sd.PortAudioError):
            if not self.fallback:
                raise
            self.device, self.fallback = None, False   # the echo-cancelled device is not there: the default one
            return self.start()
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
