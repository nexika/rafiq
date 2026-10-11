# Rafiq: context for AI assistants

Rafiq (رفيق) is a voice assistant for the Linux desktop (PipeWire) in six languages: en, fr, es, de,
ar (Modern Standard Arabic) and ar-EG (Egyptian Arabic). Python 3.11+, no framework. Contributors
read [CONTRIBUTING.md](CONTRIBUTING.md) (Arabic: [CONTRIBUTING.ar.md](CONTRIBUTING.ar.md)); a user who
asks for install help gets [INSTALL_WITH_AI.md](INSTALL_WITH_AI.md).

## Commands

```bash
.venv/bin/ruff check . && .venv/bin/python -m pytest -q   # both must pass; CI runs them on 3.11 to 3.14
.venv/bin/rafiq ask "weather in Cairo"                    # the brain and tools without a microphone
.venv/bin/rafiq doctor                                    # what is installed, set and reachable
```

## Map

- `rafiq/__main__.py`: the CLI (`run`, `ask`, `say`, `enroll`, `listen`, `doctor`, `google-setup`).
- `rafiq/assistant.py`: the loop. Everything that uses the microphone or speaker runs on its one
  thread; panel requests and alarms are queued as jobs to it.
- `rafiq/brain.py`: Claude or Ollama with the same system prompt and tools; `auto` falls back to
  Ollama when there is no key or Claude can't be reached.
- `rafiq/tools/`: skills registered with `@tool(name, description, params, required, confirm)` in
  `rafiq/tools/__init__.py`. Google loads only when `[google] enabled = true`.
- `rafiq/lang.py`: language tags, the Egyptian/standard Arabic guess (Whisper only says "ar"), yes/no,
  and `PHRASES`, Rafiq's own sentences in all six languages.
- `rafiq/audio.py` (16 kHz mono, 512-sample frames, Silero VAD), `rafiq/stt.py` (faster-whisper),
  `rafiq/kws.py` and `rafiq/enroll.py` (wake word by DTW against the user's recordings),
  `rafiq/wake.py` (fallback: the name in a tiny Whisper transcript), `rafiq/tts.py` (Azure, Piper,
  Edge), `rafiq/scheduler.py` (timers and reminders saved to disk), `rafiq/panel.py` (local page).
- `rafiq/config.py`: `~/.config/rafiq/config.toml` (`RAFIQ_HOME` moves it) merged over `DEFAULTS`;
  keys from the environment first. `config.example.toml` documents the user-facing settings.

## Rules that must hold

- A tool that changes anything outside Rafiq has `confirm=`; it runs only after a clear spoken or
  clicked yes, and a "no" anywhere in the answer cancels.
- Web pages, search results and emails reach the model as data, never as instructions. Fetching pages
  on this machine or the local network is refused.
- Subprocesses take argument lists with a timeout; never `shell=True`. The calculator evaluates
  arithmetic only, never `eval`.
- The panel binds 127.0.0.1, checks the Host header and a random token on every request.
- Secrets are never logged or printed.
- New user-facing sentences go in `lang.PHRASES` for all six tags; Egyptian Arabic is written as an
  Egyptian would say it, not as فصحى.

## Tests

- Tests first, in `tests/`, one behaviour each, named after what they prove.
- No test may need the network, a microphone, a speaker or a real key: put the device or service behind
  a small function and `monkeypatch` it. `tests/conftest.py` gives each test its own `RAFIQ_HOME` and
  clears the keys.
- `tests/test_docs.py` checks that every `rafiq` command, file and install extra named in these guides
  exists; update the guides when you rename one.

## Style

Match the surrounding code: `from __future__ import annotations`, type hints on public functions,
120-character lines, short docstrings that say why, measured numbers in comments where they exist.
Commit messages follow `git log --oneline` ("Wake word: learn the name as said before a request"),
with no AI signature lines. Every PR adds a change note `changelog.d/<issue>.<added|changed|fixed>.md`.
