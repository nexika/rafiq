"""This computer: open apps, volume, media, how it is doing, lock it, power off. Linux with PipeWire (wpctl),
playerctl and a desktop that reads .desktop files (GNOME, KDE, ...).

Commands run as argument lists, never through a shell: no word the user says can become a command.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path

from .. import config, lang
from . import tool

APP_DIRS = ["/usr/share/applications", "/usr/local/share/applications", "~/.local/share/applications",
            "/var/lib/flatpak/exports/share/applications", "~/.local/share/flatpak/exports/share/applications",
            "/var/lib/snapd/desktop/applications"]


def _run(args: list[str], timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def desktop_entries(dirs: list[str] | None = None) -> list[dict]:
    """Every app the menu shows: its id, and the names it goes by in every language."""
    found, seen = [], set()
    for folder in dirs or APP_DIRS:
        for path in sorted(Path(folder).expanduser().glob("*.desktop")):
            if path.name in seen:
                continue
            parser = ConfigParser(interpolation=None, strict=False)
            parser.optionxform = str
            try:
                parser.read(path, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except Exception:
                continue
            if entry.get("NoDisplay", "").lower() == "true" or entry.get("Type", "Application") != "Application":
                continue
            names = [v for k, v in entry.items() if k.startswith(("Name", "GenericName"))]
            keywords = [w for k, v in entry.items() if k.startswith("Keywords") for w in v.split(";") if w]
            seen.add(path.name)
            found.append({"id": path.stem, "name": entry.get("Name", path.stem),
                          "names": [n.lower() for n in names], "keywords": [k.lower() for k in keywords]})
    return found


def find_app(query: str, entries: list[dict]) -> dict | None:
    """The best match for a spoken app name: exact name, then a name starting with it, then a name or
    keyword containing it."""
    q = query.lower().strip()
    if not q:
        return None
    for test in (lambda e: q in e["names"] or q == e["id"].lower(),
                 lambda e: any(n.startswith(q) for n in e["names"]),
                 lambda e: any(q in n for n in e["names"]) or q in e["id"].lower(),
                 lambda e: q in e["keywords"]):
        matches = [e for e in entries if test(e)]
        if matches:
            return min(matches, key=lambda e: len(e["name"]))
    return None


# ---------------------------------------------------------------------- app catalogue
# Games and launchers the menu does not know, from [apps] in config.toml and from apps.json (the ones
# added by voice). Each runs as an argument list: a Steam game, a Flatpak, or a command.

FLATPAK_ID = re.compile(r"[A-Za-z][\w-]*(\.[A-Za-z_][\w-]*){2,}")
STEAM_LINK = re.compile(r"steam://rungameid/(\d+)")


def launch_args(spec: dict) -> list[str]:
    """The command for one catalogue entry: {"steam": 49520}, {"flatpak": "com.x.Y"} or
    {"command": ["prog", "arg"]}. Raises ValueError for anything else."""
    kinds = [k for k in ("steam", "flatpak", "command") if k in spec]
    if len(kinds) != 1 or set(spec) - {"steam", "flatpak", "command"}:
        raise ValueError("an app needs exactly one of steam, flatpak or command")
    value = spec[kinds[0]]
    if kinds[0] == "steam":
        text = str(value).strip()
        link = STEAM_LINK.fullmatch(text)
        game = link.group(1) if link else text
        if isinstance(value, bool) or not game.isdigit():
            raise ValueError("steam is a game id like 49520 or a steam://rungameid/ link")
        return ["xdg-open", f"steam://rungameid/{game}"]
    if kinds[0] == "flatpak":
        if not isinstance(value, str) or not FLATPAK_ID.fullmatch(value):
            raise ValueError("flatpak is an app id like com.valvesoftware.Steam")
        return ["flatpak", "run", value]
    if not isinstance(value, list) or not value or not all(isinstance(a, str) and a and "\0" not in a
                                                            for a in value):
        raise ValueError("command is a list of words, the program first, never a shell line")
    return [os.path.expanduser(value[0]), *value[1:]]


def _added_path() -> Path:
    return config.home() / "apps.json"


def _added() -> dict:
    try:
        data = json.loads(_added_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def catalogue(cfg: dict) -> list[dict]:
    """The catalogue as app entries with their command; the config wins over a voice-added name, and
    an entry that does not make sense is left out."""
    configured = cfg.get("apps", {}) if isinstance(cfg.get("apps"), dict) else {}
    taken = {name.lower() for name in configured}
    merged = {**{k: v for k, v in _added().items() if k.lower() not in taken}, **configured}
    found = []
    for name, spec in merged.items():
        try:
            args = launch_args(spec) if isinstance(spec, dict) else None
        except ValueError:
            args = None
        if args:
            found.append({"id": name, "name": name, "names": [name.lower()], "keywords": [], "launch": args})
    return found


def _spawn(args: list[str]) -> None:
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _cfg(ctx) -> dict:
    return ctx.cfg if ctx is not None else config.load()


@tool("open_app", "Open an application or game by name, e.g. 'Firefox', 'Files', 'Calculator', 'Borderlands'.",
      {"name": {"type": "string", "description": "the app's name, in English where possible"}},
      required=("name",), level="act")
def open_app(name: str, ctx=None) -> str:
    # The catalogue comes first, so on a tie the name the user chose wins over the menu's.
    app = find_app(name, catalogue(_cfg(ctx)) + desktop_entries())
    if not app:
        return f"error: no installed app matches {name!r}"
    try:
        _spawn(app.get("launch") or ["gtk-launch", app["id"]])
    except OSError as exc:
        return f"error: could not start {app['name']}: {exc.strerror or exc}"
    return f"opened {app['name']}"


@tool("list_apps", "Names of the installed applications and games, to find the right one to open.",
      {"filter": {"type": "string", "description": "only names containing this"}},
      level="safe", expose=True, card="list")
def list_apps(filter: str = "", ctx=None) -> str:  # noqa: A002
    entries = catalogue(_cfg(ctx)) + desktop_entries()
    names = sorted({e["name"] for e in entries if filter.lower() in e["name"].lower()})
    return ", ".join(names[:80]) or "none"


def _new_spec(steam: str = "", flatpak: str = "", command: list | None = None) -> dict:
    spec: dict = {}
    if steam:
        spec["steam"] = steam
    if flatpak:
        spec["flatpak"] = flatpak
    if command:
        spec["command"] = command
    return spec


def _describe(args: dict) -> str:
    name = str(args.get("name", "")).strip() or "a new app"
    if args.get("steam"):
        game = STEAM_LINK.fullmatch(str(args["steam"]).strip())
        what = f"opens Steam game {game.group(1) if game else args['steam']}"
    elif args.get("flatpak"):
        what = f"runs the Flatpak {args['flatpak']}"
    elif isinstance(args.get("command"), list):
        what = "runs " + " ".join(str(a) for a in args["command"])
    else:
        what = "runs nothing yet"
    return f"add an app called {name} that {what}"


@tool("add_app", "Remember a new app or game to open by name: a Steam game id, a Flatpak id, or a command "
      "as a list of words (program first). Rafiq asks the user first.",
      {"name": {"type": "string", "description": "the name the user will say"},
       "steam": {"type": "string", "description": "a Steam game id like 49520, or a steam://rungameid/ link"},
       "flatpak": {"type": "string", "description": "a Flatpak app id like com.mojang.Minecraft"},
       "command": {"type": "array", "items": {"type": "string"},
                   "description": "the program and its arguments, one word each, never a shell line"}},
      required=("name",), confirm=_describe, level="critical")
def add_app(name: str, steam: str = "", flatpak: str = "", command: list | None = None, ctx=None) -> str:
    name = name.strip()
    if not name or len(name) > 60:
        return "error: the app needs a name of 1 to 60 characters"
    spec = _new_spec(steam, flatpak, command)
    try:
        args = launch_args(spec)
    except ValueError as exc:
        return f"error: {exc}"
    if "command" in spec and not shutil.which(args[0]):
        return f"error: no program {args[0]!r} on this computer"
    if name.lower() in {k.lower() for k in (_cfg(ctx).get("apps") or {})}:
        return f"error: {name} is set in config.toml; change it there"
    added = {k: v for k, v in _added().items() if k.lower() != name.lower()}
    added[name] = spec
    path = _added_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(added, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return f"added {name}"


# ---------------------------------------------------------------------- volume

SINK = "@DEFAULT_AUDIO_SINK@"


def parse_volume(text: str) -> tuple[int, bool]:
    m = re.search(r"Volume:\s*([\d.]+)", text)
    return (round(float(m.group(1)) * 100) if m else 0), "[MUTED]" in text


@tool("volume", "Get or change the speaker volume.",
      {"level": {"type": "integer", "description": "set the volume to this percent, 0 to 100"},
       "change": {"type": "integer", "description": "raise (positive) or lower (negative) by this many percent"},
       "mute": {"type": "boolean", "description": "true to mute, false to unmute"}}, level="act")
def volume(level: int | None = None, change: int | None = None, mute: bool | None = None) -> str:
    current, muted = parse_volume(_run(["wpctl", "get-volume", SINK]).stdout)
    if mute is not None:
        _run(["wpctl", "set-mute", SINK, "1" if mute else "0"])
        muted = bool(mute)
    target = None
    if level is not None:
        target = int(level)
    elif change is not None:
        target = current + int(change)
    if target is not None:
        target = max(0, min(100, target))
        _run(["wpctl", "set-volume", SINK, f"{target / 100:.2f}"])
        current = target
    return f"volume {current}%" + (", muted" if muted else "")


# ---------------------------------------------------------------------- media

# Any player that speaks MPRIS over D-Bus (Spotify, Firefox and Chromium videos, VLC, mpv...), through
# playerctl. What the user asks for, and the playerctl command for it.
MEDIA = {"play": "play", "pause": "pause", "toggle": "play-pause", "next": "next", "previous": "previous",
         "stop": "stop"}
MEDIA_DONE = {"play": "playing", "pause": "paused", "toggle": "toggled", "next": "skipped to the next on",
              "previous": "went back on", "stop": "stopped"}


def active_player(run=_run) -> tuple[str, str] | None:
    """The player to control and its status: one that is playing, else one that is paused, else the
    first one."""
    result = run(["playerctl", "--list-all"])
    players = [p.strip() for p in result.stdout.splitlines() if p.strip()] if result.returncode == 0 else []
    statuses = [(p, run(["playerctl", "--player", p, "status"]).stdout.strip()) for p in players]
    for wanted in ("Playing", "Paused"):
        for player, status in statuses:
            if status == wanted:
                return player, status
    return statuses[0] if statuses else None


@tool("media", "Control whatever is playing music or video (Spotify, a browser video, VLC...): play, pause, "
      "next, previous, or say what is playing.",
      {"action": {"type": "string", "enum": [*MEDIA, "status"],
                  "description": "toggle switches between play and pause; status says what is playing"}},
      required=("action",), level="act")
def media(action: str, run=_run) -> str:
    if action not in MEDIA and action != "status":
        return f"error: action is one of {', '.join([*MEDIA, 'status'])}"
    try:
        found = active_player(run)
        if not found:
            return "error: nothing is playing music or video"
        player, status = found
        if action == "status":
            title = run(["playerctl", "--player", player, "metadata", "--format",
                         "{{artist}} - {{title}}"]).stdout.strip().strip(" -")
            return f"{player} is {status.lower()}" + (f": {title}" if title else "")
        result = run(["playerctl", "--player", player, MEDIA[action]])
    except FileNotFoundError:
        return "error: playerctl is not installed"
    if result.returncode != 0:
        return f"error: {result.stderr.strip()[:200]}"
    return f"{MEDIA_DONE[action]} {player}"


# ---------------------------------------------------------------------- audio output

@dataclass
class Sink:
    id: int
    name: str
    default: bool


def parse_sinks(text: str) -> list[Sink]:
    """The audio outputs in `wpctl status`: the Sinks list of the Audio section."""
    sinks, section, in_sinks = [], "", False
    for line in text.splitlines():
        if line and not line[0].isspace() and line[0] not in "│├└":
            section = line.strip()
        tree = line.strip(" │")
        if tree.startswith(("├─", "└─")):
            in_sinks = section == "Audio" and tree.endswith("Sinks:")
            continue
        m = re.match(r"(\*?)\s*(\d+)\.\s+(.+?)\s*(\[vol:.*\])?$", tree)
        if in_sinks and m:
            sinks.append(Sink(int(m.group(2)), m.group(3), m.group(1) == "*"))
    return sinks


# What people call each kind of output in the six languages, and words in the names of such devices.
OUTPUT_WORDS = {
    "headphones": ["headphone", "headset", "earphone", "earbud", "casque", "écouteur", "ecouteur", "auricular",
                   "cascos", "kopfhörer", "kopfhoerer", "سماعات الرأس", "سماعة الرأس", "سماعات الأذن",
                   "سماعات", "سماعة", "هيدفون", "هيدسيت"],
    "speakers": ["speaker", "enceinte", "haut-parleur", "haut parleur", "altavoz", "altavoces", "bocina",
                 "lautsprecher", "boxen", "مكبرات الصوت", "مكبر الصوت", "سبيكر", "السماعات الخارجية"],
    "tv": ["tv", "television", "télé", "tele", "télévision", "televisión", "fernseher", "تلفزيون",
           "تليفزيون", "شاشة"],
}
SINK_HINTS = {"headphones": ["headphone", "headset", "buds", "airpods"],
              "speakers": ["speaker", "analog stereo"],
              "tv": ["hdmi", "displayport", "tv"]}


def _says(text: str, word: str) -> bool:
    """`word` in `text`, at the start of a word for Latin script (so "tv" is not inside "tvoje"); anywhere
    for Arabic, whose words take prefixes such as ال."""
    if any("\u0600" <= c <= "\u06ff" for c in word):
        return word in text
    return re.search(r"(?<!\w)" + re.escape(word), text) is not None


def output_kind(spoken: str) -> str | None:
    """headphones, speakers or tv from what the user called the output, in any of the six languages."""
    q = spoken.lower().strip()
    words = sorted(((w, kind) for kind, ws in OUTPUT_WORDS.items() for w in ws), key=lambda p: -len(p[0]))
    return next((kind for w, kind in words if _says(q, w)), None)


def find_sink(spoken: str, sinks: list[Sink], aliases: dict) -> Sink | None:
    """The output the user named: a name from the config ([audio.outputs], by its own name or by kind),
    then part of the device's name, then a device that looks like that kind."""
    q = spoken.lower().strip()
    if not q:
        return None
    kind = output_kind(q)

    def named(part: str) -> Sink | None:
        return next((s for s in sinks if part.lower() in s.name.lower()), None)

    for key, part in aliases.items():
        if (_says(q, key.lower()) or key.lower() == kind) and named(str(part)):
            return named(str(part))
    if named(q):
        return named(q)
    for hint in SINK_HINTS.get(kind, []):
        if named(hint):
            return named(hint)
    return None


def _outputs(sinks: list[Sink]) -> str:
    return ", ".join(s.name + (" (current)" if s.default else "") for s in sinks) or "none"


@tool("audio_output", "Switch where Rafiq's voice and the computer's sound play, e.g. 'headphones', 'speakers', "
      "'TV', or a name the user set; with no name, list the outputs.",
      {"name": {"type": "string", "description": "the output as the user said it, in their language"}}, level="act")
def audio_output(name: str = "", ctx=None) -> str:
    sinks = parse_sinks(_run(["wpctl", "status"]).stdout)
    if not name.strip():
        return "outputs: " + _outputs(sinks)
    aliases = ctx.cfg.get("audio", {}).get("outputs", {}) if ctx else {}
    sink = find_sink(name, sinks, aliases)
    if not sink:
        return f"error: no output matches {name!r}; outputs: {_outputs(sinks)}"
    result = _run(["wpctl", "set-default", str(sink.id)])
    return f"now playing on {sink.name}" if result.returncode == 0 else f"error: {result.stderr.strip()[:200]}"


# ---------------------------------------------------------------------- status

@tool("system_status", "How the computer is doing: CPU, memory, disk, battery, temperature, uptime, GPU.",
      level="safe", expose=True, card="facts")
def system_status() -> str:
    import psutil

    parts = [f"CPU {psutil.cpu_percent(interval=0.3):.0f}%"]
    mem = psutil.virtual_memory()
    parts.append(f"memory {mem.percent:.0f}% used ({mem.available / 2**30:.1f} GB free)")
    disk = psutil.disk_usage("/")
    parts.append(f"disk {disk.percent:.0f}% used ({disk.free / 2**30:.0f} GB free)")
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery:
        plug = "charging" if battery.power_plugged else "on battery"
        parts.append(f"battery {battery.percent:.0f}% ({plug})")
    temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
    hottest = max((t.current for group in temps.values() for t in group if t.current), default=None)
    if hottest:
        parts.append(f"hottest sensor {hottest:.0f}°C")
    up = int(time.time() - psutil.boot_time())
    parts.append(f"up {up // 3600} h {up % 3600 // 60} min")
    parts += [describe_gpu(g) for g in gpus()]
    return ", ".join(parts)


# ---------------------------------------------------------------------- graphics card

DRM = Path("/sys/class/drm")
AMD = "0x1002"


@dataclass
class Gpu:
    name: str
    load: float | None = None  # percent busy
    temperature: float | None = None  # °C
    memory_used: int | None = None  # bytes of video memory
    memory_total: int | None = None


def _read_number(path: Path) -> float | None:
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def amd_gpus(drm: Path = DRM) -> list[Gpu]:
    """AMD cards through the amdgpu driver's files: no extra tool needed. card1-eDP-1 and the like are
    screens, not cards, so only cardN is read."""
    found = []
    for card in sorted(drm.glob("card[0-9]*")):
        device = card / "device"
        try:
            if "-" in card.name or (device / "vendor").read_text().strip() != AMD:
                continue
        except OSError:
            continue
        temp = next((t for t in map(_read_number, sorted(device.glob("hwmon/*/temp1_input"))) if t is not None),
                    None)
        used, total = _read_number(device / "mem_info_vram_used"), _read_number(device / "mem_info_vram_total")
        found.append(Gpu("AMD GPU", load=_read_number(device / "gpu_busy_percent"),
                         temperature=temp / 1000 if temp is not None else None,
                         memory_used=int(used) if used is not None else None,
                         memory_total=int(total) if total is not None else None))
    return found


def nvidia_gpus(nvml=None) -> list[Gpu]:
    """NVIDIA cards through NVML (pip install nvidia-ml-py). Without it or without the driver: none."""
    if nvml is None:
        try:
            import pynvml as nvml
        except ImportError:
            return []
    try:
        nvml.nvmlInit()
    except nvml.NVMLError:
        return []
    found = []
    try:
        for i in range(nvml.nvmlDeviceGetCount()):
            handle = nvml.nvmlDeviceGetHandleByIndex(i)
            name = nvml.nvmlDeviceGetName(handle)
            memory = nvml.nvmlDeviceGetMemoryInfo(handle)
            found.append(Gpu(name.decode() if isinstance(name, bytes) else name,
                             load=nvml.nvmlDeviceGetUtilizationRates(handle).gpu,
                             temperature=nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU),
                             memory_used=memory.used, memory_total=memory.total))
    except nvml.NVMLError:
        pass  # keep the cards read so far
    finally:
        nvml.nvmlShutdown()
    return found


def gpus() -> list[Gpu]:
    return nvidia_gpus() + amd_gpus()


def describe_gpu(gpu: Gpu) -> str:
    figures = []
    if gpu.load is not None:
        figures.append(f"{gpu.load:.0f}% busy")
    if gpu.temperature is not None:
        figures.append(f"{gpu.temperature:.0f}°C")
    if gpu.memory_used is not None and gpu.memory_total:
        figures.append(f"video memory {gpu.memory_used / 2**30:.1f} of {gpu.memory_total / 2**30:.1f} GB used")
    return f"{gpu.name} " + ", ".join(figures) if figures else f"{gpu.name}: no figures"


@tool("gpu_status", "How the graphics card is doing: load, temperature, video memory.", level="safe")
def gpu_status() -> str:
    return ", ".join(describe_gpu(g) for g in gpus()) or "no graphics card figures available"


# ---------------------------------------------------------------------- lock and power

@tool("lock_screen", "Lock the screen.", level="act")
def lock_screen() -> str:
    session = os.environ.get("XDG_SESSION_ID")
    result = _run(["loginctl", "lock-session"] + ([session] if session else []))
    return "locked" if result.returncode == 0 else f"error: {result.stderr.strip()[:200]}"


POWER = {"shutdown": (["systemctl", "poweroff"], "shut down the computer"),
         "restart": (["systemctl", "reboot"], "restart the computer"),
         "suspend": (["systemctl", "suspend"], "put the computer to sleep")}
WARN_AT = 10            # seconds before a delayed shutdown, the last spoken warning


class DelayedShutdown:
    """One shutdown counting down. It lives only in Rafiq's memory: if Rafiq stops, the countdown
    stops with it, so starting Rafiq again never powers the computer off by surprise."""

    def __init__(self, run=None, spawn=None):
        self.run = run or (lambda args: _run(args))
        self.spawn = spawn or self._thread
        self.wait = None                     # tests replace the wait; None waits for a cancel
        self.cancelled = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.pending = False

    def _thread(self, fn) -> None:
        self.thread = threading.Thread(target=fn, name="rafiq-shutdown", daemon=True)
        self.thread.start()

    def start(self, seconds: int, command: list[str], on_warn, on_fail=None) -> bool:
        """Count down, call on_warn(seconds left) WARN_AT seconds before the end, then run the
        command. False when a countdown is already running."""
        with self.lock:
            if self.pending:
                return False
            self.pending = True
            cancelled = self.cancelled = threading.Event()

        def count():
            waits = [seconds - WARN_AT, WARN_AT] if seconds > WARN_AT else [seconds]
            for i, step in enumerate(waits):
                if (self.wait or cancelled.wait)(step):
                    return
                if i < len(waits) - 1:
                    on_warn(WARN_AT)
            with self.lock:
                if cancelled.is_set():
                    return
                self.pending = False
            result = self.run(command)
            if on_fail and result is not None and result.returncode != 0:
                on_fail()

        self.spawn(count)
        return True

    def cancel(self) -> bool:
        with self.lock:
            was, self.pending = self.pending, False
            self.cancelled.set()
        return was


SHUTDOWN = DelayedShutdown()


@tool("power", "Shut down, restart or suspend the computer. Rafiq asks the user first. A shutdown "
               "waits a little (30 seconds by default) and can be stopped with cancel_shutdown.",
      {"action": {"type": "string", "enum": list(POWER)}}, required=("action",),
      confirm=lambda args: POWER.get(args.get("action", ""), (None, "change the power state"))[1], level="critical")
def power(action: str, ctx=None) -> str:
    if action not in POWER:
        return f"error: action is one of {', '.join(POWER)}"
    delay = int(ctx.cfg.get("pc", {}).get("shutdown_delay", 30)) if ctx else 0
    if action == "shutdown" and delay > 0:
        tag = ctx.tag
        say = getattr(ctx, "say", None) or (lambda text, tag: None)
        started = SHUTDOWN.start(
            delay, POWER["shutdown"][0],
            on_warn=lambda left: say(lang.phrase("shutdown_soon", tag, seconds=str(left)), tag),
            on_fail=lambda: say(lang.phrase("shutdown_failed", tag), tag))
        if not started:
            return "a shutdown is already counting down; cancel_shutdown stops it"
        return f"shutting down in {delay} seconds; the user can say 'cancel the shutdown' to stop it"
    result = _run(POWER[action][0])
    return "done" if result.returncode == 0 else f"error: {result.stderr.strip()[:200]}"


@tool("cancel_shutdown", "Stop a shutdown that is counting down (\"cancel the shutdown\", \"annule "
                         "l'extinction\", \"ألغِ إيقاف التشغيل\", \"إلغي القفل\").", level="act")
def cancel_shutdown() -> str:
    return "the shutdown is cancelled" if SHUTDOWN.cancel() else "no shutdown is pending"
