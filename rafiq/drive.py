"""The operator's loop (#18): a model works the desktop from screenshots, and Rafiq runs each step it asks for.

A driver (rafiq/drivers.py: Claude, OpenAI or a local model) gets the task and a screenshot, and answers with
actions: click, type, press keys, scroll. Rafiq runs them in order on one input session for the whole run
(rafiq/pointer.py), then sends the results and a new screenshot back, until the driver says it is done.

Rafiq keeps its own limits, whatever the model says:
- At most MAX_ACTIONS input actions and MAX_SECONDS seconds per run; screenshots, zooms and waits are free.
- Stop (the widget, the dashboard) or Escape (rafiq/shortcuts.py) ends the run before the next action.
- guard() refuses the plain ways to harm: the Super key (the Activities search starts any app), system key combos
  (a terminal, a text console, the run dialog, logging out), pasting, typing a terminal's name, and typing that
  looks like a command, judged on everything typed in the run. A refused step ends the run. It is a backstop: a
  model that obeys a page could still click its way to harm, so the model's instructions, the yes before a run,
  the limits and Stop and Escape matter as much.
- Escape is kept for the user: the model can't press it (it would end its own run).
- While a run goes, the panel and the widget take no requests (RUNNING), so the model can't type one in.
- The driver hands a step back to the user (need_user) for passwords, purchases, sending, deleting, installing
  or system commands; the run ends there and Rafiq says what is left.
What the model writes about the screen is data from a model that read the screen, never instructions to Rafiq.
"""

from __future__ import annotations

import io
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from . import pointer

MAX_ACTIONS = 12           # decided in #18, after Jarvis's operator
MAX_SECONDS = 90
MAX_WAIT = 3               # seconds; a model asking to wait longer gets this much
MAX_HOLD = 3               # seconds a key may be held
MAX_TYPED = 500            # characters in one `type`
NOT_EXECUTED = "Not executed: an earlier computer action in this turn failed."
INPUT = frozenset({"click", "drag", "move", "mouse_down", "mouse_up", "scroll", "type", "key", "hold"})


class DriverError(Exception):
    """The model couldn't be asked (no network, a refused key): the run ends with what went wrong."""


@dataclass(frozen=True)
class Action:
    """One step, in pixels of the screenshot the driver was given."""
    kind: str                       # click, drag, move, mouse_down, mouse_up, scroll, type, key, hold, wait,
    x: float | None = None          # screenshot, zoom, cursor; anything else is unknown and fails
    y: float | None = None
    button: str = "left"
    count: int = 1
    mods: tuple = ()
    path: tuple = ()
    dx: int = 0
    dy: int = 0
    text: str = ""
    repeat: int = 1
    seconds: float = 0.0
    region: tuple = ()


@dataclass
class Turn:
    actions: list[Action]
    done: str | None = None          # the driver's last words, when it has finished
    need_user: str | None = None     # what the user must do; the run ends


@dataclass
class Shot:
    png: bytes
    width: int
    height: int


@dataclass
class Result:
    ok: bool
    text: str = "OK"
    image: Shot | None = None


@dataclass
class Outcome:
    ended: str                       # done, need_user, limit, time, stopped, refused, error
    text: str
    steps: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------- guards

_SYSTEM_COMBOS = {
    frozenset({"ctrl", "alt", "delete"}): "Ctrl+Alt+Delete logs out or restarts",
    frozenset({"ctrl", "alt", "backspace"}): "Ctrl+Alt+Backspace ends the desktop session",
    frozenset({"ctrl", "alt", "t"}): "Ctrl+Alt+T opens a terminal",
    frozenset({"alt", "f2"}): "Alt+F2 runs a command",
    frozenset({"alt", "sysrq"}): "SysRq talks to the kernel",
    frozenset({"ctrl", "v"}): "pasting: what a page put on the clipboard isn't checked",
    frozenset({"ctrl", "shift", "v"}): "pasting: what a page put on the clipboard isn't checked",
    frozenset({"shift", "insert"}): "pasting: what a page put on the clipboard isn't checked",
}
CLICK_MODS = frozenset({"ctrl", "shift", "alt"})
# A command where one would start (the beginning of what was typed, or after ; & | ` ( or a new line), and the
# shell's own syntax. Words that are also ordinary words (su, python, kill, find, cat) count only with a flag or a
# path after them, so "Gracias por su ayuda" or a "python tutorial" search pass.
_START = r"(?:^|[\n;&|`(])\s*"
_COMMANDS = (r"sudo|pkexec|doas|su\s+-|chmod|chown|chattr|mkfs\S*|dd\s+if=|rm|rmdir|systemctl|shutdown|reboot|"
             r"poweroff|curl|wget\d*|scp|ssh|sftp|rsync|ncat|nc\s+-|crontab|gsettings|dconf|flatpak|apt(?:-get)?|dnf|"
             r"yum|pacman|snap|pip3?|npm|tee|gio|xdg-open|eval|exec")
_WORDS_WITH_ARGS = (r"(?:sh|bash|zsh|fish|python[\d.]*|perl|ruby|node|kill(?:all)?|find|cat|cp|mv|ln|echo)"
                    r"\s+[-~/.$'\"]")
_SHELL = re.compile(
    rf"{_START}(?:{_COMMANDS})(?=\s|$)|{_START}{_WORDS_WITH_ARGS}"
    r"|(?:^|\s)(?:/usr)?/s?bin/\w|(?:^|\s)\\\w|\$\(|`|&&|\|\||\|\s*(?:sh|bash|zsh)\b|>\s*[/~]", re.IGNORECASE)
# Windows (#242): commands of cmd and PowerShell, with the same rule for words that are also ordinary words (del,
# start, format, reg, rd only with a /flag, a -flag, a drive or reg's own verbs after them), cmd's %VARIABLES%, the
# registry's paths and the system folder.
_WIN_COMMANDS = (r"diskpart|bcdedit|takeown|i?cacls|schtasks|runas|msiexec|winget|choco|scoop|wmic|certutil|"
                 r"bitsadmin|mshta|rundll32|regsvr32|[cw]script|regedit|iwr|iex|irm|"
                 r"(?:invoke|start|stop|restart|remove|set|new)-[a-z]+")
_WIN_WITH_ARGS = r"(?:del|erase|rd|rmdir|format|start|reg|sc|attrib|copy|move|xcopy|robocopy|net)\s+(?:/|-|[a-z]:)"
_WIN_SHELL = re.compile(rf"{_START}(?:{_WIN_COMMANDS})(?=\s|$)|{_START}{_WIN_WITH_ARGS}"
                        rf"|{_START}reg\s+(?:add|delete|import|load|unload|save|restore|copy|export)\b"
                        r"|%\w+%|\b[a-z]:\\windows\\|\bHK(?:CU|LM|CR|CC|U|EY_\w+)\\", re.IGNORECASE)
# Typing a terminal's name means opening one, from the Activities search, an app launcher or the Start menu.
_TERMINALS = re.compile(r"\b(?:terminal|console|konsole|ptyxis|xterm|kitty|alacritty|tilix|guake|wezterm|kgx|"
                        r"powershell|pwsh|cmd(?:\.exe)?(?!\s+key)|command\s+prompt|conhost)\b"
                        r"|طرفية|الطرفية", re.IGNORECASE)


def _combo(text: str) -> frozenset[str]:
    canonical = {"control": "ctrl", "option": "alt", "meta": "super", "cmd": "super", "win": "super", "del": "delete",
                 "esc": "escape"}
    return frozenset(canonical.get(p, p) for p in (w.strip().lower() for w in text.split("+")) if p)


def _number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def guard(action: Action, typed: str = "") -> str | None:
    """Why Rafiq refuses this step, whatever the model said; None when it may run. `typed` is what was typed
    before in this run, so a command typed in pieces is seen whole.

    This is a backstop, not the main protection: it catches the plain ways to a terminal, a console or the
    clipboard, but a model that follows a page's orders could still click its way to harm. The model's own
    instructions, the yes before a run, the limits and the Stop button and Escape do the rest."""
    if action.kind in ("key", "hold"):
        keys = _combo(action.text)
        if "super" in keys:
            return "the Super key opens the Activities search, which starts any app"
        for combo, why in _SYSTEM_COMBOS.items():
            if combo <= keys:
                return why
        if {"ctrl", "alt"} <= keys and any(re.fullmatch(r"f([1-9]|1[0-2])", k) for k in keys):
            return "Ctrl+Alt+F-keys switch to a text console"
        if action.kind == "hold" and action.seconds > MAX_HOLD:
            return f"holding a key for more than {MAX_HOLD} seconds"
    if action.kind in ("click", "drag", "scroll"):
        if action.kind == "click" and action.button == "middle":
            return "a middle click pastes what a page may have selected"
        if not set(action.mods) <= CLICK_MODS:
            return "only Ctrl, Shift and Alt may be held with the mouse"
        points = [(action.x, action.y)] if action.kind != "drag" else list(action.path)
        if any(not (v is None or _number(v)) for p in points for v in p) or (
                action.kind == "drag" and any(v is None for p in points for v in p)):
            return "a point that isn't a number"
    if action.kind == "type":
        if len(action.text) > MAX_TYPED:
            return f"typing more than {MAX_TYPED} characters at once"
        if _TERMINALS.search(action.text):
            return "typing a terminal's name, which opens one"
        if _SHELL.search(typed + action.text) or _WIN_SHELL.search(typed + action.text):
            return "typing what looks like a command for a terminal"
    return None


# ---------------------------------------------------------------------- screenshots

def _fit(png: bytes, max_side: int) -> tuple[Shot, tuple[int, int]]:
    """The screenshot as the driver sees it (long side at most max_side), and the real screen's size."""
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    real = image.size
    scale = min(1.0, max_side / max(real))
    if scale < 1.0:
        image = image.resize((round(real[0] * scale), round(real[1] * scale)), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return Shot(out.getvalue(), image.width, image.height), real


def _zoom(png: bytes, region: tuple, scale: float, max_side: int) -> Shot:
    """A full-resolution crop of the region (in the driver's pixels), fitted like a screenshot."""
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    x0, y0, x1, y1 = (round(v * scale) for v in region)
    x0, x1 = sorted((max(0, min(image.width, x0)), max(0, min(image.width, x1))))
    y0, y1 = sorted((max(0, min(image.height, y0)), max(0, min(image.height, y1))))
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise pointer.PointerError("that region is empty")
    crop = image.crop((x0, y0, x1, y1))
    out = io.BytesIO()
    crop.save(out, "PNG")
    return _fit(out.getvalue(), max_side)[0]


# ---------------------------------------------------------------------- the run

def _summary(action: Action) -> str:
    if action.kind == "click":
        what = {1: "clicked", 2: "double-clicked", 3: "triple-clicked"}.get(action.count, "clicked")
        return f"{action.button + ' ' if action.button != 'left' else ''}{what}".strip()
    if action.kind == "type":
        return f"typed {len(action.text)} characters"         # never the text: it can be the user's own
    if action.kind in ("key", "hold"):
        return f"pressed {action.text}"
    return {"drag": "dragged", "move": "moved the pointer", "scroll": "scrolled", "mouse_down": "pressed the button",
            "mouse_up": "released the button"}.get(action.kind, action.kind)


RUNNING = threading.Event()   # set during a run: the panel and the widget take no requests then
SETTLE = 0.5                # seconds for the screen to change after the last action, before the next screenshot


class _Run:
    def __init__(self, driver, capture, remote, sleep, max_side):
        self.driver, self.capture, self.remote, self.sleep = driver, capture, remote, sleep
        self.max_side, self.scale, self.last_png = max_side, 1.0, b""
        self.pointer_at: tuple[float, float] | None = None
        self.typed = ""             # everything typed in the run, for guard()
        self.button_down = False    # a mouse_down left pressed is released when the run ends

    def shot(self) -> Shot:
        self.last_png = self.capture()
        shot, real = _fit(self.last_png, self.max_side)
        self.scale = real[0] / shot.width
        return shot

    def _point(self, action: Action) -> tuple[float, float]:
        if action.x is not None and action.y is not None:
            self.pointer_at = (float(action.x), float(action.y))
        elif self.pointer_at is None:
            raise pointer.PointerError("no point given and the pointer hasn't moved yet")
        x, y = self.pointer_at
        return x * self.scale, y * self.scale

    def do(self, action: Action) -> Result:
        r, kind = self.remote, action.kind
        if kind == "screenshot":
            return Result(True, image=self.shot())
        if kind == "zoom":
            return Result(True, image=_zoom(self.last_png or self.capture(), action.region, self.scale, self.max_side))
        if kind == "cursor":
            x, y = self.pointer_at or (0, 0)
            return Result(True, text=f"{round(x)}, {round(y)}")
        if kind in ("key", "hold") and "escape" in _combo(action.text):
            # Escape is the user's key to stop the run (bound for it): pressed by the model it would stop the run.
            raise pointer.PointerError("Escape is kept for the user to stop the run; close it another way, such as "
                                       "clicking outside it")
        note = None
        if kind == "wait":
            self.sleep(min(float(action.seconds or 1), MAX_WAIT))
        elif kind == "click":
            r.click(*self._point(action), button=action.button, count=action.count, mods=action.mods)
        elif kind == "move":
            r.move(*self._point(action))
        elif kind == "drag":
            path = [(x * self.scale, y * self.scale) for x, y in action.path]
            if len(path) < 2:
                raise pointer.PointerError("a drag needs a start and an end")
            r.drag(path, mods=action.mods)
            self.pointer_at = action.path[-1]
        elif kind in ("mouse_down", "mouse_up"):
            r.mouse(kind == "mouse_down")
            self.button_down = kind == "mouse_down"
        elif kind == "scroll":
            r.scroll(*self._point(action), dx=action.dx, dy=action.dy)
        elif kind == "type":
            note = r.type(action.text)
            self.typed += action.text
        elif kind == "key":
            r.key(action.text, repeat=max(1, min(int(action.repeat), 20)))
        elif kind == "hold":
            r.hold(action.text, float(action.seconds), sleep=self.sleep)
        else:
            raise pointer.PointerError(f"{kind} isn't something Rafiq can do")
        return Result(True, text=note or "OK")

    def release(self) -> None:
        """Nothing stays pressed for the user after the run, however it ended."""
        if self.button_down and self.remote is not None:
            try:
                self.remote.mouse(False)
            except (pointer.PointerError, OSError):
                pass
            self.button_down = False


def _ask(call, *args) -> Turn:
    """A driver's turn; an answer it couldn't read is the model's failure, not a crash of the run."""
    try:
        return call(*args)
    except DriverError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
        raise DriverError(f"the model's answer couldn't be read ({type(exc).__name__})") from exc


def run(task: str, driver, capture: Callable[[], bytes], remote: Callable, stopped: Callable[[], bool],
        clock: Callable[[], float], on_step: Callable[[str], None] | None = None, sleep=None) -> Outcome:
    """One operator run. `remote(width, height)` gives the input session for a screen of that size."""
    import time

    sleep = sleep or time.sleep
    steps: list[str] = []
    state = _Run(driver, capture, None, sleep, driver.max_side)

    def end(ended: str, text: str) -> Outcome:
        return Outcome(ended, text, steps)

    RUNNING.set()
    try:
        first = state.shot()
        real = (round(first.width * state.scale), round(first.height * state.scale))
        with remote(*real) as state.remote:
            try:
                # From here: the first time, GNOME's "allow remote interaction?" took its time before this.
                deadline = clock() + MAX_SECONDS
                turn = _ask(driver.begin, task, first)
                count = 0
                while True:
                    if turn.need_user:
                        return end("need_user", f"Stopped for the user: {turn.need_user}")
                    if not turn.actions:
                        return end("done", turn.done or "Done.")
                    results: list[Result] = []
                    failed = moved = False
                    for action in turn.actions:
                        if failed:
                            results.append(Result(False, NOT_EXECUTED))
                            continue
                        if stopped():
                            return end("stopped", "Stopped: the user pressed Stop or Escape.")
                        if clock() > deadline:
                            return end("time", f"Stopped after {MAX_SECONDS} seconds, the most one request may take.")
                        why = guard(action, state.typed)
                        if why:
                            return end("refused", f"Refused: {why}. Rafiq doesn't do that for a model; nothing "
                                                  "more was done.")
                        if action.kind in INPUT:
                            if count >= MAX_ACTIONS:
                                return end("limit", f"Stopped after {MAX_ACTIONS} actions, the most for one request.")
                            count += 1
                        try:
                            results.append(state.do(action))
                        except (pointer.PointerError, OSError, ValueError, KeyError, TypeError) as exc:
                            results.append(Result(False, f"error: {exc}"))
                            failed = True
                            continue
                        if action.kind in INPUT:
                            moved = True
                            steps.append(_summary(action))
                            if on_step:
                                on_step(steps[-1])
                    if clock() > deadline:
                        return end("time", f"Stopped after {MAX_SECONDS} seconds, the most one request may take.")
                    if moved and driver.wants_shot_each_turn:
                        sleep(SETTLE)
                    turn = _ask(driver.answer, results, state.shot() if driver.wants_shot_each_turn else None)
            finally:
                state.release()
    except (pointer.PointerError, DriverError) as exc:
        return end("error", f"error: {exc}")
    finally:
        RUNNING.clear()
