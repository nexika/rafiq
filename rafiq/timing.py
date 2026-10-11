"""How long each stage of an answer took: speech-to-text, the model, each tool, the voice, and the
first sound after the user stopped talking.

One JSON line per measurement in logs/timing.log, with the stage, the milliseconds and labels such as
the backend or the tool's name. Never what was said or answered. `rafiq doctor` shows the medians.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from pathlib import Path

from . import config

MAX_BYTES = 500_000          # past this the older half of the log is dropped
STAGES = {"stt": "speech-to-text", "model": "model", "tool": "tool", "voice": "voice",
          "first_sound": "first sound"}

_lock = threading.Lock()


def path() -> Path:
    return config.home() / "logs" / "timing.log"


def record(stage: str, seconds: float, **labels: str) -> None:
    """Note one measurement. A log that cannot be written never stops an answer."""
    line = json.dumps({"time": round(time.time(), 3), "stage": stage, "ms": round(seconds * 1000), **labels})
    file = path()
    with _lock:
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            if file.is_file() and file.stat().st_size > MAX_BYTES:
                lines = file.read_text(encoding="utf-8").splitlines()
                file.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
            with file.open("a", encoding="utf-8") as out:
                out.write(line + "\n")
        except OSError:
            pass


class Timer:
    """`with Timer("tool", name="weather"):` records how long the block took."""

    def __init__(self, stage: str, **labels: str):
        self.stage, self.labels = stage, labels

    def __enter__(self) -> Timer:
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        record(self.stage, time.perf_counter() - self.start, **self.labels)


def summary(last: int = 200) -> dict[str, tuple[int, int]]:
    """The median milliseconds and the count of each stage, over the last measurements."""
    try:
        lines = path().read_text(encoding="utf-8").splitlines()[-last:]
    except OSError:
        return {}
    times: dict[str, list[int]] = {}
    for line in lines:
        try:
            entry = json.loads(line)
            times.setdefault(entry["stage"], []).append(int(entry["ms"]))
        except (ValueError, KeyError, TypeError):
            continue
    return {stage: (round(statistics.median(ms)), len(ms)) for stage, ms in times.items()}


def line(last: int = 200) -> str:
    """One line for `rafiq doctor`."""
    found = summary(last)
    if not found:
        return "no timings yet (rafiq run and rafiq ask write them)"
    order = [s for s in STAGES if s in found] + sorted(s for s in found if s not in STAGES)
    parts = [f"{STAGES.get(s, s)} {found[s][0]} ms" for s in order]
    return f"{', '.join(parts)} (last {sum(n for _, n in found.values())} timings, medians)"
