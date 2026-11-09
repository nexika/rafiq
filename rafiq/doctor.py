"""`rafiq doctor`: is each integration ready, and which local models fit this machine.

Every outside thing (the network, programs, Python modules, the GPU) goes through a Probe, so the
checks run in tests without any of them. Keys are sent where they belong and never printed.
"""

from __future__ import annotations

import importlib.util
import shutil
import socket
import subprocess
from dataclasses import dataclass

from . import config

OLLAMA_START = "start it with: ollama serve (install: https://ollama.com)"

# Local brains Rafiq is tuned for, biggest first, with the memory each takes once loaded (GB, about).
OLLAMA_MODELS = [("qwen2.5:14b", 9.0), ("qwen2.5:7b", 4.7), ("qwen2.5:3b", 1.9), ("qwen2.5:1.5b", 1.0)]
WHISPER_ORDER = ["tiny", "base", "small", "medium"]


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""
    fix: str = ""
    optional: bool = False   # not set up is fine: Rafiq works without it


@dataclass
class Machine:
    ram_gb: float
    gpu: str = ""
    vram_gb: float = 0.0


@dataclass
class Advice:
    ollama_model: str
    whisper_model: str
    reason: str


class Probe:
    """The real machine. Tests pass a fake one."""

    def get(self, url: str, **kwargs):
        import httpx

        return httpx.get(url, timeout=5, **kwargs)

    def which(self, program: str) -> str | None:
        return shutil.which(program)

    def has_module(self, name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    def run(self, args: list[str], timeout: float = 5) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)

    def input_devices(self) -> list[str]:
        """The names of the audio inputs (the microphone, a BlackHole loopback on macOS)."""
        try:
            import sounddevice

            return [str(d.get("name", "")) for d in sounddevice.query_devices() if d.get("max_input_channels", 0)]
        except Exception:
            return []


# ---------------------------------------------------------------------- integrations

def _ollama(cfg: dict, probe: Probe) -> Check:
    import httpx

    brain = cfg["brain"]
    model, url = brain["ollama_model"], brain["ollama_url"]
    try:
        resp = probe.get(f"{url}/api/tags")
        resp.raise_for_status()
        names = [m["name"] for m in resp.json().get("models", [])]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return Check("Ollama", False, f"not reachable at {url}", OLLAMA_START)
    if model in names:
        return Check("Ollama", True, f"{model} installed")
    return Check("Ollama", False, f"{model} missing", f"ollama pull {model}")


def _claude(cfg: dict, probe: Probe) -> Check:
    if not probe.has_module("anthropic"):
        return Check("Claude", False, "the anthropic library is missing", "pip install anthropic")
    if not cfg.get("anthropic_api_key"):
        return Check("Claude", False, "no API key", "set ANTHROPIC_API_KEY to use Claude", optional=True)
    return Check("Claude", True, cfg["brain"]["claude_model"])


def _azure(cfg: dict, probe: Probe) -> Check:
    import httpx

    from .tts import Speaker

    speaker = Speaker(cfg)
    if not (speaker.key and speaker.region):
        return Check("Azure voices", False, "not set up (Egyptian Arabic voice)",
                     "set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION", optional=True)
    try:
        resp = probe.get(f"https://{speaker.region}.tts.speech.microsoft.com/cognitiveservices/voices/list",
                         headers={"Ocp-Apim-Subscription-Key": speaker.key})
    except httpx.HTTPError as exc:
        return Check("Azure voices", False, f"{speaker.region} not reachable ({type(exc).__name__})")
    if resp.status_code in (401, 403):
        return Check("Azure voices", False, f"key refused ({resp.status_code})",
                     "check AZURE_SPEECH_KEY and that it belongs to AZURE_SPEECH_REGION")
    if resp.status_code != 200:
        return Check("Azure voices", False, f"{speaker.region} answered {resp.status_code}")
    return Check("Azure voices", True, speaker.region)


def _google(cfg: dict, probe: Probe) -> Check:
    if not (probe.has_module("googleapiclient") and probe.has_module("google_auth_oauthlib")):
        return Check("Google", False, "calendar and email libraries missing", "pip install 'rafiq[google]'",
                     optional=True)
    if not (config.home() / "google" / "token.json").is_file():
        return Check("Google", False, "not connected", "rafiq google-setup", optional=True)
    if not cfg["google"].get("enabled", False):
        return Check("Google", False, "connected but off",
                     f"set [google] enabled = true in {config.home() / 'config.toml'}", optional=True)
    return Check("Google", True, "calendar and email")


def _weather(cfg: dict, probe: Probe) -> Check:
    import httpx

    city = cfg.get("place", {}).get("city", "")
    try:
        resp = probe.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": city or "London", "count": 1, "format": "json"})
        resp.raise_for_status()
        found = bool(resp.json().get("results"))
    except (httpx.HTTPError, ValueError, AttributeError):
        return Check("Weather", False, "Open-Meteo not reachable", "check the internet connection")
    if not city:
        return Check("Weather", True, "Open-Meteo answers", "set [place] city for \"what's the weather?\"")
    if not found:
        return Check("Weather", False, f"no place called {city!r}", "fix [place] city in config.toml")
    return Check("Weather", True, city)


def _modules(label: str, names: list[str], probe: Probe) -> Check:
    missing = [n for n in names if not probe.has_module(n)]
    if missing:
        return Check(label, False, f"missing {', '.join(missing)}", f"pip install {' '.join(missing)}")
    return Check(label, True)


def _programs(label: str, names: list[str], probe: Probe, fix: str) -> Check:
    missing = [n for n in names if not probe.which(n)]
    if missing:
        return Check(label, False, f"missing {', '.join(missing)}", fix)
    return Check(label, True, ", ".join(names))


def _songs(cfg: dict, probe: Probe) -> Check:
    """Never sends a request: each one counts against the AudD plan."""
    if not cfg.get("songs", {}).get("audd_token"):
        return Check("Song recognition", False, "no AudD token", "set AUDD_API_TOKEN (https://audd.io)",
                     optional=True)
    from . import desktop

    if desktop.system() == "windows":
        if not probe.which("ffmpeg"):
            return Check("Song recognition", False, "room only, missing ffmpeg", "winget install -e --id Gyan.FFmpeg",
                         optional=True)
        return Check("Song recognition", True, "AudD, from the microphone (the computer's own sound is not on "
                     "Windows yet)")
    missing = [n for n in (("ffmpeg",) if desktop.system() == "mac" else ("parec", "ffmpeg")) if not probe.which(n)]
    if missing:
        return Check("Song recognition", False, f"room only, missing {', '.join(missing)}",
                     ("brew install ffmpeg (BlackHole for the Mac's own sound: see above)" if desktop.system() == "mac"
                      else "install pulseaudio-utils (parec) for the computer's sound and ffmpeg for files"),
                     optional=True)
    return Check("Song recognition", True, "AudD")


def _dmx(cfg: dict) -> Check:
    """Only the settings: a DMX frame sent to check would change the lights."""
    from .tools import dmx

    try:
        found = dmx.lights(cfg)
        if not found:
            return Check("Video lights (DMX)", False, "none set up", "add [[dmx.lights]] (see config.example.toml)",
                         optional=True)
        host = dmx.node(cfg)
    except ValueError as exc:
        return Check("Video lights (DMX)", False, str(exc), "fix [dmx] in the config")
    return Check("Video lights (DMX)", True, f"{len(found)} lights on {host}")


def _music(cfg: dict, probe: Probe) -> Check:
    """The music service named in [music]; for Deezer one search in the public catalogue, which needs no key."""
    import httpx

    from .tools import music_service

    service = music_service(cfg)
    if not service:
        return Check("Music", False, "no service named", 'set [music] service = "deezer" or "spotify"',
                     optional=True)
    if service == "spotify":
        ready = cfg.get("spotify", {}).get("enabled") and (config.home() / "spotify" / "token.json").is_file()
        return Check("Music", bool(ready), "Spotify",
                     "" if ready else "rafiq spotify-setup, then [spotify] enabled = true")
    if service != "deezer":
        return Check("Music", False, f"unknown service {service!r}", "set [music] service to spotify or deezer")
    try:
        resp = probe.get("https://api.deezer.com/search/track", params={"q": "rafiq", "limit": 1},
                         follow_redirects=False)
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return Check("Music", False, "Deezer's catalogue is not reachable", "check the network")
    if resp.status_code != 200 or not isinstance(data, dict) or "error" in data:
        error = data.get("error") if isinstance(data, dict) else None
        why = str(error.get("message", "")) if isinstance(error, dict) else f"status {resp.status_code}"
        return Check("Music", False, f"Deezer's catalogue refused: {why[:120]}", "try again in a minute")
    return Check("Music", True, "Deezer: search and open links; playlists aren't offered (Deezer isn't creating "
                 "new developer apps)")


def _not_linux() -> bool:
    from . import desktop

    return desktop.system() != "linux"


def _operator(cfg: dict) -> Check:
    """Which model would drive "take control and ..." (#18); nothing is sent to check."""
    from . import drive, drivers

    which = drivers.pick(cfg)
    if which is None:
        return Check("Operator (take control)", False, "no model can drive it",
                     "a Claude key, rafiq chatgpt-login (or an OpenAI key), or [operator] ollama_model", optional=True)
    where = drivers.where(cfg, which)
    return Check("Operator (take control)", True, f"{where[0].upper()}{where[1:]}; at most {drive.MAX_ACTIONS} "
                 f"steps and {drive.MAX_SECONDS} s, Escape or Stop ends it")


def _phone_page(cfg: dict) -> Check:
    """Is the phone page up where `tailscale serve` reaches it, and does Tailscale send the phone there (#238)? It
    connects to check and sends nothing."""
    from . import mobile, sock

    settings = cfg["mobile"]
    port, listen = int(settings["port"]), settings.get("listen", "socket")
    if not settings.get("enabled"):
        return Check("Phone page", True, "off ([mobile] enabled = false)", optional=True)
    if not sock.unix_available():
        return Check("Phone page", True, "not on Windows yet ([mobile] enabled is ignored)", optional=True)
    if listen not in ("socket", "port"):
        return Check("Phone page", False, f'[mobile] listen = {listen!r}', 'set it to "socket" (or "port")')
    move = f"tailscale serve --https=443 off, then sudo tailscale serve --bg unix:{mobile.socket_path()}"
    if listen == "port":
        return _phone_port(port, move)
    targets = mobile.serve_targets()
    if targets is not None and mobile.on_port(targets, port):
        return Check("Phone page", False, f"`tailscale serve` still sends the phone to 127.0.0.1:{port}, which "
                     "another account could take", move)
    where = mobile.socket_path()
    if not sock.ours(where):
        return Check("Phone page", False, f"no socket of yours at {where}", "start it with: rafiq run")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(2)
    try:
        probe.connect(str(where))
    except OSError:
        return Check("Phone page", False, "its socket is there, but nothing answers", "start it with: rafiq run")
    finally:
        probe.close()
    if targets is None:
        return Check("Phone page", False, f"answers on its socket, {where}, but Tailscale couldn't be asked where it "
                     "sends the phone, so `rafiq run` won't start", "check that `tailscale serve status --json` works "
                     "for you (tailscale installed, on PATH and running)")
    if f"unix:{where}" not in targets:
        return Check("Phone page", False, f"answers on its socket, {where}, but `tailscale serve` doesn't send the "
                     "phone there", f"sudo tailscale serve --bg unix:{where}")
    return Check("Phone page", True, f"answers on its socket, {where}")


def _phone_port(port: int, move: str) -> Check:
    """`listen = "port"`: whose program holds it, where /proc can say. Never ok without a note: while Rafiq is
    stopped another account could take the port and collect the phones' tokens."""
    import os

    from . import mobile, panel

    warning = "; while Rafiq is stopped another account could take it and collect your phones' tokens"
    probe = socket.socket()
    probe.settimeout(2)
    try:
        probe.connect(("127.0.0.1", port))
        owner = panel.listener_owner(port) if mobile.owner_known() else None
    except OSError:
        return Check("Phone page", False, f"nothing answers on 127.0.0.1:{port}{warning}", "start it with: rafiq run")
    finally:
        probe.close()
    if owner is not None and owner != os.getuid():
        return Check("Phone page", False, f"another account's program holds 127.0.0.1:{port}: your phone may be "
                     "talking to it", "tailscale serve --https=443 off now; then rafiq mobile unpair and pair each "
                     "phone again")
    return Check("Phone page", False, f"on 127.0.0.1:{port}{warning}", f'set [mobile] listen = "socket": {move}',
                 optional=True)

def _follow_ups(cfg: dict) -> Check:
    """How often a follow-up was asked about or refused only because an earlier request read something (#235)."""
    from . import brain

    if not cfg["brain"].get("carry_marks", True):
        return Check("Follow-up checks", True, "off: only the request that read an email, the screen or a page is "
                     "checked ([brain] carry_marks = false)", optional=True)
    count = brain.carried_this_week()
    return Check("Follow-up checks", True, f"{count} this week: follow-ups asked about or refused because an "
                 "earlier request read an email, the screen or a page")


def _pointer(probe: Probe) -> Check:
    """How click_on_screen clicks here; nothing is clicked to check."""
    import os

    from . import pointer

    how = pointer.method(os.environ, probe.which)
    if how == "portal":
        if not probe.has_module("jeepney"):
            return Check("Click on screen", False, "needs jeepney", "pip install jeepney", optional=True)
        return Check("Click on screen", True, "RemoteDesktop portal, where the desktop offers it (GNOME, KDE); "
                     "GNOME asks the first time")
    if how == "xdotool":
        return Check("Click on screen", True, "xdotool")
    return Check("Click on screen", False, "no way to click", "install xdotool (X11)", optional=True)


def _browser(cfg: dict, probe: Probe) -> Check:
    """Only what is installed and whether it runs: nothing is asked of the browser."""
    from . import browser

    if not cfg.get("browser", {}).get("enabled"):
        return Check("Browser tabs", False, "off", "rafiq browser, then [browser] enabled = true", optional=True)
    try:
        engine, program = browser.find(cfg, probe.which)
    except browser.BrowserError as exc:
        return Check("Browser tabs", False, str(exc), "set [browser] engine to auto, chrome or firefox")
    if not program:
        return Check("Browser tabs", False, *browser.missing(engine))
    return Check("Browser tabs", True, f"{program}, {'running' if browser.running(engine) else 'not running'}")


def integrations(cfg: dict, probe: Probe | None = None) -> list[Check]:
    """One check per brain, voice service and skill that needs something outside Rafiq."""
    probe = probe or Probe()
    return [
        _claude(cfg, probe),
        _ollama(cfg, probe),
        _azure(cfg, probe),
        _google(cfg, probe),
        _weather(cfg, probe),
        _songs(cfg, probe),
        _dmx(cfg),
        _browser(cfg, probe),
        *([] if _not_linux() else [_pointer(probe), _operator(cfg)]),   # neither is offered off Linux yet
        _music(cfg, probe),
        _follow_ups(cfg),
        _phone_page(cfg),
        _modules("Web search", ["ddgs", "trafilatura"], probe),
        *_desktop(probe),
    ]


def _desktop(probe: Probe) -> list[Check]:
    """The programs behind the volume, opening, locking and notifications, per platform (#192)."""
    from . import desktop

    if desktop.system() == "windows":
        return _windows_desktop(probe)
    if desktop.system() != "mac":
        return [_programs("Volume", ["wpctl"], probe, "install PipeWire (wireplumber)"),
                _programs("Open apps and pages", ["xdg-open"], probe, "install xdg-utils"),
                _programs("Lock screen", ["loginctl"], probe, "needs systemd-logind"),
                _programs("Desktop notifications", ["notify-send"], probe, "install libnotify")]
    checks = [_programs("Volume", ["osascript"], probe, "osascript comes with macOS"),
              _programs("Open apps and pages", ["open"], probe, "open comes with macOS"),
              _programs("Lock screen", ["pmset"], probe, "pmset comes with macOS"),
              _programs("Desktop notifications", ["osascript"], probe, "osascript comes with macOS")]
    outputs = _programs("Switch outputs", ["SwitchAudioSource"], probe, "brew install switchaudio-osx")
    outputs.optional = True
    checks.append(outputs)
    checks.append(_accessibility(probe))
    has_blackhole = any("blackhole" in name.lower() for name in probe.input_devices())
    checks.append(Check("The Mac's own sound", has_blackhole, "BlackHole" if has_blackhole else "no BlackHole input",
                        "" if has_blackhole else "brew install blackhole-2ch, then a Multi-Output Device with it in "
                        "Audio MIDI Setup", optional=True))
    return checks


NOT_ON_WINDOWS = ("dictation, hand gestures, clicking on and looking at the screen, media keys, switching outputs, "
                  "the computer's own sound, presence and the phone page")


def _windows_desktop(probe: Probe) -> list[Check]:
    """The Windows calls behind the volume, opening, locking and notifications, and whether the settings folder's
    ACL keeps the keys the user's (#193). Nothing is changed to check."""
    from . import config, desktop

    volume = probe.has_module("pycaw")
    private = desktop.is_private(config.home())
    return [
        Check("Volume", volume, "Core Audio (pycaw)" if volume else "pycaw is not installed",
              "" if volume else 'pip install -e ".[windows]"'),
        _programs("Open apps and pages", ["explorer.exe"], probe, "explorer.exe comes with Windows"),
        _programs("Lock screen", ["rundll32.exe"], probe, "rundll32.exe comes with Windows"),
        _programs("Desktop notifications", ["powershell.exe"], probe, "Windows PowerShell comes with Windows"),
        Check("Settings folder private", private is True,
              f"{config.home()} is yours only" if private else
              (f"{config.home()}: its permissions couldn't be read" if private is None else
               f"{config.home()} can be opened by other accounts"),
              "" if private else "run any rafiq command once to close it, or move it with RAFIQ_HOME to an NTFS drive"),
        Check("Not on Windows yet", True, NOT_ON_WINDOWS, optional=True),
    ]


def _accessibility(probe: Probe) -> Check:
    """Typing, gesture keys and power go through System Events, which answers only with the permission."""
    label, fix = "Typing and keys (Accessibility)", ("allow your terminal (or Python) in System Settings > Privacy & "
                                                     "Security > Accessibility")
    try:
        done = probe.run(["osascript", "-e", 'tell application "System Events" to return UI elements enabled'])
    except (OSError, subprocess.SubprocessError):
        return Check(label, False, "osascript did not answer", fix, optional=True)
    allowed = (done.stdout or "").strip() == "true"
    return Check(label, allowed, "allowed" if allowed else "not allowed", "" if allowed else fix, optional=True)


# ---------------------------------------------------------------------- models for this machine

def machine(probe: Probe | None = None, ram_gb: float | None = None) -> Machine:
    """Memory, and the NVIDIA GPU if nvidia-smi sees one (others count as no GPU)."""
    probe = probe or Probe()
    if ram_gb is None:
        import psutil

        ram_gb = psutil.virtual_memory().total / 2**30
    gpu, vram = "", 0.0
    try:
        out = probe.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
        if out.returncode == 0 and out.stdout.strip():
            name, mib = out.stdout.strip().splitlines()[0].rsplit(",", 1)
            gpu, vram = name.strip(), float(mib) / 1024
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return Machine(ram_gb=ram_gb, gpu=gpu, vram_gb=vram)


def suggest(m: Machine) -> Advice:
    """The biggest local brain that answers at a talking pace, and the Whisper model to go with it.

    On a GPU the model must fit in its memory with 1 GB to spare. On the CPU it needs twice its size
    plus 4 GB for the system and Whisper, which also keeps it fast enough to talk to.
    """
    for name, size in OLLAMA_MODELS:
        if m.vram_gb and size + 1 <= m.vram_gb:
            ollama, reason = name, f"fits in the {m.gpu or 'GPU'}'s {m.vram_gb:.0f} GB"
            break
    else:
        for name, size in OLLAMA_MODELS:
            if size * 2 + 4 <= m.ram_gb:
                ollama, reason = name, f"runs on the CPU with {m.ram_gb:.0f} GB of memory"
                break
        else:
            ollama, reason = OLLAMA_MODELS[-1][0], f"the smallest; {m.ram_gb:.0f} GB of memory is tight"
    whisper = "medium" if m.vram_gb >= 5 else "small" if m.ram_gb >= 8 else "base"
    return Advice(ollama, whisper, reason)


def _size(model: str) -> float | None:
    return dict(OLLAMA_MODELS).get(model)


def advice_lines(cfg: dict, m: Machine) -> list[str]:
    advice = suggest(m)
    gpu = f", {m.gpu} ({m.vram_gb:.0f} GB)" if m.gpu else ", no NVIDIA GPU found"
    lines = [f"  this machine: {m.ram_gb:.0f} GB of memory{gpu}",
             f"  suggested local brain: {advice.ollama_model} ({advice.reason})",
             f"  suggested speech model: whisper {advice.whisper_model}"]
    model, ideal = cfg["brain"]["ollama_model"], _size(advice.ollama_model)
    current = _size(model)
    if current is not None and current > ideal:
        lines.append(f"  {model} is too big for this machine: set [brain] ollama_model = "
                     f"\"{advice.ollama_model}\" (then: ollama pull {advice.ollama_model})")
    elif current is not None and current < ideal:
        lines.append(f"  this machine has room for {advice.ollama_model}, which answers better than {model}")
    whisper = cfg["speech"]["whisper_model"]
    if whisper in WHISPER_ORDER and WHISPER_ORDER.index(whisper) > WHISPER_ORDER.index(advice.whisper_model):
        lines.append(f"  whisper {whisper} will be slow here: set [speech] whisper_model = "
                     f"\"{advice.whisper_model}\"")
    return lines
