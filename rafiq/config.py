"""Settings: ~/.config/rafiq/config.toml (RAFIQ_HOME moves the folder), defaults for everything.

Secrets come from the environment first (ANTHROPIC_API_KEY), then the file, and are never logged.
"""

from __future__ import annotations

import copy
import os
import secrets
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "brain": {
        # "auto": Claude when an API key is set and the network answers, else Ollama.
        "mode": "auto",
        "claude_model": "claude-haiku-5-5",
        "ollama_model": "qwen2.5:7b",
        "ollama_url": "http://127.0.0.1:11434",
        "max_tokens": 600,
    },
    "speech": {
        "whisper_model": "small",       # tiny, base, small, medium; medium is better for Arabic but slower
        "wake_model": "tiny",            # the small model that only listens for the name
        "languages": ["en", "fr", "es", "de", "ar"],
        "follow_up_seconds": 6,          # after an answer, listen this long without the name
        "max_utterance_seconds": 20,
    },
    "voice": {
        # auto: Azure AI Speech when AZURE_SPEECH_KEY and AZURE_SPEECH_REGION are set, else Piper (offline).
        "engine": "auto",
        "azure_key": "",
        "azure_region": "",
        "rate": "+0%",
        "voices": {
            "en": "en-US-AndrewNeural",
            "fr": "fr-FR-DeniseNeural",
            "es": "es-ES-AlvaroNeural",
            "de": "de-DE-ConradNeural",
            "ar": "ar-SA-HamedNeural",
            "ar-EG": "ar-EG-ShakirNeural",
        },
        "piper_voices": {},              # language -> a Piper .onnx voice; default: data/piper/<voice>.onnx
    },
    "panel": {"host": "127.0.0.1", "port": 8765},
    "place": {"city": "", "units": "metric"},
    "google": {"enabled": False},
    "notes": {"folder": ""},             # empty: data/notes; or any folder, such as an Obsidian vault
    "anthropic_api_key": "",
}


def home() -> Path:
    return Path(os.environ.get("RAFIQ_HOME") or Path.home() / ".config" / "rafiq")


def data_dir() -> Path:
    path = home() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: Path | None = None) -> dict:
    path = path or home() / "config.toml"
    cfg = copy.deepcopy(DEFAULTS)
    if path.is_file():
        cfg = _merge(cfg, tomllib.loads(path.read_text(encoding="utf-8")))
    if os.environ.get("ANTHROPIC_API_KEY"):
        cfg["anthropic_api_key"] = os.environ["ANTHROPIC_API_KEY"]
    return cfg


def panel_token() -> str:
    """A random token the panel page must send back, so no other web page can drive Rafiq."""
    path = home() / "panel.token"
    if path.is_file():
        token = path.read_text().strip()
        if len(token) >= 32:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token)
    path.chmod(0o600)
    return token
