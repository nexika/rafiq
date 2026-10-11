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
        # everyday: claude_model, fast and cheap; quality: quality_model; offline: Ollama and Piper only.
        "profile": "everyday",
        "claude_model": "claude-haiku-5-5",
        "quality_model": "claude-opus-5-5",
        "ollama_model": "qwen2.5:7b",
        "ollama_url": "http://127.0.0.1:11434",
        "max_tokens": 600,
        # A request that keeps calling tools is stopped, and Rafiq says so: at most this many
        # model rounds, tool calls in all, and seconds from the request to the answer.
        "limits": {"max_rounds": 6, "max_tool_calls": 12, "max_seconds": 120},
        "persona": "neutral",            # neutral, concise or butler; changing it by voice or in the panel wins
    },
    "speech": {
        "whisper_model": "small",       # tiny, base, small, medium; medium is better for Arabic but slower
        "wake_model": "tiny",            # the small model that only listens for the name
        "languages": ["en", "fr", "es", "de", "ar"],
        "follow_up_seconds": 6,          # after an answer, listen this long without the name
        "max_utterance_seconds": 20,
        # Speech to text. auto: Whisper on this computer; a cloud service only for the languages in
        # stt_by_language, or when Whisper can't run. local: never the cloud. azure or openai: every language.
        "stt_engine": "auto",
        "stt_by_language": {},           # language -> "azure", "openai" or "local", e.g. {"ar": "azure"}
        "azure_locales": {"ar": "ar-EG"},  # the Azure recogniser per language (AZURE_SPEECH_KEY, as for the voice)
        "openai_model": "gpt-4o-mini-transcribe",
        "openai_key": "",                # or OPENAI_API_KEY
        "offline": False,                # true: what you say never leaves this computer
    },
    "voice": {
        # auto: Azure AI Speech when AZURE_SPEECH_KEY and AZURE_SPEECH_REGION are set, then ElevenLabs when
        # ELEVENLABS_API_KEY is set, then Kokoro when its files are in data/kokoro/, then Piper (offline).
        "engine": "auto",
        "azure_key": "",
        "azure_region": "",
        "elevenlabs_key": "",
        "elevenlabs_model": "eleven_flash_v2_5",
        "elevenlabs_voices": {},         # language (or "default") -> ElevenLabs voice id
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
    "audio": {
        # what the user calls an output -> part of its name in `wpctl status`, e.g. headphones = "WH-1000XM4"
        "outputs": {},
    },
    "interruption": {
        # Say "stop" over Rafiq to cut its speech. The microphone then stays open while Rafiq speaks.
        "enabled": True,
        "factor": 1.8,                   # how much louder than Rafiq's echo you must be
        "min_level": 0.01,               # the quietest sound (RMS) that counts
        "frames": 3,                     # loud 32 ms frames in a row before it listens
        "echo_cancel": True,             # use PipeWire's echo-cancel microphone when it is loaded
    },
    "panel": {"host": "127.0.0.1", "port": 8765},
    "place": {"city": "", "units": "metric"},
    "pc": {"shutdown_delay": 30},            # seconds "shut down" waits; 0 shuts down at once
    "google": {"enabled": False},
    # Computers Rafiq can wake: name -> network card address, or a table {mac, broadcast, port}.
    "wake_on_lan": {"broadcast": "255.255.255.255", "port": 9, "machines": {}},
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
