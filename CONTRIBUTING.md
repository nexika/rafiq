# Contributing to Rafiq

*[النسخة العربية](CONTRIBUTING.ar.md)*

Thank you for helping. Rafiq is a voice assistant for the Linux desktop in English, French, Spanish,
German, Modern Standard Arabic and Egyptian Arabic. This page is what you need before a first pull
request. [CLAUDE.md](CLAUDE.md) has the same rules in short for AI coding assistants.

## Set up

```bash
git clone https://github.com/nexika/rafiq.git && cd rafiq
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,google,offline-voice]"
.venv/bin/ruff check . && .venv/bin/python -m pytest -q
```

The tests need no microphone, speaker, network or keys: you can run them anywhere, as CI does on
Python 3.11 to 3.14. To try Rafiq for real, see the [README](README.md) or
[INSTALL_WITH_AI.md](INSTALL_WITH_AI.md); `rafiq ask "..."` uses the same brain and tools without a
microphone.

## How it fits together

```
microphone → audio.py (speech detection) → kws.py / wake.py ("Rafiq"?) → stt.py (Whisper)
  → brain.py (Claude or Ollama) ⇄ tools/ → tts.py (voice) → speaker
                                          ↘ panel.py (127.0.0.1)
```

| File | What it does |
|---|---|
| `rafiq/__main__.py` | the `rafiq` command: run, ask, say, enroll, listen, doctor, google-setup |
| `rafiq/assistant.py` | the loop; everything that uses the microphone or speaker runs on its one thread |
| `rafiq/brain.py` | one turn with Claude or Ollama: the model asks for tools, Rafiq runs them, the model answers |
| `rafiq/tools/` | the skills; each is a function registered with `@tool(...)` |
| `rafiq/lang.py` | language tags, Egyptian or standard Arabic, yes or no, and Rafiq's own phrases |
| `rafiq/kws.py`, `rafiq/enroll.py` | the wake word from your own recordings |
| `rafiq/config.py` | settings in `~/.config/rafiq/config.toml`, with a default for everything |
| `rafiq/panel.py`, `rafiq/panel.html` | the local page |

## Adding a skill

A skill is a plain function in `rafiq/tools/` with a description and a JSON schema for its inputs:

```python
@tool("volume", "Get or change the speaker volume.",
      {"level": {"type": "integer", "description": "set the volume to this percent, 0 to 100"}})
def volume(level: int | None = None) -> str:
    ...
```

- Return a short string for the model; start it with `error:` when something went wrong.
- If it changes anything outside Rafiq (sends, deletes, buys, powers off), pass `confirm=` with the
  action in words the user hears ("shut down the computer"). Rafiq says it back and runs it only on a
  clear yes.
- Text from outside (web pages, emails, search results) is data for the model, never instructions.
- Run commands as argument lists with a timeout, never through a shell.
- Anything the user hears from Rafiq itself goes in `lang.PHRASES`, in all six languages, with
  Egyptian Arabic written as an Egyptian would say it.

## Tests

- Write the failing test first, in `tests/`, named after what it proves
  (`test_actions_that_change_something_are_confirmed_first`).
- Hardware, accounts and network services (microphone, speaker, Claude, Ollama, Azure, Google, the web)
  sit behind a small function or class that tests replace with `monkeypatch`. A test that needs the
  network or a device does not belong in the suite.
- `tests/conftest.py` gives every test its own settings folder and removes real keys.

## Style

- Match the code around you: `from __future__ import annotations`, type hints on public functions,
  lines up to 120 characters, `ruff check .` clean.
- Comments and docstrings say why, in plain words, and give measured numbers where there are some
  ("caught 6 of 8 phrases").
- Keep a change to one issue.

## Pull requests

1. Branch from `main`; never push to `main`.
2. Add a change note: a file `changelog.d/<issue>.<added|changed|fixed>.md` with one sentence saying
   what users can now do, for example `changelog.d/78.added.md`.
3. Commit messages are short, in the style of `git log --oneline`: "Wake word: learn the name as said
   before a request".
4. Open the pull request with what and why, how you tested it, and `Closes #<issue>`. The check
   "CI passed" must be green before it can merge.

Bugs and ideas go in [the issues](https://github.com/nexika/rafiq/issues). For a bug, include what
`rafiq doctor` prints.
