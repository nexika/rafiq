[![CI](https://github.com/nexika/rafiq/actions/workflows/ci.yml/badge.svg)](https://github.com/nexika/rafiq/actions/workflows/ci.yml)

# Rafiq (رفيق)

A voice assistant for your Linux desktop that speaks English, French, Spanish, German, Modern Standard
Arabic and Egyptian Arabic. Say "Rafiq" (or "يا رفيق"), ask, and it answers out loud in the language
and dialect you used. *Rafiq* means companion.

```
microphone → speech detection → "Rafiq"? → Whisper → Claude, OpenAI or Ollama + tools → voice → speaker
                                                                           ↘ local panel (127.0.0.1)
```

## What it can do

| Skill | Examples |
|---|---|
| This computer | "open Firefox", "launch Borderlands", "add an app called RetroArch" (asks first), "volume to 30", "mute", "pause", "next song", "what is playing?", "switch to the headphones", "how's the battery?", "how's the GPU?", "lock the screen", "shut down" (asks first), "wake the gaming PC" (asks first, [setup](docs/wake-on-lan.md)) |
| Hand gestures | open palm pauses, thumbs-up plays, a fist stops Rafiq talking, swipes change tabs or the volume (see below) |
| Everyday | "what time is it in Tokyo?", "set a timer for 10 minutes", "remind me tomorrow at 9 to call mum", "weather in Cairo", "what's the weather?" (where you are), "15% of 240", "5 miles in km" |
| Notes | "note: buy batteries", "idea: a lamp that follows the sun", "give me a random idea from my notes", "what did I note today?" (Markdown files in `~/.config/rafiq/data/notes`, or `[notes] folder` such as an Obsidian vault) |
| The screen | "what's this error?", "read this", "translate this into Arabic" (asks first: the screenshot goes to Claude) |
| Web | "search for…", "summarise this page", "translate this article into Arabic", "open youtube.com" |
| Browser tabs | "what tabs are open?", "summarise this tab", "translate the Gmail tab into Arabic", "switch to the YouTube tab", in Rafiq's own Chrome or Firefox (see below) |
| Learning | With Nexika's prof plugin for Claude Code installed, Rafiq explains the screen, a video or anything else at the level in your prof profile (beginner, junior or intermediate), and, after your yes ("save that to my notes"), records what was explained so prof can review and quiz you later. It only reads prof's profile and asks prof's own helper; your learner data stays out of the logs |
| Videos | "explain this video" or "what is this lecture about?" while it plays on the computer: Rafiq listens to the computer's sound, transcribes it here with Whisper, and explains it when it ends or when you say stop (at most 20 minutes, `[video] max_minutes`). The sound never leaves the computer; only the transcript goes to the brain. Needs `pulseaudio-utils` |
| Songs | "what's this song?" (listens to the room for 8 seconds), "what's playing on my computer?", "what song is ~/Music/track.mp3?", "which songs did you recognise?" (needs an [AudD](https://audd.io) token in `AUDD_API_TOKEN`, and `pulseaudio-utils` for the computer's sound) |
| Clipboard | "translate what I copied into Arabic", "summarise my clipboard", "explain this error", "copy that" (needs `wl-clipboard` on Wayland, `xclip` on X11) |
| Language practice | "let's practise French, level A2, about food": a simple conversation with short spoken corrections (the full list in the panel, slower voice for A1 and A2), then "let's stop" lists what to work on; common mistakes are kept in `data/practice.json` |
| Read aloud | "read this to me" (the selected text), "read the clipboard", "read this article aloud", "read ~/paper.pdf"; each sentence in its language's voice, pause, skip and stop from the panel |
| Your project | "run the tests", "what's my git status?", "why did CI fail?", "what's on my PR?", through Nexika's barq, tabib and mizan when they are on the PATH; set the project folder in the panel or as `[dev] project` |
| Interpreter | "interpret between me and her, Egyptian and German", "ترجم بيني وبينه بالمصري والفرنساوي": each turn is said in the other language until someone says "stop" or "كفاية" |
| Money | "import my bank statement from Downloads", "put Uber under transport", "how much did I spend in September?", "what subscriptions am I paying for?" (all on this computer; amounts and payees stay out of the logs) |
| Content | "add a video idea: desk tour, out by the 20th", "the desk tour is scripted", "what videos are late?" (idea → script → shoot → edit → published) |
| Scenes | "film mode", "turn everything off" (offers to shut down), a morning scene at the first start of the day: your own lists of steps in the config, named in any language |
| Home | "which lights are on?", "turn off the coffee plug" (asks first), through Home Assistant |
| Video lights | "key light to 60%, 5600 K", "fill off", "all the lights at 30%", over DMX through an Art-Net node (see below) |
| Background tasks | "research the best e-bike under 2000 euros" (Rafiq says it is on it and reads the answer aloud when it is ready), "how are my tasks going?", "cancel the e-bike research" (or Cancel in the panel); needs Claude, and the panel shows what each task cost |
| Memory | "remember that my sister is called Sara", "what do you know about my project?", "forget that" |
| Style | "be more concise", "talk like a butler", "back to normal": neutral, concise, or a dry, witty butler (also in the panel) |
| Daily brief | "brief me", "good morning": the time, weather, today's calendar, new email and what is due today in one answer; `brief.at_first_start = true` says it at the first start of each day |
| Morning paper | "print my paper" (asks first, never twice a day) |
| Prayer (off until `prayer.enabled = true`) | "when is maghrib?", "what's the Hijri date?", "how long until iftar?", "recite Surat Al-Mulk", "play Al-Baqarah verses 1 to 5 by Husary", "stop the recitation" |
| Google | "what's on my calendar today?", "add lunch with Sara on Friday at 1" (asks first), "any new email?", "email Sara that I'm running late", "reply to Sara that I'll be late" (reads the draft back, asks first), "delete that email" (to the trash, asks first), "where are my parcels?" |
| Instagram | "how are my Instagram accounts doing?", "how many followers did studio gain since yesterday?" |
| Discord | "anything on Discord?": your mentions from the last 24 hours and today's messages |
| Smart plugs | "turn on the desk lamp", "how much is the PC drawing?", "turn off the PC plug" (asks first when you mark it so) |
| OBS Studio | "start recording" (asks first), "go live" (asks first), "switch to the camera scene", "clip that" |

It answers Egyptian Arabic in Egyptian Arabic: "الساعة كام دلوقتي؟" gets a reply an Egyptian friend
would say, not formal فصحى. Timers and reminders are spoken in the language they were set in.
English words inside Arabic work too: "افتحلي الـ settings", "شغّل الـ playlist بتاعتي" or
"ابعت email لسارة" reach the brain with the English words in Latin letters. To see how well Whisper
hears your own mixed speech, record sentences as `name.wav` with their text in `name.txt` and run
`rafiq stt-score <folder>` (it compares the small and medium models, with and without this handling).

## Install

Linux with PipeWire (Fedora, Ubuntu 22.10+), Python 3.11 or newer, `ffmpeg`.

New to this? [INSTALL_WITH_AI.md](INSTALL_WITH_AI.md) ([بالعربية](INSTALL_WITH_AI.ar.md)) is a prompt
to paste into any chatbot, which then walks you through the install one command at a time.

```bash
git clone https://github.com/nexika/rafiq.git && cd rafiq
./install.sh
```

`install.sh` creates `.venv`, installs Rafiq with the offline voices and Google, links
`~/.local/bin/rafiq` so `rafiq` works from any folder (it says so if `~/.local/bin` is not on your PATH
yet), then starts `rafiq setup`, a wizard that:

1. asks your name, the brain (auto, Claude or Ollama), the Claude and Azure keys (both optional, typed
   hidden) and your city, and writes `~/.config/rafiq/config.toml`, readable only by you;
2. downloads the offline Piper voices, the Whisper models (about 550 MB) and the Ollama model;
3. tests the microphone level, then runs `rafiq doctor`;
4. records the wake word (`rafiq enroll`, about two minutes) and greets you out loud.

A step that fails is reported and the rest goes on; run `rafiq setup` again at any time (Enter keeps
what you chose before). `./install.sh --no-setup` installs only; `rafiq setup --no-enroll` skips the
recording. By hand: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev,google,offline-voice]"`.

Optional extras: `.venv/bin/pip install --no-deps openwakeword` for a trained wake word later, and
`.venv/bin/pip install -e ".[nvidia]"` for GPU figures on an NVIDIA card (AMD needs nothing).

`rafiq doctor` lists your microphones; pick one with `[audio] device` in the settings. A microphone
that refuses 16 kHz is opened at a rate it takes and resampled. At start-up Rafiq listens to the room
for a second and fits its speech thresholds to the noise, so stay quiet for a moment
(`[audio] auto_calibration = false` turns this off).

Offline voices go in `~/.config/rafiq/data/piper/` (see below).

`rafiq backup` saves your settings, keys and recordings to
`~/.config/rafiq/backups/rafiq-backup-<date>.tar.gz` (or `--to <folder>`), readable only by you. It never
uploads anything: the archive holds your keys, so keep it out of public places.

### Start at login

`rafiq install-service` makes Rafiq a systemd user service: it starts when you log in, restarts if
it fails, and logs to the journal (`journalctl --user -u rafiq -f`). Running it again keeps a single
Rafiq. The service does not see keys exported in your shell, so put them in
`~/.config/rafiq/rafiq.env` as `ANTHROPIC_API_KEY=...` lines. `rafiq uninstall-service` removes it.

## The brain: Claude, with Ollama as the fallback

- **Claude** (recommended): fast, good at tools and at all six languages. Set an API key from
  [console.anthropic.com](https://console.anthropic.com): `export ANTHROPIC_API_KEY=...`. The default
  model is `claude-haiku-5-5`, chosen for speed; change `brain.claude_model` for a stronger one.
- **OpenAI**: `export OPENAI_API_KEY=...`; the default model is `gpt-5-mini` (`brain.openai_model`).
  Rafiq uses OpenAI's chat completions, so `brain.openai_url` can also point at another server that
  speaks it (DeepSeek, Gemini's OpenAI endpoint, a local one), with that server's key.
- **Ollama**: free and private, used when there is no key or the others can't be reached. On a laptop
  without a GPU, a 7B model takes about 10 to 20 seconds per answer, and it chooses tools and keeps
  the language less reliably than Claude. Rafiq warms the model up when it starts, so only the first
  answer is slow. It gets a smaller set of tools than Claude (fewer choices, fewer wrong ones;
  `brain.ollama_tools = "all"` sends every tool), and a tool call with broken arguments is sent back
  to it once to fix. `rafiq toolcheck` measures how often it picks the right tool, with every tool
  and with its set.

In auto mode Rafiq tries Claude, then OpenAI (each only when its key is set), then Ollama.
`rafiq run --brain claude|openai|ollama|auto` picks one for a session; `brain.mode` in the config
sets it. In the panel, the Brain menu lists the models your key can really use, asked of the
provider itself, and switches to one after asking again; the choice lasts until Rafiq stops.

### Modes: everyday, quality, offline

- **Everyday** (the default): the fast model, `brain.claude_model`.
- **Quality**: the strongest model, `brain.quality_model` (`claude-opus-5-5`), slower and costlier.
- **Offline**: nothing leaves the computer: only Ollama and the Piper voices, whatever `brain.mode`
  and `voice.engine` say. Speech-to-text is always local.

Switch by voice ("Rafiq, switch to offline mode", "what mode are you in?"), with the switch at the
top of the panel, or for a session with `rafiq run --mode quality`. `brain.profile` in the config
sets the mode Rafiq starts in; a switch lasts until Rafiq stops.

### Modes: everyday, quality, offline

- **Everyday** (the default): the fast model, `brain.claude_model`.
- **Quality**: the strongest model, `brain.quality_model` (`claude-opus-5-5`), slower and costlier.
- **Offline**: nothing leaves the computer: only Ollama and the Piper voices, whatever `brain.mode`
  and `voice.engine` say. Speech-to-text is always local.

Switch by voice ("Rafiq, switch to offline mode", "what mode are you in?"), with the switch at the
top of the panel, or for a session with `rafiq run --mode quality`. `brain.profile` in the config
sets the mode Rafiq starts in; a switch lasts until Rafiq stops.

## The voice

| Engine | Languages | Needs |
|---|---|---|
| **Azure AI Speech** (default when a key is set) | all six, Egyptian voices `ar-EG-ShakirNeural` and `ar-EG-SalmaNeural` | `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`; the free tier covers 500,000 characters a month |
| **ElevenLabs** (next when a key is set) | all six through one multilingual voice (`eleven_flash_v2_5`); it speaks Arabic but not in a chosen dialect, so pick an Egyptian voice from the Voice Library for `ar-EG` in `[voice.elevenlabs_voices]` | `ELEVENLABS_API_KEY` |
| **Kokoro** (offline, next when its files are there) | en, fr, es only (Kokoro has no Arabic or German, so those go to Piper) | `pip install -e ".[kokoro]"` and `kokoro-v1.0.onnx`, `voices-v1.0.bin` in `~/.config/rafiq/data/kokoro/` |
| **Piper** (offline) | en, fr, es, de, ar; no Egyptian voice, so Egyptian is read by the Jordanian Arabic voice | voice files in `~/.config/rafiq/data/piper/` |
| Edge read-aloud | the same voices as Azure | nothing, but it is unofficial and Microsoft blocked it in October 2026 |

`voice.engine` picks one engine (`azure`, `elevenlabs`, `kokoro`, `piper`, `edge`); Piper is always
tried after it. The Kokoro files are on the
[kokoro-onnx releases page](https://github.com/thewh1teagle/kokoro-onnx/releases) (model-files-v1.0).

`rafiq setup` downloads the Piper voices; by hand:

```bash
cd ~/.config/rafiq/data/piper && ~/rafiq/.venv/bin/python -m piper.download_voices \
  en_US-ryan-medium fr_FR-tom-medium es_ES-davefx-medium de_DE-thorsten-medium ar_JO-kareem-medium
```

Rafiq starts talking with the first sentence of an answer while Claude is still writing the rest, and
says a short "let me look that up" before a slow tool (the web, the weather, Gmail, the calendar). How
long each stage took (speech-to-text, model, tools, voice, first sound) goes to
`~/.config/rafiq/logs/timing.log`, without what was said; `rafiq doctor` shows the medians.

## Hearing: Whisper, with the cloud for chosen languages

Whisper runs on your computer and hears every language by default. For a language it hears badly,
send that language to a cloud service; Whisper still tells the language first, on your computer:

```toml
[speech.stt_by_language]
ar = "azure"      # Azure's Egyptian Arabic recogniser (ar-EG), with the voice's AZURE_SPEECH_KEY
```

| Service | Needs |
|---|---|
| **Azure AI Speech** | `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` (the same key as the voice); `speech.azure_locales` picks the recogniser |
| **OpenAI** (`gpt-4o-mini-transcribe`) | `OPENAI_API_KEY` |

When Whisper can't run, a cloud service with a key hears everything; when the cloud can't be
reached, Whisper does. The name "Rafiq" is always heard on your computer. `speech.stt_engine = "local"`
or `speech.offline = true` keeps what you say on your computer.

## Use it

```bash
rafiq run                 # listen for "Rafiq"
rafiq panel               # open its panel in the browser (a one-time link; --print to copy it)
rafiq ask "weather in Paris tomorrow"     # type instead of talking (add --speak to hear it)
rafiq say --lang ar-EG "أهلًا، أنا رفيق"  # try a voice
rafiq listen              # make the running Rafiq listen now
rafiq wake-pc aa:bb:cc:dd:ee:ff           # start a computer that is off (Wake-on-LAN must be on)
rafiq mute                # stop listening, or start again (rafiq mute on / off to choose)
rafiq show                # show the last answer on screen again
rafiq dictate             # start (or stop) typing what you say into the focused window
```

While `rafiq run` is up, `rafiq ask` hands the request to it, so both share the same browser and tools.
It answers by itself when nothing is running, or when you choose `--brain` or `--mode`.

**Dictation**: say "Rafiq, dictate" (or "dictée", "dicta", "Diktat", "إملاء"), press the panel's
Dictate button, or bind `rafiq dictate` to a shortcut. Each thing you say is typed into the focused
window, in any of the six languages, with spoken punctuation ("comma", "question mark", "new line";
"virgule", "coma", "Komma", "فاصلة", "علامة استفهام", "سطر جديد"). Say "stop dictating" ("arrête la
dictée", "deja de dictar", "Diktat beenden", "أوقف الإملاء", "بطل إملاء") or press the shortcut again
to end. The panel turns amber while dictating. Typing needs `wtype` or `ydotool` with `wl-clipboard`
on Wayland (on GNOME, ydotool, with its `ydotoold` service running), or `xdotool` and `xclip` on
X11. ydotool cannot type Arabic or accented letters, so that text is pasted through the clipboard,
which then holds it; `rafiq doctor` shows which tools it found.

**A keyboard shortcut** is the most reliable way to call it: in GNOME, Settings → Keyboard → Custom
Shortcuts, command `/path/to/rafiq/.venv/bin/rafiq listen`, keys such as Super+R. Add a second one
for `rafiq mute`, such as Ctrl+Alt+M (GNOME on Wayland does not let apps grab keys themselves). While
muted Rafiq ignores the microphone, even its name; the Talk button and `rafiq listen` still work once.

Say **"go to sleep"** ("نام", "mets-toi en veille") to end a conversation at once: Rafiq stops waiting
for a follow-up and drops a question it was asking before an action.

**To interrupt Rafiq**, say "stop", "attends", "espera", "warte", "توقف" or "استنى" while it speaks.
The microphone stays open: you must be about 1.8 times louder than Rafiq's own voice (`[interruption]
factor`). With PipeWire, `pactl load-module module-echo-cancel` removes most of that echo and Rafiq
listens through it on its own. `enabled = false` mutes the microphone while Rafiq speaks, as before.

The **panel** shows what Rafiq heard, said and which tools it used, with a Talk button, a text box,
Yes/No buttons when it asks before an action, and the choice of style. While it reads aloud, it shows Pause/Resume, Skip and
Stop. The microphone is off while Rafiq speaks, so a reading is paused from the panel; "Rafiq, resume
reading" or "stop reading" work once it is paused. The selection needs `wl-clipboard` (Wayland) or
`xclip`/`xsel` (X11); PDFs need `pip install 'rafiq[pdf]'`. Its other pages:

- **Models**: the Ollama models with what fits this computer's memory; install, benchmark, activate
  and delete them, and pick the Whisper model that hears you. Choices are kept in
  `~/.config/rafiq/choices.json` and win over `config.toml`.
- **Status**: whether each part is up: Ollama, Claude, Whisper, the voice, Google, satellites.
- **Permissions**: whether each tool runs freely or asks first, and the approvals you remembered
  with "Always", each with a Revoke button.


Its **Subscriptions** page (the link at the top of the panel) lists what you pay for again and again: monthly and yearly totals per currency, the charges of the next 30 days, alerts when one
charges tomorrow or its amount changed, and a split by category. They are kept in
`~/.config/rafiq/data/subscriptions.json` and reach the model only when you ask ("how much do my
subscriptions cost?"). With Google set up, "Look for receipts in Gmail" searches recent receipts and
proposes what it finds; each one is merged only when you press Merge, and a new price for a known
subscription updates it and raises the "amount changed" alert.

**On screen**, each answer also appears as a desktop notification, with a line for the weather, a
timer or music when the answer used one. It never takes the focus, and the next answer replaces it.
Say "show it again" (or run `rafiq show`) to bring back the last one. With `silent = true` under
`[overlay]` Rafiq answers on screen only; `display = "off"` turns the notifications off.

**Spoken notifications** tell you when a long job ends, so you can stop watching terminals:

```bash
rafiq notify --done "the benchmark"   # "Finished: the benchmark", in the language you last spoke
make test; rafiq notify --failed tests
rafiq mute                            # muted: shown in the panel, not spoken
rafiq hooks claude-code               # a Claude Code Stop hook ("Finished: Claude Code")
rafiq hooks shell                     # `announce <command>` and `watch-ci` (GitHub Actions) for your shell
```

Set `notify.quiet_hours = "22:00-07:00"` in the config to keep them silent at night.

**What did you hear?** Rafiq keeps what you asked and what it answered in
`~/.config/rafiq/data/history.json` (readable only by you, on this computer only), for 7 days by
default (`history.retention_days`; 0 keeps nothing). Only requests are kept: what followed the name, a
follow-up, or what you typed; the listening for the name is never written down. The panel's
**History** page searches it and deletes one entry or all of them. Say "forget what I just said"
("oublie ça", "vergiss das", "انسى اللي قلته"...) to delete the last one by voice; Rafiq handles that
itself, so it never reaches a model. A badge at the top of the panel says whether requests leave this
computer right now: Claude, Azure or Edge voices, or fully offline with Ollama and Piper.

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
- Another name: set `[wake] names = ["Jarvis"]` in the config, then run `rafiq enroll` again. Rafiq
  answers to it as well as "Rafiq", with or without recordings. Pick a name of five letters or more
  that sounds unlike everyday words.
- Wakes too rarely or too often: `rafiq enroll --sensitivity 0.6` (looser) or `0.3` (stricter) refits
  the saved recordings without recording again.
- It is tuned to one voice: enrolling again replaces the recordings.

**Without recordings**, a small Whisper model listens for the name: it caught 6 of 8 phrases and woke
once by mistake ("Rafael"). The Talk button and the keyboard shortcut always work.

## Prayer times, the Hijri date and the Quran

Off by default: set `enabled = true` under `[prayer]` (see `config.example.toml`).

- **Prayer times** are calculated on this computer from the sun's position, for `prayer.latitude` and
  `prayer.longitude` (or the city in `[place]`), with the method you choose: Egyptian General
  Authority of Survey (the default), Umm al-Qura, Muslim World League, ISNA, Karachi, Dubai, Kuwait,
  Qatar, Singapore or France, and the standard or Hanafi asr. Rafiq always names the method it used.
  They agree with aladhan.com to the minute for Cairo and Mecca; if your mosque differs, move any
  prayer by minutes under `[prayer.adjust]`.
- **Reminders**: none until you set them, e.g. `fajr = 10` under `[prayer.reminders]` for ten minutes
  before fajr, said in `prayer.language`.
- **The Hijri date** follows the Umm al-Qura calendar (2018 to 2039, the tabular calendar outside it).
  Where the month starts with the moon sighting, `prayer.hijri_adjust = 1` or `-1` moves it a day.
- **Ramadan**: during Ramadan, prayer times come with the time left until iftar and until suhoor ends.
- **Recitation** streams verse by verse from [EveryAyah](https://everyayah.com), which allows it;
  nothing is saved. Reciters: alafasy (default), abdul_basit, husary, minshawi, sudais, muaiqly.
## Hand gestures

With a webcam, held hand gestures control the computer:

| Gesture | Does |
|---|---|
| Open palm, held | pause what is playing |
| Thumbs-up, held | play |
| Fist, held | stop Rafiq talking |
| One finger, then swipe left or right | previous or next tab (tab mode lasts 5 seconds) |
| Two fingers, then swipe left or right | volume down or up |
| Two hands moving apart or together | zoom in or out |

```bash
.venv/bin/pip install -e ".[gestures]"   # MediaPipe and OpenCV
rafiq gestures demo        # the camera with the hand landmarks and the gesture it sees
rafiq gestures calibrate   # the same, with sliders for how long to hold and the pause between gestures; s saves
rafiq gestures on          # in the running Rafiq (bind to a shortcut); "Rafiq, turn on hand gestures" works too
```

`enabled = true` under `[gestures]` turns them on when Rafiq starts. The camera runs in its own
process, which sends Rafiq only the name of each gesture, never an image, with a token made each
time it starts; only the actions above can happen. Media keys go through `playerctl`; tabs and zoom
press keys with `ydotool` on Wayland (its `ydotoold` service must run) or `xdotool` on X11.

## Satellites: another room

A small computer with a microphone and a speaker (a Raspberry Pi in the kitchen) can run Rafiq as a
**satellite**: it hears "Rafiq" itself (with `rafiq enroll` recordings made on that device, or a small
Whisper model), sends the request to the main computer, and speaks the answer. The main computer knows
the room, so "turn on the light" said in the kitchen means the kitchen's.

1. On the main computer, `rafiq satellite token --name kitchen` prints a block for its config:
   ```toml
   [satellites]
   host = "192.168.1.10"           # this computer's address on your home network (default 127.0.0.1)

   [satellites.devices.kitchen]
   token = "..."
   room = "kitchen"
   risky = "ask"                   # "block": no actions that need a yes from this room
   blocked_tools = ["send_email"]  # tools this room may not use at all
   ```
   `rafiq run` then also listens for satellites on port 8766.
2. On the satellite, install Rafiq and set:
   ```toml
   [satellite]
   hub = "192.168.1.10:8766"
   name = "kitchen"
   token = "..."                   # the same token
   ```
   and run `rafiq satellite run`.
3. Without any hardware, `rafiq satellite simulate request.wav` sends a 16 kHz mono recording as if it
   were said to a satellite, and prints what was heard, the answer and how long it took.

Each satellite has its own token, compared in constant time. Requests travel as plain TCP unless you
set `tls_cert` and `tls_key` under `[satellites]` (and `tls = true`, with `tls_ca` for a self-signed
certificate, under `[satellite]`): keep satellites on a private network otherwise. Actions that need a
yes are asked on the satellite and answered there.

**On a Raspberry Pi**, [docs/satellite.md](docs/satellite.md) covers the install, the speakerphone,
tuning the wake word (`rafiq satellite scores`), running it as a service at boot (`rafiq satellite
unit`) and sharing the speaker with a Spotify receiver. A satellite the hub refuses stops with exit
code 2, so the service doesn't retry it forever.

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

## The brain on another machine

The brain (models, keys, confirmations, alarms) can run on an always-on machine, with your PC lending
it the screen, the speakers and the apps:

```bash
# on the always-on machine
rafiq agent-token desk               # prints the token for the PC called "desk", once
rafiq run --server                   # [server] host and port in the config; default 127.0.0.1:8766

# on the PC
export RAFIQ_AGENT_TOKEN=...
rafiq agent --server http://100.101.12.3:8766 --name desk --speak
```

The agent only connects outward (the PC opens no port), asks the server for the next action and runs
only the ones on its list: open an app, list apps, volume, system status, lock the screen, power
(still asked first, on the PC), open a web address, say something, ask yes or no. Each PC has its own
token, and the server keeps only its hash (`rafiq agent-token desk --remove` cuts that PC off). The
token travels over TLS (`[server] tls_cert` and `tls_key`) or a private network: without TLS the
server only listens on a private address and the agent only connects to one, such as Tailscale.

For now you type requests in the agent's terminal; the microphone stays on the PC in a later step.

## Safety

- Anything that changes something outside Rafiq (power off, send an email, add an event) is said
  back to you and runs only on a clear "yes" (or the panel's Yes button). A "no" anywhere in the
  answer cancels. The panel's Always button, or "yes, always" ("oui, toujours", "sí, siempre", "ja,
  immer", "نعم، دائمًا", "أيوه على طول"), stops the asking for that one tool until you revoke it on the
  Permissions page. Shutting down, sending, answering or deleting mail and deleting an event ask every
  time. With a voice print (`rafiq voiceid`), a spoken yes must also be in your voice.
- "Shut down" then waits 30 seconds and warns you 10 seconds before; "cancel the shutdown" stops it
  (`[pc] shutdown_delay` changes the wait, 0 shuts down at once).
- Web pages, search results, emails, Discord messages and the clipboard are passed to the model as data,
  never as instructions. The clipboard is read only when you ask about what you copied. Pages on this computer or the local network (your router, Rafiq's own panel) are never fetched.
  Once Rafiq has read something private in a request (a tab, an email, the clipboard), an address it would
  fetch or open, or a search, that isn't in what you said is read back and runs only on your yes.
- Rafiq's browser is a separate Chrome or Firefox profile with a debugging port on 127.0.0.1 only. Rafiq
  talks only to the port its own profile names, once the process there is your account's browser with
  that profile, and only runs fixed expressions that read the page's title and text, out of the page's own
  reach. Tab titles and page text reach the model as data, never as instructions, stay out of the log, and
  never come with a tool that fetches an address. While that browser is open, any program on the
  computer, under any account, could drive it through the port: that is how these protocols work. Sign in there
  only to the accounts you choose, and don't use it on a computer other people log into. Reading your own
  open tab fetches nothing, so it may show a page on your network.
- A question about the screen sends a screenshot of the whole screen to Claude, in the cloud, so Rafiq
  asks first (`screen.ask_first`). Offline it never leaves the computer: only a local vision model set
  in `screen.ollama_model` may see it. On GNOME Wayland the desktop also asks you to allow screenshots.
- "What's this song?" sends a few seconds of sound to AudD: 8 seconds from the room or from what the
  computer plays, or 12 seconds decoded from a file, never the whole file, and never Rafiq's own
  recordings of your voice. Rafiq finishes speaking before it listens, stops if you mute it, sends
  nothing when muted, offline or asked from a satellite, and keeps the last 20 songs in
  `~/.config/rafiq/data/songs.json`.
- What you ask Rafiq to remember is kept in `~/.config/rafiq/data/memory.json`, a plain list you can
  read and edit; "forget" deletes it from the file. Remembered facts go to the model as data with
  each request, never as instructions.
- "What's the weather?" with no city set asks GNOME's location service, at city accuracy. Your IP
  is sent to a lookup service (ipapi.co) only if you set `ip_lookup = true` under `[place]`.
- Commands run as argument lists, never through a shell. The calculator only does arithmetic.
  The project tools only read (tests, git status, CI, PR); the model picks the question, never the command.
- The panel listens on 127.0.0.1 only, and every request must carry a random token that only the
  panel page knows; requests under another host name are refused.
- The phone bridge is off until you turn it on, needs its own token, runs only safe tools, and listens
  only on a private address (this computer, the LAN or Tailscale), never on every network.
- Keys are read from the environment or `~/.config/rafiq/config.toml` and never logged. The Google
  token is stored readable only by you.
- The log (`~/.config/rafiq/data/logs/rafiq.log`) has email addresses, keys, phone numbers and
  amounts replaced by a mark, and is deleted after 30 days (`[privacy] log_days`). Results from
  background tasks are redacted the same way before they are said aloud.

## Always on

Rafiq can run day and night on this PC, a mini-PC, a NAS, a Raspberry Pi or a VPS, as a systemd
service. [docs/hosting.md](docs/hosting.md) explains where to run it and how to keep its panel
private. On the server, `rafiq status` says whether it is running and safe, and `rafiq update` pulls
the new version, reinstalls it and restarts the service.
## From your phone

Dictate a note, share a link or send a command ("Hey Siri, tell Rafiq: what's on my shopping list?").

1. Install [Tailscale](https://tailscale.com) on this computer (`sudo tailscale up`) and on the phone,
   signed in to the same account. The phone then reaches the computer from anywhere, and nothing is
   opened to the internet.
2. Under `[phone]` in the config, set `enabled = true` and `host = "tailscale"`, then restart `rafiq run`.
3. Run `rafiq phone`: it prints the address (this computer's Tailscale address) and the token for the
   shortcut.
4. On an iPhone, make a Shortcut with **Dictate Text**, then **Get Contents of URL** (method POST,
   header `Authorization: Bearer <token>`, JSON body `text` = the dictated text), then **Speak Text**
   with the reply's `answer`. On Android, do the same with HTTP Shortcuts or Tasker.

The JSON body is `{"text": "..."}` for a command, `{"kind": "note", "list": "shopping", "text": "milk"}`
for a note (lists: `ideas`, `shopping`, `tasks`; ideas by default), or
`{"kind": "share", "text": "...", "url": "https://..."}` for a shared link. At home, "what's on my
shopping list?" reads the notes back.

A command from the phone only runs safe tools: the time, weather, timers and reminders, notes,
calculations, web search and the computer's status. Anything else (power, apps, volume, email,
calendar) answers "do that at home by voice", and nothing from the phone is ever confirmed.

The bridge only listens on a private address: this computer (`127.0.0.1`, the default), Tailscale
(`"tailscale"`), or this computer's address on your home network (such as `"192.168.1.20"`, for a phone
on the same Wi-Fi). It refuses `0.0.0.0` and public addresses, and Rafiq then starts without it: the
token travels in plain HTTP, so only a private network may carry it. Public tunnels such as ngrok are
not supported.

## The widget

A small window that shows what Rafiq is doing, step by step: the state and the step running now, or, made
larger, the whole request with its plan, steps, explanations, cards and answer. It is a separate app built
with Tauri, in `widget/`, and connects to the running Rafiq on this computer only. You can type a request
in it, answer Rafiq's questions and press stop. When Rafiq looks at your screen, it shows the screenshot
with numbered circles, highlights and arrows on what its answer talks about. [widget/README.md](widget/README.md) says how to build it.

## Rafiq's tools in other programs (MCP)

`rafiq mcp` offers Rafiq's harmless tools to MCP clients such as Claude Desktop or Claude Code, over
stdio: the time, the weather, web search and reading a page, how the computer is doing, arithmetic and
unit conversion. Nothing that changes anything is offered (no power, email, apps, volume, or setting
timers), and never a tool that would ask you first. Every call is logged to
`~/.config/rafiq/data/mcp.log`.

Three more tools let the other program show its work in Rafiq's panel (and the widget, later):
`show_steps` shows its plan and how far it got, `show_card` shows music, news, weather or a timer,
and `explain` shows an explanation and reads it aloud with Rafiq's voice. They need `rafiq run` to be
running, and reach it through the panel with its token. Rafiq says first that another program is
speaking, and stays quiet while muted, in quiet hours or while it waits for your yes. Rafiq's own brain
gets them too, on requests that keep every tool.

```bash
claude mcp add rafiq -- /path/to/rafiq/.venv/bin/rafiq mcp
```

For Claude Desktop, add to `claude_desktop_config.json`:
`"mcpServers": {"rafiq": {"command": "/path/to/rafiq/.venv/bin/rafiq", "args": ["mcp"]}}`.

## Voice ID: only your voice confirms

After `rafiq enroll`, run `rafiq voiceid` once. It turns your recordings into a voice print (speaker
embeddings from WeSpeaker's ResNet34 model, about 26 MB, downloaded on first use and checked against
its checksum). From then on, a spoken yes to an action (power off, send an email, add an event) is
compared with your voice print. When Rafiq is not sure it was you, it says so and only the panel's
Yes button goes ahead. A no is taken from anyone.

`rafiq voiceid` reports how many of your own recordings it would refuse; add `--others DIR` with
16 kHz `.wav` files of other people (or of a video) to count false accepts too. `rafiq voiceid --off`
deletes the voice print. The panel shows each check's similarity next to the threshold.

Tested with five synthetic voices (Piper, one per language), each enrolled with ten recordings and
checked against the others' answers: no other voice was accepted (0 of 60). A single word ("yes",
"oui", "نعم") is too short to recognise and was never accepted (0 of 10); "yes, go ahead" was accepted
4 times in 5. Say a few words, or use the panel.

Voice ID can be fooled by a recording or a cloned voice: it is an extra check on top of the spoken
yes, never the only one.

## Google calendar and Gmail

1. In a [Google Cloud project](https://console.cloud.google.com/), enable the Calendar and Gmail APIs,
   and create an OAuth client of type **Desktop app**.
2. Save its JSON as `~/.config/rafiq/google/credentials.json`.
3. Run `rafiq google-setup` and approve in the browser.
4. Set `enabled = true` under `[google]` in the config.

Rafiq reads every calendar in your list, subscribed ones too, and adds or deletes events (after asking)
in your primary calendar only. A token made before Rafiq read the calendar list only sees the primary
calendar: run `rafiq google-setup` again to give it the others.

Replying and moving mail to the trash need the `gmail.modify` permission. If you connected Google
before Rafiq had them, run `rafiq google-setup` again.

## Spotify

"Play some jazz on Spotify", "next", "play it on the kitchen speaker", "add this song to my Road Trip
playlist". Playback control needs Spotify Premium.

1. Create an app in the [Spotify developer dashboard](https://developer.spotify.com/dashboard) with the
   redirect URI `http://127.0.0.1:8888/callback`.
2. Put its client ID under `[spotify] client_id` in the config (or in `SPOTIFY_CLIENT_ID`).
3. Run `rafiq spotify-setup` and approve in the browser. No client secret is needed; the token is
   stored readable only by you.
4. Set `enabled = true` under `[spotify]` in the config.

Adding a song to a playlist is asked first. With no playlist named, songs go to "Rafiq finds", which
Rafiq creates the first time.

## Discord

Rafiq reads Discord through a bot of your own, never your user account (self-bots are against
Discord's rules, and a user token is refused).

1. Create an application at the [Discord developer portal](https://discord.com/developers/applications),
   add a bot and turn on its **Message Content** intent.
2. Invite it to your servers with the **View Channels** and **Read Message History** permissions.
3. Put its token in `DISCORD_BOT_TOKEN` (or `bot_token` under `[discord]`), your own Discord user id in
   `user_id` (Developer Mode, then right-click your name, Copy User ID), and set `enabled = true`.

## Instagram statistics

Followers gained or lost, new posts and views on recent videos, compared with yesterday, for every
Instagram Business or Creator account linked to your Facebook Pages (official Instagram Graph API).

1. Create an app at [Meta for Developers](https://developers.facebook.com/) and set
   `INSTAGRAM_APP_ID` and `INSTAGRAM_APP_SECRET` (or `app_id` and `app_secret` under `[instagram]`).
2. In the Graph API Explorer, get a user token with `instagram_basic`, `instagram_manage_insights` and
   `pages_show_list`.
3. Run `rafiq instagram-setup <token>`: Rafiq swaps it for a long-lived token, refreshed by itself.
4. Set `enabled = true` under `[instagram]` in the config.

Each answer saves a snapshot of the day in `~/.config/rafiq/data/instagram.json` (kept 90 days); the
next day's answer compares with it.

## Home devices (Nest, Hue, plugs)

Rafiq controls home devices through [Home Assistant](https://www.home-assistant.io/), which already
knows Google Nest, Philips Hue, smart plugs and many other brands, so there is no Google Device Access
registration to pay for.

1. In Home Assistant, open your profile and create a **long-lived access token**.
2. Set `url` under `[home_assistant]` in the config (for example `http://homeassistant.local:8123`), and
   the token in `HOME_ASSISTANT_TOKEN` (or `token` in the config).

Then ask "which lights are on?" or "turn off the coffee plug" (asks first). Lights, switches and plugs,
fans, thermostats, media players and humidifiers can be switched on and off; locks, alarms and garage
doors never are.

## Philips Hue lights

"Turn on the living room", "kitchen at 30%", "make the bedroom blue". Rafiq talks to the Hue bridge on
your network directly; no Hue cloud account is used.

1. Run `rafiq hue-setup` (or `rafiq hue-setup --host 192.168.1.20` if it does not find the bridge) and
   press the round link button on the bridge.
2. Set `enabled = true` under `[hue]` in the config.

Rooms are the Hue app's rooms and zones, matched loosely. To say a room in another language than the
Hue app's, add it under `[hue.rooms]`, e.g. `"الصالة" = "Living room"`.

## Browser tabs

"What tabs are open?", "summarise this page", "translate the article tab into Arabic", "switch to the
YouTube tab". Rafiq reads the page as the browser shows it, so pages you are signed in to and pages built
by JavaScript work.

1. Install Google Chrome, Chromium or Firefox. Ubuntu's snap Chromium may refuse Rafiq's profile, which
   lives in a hidden folder; Google Chrome, Chromium on Fedora, or Firefox works.
2. Under `[browser]` in the config, set `enabled = true`. `engine = "auto"` starts Chrome if it is
   installed, else Firefox; set `engine = "firefox"` (or `"chrome"`) to choose.
3. Run `rafiq browser` (or `rafiq browser https://…`). It opens the browser with Rafiq's own profile, in
   `~/.config/rafiq/data/browser/chrome` or `…/firefox`: sign in there only to the sites you want Rafiq
   to read. Your everyday browser and its profile are never touched.

Chrome is driven through its DevTools Protocol, Firefox through WebDriver BiDi; both read the page out of
its own scripts' reach. Firefox allows one remote session at a time, and Rafiq keeps it while that Firefox
is open.

The tab list shows titles and sites, never full addresses. With Claude as the brain, the text of a tab
you ask about goes to Claude, as the clipboard does.

"Close this tab" and "close the other tabs" ask first, naming the sites (not the titles, which a page
chooses itself), and close only the tabs the question named: not one that came to the front or opened
while you answered. "Yes, always" can be remembered for closing one tab, never for closing all the
others, which asks every time. Sites for banks, taxes and health are read-only: Rafiq reads them but never closes them,
and "close the other tabs" leaves them open and says so. They are recognised by a word in the site's
name (bank, banque, tax, impot, steuer, health, salud, hospital, …) or a known site (paypal.com,
impots.gouv.fr, irs.gov, doctolib, nhs.uk, nbe.com.eg, …); add your own under `[browser] read_only`,
such as `["mycreditunion.org"]`. Clicking and filling forms are not done yet.

## Video and stage lights (DMX)

"Key light to 60%, 5600 K", "turn off the fill", "all the lights at 30%". Rafiq speaks DMX, the open
standard most video and stage lights take (Aputure, Godox, Nanlite and others in their DMX mode),
through an Art-Net node: a small box on your network, or a light with Art-Net built in. Bluetooth
control of amaran lights is not supported, because it needs keys taken from the vendor's app.

1. Set the light to DMX in its menu and give it a start address, then connect it to the Art-Net node.
2. Under `[dmx]` in the config, set `host` to the node's address on your network (it must be a local
   address) and `universe` to the node's universe.
3. Add one `[[dmx.lights]]` per light: its `name`, its start `address`, its `channels` in the order the
   manual's DMX table gives them, and its `kelvin` range. A channel is `dimmer`, `cct` (colour
   temperature), their 16-bit forms `dimmer16` and `cct16` (two channels, coarse then fine), or `none`
   for a channel to leave at zero.

`rafiq doctor` checks the settings: addresses in range, no two lights on the same channels. Rafiq keeps
what it last set in `~/.config/rafiq/data/dmx.json` and sends every light's channels each time, so
changing one light leaves the others as they were. Rafiq sends a frame only when you ask for a change,
so set the Art-Net node to hold the last frame ("hold last look" or "merge: hold" in its settings);
some nodes otherwise go dark a few seconds after the last frame.

## Smart plugs and energy meters

Shelly and Tasmota plugs are reached over their local HTTP API, with no cloud; anything else can go
through Home Assistant. List each plug under `[[plugs]]` with the name you will say:

```toml
[[plugs]]
name = "PC"
kind = "shelly"                 # shelly (Gen2 and later), shelly-gen1, tasmota or home-assistant
host = "192.168.1.40"
ask_before_off = true           # confirm out loud before turning it off

[[plugs]]
name = "heater"
kind = "home-assistant"
entity = "switch.heater"
power_entity = "sensor.heater_power"   # optional, W or kW
energy_entity = "sensor.heater_energy" # optional, kWh

[home_assistant]
url = "http://homeassistant.local:8123"
token = ""                      # a long-lived access token, or set HOME_ASSISTANT_TOKEN
```

Rafiq only picks a plug by its name, never an address. Plugs protected by a password are not
supported yet.

## OBS Studio

1. OBS 28 or newer: in OBS, **Tools > WebSocket Server Settings**, tick **Enable WebSocket server**
   and note the password.
2. `.venv/bin/pip install -e ".[obs]"`
3. Under `[obs]` in the config set `enabled = true` and the `password` (or set `OBS_WEBSOCKET_PASSWORD`).
4. For "clip that", turn on the replay buffer in OBS (Settings > Output > Replay Buffer) and start it.

## The morning paper on paper

Rafiq prints the day's paper with CUPS (`lp`), once a day at most and only after you say yes. It
prints `~/.config/rafiq/data/paper/YYYY-MM-DD.pdf` (or `file` under `[paper]`, with `{date}` in it).

- Every morning at a fixed time: `rafiq paper timer --at 07:00` writes a systemd user timer; turn it on
  with `systemctl --user daemon-reload && systemctl --user enable --now rafiq-paper.timer`. At that
  time the running Rafiq asks out loud (or in the panel) whether to print.
- After the first brief of the day: `after_brief = true` under `[paper]`.
- Now, from a terminal: `rafiq paper print` (asks first).

A lock file (`data/paper.lock`) holds the date of the last print, so it never comes out twice.

## Settings

`~/.config/rafiq/config.toml` (`RAFIQ_HOME` moves the folder). See `config.example.toml`; everything is
optional.

## Development

```bash
.venv/bin/ruff check . && .venv/bin/python -m pytest -q
```

How the code fits together, how to add a skill and how to send a pull request:
[CONTRIBUTING.md](CONTRIBUTING.md) ([بالعربية](CONTRIBUTING.ar.md)). AI coding assistants read
[CLAUDE.md](CLAUDE.md).

## License

MIT
