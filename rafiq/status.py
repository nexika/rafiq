"""The status page: is each part of Rafiq up? Each part is up (True), down (False) or not used (None),
with a short detail. The checks are local and quick: no request leaves the computer.
"""

from __future__ import annotations

from . import config as config_mod


def _part(part: str, up: bool | None, detail: str) -> dict:
    return {"part": part, "up": up, "detail": detail}


def check_all(cfg: dict, models, speaker) -> list[dict]:
    brain = cfg["brain"]
    parts = []

    if brain.get("mode") == "claude":
        parts.append(_part("ollama", None, "not used: the brain is set to Claude"))
    else:
        installed = models.installed()
        model = brain["ollama_model"]
        if installed is None:
            parts.append(_part("ollama", False, f"not reachable at {brain['ollama_url']}"))
        elif model not in installed:
            parts.append(_part("ollama", False, f"{model} is not installed"))
        else:
            parts.append(_part("ollama", True, model))

    if cfg.get("anthropic_api_key"):
        parts.append(_part("claude", True, brain["claude_model"]))
    else:
        parts.append(_part("claude", None, "no API key"))

    whisper = models.whisper_page()
    size = whisper["active"]
    downloaded = any(m["downloaded"] for m in whisper["models"] if m["name"] == size)
    parts.append(_part("whisper", downloaded, size if downloaded else f"{size} is not downloaded yet"))

    engine = speaker.engines()[0]
    if engine == "azure" and getattr(speaker, "key", "") and getattr(speaker, "region", ""):
        parts.append(_part("voice", True, "Azure"))
    elif speaker.piper_path("en") is not None:
        parts.append(_part("voice", True, "Piper (offline)"))
    else:
        parts.append(_part("voice", False, "no voice found: set an Azure key or add Piper voices"))

    if not cfg.get("google", {}).get("enabled"):
        parts.append(_part("google", None, "not enabled"))
    elif (config_mod.home() / "google" / "token.json").is_file():
        parts.append(_part("google", True, "connected"))
    else:
        parts.append(_part("google", False, "run: rafiq google-setup"))

    # Satellites (other rooms' microphones) are not part of Rafiq yet: none can be up.
    parts.append(_part("satellites", None, "none set up"))
    return parts
