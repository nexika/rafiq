# Rafiq (رفيق)

A voice assistant for your Linux desktop that speaks English, French, Spanish, German, Modern Standard
Arabic and Egyptian Arabic. Say "Rafiq" (or "يا رفيق"), ask, and it answers out loud in the language
and dialect you used. *Rafiq* means companion.

```
microphone → speech detection → "Rafiq"? → Whisper → Claude or Ollama + tools → voice → speaker
                                                                   ↘ local panel (127.0.0.1)
```

## What it can do

| Skill | Examples |
|---|---|
| This computer | "open Firefox", "volume to 30", "mute", "how's the battery?", "lock the screen", "shut down" (asks first) |
| Everyday | "what time is it in Tokyo?", "set a timer for 10 minutes", "remind me tomorrow at 9 to call mum", "weather in Cairo", "15% of 240", "5 miles in km" |
| Web | "search for…", "summarise this page", "translate this article into Arabic", "open youtube.com" |
| Google | "what's on my calendar today?", "add lunch with Sara on Friday at 1" (asks first), "any new email?", "email Sara that I'm running late" (asks first) |

It answers Egyptian Arabic in Egyptian Arabic: "الساعة كام دلوقتي؟" gets a reply an Egyptian friend
would say, not formal فصحى. Timers and reminders are spoken in the language they were set in.

## Install

Linux with PipeWire (Fedora, Ubuntu 22.10+), Python 3.11 or newer, `ffmpeg`.

```bash
git clone <this repo> rafiq && cd rafiq
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,google,offline-voice]"
.venv/bin/pip install --no-deps openwakeword   # optional, for a trained wake word later
.venv/bin/rafiq doctor
```

The first start downloads the Whisper models (about 550 MB). Offline voices go in
`~/.config/rafiq/data/piper/` (see below).

## The brain: Claude, with Ollama as the fallback

- **Claude** (recommended): fast, good at tools and at all six languages. Set an API key from
  [console.anthropic.com](https://console.anthropic.com): `export ANTHROPIC_API_KEY=...`. The default
  model is `claude-haiku-5-5`, chosen for speed; change `brain.claude_model` for a stronger one.
- **Ollama**: free and private, used when there is no key or Claude can't be reached. On a laptop
  without a GPU, a 7B model takes about 10 to 20 seconds per answer, and it chooses tools and keeps
  the language less reliably than Claude. Rafiq warms the model up when it starts, so only the first
  answer is slow.

`rafiq run --brain claude|ollama|auto` picks one for a session; `brain.mode` in the config sets it.

## The voice

| Engine | Languages | Needs |
|---|---|---|
| **Azure AI Speech** (default when a key is set) | all six, Egyptian voices `ar-EG-ShakirNeural` and `ar-EG-SalmaNeural` | `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`; the free tier covers 500,000 characters a month |
| **Piper** (offline) | en, fr, es, de, ar; no Egyptian voice, so Egyptian is read by the Jordanian Arabic voice | voice files in `~/.config/rafiq/data/piper/` |
| Edge read-aloud | the same voices as Azure | nothing, but it is unofficial and Microsoft blocked it in October 2026 |

Download the Piper voices once:

```bash
cd ~/.config/rafiq/data/piper && ~/rafiq/.venv/bin/python -m piper.download_voices \
  en_US-ryan-medium fr_FR-tom-medium es_ES-davefx-medium de_DE-thorsten-medium ar_JO-kareem-medium
```

## Use it

```bash
rafiq run                 # listen for "Rafiq"; the panel opens at http://127.0.0.1:8765/
rafiq ask "weather in Paris tomorrow"     # type instead of talking (add --speak to hear it)
rafiq say --lang ar-EG "أهلًا، أنا رفيق"  # try a voice
rafiq listen              # make the running Rafiq listen now
```

**A keyboard shortcut** is the most reliable way to call it: in GNOME, Settings → Keyboard → Custom
Shortcuts, command `/path/to/rafiq/.venv/bin/rafiq listen`, keys such as Super+R.

The **panel** shows what Rafiq heard, said and which tools it used, with a Talk button, a text box,
and Yes/No buttons when it asks before an action.

## The wake word

Rafiq listens for its name with a small Whisper model on the first 2.5 seconds of each thing it hears.
In tests with synthetic speech in the six languages, it caught the name in 6 of 8 phrases and woke once
by mistake (on "Rafael"). Push-to-talk and the keyboard shortcut always work. A trained wake-word
model would be much more accurate: [Picovoice Porcupine](https://picovoice.ai/) makes one for "Hey
Rafiq" in a minute (free for personal use), or train an [openWakeWord](https://github.com/dscripka/openWakeWord)
model on a machine with a GPU.

## Safety

- Anything that changes something outside Rafiq (power off, send an email, add an event) is said
  back to you and runs only on a clear "yes" (or the panel's Yes button). A "no" anywhere in the
  answer cancels.
- Web pages, search results and emails are passed to the model as data, never as instructions.
  Pages on this computer or the local network (your router, Rafiq's own panel) are never fetched.
- Commands run as argument lists, never through a shell. The calculator only does arithmetic.
- The panel listens on 127.0.0.1 only, and every request must carry a random token that only the
  panel page knows; requests under another host name are refused.
- Keys are read from the environment or `~/.config/rafiq/config.toml` and never logged. The Google
  token is stored readable only by you.

## Google calendar and Gmail

1. In a [Google Cloud project](https://console.cloud.google.com/), enable the Calendar and Gmail APIs,
   and create an OAuth client of type **Desktop app**.
2. Save its JSON as `~/.config/rafiq/google/credentials.json`.
3. Run `rafiq google-setup` and approve in the browser.
4. Set `enabled = true` under `[google]` in the config.

## Settings

`~/.config/rafiq/config.toml` (`RAFIQ_HOME` moves the folder). See `config.example.toml`; everything is
optional.

## Development

```bash
.venv/bin/ruff check . && .venv/bin/python -m pytest -q
```

## License

MIT
