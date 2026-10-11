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
| Everyday | "what time is it in Tokyo?", "set a timer for 10 minutes", "remind me tomorrow at 9 to call mum", "weather in Cairo", "15% of 240", "5 miles in km" |
| Notes | "note: buy batteries", "idea: a lamp that follows the sun", "give me a random idea from my notes", "what did I note today?" (Markdown files in `~/.config/rafiq/data/notes`, or `[notes] folder` such as an Obsidian vault) |
| The screen | "what's this error?", "read this", "translate this into Arabic" (asks first: the screenshot goes to Claude) |
| Web | "search for…", "summarise this page", "translate this article into Arabic", "open youtube.com" |
| Scenes | "film mode", "turn everything off" (offers to shut down), a morning scene at the first start of the day: your own lists of steps in the config, named in any language |
| Home | "which lights are on?", "turn off the coffee plug" (asks first), through Home Assistant |
| Background tasks | "research the best e-bike under 2000 euros" (Rafiq says it is on it and reads the answer aloud when it is ready), "how are my tasks going?"; needs Claude, and the panel shows what each task cost |
| Memory | "remember that my sister is called Sara", "what do you know about my project?", "forget that" |
| Style | "be more concise", "talk like a butler", "back to normal": neutral, concise, or a dry, witty butler (also in the panel) |
| Google | "what's on my calendar today?", "add lunch with Sara on Friday at 1" (asks first), "any new email?", "email Sara that I'm running late", "reply to Sara that I'll be late" (reads the draft back, asks first), "delete that email" (to the trash, asks first), "where are my parcels?" |
| Instagram | "how are my Instagram accounts doing?", "how many followers did studio gain since yesterday?" |
| Smart plugs | "turn on the desk lamp", "how much is the PC drawing?", "turn off the PC plug" (asks first when you mark it so) |
| OBS Studio | "start recording" (asks first), "go live" (asks first), "switch to the camera scene", "clip that" |

It answers Egyptian Arabic in Egyptian Arabic: "الساعة كام دلوقتي؟" gets a reply an Egyptian friend
would say, not formal فصحى. Timers and reminders are spoken in the language they were set in.

## Install

Linux with PipeWire (Fedora, Ubuntu 22.10+), Python 3.11 or newer, `ffmpeg`.

New to this? [INSTALL_WITH_AI.md](INSTALL_WITH_AI.md) ([بالعربية](INSTALL_WITH_AI.ar.md)) is a prompt
to paste into any chatbot, which then walks you through the install one command at a time.

```bash
git clone https://github.com/nexika/rafiq.git && cd rafiq
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,google,offline-voice]"
.venv/bin/pip install --no-deps openwakeword   # optional, for a trained wake word later
.venv/bin/pip install -e ".[nvidia]"            # optional, GPU figures on an NVIDIA card (AMD needs nothing)
.venv/bin/rafiq doctor
.venv/bin/rafiq enroll       # record "Rafiq" in your voice (about two minutes)
```

`rafiq doctor` lists your microphones; pick one with `[audio] device` in the settings. A microphone
that refuses 16 kHz is opened at a rate it takes and resampled. At start-up Rafiq listens to the room
for a second and fits its speech thresholds to the noise, so stay quiet for a moment
(`[audio] auto_calibration = false` turns this off).

The first start downloads the Whisper models (about 550 MB). Offline voices go in
`~/.config/rafiq/data/piper/` (see below).

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

Download the Piper voices once:

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
rafiq run                 # listen for "Rafiq"; the panel opens at http://127.0.0.1:8765/
rafiq ask "weather in Paris tomorrow"     # type instead of talking (add --speak to hear it)
rafiq say --lang ar-EG "أهلًا، أنا رفيق"  # try a voice
rafiq listen              # make the running Rafiq listen now
rafiq mute                # stop listening, or start again (rafiq mute on / off to choose)
```

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
Yes/No buttons when it asks before an action, and the choice of style.

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
  answer cancels.
- "Shut down" then waits 30 seconds and warns you 10 seconds before; "cancel the shutdown" stops it
  (`[pc] shutdown_delay` changes the wait, 0 shuts down at once).
- Web pages, search results and emails are passed to the model as data, never as instructions.
  Pages on this computer or the local network (your router, Rafiq's own panel) are never fetched.
- A question about the screen sends a screenshot of the whole screen to Claude, in the cloud, so Rafiq
  asks first (`screen.ask_first`). Offline it never leaves the computer: only a local vision model set
  in `screen.ollama_model` may see it. On GNOME Wayland the desktop also asks you to allow screenshots.
- What you ask Rafiq to remember is kept in `~/.config/rafiq/data/memory.json`, a plain list you can
  read and edit; "forget" deletes it from the file. Remembered facts go to the model as data with
  each request, never as instructions.
- Commands run as argument lists, never through a shell. The calculator only does arithmetic.
- The panel listens on 127.0.0.1 only, and every request must carry a random token that only the
  panel page knows; requests under another host name are refused.
- Keys are read from the environment or `~/.config/rafiq/config.toml` and never logged. The Google
  token is stored readable only by you.

## Rafiq's tools in other programs (MCP)

`rafiq mcp` offers Rafiq's harmless tools to MCP clients such as Claude Desktop or Claude Code, over
stdio: the time, the weather, web search and reading a page, how the computer is doing, arithmetic and
unit conversion. Nothing that changes anything is offered (no power, email, apps, volume, or setting
timers), and never a tool that would ask you first. Every call is logged to
`~/.config/rafiq/data/mcp.log`.

```bash
claude mcp add rafiq -- /path/to/rafiq/.venv/bin/rafiq mcp
```

For Claude Desktop, add to `claude_desktop_config.json`:
`"mcpServers": {"rafiq": {"command": "/path/to/rafiq/.venv/bin/rafiq", "args": ["mcp"]}}`.

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
