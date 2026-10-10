"""Barge-in: saying "stop" (or "attends", "espera", "warte", "توقف", "استنى") over Rafiq cuts its speech.

The microphone stays open while Rafiq speaks. Two checks keep Rafiq's own voice from stopping it: a
sound must be clearly louder than the echo of the speaker (EchoGate), and what was said must be a short
stop word. With PipeWire's echo-cancel module loaded, the microphone hears much less of the echo.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import numpy as np

from . import lang
from .audio import EchoGate, Endpointer


class BargeIn:
    def __init__(self, transcribe: Callable[[np.ndarray], str], on_stop: Callable[[], None],
                 factor: float = 1.8, min_level: float = 0.01, frames: int = 3):
        self.transcribe, self.on_stop = transcribe, on_stop
        self.gate = EchoGate(factor, min_level)
        # Loud frames stand in for speech: `frames` in a row start an utterance, 0.3 s of echo ends it.
        self.endpointer = Endpointer(start_frames=frames, end_seconds=0.3, pre_seconds=0.2, max_seconds=2.0)
        self._thread: threading.Thread | None = None
        self._done = threading.Event()

    def reset(self) -> None:
        self.gate.reset()
        self.endpointer.reset()

    def push(self, frame: np.ndarray) -> bool:
        """Feed one frame heard while Rafiq speaks; True when it was told to stop (and on_stop ran)."""
        utterance = self.endpointer.push(frame, 1.0 if self.gate.loud(frame) else 0.0)
        if utterance is None or not lang.is_stop(self.transcribe(utterance)):
            return False
        self.on_stop()
        return True

    # ------------------------------------------------------------------ while Rafiq speaks

    def start(self, read: Callable[[float], np.ndarray | None]) -> None:
        """Listen on a thread of its own, reading frames with read(timeout), until stop()."""
        self.reset()
        self._done.clear()
        self._thread = threading.Thread(target=self._listen, args=(read,), name="rafiq-barge-in", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._done.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _listen(self, read) -> None:
        while not self._done.is_set():
            frame = read(0.1)
            if frame is not None and self.push(frame):
                return
