[![CI](https://github.com/nexika/rafiq/actions/workflows/ci.yml/badge.svg)](https://github.com/nexika/rafiq/actions/workflows/ci.yml)

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
git clone https://github.com/nexika/rafiq.git && cd rafiq
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,google,offline-voice]"
.venv/bin/pip install --no-deps openwakeword   # optional, for a trained wake word later
.venv/bin/rafiq doctor
.venv/bin/rafiq enroll       # record "Rafiq" in your voice (about two minutes)
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

**Record your voice once** (about two minutes): `rafiq enroll` asks you to say "Rafiq" and "يا رفيق"
three times each, then to call Rafiq with any request three times ("Rafiq, what time is it?"), then
three ordinary sentences. Rafiq compares the start of everything it hears with your recordings
(log-mel frames matched with dynamic time warping) and sets its threshold between your recordings of
the name and your ordinary speech, so it fits your voice and microphone. Most speech that isn't the
name is dismissed in a few hundred milliseconds, without running Whisper.

The requests matter: a word said alone ends long and falling, but before a request it runs into the
next word. Rafiq cuts the name out of each request recording and keeps that too.

| Tested with synthetic voices, 3 random draws each | wakes on the name | wakes by mistake |
|---|---|---|
| English, name recordings only | 96% | 2% |
| English, with request recordings | **100%** | **2%** |
| Arabic, name recordings only | 50% | 5% |
| Arabic, with request recordings | **100%** | **5%** |

The mistakes came from look-alikes such as "Rafael" and "يا ريت". A real voice in a real room is the
test that counts: the panel shows each wake-up's score next to the threshold.

- Other forms: `rafiq enroll --forms "Rafiq" "Hey Rafiq" "يا رفيق"`.
- Wakes too rarely or too often: `rafiq enroll --sensitivity 0.6` (looser) or `0.3` (stricter) refits
  the saved recordings without recording again.
- It is tuned to one voice: enrolling again replaces the recordings.

**Without recordings**, a small Whisper model listens for the name: it caught 6 of 8 phrases and woke
once by mistake ("Rafael"). The Talk button and the keyboard shortcut always work.

## Presence: when your phone leaves or comes home

When your phone has been off the home Wi-Fi for 10 minutes, Rafiq plays the away scene; when it is
back, the welcome-home scene, and says "welcome home". "Rafiq, turn off presence" stops it (and it
stays off after a restart); "turn on presence" starts it again.

Rafiq looks for the phone every 30 seconds: a ping, and when that gets no answer, the computer's ARP
table, because a sleeping phone often ignores pings but still answers ARP. A phone that drops off for
a few minutes while asleep still counts as home. When Rafiq starts, it first learns where the phone
is, without playing a scene.

1. **Give the phone a fixed IP**: in your router, reserve an address for the phone (often called a
   DHCP reservation or static lease).
2. **Turn off the private (random) Wi-Fi address for your home network**, or the router may see a
   new phone: on Android, Wi-Fi → your network → Privacy → "Use device MAC"; on an iPhone, Wi-Fi →
   (i) → "Private Wi-Fi Address" off (or "Fixed" on iOS 18).
3. Set it in `~/.config/rafiq/config.toml`:

```toml
[presence]
phone_ip = "192.168.1.20"
phone_mac = "aa:bb:cc:dd:ee:ff"           # optional: only this phone counts at that address
away_command = ["/home/you/bin/scene", "away"]
home_command = ["/home/you/bin/scene", "home"]
```

The scenes are commands of your own for now (run as argument lists, never through a shell), so they
can drive any lights or plugs you already control from the terminal.

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
