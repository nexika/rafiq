"""`rafiq setup`: the first-run wizard. It asks for the brain and the keys, writes the config, downloads the
voices and speech models, tests the microphone and every integration, records the wake word and ends with
a spoken welcome.

Everything that touches the network or a device goes through `Actions`, so the wizard runs in tests with
fakes. A step that fails is reported and the wizard goes on: setup can be run again at any time.
"""

from __future__ import annotations

import getpass
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import config, lang
from .tts import PIPER_FILES

MODES = ("auto", "claude", "ollama")
QUIET = 0.01              # a peak below this over a few seconds of talking: the microphone is muted or far
WELCOME = {
    "en": "Hello{name}, I'm Rafiq. Say my name whenever you need me.",
    "fr": "Bonjour{name}, je suis Rafiq. Dis mon nom quand tu as besoin de moi.",
    "es": "Hola{name}, soy Rafiq. Di mi nombre cuando me necesites.",
    "de": "Hallo{name}, ich bin Rafiq. Sag meinen Namen, wenn du mich brauchst.",
    "ar": "مرحبًا{name}، أنا رفيق. نادِني باسمي متى احتجت إليّ.",
    "ar-EG": "أهلًا{name}، أنا رفيق. نادي عليّا في أي وقت تحتاجني.",
}


# ---------------------------------------------------------------- writing TOML (the standard library only reads it)

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        # JSON's escapes are all valid in a TOML basic string.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def to_toml(data: dict, prefix: str = "") -> str:
    """The settings as TOML: plain values first, then one [table] per nested dict."""
    lines = [f"{_key(k)} = {_value(v)}" for k, v in data.items() if not isinstance(v, dict)]
    out = "\n".join(lines) + ("\n" if lines else "")
    for key, value in data.items():
        if isinstance(value, dict):
            name = f"{prefix}{_key(key)}"
            body = to_toml(value, name + ".")
            if any(not isinstance(v, dict) for v in value.values()) or not value:
                out += f"\n[{name}]\n"
            out += body
    return out


def save(settings: dict, path: Path | None = None) -> Path:
    """Write the config readable only by the user: it can hold API keys."""
    path = path or config.home() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# Written by `rafiq setup`. See config.example.toml for every setting.\n\n" + to_toml(settings))
    path.chmod(0o600)
    return path


# ---------------------------------------------------------------- the machine


def _download_voices(names: list[str], folder: Path) -> None:
    if importlib.util.find_spec("piper") is None:
        raise RuntimeError("Piper is not installed: pip install -e \".[offline-voice]\"")
    subprocess.run([sys.executable, "-m", "piper.download_voices", *names], cwd=folder, check=True, timeout=1800)


def _download_whisper(size: str) -> None:
    from faster_whisper import WhisperModel

    WhisperModel(size, device="cpu", compute_type="int8")


def _pull_ollama(model: str) -> bool:
    return subprocess.run(["ollama", "pull", model], timeout=7200).returncode == 0


def _record(seconds: float) -> np.ndarray:
    from .audio import Microphone

    mic, frames = Microphone(), []
    mic.start()
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            frame = mic.read(timeout=0.1)
            if frame is not None:
                frames.append(frame)
    finally:
        mic.stop()
    return np.concatenate(frames) if frames else np.zeros(0, dtype="float32")


def _check() -> int:
    from .__main__ import cmd_doctor

    return cmd_doctor(None)


def _enroll() -> int:
    from . import enroll

    return enroll.run()


def _speak(cfg: dict, text: str, tag: str) -> bool:
    from .tts import Speaker

    return Speaker(cfg).say(text, tag)


@dataclass
class Actions:
    download_voices: Callable[[list[str], Path], None] = _download_voices
    download_whisper: Callable[[str], None] = _download_whisper
    # None when Ollama is not installed: the wizard then says where to get it.
    pull_ollama: Callable[[str], bool] | None = field(
        default_factory=lambda: _pull_ollama if shutil.which("ollama") else None)
    record: Callable[[float], np.ndarray] = _record
    check: Callable[[], object] = _check
    enroll: Callable[[], int] = _enroll
    speak: Callable[[dict, str, str], bool] = _speak


# ---------------------------------------------------------------- the wizard


def _choose(ask, question: str, options: tuple[str, ...], default: str) -> str:
    while True:
        answer = ask(f"{question} [{'/'.join(options)}] (Enter: {default}) ").strip()
        if not answer:
            return default
        if answer in options:
            return answer


def _text(ask, question: str, default: str) -> str:
    return ask(f"{question}" + (f" (Enter: {default}) " if default else " ")).strip() or default


def _step(say, label: str, fn) -> None:
    try:
        fn()
    except Exception as exc:          # one broken step must not stop the rest of the setup
        say(f"  no  {label}: {type(exc).__name__}: {exc}")


def questions(ask, ask_secret, say, settings: dict) -> str:
    """Ask for the settings, updating `settings` in place; returns the language to greet in."""
    defaults = config.load()
    name = _text(ask, "What should I call you?", settings.get("user_name", ""))
    if name:
        settings["user_name"] = name
    say("The brain: Claude (fast, needs an API key) or Ollama (free and private, on this computer). "
        "auto uses Claude when a key is set and Ollama otherwise.")
    brain = settings.setdefault("brain", {})
    brain["mode"] = _choose(ask, "Brain", MODES, brain.get("mode", "auto"))
    if brain["mode"] != "ollama":
        if os.environ.get("ANTHROPIC_API_KEY"):
            say("  Claude API key: using ANTHROPIC_API_KEY from the environment.")
        else:
            saved = " (Enter keeps the saved one)" if settings.get("anthropic_api_key") else ""
            key = ask_secret(f"Claude API key from console.anthropic.com{saved}, Enter to skip: ").strip()
            if key:
                settings["anthropic_api_key"] = key
    voice = settings.setdefault("voice", {})
    if os.environ.get("AZURE_SPEECH_KEY"):
        say("  Azure voices: using AZURE_SPEECH_KEY from the environment.")
    else:
        say("Azure AI Speech gives the best voices, including Egyptian Arabic; without it Rafiq speaks offline.")
        saved = " (Enter keeps the saved one)" if voice.get("azure_key") else ""
        key = ask_secret(f"Azure Speech key{saved}, Enter to skip: ").strip()
        if key:
            voice["azure_key"] = key
            voice["azure_region"] = _text(ask, "Azure region, such as westeurope:", voice.get("azure_region", ""))
    if not voice:
        del settings["voice"]
    place = settings.get("place", {})
    city = _text(ask, "Your city, for the weather:", place.get("city", defaults["place"]["city"]))
    if city:
        settings.setdefault("place", {})["city"] = city
    return _choose(ask, "Which language should I greet you in?", lang.TAGS, "en")


def run(ask=input, ask_secret=getpass.getpass, say=print, actions: Actions | None = None,
        enroll: bool = True) -> int:
    actions = actions or Actions()
    path = config.home() / "config.toml"
    settings = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    say(f"Setting up Rafiq. Your answers go in {path}; Enter keeps the suggestion.")
    tag = questions(ask, ask_secret, say, settings)
    save(settings, path)
    say(f"Saved {path}.")
    cfg = config.load(path)

    folder = config.data_dir() / "piper"
    folder.mkdir(parents=True, exist_ok=True)
    missing = [n for n in PIPER_FILES.values() if not (folder / f"{n}.onnx").is_file()]
    if missing:
        say(f"Downloading {len(missing)} offline voices...")
        _step(say, "offline voices", lambda: actions.download_voices(missing, folder))

    for size in dict.fromkeys([cfg["speech"]["whisper_model"], cfg["speech"]["wake_model"]]):
        say(f"Downloading the speech model {size} (once)...")
        _step(say, f"speech model {size}", lambda size=size: actions.download_whisper(size))

    if cfg["brain"]["mode"] != "claude":
        model = cfg["brain"]["ollama_model"]
        if actions.pull_ollama is None:
            say(f"  Ollama is not installed: get it from https://ollama.com, then run: ollama pull {model}")
        else:
            say(f"Downloading {model} for Ollama (several GB the first time)...")
            _step(say, f"Ollama {model}", lambda: actions.pull_ollama(model) or say(f"  no  Ollama {model}"))

    def microphone():
        ask("Testing the microphone: press Enter, then talk for three seconds. ")
        audio = actions.record(3.0)
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        if peak >= QUIET:
            say(f"  ok  microphone: level {peak:.2f}")
        else:
            say(f"  no  microphone: too quiet (level {peak:.4f}); check it is not muted in the sound settings")

    _step(say, "microphone", microphone)
    say("Checking everything:")
    _step(say, "checks", actions.check)

    if enroll and _choose(ask, "Record your voice saying my name now (about two minutes)?", ("y", "n"), "y") == "y":
        _step(say, "wake word", actions.enroll)

    name = cfg.get("user_name", "")
    welcome = WELCOME[tag].format(name=(", " if not tag.startswith("ar") else " يا ") + name if name else "")
    say(welcome)
    _step(say, "welcome", lambda: actions.speak(cfg, welcome, tag))
    say("Done. Start Rafiq with: rafiq run")
    return 0
