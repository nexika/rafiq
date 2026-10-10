"""Modes: how Rafiq trades speed, quality and privacy, switched by voice or in the panel.

- everyday: the fast, cheap Claude model (brain.claude_model), with Ollama as the fallback.
- quality: the strongest Claude model (brain.quality_model), for harder questions.
- offline: nothing leaves the computer: only the Ollama model and Piper voices. Speech-to-text is
  always local (faster-whisper), so it needs no change.

The mode lives in the settings dict every part shares (brain.profile), so a switch takes effect on the
next request without restarting. brain.mode (auto, claude, ollama) still picks the backend in the
everyday and quality modes; offline overrides it.
"""

from __future__ import annotations

MODES = ("everyday", "quality", "offline")


def current(cfg: dict) -> str:
    mode = cfg["brain"].get("profile", "everyday")
    return mode if mode in MODES else "everyday"


def offline(cfg: dict) -> bool:
    return current(cfg) == "offline"


def switch(cfg: dict, mode: str) -> str:
    """Change the mode; raises ValueError for a name that is not a mode."""
    mode = mode.strip().lower()
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; the modes are {', '.join(MODES)}")
    cfg["brain"]["profile"] = mode
    return mode
