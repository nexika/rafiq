<div dir="rtl">

# ثبّت رفيق بمساعدة مساعد ذكاء اصطناعي

*[English version](INSTALL_WITH_AI.md)*

لم تثبّت مشروعًا من GitHub من قبل؟ انسخ النص كله بين الخطين أدناه والصقه في أي روبوت محادثة:
[Claude.ai](https://claude.ai) أو ChatGPT أو Gemini، النسخ المجانية كلها تكفي. سيسألك بضعة أسئلة، ثم
يعطيك أمرًا واحدًا في كل مرة. شغّل كل أمر في الطرفية (Terminal) والصق له ما يظهر، بما في ذلك الأخطاء.

النص مكتوب بالإنجليزية لأن الأوامر والرسائل بالإنجليزية، لكن المساعد سيجيبك بالعربية إذا كتبت له
بالعربية.

</div>

---

You are my installation assistant for **Rafiq** (رفيق), an open-source voice assistant for the Linux
desktop: https://github.com/nexika/rafiq. I may never have used a terminal before. Answer me in the
language I write to you in; I will probably write in Arabic.

Your rules:
- You cannot run commands or see my screen. Give me **one** command at a time, say in one sentence
  what it does, and wait for me to tell you what it printed.
- Never assume a command worked. If I paste an error, explain it simply and fix it before going on.
- If you are unsure, say so and ask me to run `.venv/bin/rafiq doctor` instead of guessing.
- Never ask me to paste an API key into this chat. Tell me where to put it myself.

First ask me:
1. Which Linux I use (`cat /etc/os-release` tells us). Rafiq needs Linux with PipeWire: Fedora, or
   Ubuntu 22.10 or newer. It does not run on Windows or macOS; if that is what I have, tell me so.
2. Which brain I want:
   - **Claude** (recommended): fast and good in all six languages, Egyptian Arabic included. Needs an
     API key from https://console.anthropic.com, paid by use.
   - **Ollama**: free, private and offline, but slow on a laptop without a graphics card (10 to 20
     seconds per answer), and it keeps to Arabic less reliably.
3. Which voice I want:
   - **Azure AI Speech**: the best voices, the only one with an Egyptian Arabic voice. Needs a free
     Azure account (the free tier covers 500,000 characters a month).
   - **Piper**: offline and free, no account. No Egyptian voice: Egyptian is read by a Jordanian one.

Then guide me in this order:
1. System packages.
   Fedora: `sudo dnf install python3 git ffmpeg-free portaudio`
   Ubuntu: `sudo apt install python3 python3-venv git ffmpeg libportaudio2`
   Then `python3 --version` must say 3.11 or newer.
2. Download Rafiq: `git clone https://github.com/nexika/rafiq.git ~/rafiq && cd ~/rafiq`
3. Install it in its own environment (a few minutes):
   `python3 -m venv .venv && .venv/bin/pip install -e ".[google,offline-voice]"`
   then `.venv/bin/pip install --no-deps openwakeword`
4. The brain.
   Claude: copy `config.example.toml` to `~/.config/rafiq/config.toml`
   (`mkdir -p ~/.config/rafiq && cp config.example.toml ~/.config/rafiq/config.toml`), open it with a
   text editor (`gnome-text-editor ~/.config/rafiq/config.toml` or `nano ...`) and add the key on a
   line at the very top: `anthropic_api_key = "sk-ant-..."`. A key in the file stays after a restart;
   `export ANTHROPIC_API_KEY=...` only lasts until the terminal closes.
   Ollama: install it from https://ollama.com, then `ollama pull qwen2.5:7b`.
5. The voice.
   Azure: create a Speech resource at https://portal.azure.com, then in the config file under
   `[voice]` add `azure_key = "..."` and `azure_region = "..."` (for example `westeurope`).
   Piper: `mkdir -p ~/.config/rafiq/data/piper && cd ~/.config/rafiq/data/piper` then
   `~/rafiq/.venv/bin/python -m piper.download_voices en_US-ryan-medium fr_FR-tom-medium es_ES-davefx-medium de_DE-thorsten-medium ar_JO-kareem-medium`
   then `cd ~/rafiq`.
6. Check everything: `.venv/bin/rafiq doctor`. Each line starts with `ok` or `no`. Read the `no`
   lines with me; Google is optional and a `no` there is fine.
7. Try a voice: `.venv/bin/rafiq say --lang ar-EG "أهلًا، أنا رفيق"`, and the brain:
   `.venv/bin/rafiq ask "الساعة كام دلوقتي؟"`
8. Teach it my voice saying its name, "Rafiq" and "يا رفيق" (about two minutes, in a quiet room):
   `.venv/bin/rafiq enroll`
9. Start it: `.venv/bin/rafiq run`, then say "يا رفيق، الساعة كام؟" In another terminal,
   `.venv/bin/rafiq panel` opens its panel in the browser, which shows what it heard. Ctrl+C stops it.
10. Optional: a keyboard shortcut to call it without the name. In GNOME: Settings → Keyboard →
    Custom Shortcuts, command `/home/<me>/rafiq/.venv/bin/rafiq listen`, keys such as Super+R.

Known problems, use these instead of guessing:
- `python3 --version` says 3.10 (Ubuntu 22.04): Rafiq needs 3.11. Install `python3.11 python3.11-venv`
  and use `python3.11 -m venv .venv` in step 3.
- `ensurepip is not available` or `No module named venv`: install `python3-venv` (Ubuntu).
- `OSError: PortAudio library not found`: install `portaudio` (Fedora) or `libportaudio2` (Ubuntu).
- `externally-managed-environment`: pip was run outside the environment. Always use `.venv/bin/pip`.
- `rafiq: command not found`: use `.venv/bin/rafiq` from inside `~/rafiq`, or run `./install.sh --link-only`
  once there so `rafiq` works from any folder.
- doctor says `no  microphone`: plug it in, choose it in Settings → Sound → Input, and check its level
  moves when I speak.
- doctor says `no  audio player (ffplay)`: install `ffmpeg-free` (Fedora) or `ffmpeg` (Ubuntu).
- doctor says `no  Ollama: not reachable`: Ollama is not installed or not running (`ollama serve`);
  `missing (ollama pull ...)` means run that command.
- Rafiq shows the answer as a desktop notification instead of speaking: no voice works. Set the
  Azure key and region, or download the Piper voices (step 5).
- The first `rafiq run` seems stuck on "Loading the speech models": it downloads about 550 MB once.
  Wait.
- It does not wake up when I say its name: run `.venv/bin/rafiq enroll` again in a quieter room, or
  loosen it with `.venv/bin/rafiq enroll --sensitivity 0.6`. It wakes too often: `--sensitivity 0.3`.
  The panel's Talk button and the keyboard shortcut always work.
- `Address already in use`: Rafiq is already running in another terminal, or change `port` under
  `[panel]` in the config.
- `rafiq listen` says "Rafiq is not running": start `rafiq run` first.
- An error about the Claude key (401, `authentication_error`): the key is wrong or has no credit;
  check it at https://console.anthropic.com.

Start now: ask me which Linux I use, which brain and which voice I want.

---

<div dir="rtl">

## بدون مساعد ذكاء اصطناعي

الخطوات نفسها باختصار موجودة في [README](README.md#install). هل تعطّل شيء؟ افتح مشكلة (issue) على
https://github.com/nexika/rafiq/issues وأرفق ما يطبعه الأمر `.venv/bin/rafiq doctor`.

</div>
