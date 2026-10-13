"""The morning paper on real paper: printed with CUPS (`lp`), once a day at most, and only after a yes.

Rafiq offers it at a fixed time (a systemd user timer runs `rafiq paper offer`, and the running Rafiq
asks out loud) or after the first brief of the day (`[paper] after_brief = true`). A lock file holds
the date of the last print, so the timer, the voice and the terminal never print the same day twice.
The PDF itself is made elsewhere; by default it is data/paper/YYYY-MM-DD.pdf.
"""

from __future__ import annotations

import fcntl
import re
import subprocess
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Protocol

from . import config as config_mod

ACTION = "print today's morning paper"
TIMER = "rafiq-paper.timer"
SERVICE = "rafiq-paper.service"


class PrintError(Exception):
    pass


class Printer(Protocol):
    def print_file(self, path: Path) -> str:
        """Send a file to the printer; returns the print job's id."""
        ...


class CupsPrinter:
    """CUPS through `lp`, as an argument list: the file name can never become a command."""

    def __init__(self, name: str = "", run=subprocess.run):
        self.name, self.run = name, run

    def print_file(self, path) -> str:
        args = ["lp"] + (["-d", self.name] if self.name else []) + ["--", str(path)]
        try:
            done = self.run(args, capture_output=True, text=True, timeout=30, check=False)
        except FileNotFoundError as exc:
            raise PrintError("lp is not installed (CUPS)") from exc
        except subprocess.TimeoutExpired as exc:
            raise PrintError("the printer did not answer") from exc
        if done.returncode != 0:
            raise PrintError((done.stderr or done.stdout or "lp failed").strip())
        found = re.search(r"request id is (\S+)", done.stdout or "")
        return found.group(1) if found else ""


class DailyLock:
    """The date of the last print, in a file every Rafiq process shares; checked and set under flock."""

    def __init__(self, path: Path):
        self.path = path

    def _locked(self, change: Callable[[str], str | None]) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            new = change(f.read().strip())
            if new is None:
                return False
            f.seek(0)
            f.truncate()
            f.write(new)
            return True

    def printed_on(self, day: date) -> bool:
        try:
            return self.path.read_text(encoding="utf-8").strip() == day.isoformat()
        except OSError:
            return False

    def claim(self, day: date) -> bool:
        """Mark the day as printed; False when it already was."""
        return self._locked(lambda last: None if last == day.isoformat() else day.isoformat())

    def release(self, day: date) -> None:
        self._locked(lambda last: "" if last == day.isoformat() else None)


class MorningPaper:
    def __init__(self, cfg: dict, printer: Printer | None = None, lock: DailyLock | None = None,
                 folder: Path | None = None):
        self.settings = cfg.get("paper", {})
        self.printer = printer or CupsPrinter(self.settings.get("printer", ""))
        self.lock = lock or DailyLock(config_mod.data_dir() / "paper.lock")
        self.folder = folder or config_mod.data_dir() / "paper"

    def pdf_for(self, day: date) -> Path:
        if self.settings.get("file"):
            return Path(self.settings["file"].replace("{date}", day.isoformat())).expanduser()
        return self.folder / f"{day.isoformat()}.pdf"

    def print_today(self, confirm: Callable[[str], bool], day: date | None = None) -> str:
        """Print the day's paper if it is there, not printed yet, and the user says yes."""
        day = day or date.today()
        if self.lock.printed_on(day):
            return "already printed today"
        pdf = self.pdf_for(day)
        if not pdf.is_file():
            return f"error: no paper for today yet ({pdf})"
        if not confirm(ACTION):
            return "declined"
        if not self.lock.claim(day):            # printed by another Rafiq while we were asking
            return "already printed today"
        try:
            job = self.printer.print_file(pdf)
        except PrintError as exc:
            self.lock.release(day)              # nothing came out: a later try may print
            return f"error: printing failed: {exc}"
        return f"printed (job {job})" if job else "printed"

    def after_brief(self, confirm: Callable[[str], bool], day: date | None = None) -> str | None:
        """Called after a brief: offers the paper when the settings ask for it, else None."""
        if not self.settings.get("after_brief"):
            return None
        return self.print_today(confirm, day)


def timer_units(at: str, python: str) -> dict[str, str]:
    """The systemd user timer that has the running Rafiq offer the paper at `at` (HH:MM) every day."""
    found = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", at)
    if not found:
        raise ValueError(f"give the time as HH:MM, not {at!r}")
    return {
        SERVICE: ("[Unit]\nDescription=Rafiq: offer to print the morning paper\n\n"
                  f"[Service]\nType=oneshot\nExecStart={python} -P -m rafiq paper offer\n"),
        TIMER: ("[Unit]\nDescription=Rafiq: the morning paper, every day\n\n"
                f"[Timer]\nOnCalendar=*-*-* {at}:00\n\n[Install]\nWantedBy=timers.target\n"),
    }
