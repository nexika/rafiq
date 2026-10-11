"""Settings: ~/.config/rafiq/config.toml (RAFIQ_HOME moves the folder), defaults for everything.

Secrets come from the environment first (ANTHROPIC_API_KEY, OPENAI_API_KEY, HOME_ASSISTANT_TOKEN,
RAFIQ_AGENT_TOKEN, DISCORD_BOT_TOKEN), then the file, and are never logged.
"""

from __future__ import annotations

import copy
import os
import secrets
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "brain": {
        # "auto": Claude, then OpenAI, for each that has an API key and answers; else Ollama.
        "mode": "auto",
        # everyday: claude_model, fast and cheap; quality: quality_model; offline: Ollama and Piper only.
        "profile": "everyday",
        "claude_model": "claude-haiku-5-5",
        "quality_model": "claude-opus-5-5",
        "openai_model": "gpt-5-mini",
        # Any server that speaks OpenAI's chat completions works here (DeepSeek, Gemini, a local one).
        "openai_url": "https://api.openai.com/v1",
        "ollama_model": "qwen2.5:7b",
        "ollama_url": "http://127.0.0.1:11434",
        "ollama_tools": "local",        # "local": a smaller set a 7B model handles well; "all"; or a list of names
        "max_tokens": 600,
        # Offer the model only the tools a request needs (none for small talk); off: every tool, always.
        "routing": True,
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
        "end_silence_seconds": 0.7,      # this much silence ends what you say (a noisy room adds a little)
        # Speech to text. auto: Whisper on this computer; a cloud service only for the languages in
        # stt_by_language, or when Whisper can't run. local: never the cloud. azure or openai: every language.
        "stt_engine": "auto",
        "stt_by_language": {},           # language -> "azure", "openai" or "local", e.g. {"ar": "azure"}
        "azure_locales": {"ar": "ar-EG"},  # the Azure recogniser per language (AZURE_SPEECH_KEY, as for the voice)
        "openai_model": "gpt-4o-mini-transcribe",
        "openai_key": "",                # or OPENAI_API_KEY
        "offline": False,                # true: what you say never leaves this computer
    },
    "audio": {
        "device": "",                    # index or part of the name (see `rafiq doctor`); "": the system default
        "rate": 0,                       # the sample rate to try first; 0: 16 kHz, then whatever the device takes
        "auto_calibration": True,        # listen to the room for a moment at start-up and fit the thresholds
        "calibration_seconds": 1.0,
        # what the user calls an output -> part of its name in `wpctl status`, e.g. headphones = "WH-1000XM4"
        "outputs": {},
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
    "tasks": {
        # Background research ("research the best e-bike under 2000 euros"); needs Claude.
        "model": "",                     # empty: brain.claude_model
        "max_tokens": 2000,
        # US dollars per million tokens, [input, output], to show what each task cost.
        "prices": {"claude-haiku-5-5": [0.10, 0.50], "claude-sonnet-5-5": [2.00, 10.00],
                   "claude-opus-5-5": [4.00, 20.00]},
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
    # `rafiq run --server`: the brain without devices, for `rafiq agent` on the PC. Without a TLS
    # certificate it only listens on a private address (the LAN or Tailscale).
    "server": {"host": "127.0.0.1", "port": 8766, "tls_cert": "", "tls_key": ""},
    # `rafiq agent`: the server to lend this PC to. The token (from `rafiq agent-token NAME` on the
    # server) can also come from RAFIQ_AGENT_TOKEN.
    "agent": {"server": "", "name": "", "token": ""},
    "place": {"city": "", "units": "metric",
              # With no city: GNOME's location service, then (only if true) an IP lookup at ipapi.co.
              "ip_lookup": False},
    "pc": {"shutdown_delay": 30},            # seconds "shut down" waits; 0 shuts down at once
    "google": {"enabled": False},
    # A Discord bot token (DISCORD_BOT_TOKEN), never a user token; user_id is your own Discord id.
    "discord": {"enabled": False, "bot_token": "", "user_id": ""},
    "notes": {"folder": ""},             # empty: data/notes; or any folder, such as an Obsidian vault
    # accounts: usernames to report on; empty for every account linked to the token.
    "instagram": {"enabled": False, "app_id": "", "app_secret": "", "accounts": []},
    "presence": {
        "phone_ip": "",                  # the phone's fixed IP on the home Wi-Fi; empty: presence is off
        "phone_mac": "",                 # optional: only this MAC counts (turn off the phone's private MAC)
        "away_minutes": 10,              # off the Wi-Fi this long before the away scene
        "check_seconds": 30,
        "away_command": [],              # the away scene: a command as an argument list
        "home_command": [],              # the welcome-home scene
        "welcome": True,                 # also say "welcome home"
    },
    # Scenes: names (in any language) and the tool calls they run; see rafiq/tools/scenes.py.
    # "Turn everything off" ends by offering to shut down; add your lights and music steps before it.
    "scenes": {
        "all_off": {
            "names": ["turn everything off", "everything off", "éteins tout", "apaga todo", "alles aus",
                      "mach alles aus", "أطفئ كل شيء", "اقفل كل حاجة", "طفي كل حاجة"],
            "steps": [{"tool": "power", "args": {"action": "shutdown"}}],
        },
    },
    "day_start": {"scene": ""},          # a scene to run at the first start of each day
    # Home Assistant, for Nest, Hue, plugs and the rest: its address and a long-lived access token.
    "home_assistant": {"url": "", "token": ""},
    # Computers Rafiq can wake: name -> network card address, or a table {mac, broadcast, port}.
    "wake_on_lan": {"broadcast": "255.255.255.255", "port": 9, "machines": {}},
    # Philips Hue: enabled after `rafiq hue-setup`; rooms maps names you say to the Hue app's room names.
    "hue": {"enabled": False, "rooms": {}},
    "screen": {
        # Questions about the screen go to Claude (in the cloud), and never while offline unless a
        # local vision model is set: an Ollama model that reads images, e.g. "qwen2.5vl:7b".
        "ollama_model": "",
        "ask_first": True,               # ask before each screenshot that goes to the cloud
        "max_side": 1568,                # shrink the screenshot to this many pixels on its long side
    },
    "plugs": [],                         # smart plugs, one table each; see the README
    # OBS Studio 28+ with its WebSocket server on; OBS_WEBSOCKET_PASSWORD overrides the password here.
    "obs": {"enabled": False, "host": "localhost", "port": 4455, "password": "", "timeout": 5},
    "anthropic_api_key": "",
    "openai_api_key": "",
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
    for key in ("anthropic_api_key", "openai_api_key"):
        if os.environ.get(key.upper()):
            cfg[key] = os.environ[key.upper()]
    if os.environ.get("HOME_ASSISTANT_TOKEN"):
        cfg["home_assistant"]["token"] = os.environ["HOME_ASSISTANT_TOKEN"]
    if os.environ.get("RAFIQ_AGENT_TOKEN"):
        cfg["agent"]["token"] = os.environ["RAFIQ_AGENT_TOKEN"]
    if os.environ.get("DISCORD_BOT_TOKEN"):
        cfg["discord"]["bot_token"] = os.environ["DISCORD_BOT_TOKEN"]
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
