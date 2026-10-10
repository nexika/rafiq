"""Timers and reminders. They are saved to disk, so a reminder set for tomorrow survives a restart; a
timer or reminder that came due while Rafiq was off is said when it starts."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Alarm:
    id: str
    due: float          # epoch seconds
    label: str
    kind: str           # "timer" or "reminder"
    tag: str            # the language it was set in, so it is said in that language


class Scheduler:
    def __init__(self, path: Path, on_due: Callable[[Alarm], None], clock=time.time):
        self.path, self.on_due, self.clock = path, on_due, clock
        self.lock = threading.Lock()
        self.alarms: dict[str, Alarm] = {}
        self._load()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _load(self) -> None:
        try:
            for item in json.loads(self.path.read_text()):
                alarm = Alarm(**item)
                self.alarms[alarm.id] = alarm
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(a) for a in self.alarms.values()], ensure_ascii=False))
        tmp.replace(self.path)

    def add(self, due: float, label: str, kind: str, tag: str) -> Alarm:
        alarm = Alarm(uuid.uuid4().hex[:8], due, label, kind, tag)
        with self.lock:
            self.alarms[alarm.id] = alarm
            self._save()
        return alarm

    def cancel(self, alarm_id: str | None = None, label: str | None = None) -> list[Alarm]:
        with self.lock:
            gone = [a for a in self.alarms.values()
                    if (alarm_id and a.id == alarm_id) or (label and label.lower() in a.label.lower())]
            for a in gone:
                del self.alarms[a.id]
            self._save()
        return gone

    def pending(self) -> list[Alarm]:
        with self.lock:
            return sorted(self.alarms.values(), key=lambda a: a.due)

    def tick(self) -> list[Alarm]:
        """Fire every alarm that is due; returns them."""
        now = self.clock()
        with self.lock:
            due = [a for a in self.alarms.values() if a.due <= now]
            for a in due:
                del self.alarms[a.id]
            if due:
                self._save()
        for a in due:
            self.on_due(a)
        return due

    def start(self) -> None:
        def loop():
            while not self._stop.wait(1.0):
                try:
                    self.tick()
                except Exception:
                    pass

        self._thread = threading.Thread(target=loop, name="rafiq-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
