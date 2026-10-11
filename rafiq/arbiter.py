"""When several microphones hear "Rafiq" at once (a satellite in the kitchen and the PC next door),
only the one that heard it best answers.

The first wake-up opens a short window (0.18 s) for the others to report theirs; when it closes, the
closest match wins and every microphone is then ignored for a few seconds (4 s), so the same "Rafiq"
arriving late, or echoing, does not start a second conversation.

Scores are those of the recordings-based spotter: lower is closer. Each microphone may have its own
recordings and threshold, so they are compared as a fraction of that microphone's threshold. On a tie,
the microphone with the higher priority wins (unlisted ones have 0).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

WINDOW_SECONDS = 0.18
LOCK_SECONDS = 4.0


class Arbiter:
    def __init__(self, window: float = WINDOW_SECONDS, lock: float = LOCK_SECONDS,
                 priority: dict[str, int] | None = None, clock: Callable[[], float] = time.monotonic):
        self.window, self.lock, self.clock = window, lock, clock
        self.priority = dict(priority or {})
        self._mutex = threading.Lock()
        self._opened: float | None = None          # when the first wake-up of this round came
        self._offers: dict[str, float] = {}       # microphone -> score as a fraction of its threshold
        self._winner: str | None = None

    def _expire(self, now: float) -> None:
        if self._opened is not None and now >= self._opened + self.window + self.lock:
            self._opened, self._offers, self._winner = None, {}, None

    def offer(self, mic: str, score: float, threshold: float = 1.0) -> bool:
        """A microphone heard the name. False if it came too late for this round (it will not answer)."""
        now = self.clock()
        with self._mutex:
            self._expire(now)
            if self._opened is None:
                self._opened = now
            elif now >= self._opened + self.window:
                return False
            ratio = score / threshold if threshold > 0 else score
            self._offers[mic] = min(ratio, self._offers.get(mic, ratio))
            return True

    def winner(self) -> str | None:
        """The microphone that answers this round, once the window has closed; None before that, or
        when nobody heard the name."""
        now = self.clock()
        with self._mutex:
            self._expire(now)
            if self._opened is None or now < self._opened + self.window:
                return None
            if self._winner is None:
                self._winner = min(self._offers, key=lambda m: (self._offers[m], -self.priority.get(m, 0)))
            return self._winner

    def claim(self, mic: str, score: float, threshold: float = 1.0) -> bool:
        """offer(), then wait for the window to close: whether this microphone should answer. For a
        listener thread per microphone; it blocks for at most the window."""
        if not self.offer(mic, score, threshold):
            return False
        with self._mutex:
            remaining = (self._opened or 0.0) + self.window - self.clock()
        if remaining > 0:
            time.sleep(remaining)
        return self.winner() == mic
