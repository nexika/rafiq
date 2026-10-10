"""Settings: ~/.config/rafiq/config.toml (RAFIQ_HOME moves the folder), defaults for everything.

Secrets come from the environment first (ANTHROPIC_API_KEY), then the file, and are never logged.
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import tomllib
from pathlib import Path

from .lang import TAGS

DEFAULTS: dict = {
    "brain": {
        # "auto": Claude when an API key is set and the network answers, else Ollama.
        "mode": "auto",
        "claude_model": "claude-haiku-5-5",
        "ollama_model": "qwen2.5:7b",
        "ollama_url": "http://127.0.0.1:11434",
        "max_tokens": 600,
        # Dollars per million tokens, by model, to show the spend in the panel: {input = 1.0, output = 5.0}.
        "prices": {},
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


# ---------------------------------------------------------------------- what the panel may change

# The only settings the panel can read or write. Keys, addresses and everything else stay out of the page.
CHOICES = {"brain.mode": ["auto", "claude", "ollama"], "voice.engine": ["auto", "azure", "piper", "edge"]}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}")
PATTERNS = {"brain.claude_model": _NAME, "brain.ollama_model": _NAME, "voice.rate": re.compile(r"[+-]\d{1,3}%"),
            **{f"voice.voices.{tag}": _NAME for tag in TAGS}}


def _get(cfg: dict, name: str):
    *tables, key = name.split(".")
    for table in tables:
        cfg = cfg.get(table, {})
    return cfg.get(key, "")


def settable(cfg: dict) -> dict:
    """The settings the panel shows, by dotted name."""
    return {name: _get(cfg, name) for name in [*CHOICES, *PATTERNS]}


def check(name: str, value) -> str:
    if name not in CHOICES and name not in PATTERNS:
        raise ValueError(f"{name} cannot be changed from the panel")
    ok = (value in CHOICES[name]) if name in CHOICES else (
        isinstance(value, str) and PATTERNS[name].fullmatch(value) is not None)
    if not isinstance(value, str) or not ok:
        raise ValueError(f"{name}: {value!r} is not allowed")
    return value


def _comment_at(line: str) -> int:
    """Where a trailing comment starts on a `key = value` line, or -1: the first # after which the
    rest of the line still reads as TOML (a # inside a string does not)."""
    for i, char in enumerate(line):
        if char == "#":
            try:
                tomllib.loads(line[:i])
                return i
            except tomllib.TOMLDecodeError:
                continue
    return -1


def _set_line(text: str, name: str, value: str) -> str:
    """The file's text with one setting changed, its comment and every other line kept."""
    *tables, key = name.split(".")
    table, lines = ".".join(tables), text.splitlines()
    new = f"{key} = {json.dumps(value)}"
    current, header = "", None
    for i, line in enumerate(lines):
        found = re.match(r"\s*\[([^\[\]]+)\]", line)
        if found:
            current = re.sub(r"\s", "", found.group(1))
            header = i if current == table else header
        elif current == table and re.match(rf"\s*\"?{re.escape(key)}\"?\s*=", line):
            at = _comment_at(line)
            lines[i] = new if at < 0 else new.ljust(at) + (" " if len(new) >= at else "") + line[at:]
            return "\n".join(lines) + "\n"
    if header is None:
        lines += ([""] if lines and lines[-1].strip() else []) + [f"[{table}]"]
        header = len(lines) - 1
    lines.insert(header + 1, new)
    return "\n".join(lines) + "\n"


def save_settings(changes: dict, cfg: dict, path: Path | None = None) -> None:
    """Check every change against the allowlist, write them to config.toml and apply them to the
    running settings. One refused change and nothing is written."""
    for name, value in changes.items():
        check(name, value)
    path = path or home() / "config.toml"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    for name, value in changes.items():
        text = _set_line(text, name, value)
    try:
        saved = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"config.toml could not be updated: {exc}") from exc
    if any(_get(saved, name) != value for name, value in changes.items()):
        raise ValueError("config.toml is written in a way the panel cannot update; edit it by hand")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.is_file():   # the file may hold a key: keep its permissions
        tmp.chmod(path.stat().st_mode & 0o777)
    tmp.replace(path)
    for name, value in changes.items():
        *tables, key = name.split(".")
        target = cfg
        for table in tables:
            target = target.setdefault(table, {})
        target[key] = value
