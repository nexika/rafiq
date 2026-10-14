"""Settings: ~/.config/rafiq/config.toml (RAFIQ_HOME moves the folder), defaults for everything.

Choices made in the panel (the active models) are kept in choices.json next to it and win over the
file, so the panel never rewrites the user's own config.toml.

Secrets come from the environment first (ANTHROPIC_API_KEY, OPENAI_API_KEY, HOME_ASSISTANT_TOKEN,
RAFIQ_AGENT_TOKEN, DISCORD_BOT_TOKEN, AUDD_API_TOKEN), then the file, and are never logged.
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
        # "auto": Claude, then OpenAI, for each that has an API key and answers; else Ollama.
        "mode": "auto",
        # everyday: claude_model, fast and cheap; quality: quality_model; offline: Ollama and Piper only.
        "profile": "everyday",
        "claude_model": "claude-haiku-5-5",
        "quality_model": "claude-opus-5-5",
        "openai_model": "gpt-5-mini",
        # Any server that speaks OpenAI's chat completions works here (DeepSeek, Gemini, a local one).
        "openai_url": "https://api.openai.com/v1",
        # How long the OpenAI-compatible server may stay silent before Rafiq gives up on it (auto mode then
        # asks Ollama). Gemini answered in 0.7 to 5.6 s; one request that hung kept the user waiting 60 s.
        "openai_wait_seconds": 15,
        "ollama_model": "qwen2.5:7b",
        "ollama_url": "http://127.0.0.1:11434",
        "ollama_tools": "local",        # "local": a smaller set a 7B model handles well; "all"; or a list of names
        "max_tokens": 600,
        # Dollars per million tokens, by model, to show the spend in the panel: {input = 1.0, output = 5.0}.
        "prices": {},
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
        "mixed": True,                   # Arabic with English words ("افتحلي الـ settings"): keep them in Latin letters
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
    "budget": {
        # Caps in `currency` (0: no cap). At warn_at of a cap Rafiq warns once; at the cap it goes
        # offline (Ollama, Piper) until the next day, or the next month for the monthly cap.
        "daily_cap": 0.0,
        "monthly_cap": 0.0,
        "warn_at": 0.8,
        "currency": "USD",
        # Per million tokens (input, output) for each Claude model; check the current price list.
        "token_prices": {"claude-haiku-5-5": {"input": 1.0, "output": 5.0}},
        # Per million characters read aloud. Azure's free tier (500,000 a month) is not deducted.
        "character_prices": {"azure": 15.0},
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
    # What Rafiq heard and answered, kept on this computer only; 0 keeps nothing.
    "history": {"retention_days": 7},
    "overlay": {
        "display": "auto",               # auto or notify: answers as desktop notifications; off: none
        "seconds": 8,                    # how long an answer stays on screen
        "silent": False,                 # true: answer on screen only, never out loud
    },
    # Hand gestures through the webcam (pip install rafiq[gestures]); on at start, or by voice or shortcut.
    "gestures": {"enabled": False, "hold_seconds": 0.6, "cooldown_seconds": 1.0, "camera": 0},
    # Devices in other rooms that hear "Rafiq" and send the request here (rafiq satellite on the device).
    # devices: name -> {token, room, blocked_tools = [...], risky = "ask" or "block"}. The hub starts
    # only when a device is listed; set host to this computer's address on a private network, or set
    # tls_cert and tls_key.
    "satellites": {"host": "127.0.0.1", "port": 8766, "tls_cert": "", "tls_key": "", "devices": {}},
    # This computer as a satellite of another Rafiq: hub = "host:port", and the name and token listed there.
    "satellite": {"hub": "", "name": "", "token": "", "tls": False, "tls_ca": ""},
    # The phone bridge (iOS Shortcuts, Android HTTP Shortcuts or Tasker). Off until you turn it on; it
    # listens on this computer only unless you choose another address (see the README).
    "phone": {"enabled": False, "host": "127.0.0.1", "port": 8767},
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
    "prayer": {
        "enabled": False,
        "method": "egyptian",            # egyptian, umm_al_qura, mwl, isna, karachi, dubai, kuwait, qatar, ...
        "asr": "standard",               # or "hanafi"
        "adjust": {},                    # minutes per prayer, e.g. {"fajr": 2}
        "reminders": {},                 # minutes before, e.g. {"fajr": 10}; none until you set them
        "language": "en",                # the language reminders are said in
        "hijri_adjust": 0,               # days, to match the local moon sighting
        "reciter": "alafasy",
    },
    "dev": {"project": ""},             # the folder "run the tests" works in; also set from the panel
    "privacy": {"log_days": 30},     # the log (data/logs) is redacted and deleted after this many days
    # client_id of your Spotify developer app; port of the login redirect http://127.0.0.1:<port>/callback
    "spotify": {"enabled": False, "client_id": "", "port": 8888},
    "paper": {
        "title": "Rafiq",
        "languages": [],                 # e.g. ["en", "ar"] for a bilingual paper; empty: the first speech language
        "feeds": [],                     # RSS or Atom addresses for the news section
        "tech_feeds": [],
        "max_articles": 6,               # per section
        "priorities": [],
        "deadlines": [],                 # [{title = "Tax return", date = "2026-10-15"}]
        # Printing with CUPS: the printer (empty: the default one), the PDF to print ("{date}" becomes
        # YYYY-MM-DD; empty: data/paper/<date>.pdf), and whether to offer it after the brief.
        "printer": "",
        "file": "",
        "after_brief": False,
    },
    "deadlines": {
        "feeds": [],                     # iCal addresses: "https://..." or {name = "Loopstr", url = "https://..."}
        "cache_minutes": 15,
        # A title with one of these words is a period away, not a deadline (as is anything longer than a day).
        "period_words": ["leave", "vacation", "holiday", "day off", "congé", "vacances", "urlaub", "إجازة"],
    },
    # The daily brief, said once a day at the first start, in this language tag (en, fr, ..., ar-EG).
    "brief": {"at_first_start": False, "language": "en"},
    # A Discord bot token (DISCORD_BOT_TOKEN), never a user token; user_id is your own Discord id.
    "discord": {"enabled": False, "bot_token": "", "user_id": ""},
    # "What's this song?": an AudD token (AUDD_API_TOKEN, https://audd.io); how long to listen; how many to keep.
    "songs": {"audd_token": "", "seconds": 8, "keep": 20},
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
    # Names Rafiq answers to besides "Rafiq"; `rafiq enroll` records them too.
    "wake": {"names": []},
    "wake_on_lan": {"broadcast": "255.255.255.255", "port": 9, "machines": {}},
    # Philips Hue: enabled after `rafiq hue-setup`; rooms maps names you say to the Hue app's room names.
    "hue": {"enabled": False, "rooms": {}},
    # Video and stage lights over DMX: the Art-Net node on your network, and one [[dmx.lights]] per light.
    "dmx": {"host": "", "universe": 0, "lights": []},
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
    # Notifications from long jobs (`rafiq notify`) are shown but not spoken in these hours.
    "notify": {"quiet_hours": ""},   # e.g. "22:00-07:00"
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
    cfg = _merge(cfg, _choices())
    for key in ("anthropic_api_key", "openai_api_key"):
        if os.environ.get(key.upper()):
            cfg[key] = os.environ[key.upper()]
    if os.environ.get("HOME_ASSISTANT_TOKEN"):
        cfg["home_assistant"]["token"] = os.environ["HOME_ASSISTANT_TOKEN"]
    if os.environ.get("RAFIQ_AGENT_TOKEN"):
        cfg["agent"]["token"] = os.environ["RAFIQ_AGENT_TOKEN"]
    if os.environ.get("DISCORD_BOT_TOKEN"):
        cfg["discord"]["bot_token"] = os.environ["DISCORD_BOT_TOKEN"]
    if os.environ.get("AUDD_API_TOKEN"):
        cfg["songs"]["audd_token"] = os.environ["AUDD_API_TOKEN"]
    return cfg


def _choices() -> dict:
    try:
        data = json.loads((home() / "choices.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_choice(section: str, key: str, value) -> None:
    """Keep a setting chosen in the panel for the next start."""
    choices = _choices()
    choices.setdefault(section, {})[key] = value
    path = home() / "choices.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(choices, indent=2), encoding="utf-8")


def _token(name: str) -> str:
    path = home() / name
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
CHOICES = {"brain.mode": ["auto", "claude", "openai", "ollama"], "voice.engine": ["auto", "azure", "piper", "edge"]}
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
def panel_token() -> str:
    """A random token the panel page must send back, so no other web page can drive Rafiq."""
    return _token("panel.token")


def phone_token() -> str:
    """The phone's own token, so it can be changed (by deleting phone.token) without the panel's."""
    return _token("phone.token")
