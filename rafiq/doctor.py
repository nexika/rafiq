"""`rafiq doctor`: is each integration ready, and which local models fit this machine.

Every outside thing (the network, programs, Python modules, the GPU) goes through a Probe, so the
checks run in tests without any of them. Keys are sent where they belong and never printed.
"""

from __future__ import annotations

import importlib.util
import shutil
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


def integrations(cfg: dict, probe: Probe | None = None) -> list[Check]:
    """One check per brain, voice service and skill that needs something outside Rafiq."""
    probe = probe or Probe()
    return [
        _claude(cfg, probe),
        _ollama(cfg, probe),
        _azure(cfg, probe),
        _google(cfg, probe),
        _weather(cfg, probe),
        _modules("Web search", ["ddgs", "trafilatura"], probe),
        _programs("Volume", ["wpctl"], probe, "install PipeWire (wireplumber)"),
        _programs("Open apps and pages", ["xdg-open"], probe, "install xdg-utils"),
        _programs("Lock screen", ["loginctl"], probe, "needs systemd-logind"),
        _programs("Desktop notifications", ["notify-send"], probe, "install libnotify"),
    ]


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
