# Running Rafiq around the clock

Rafiq can stay on day and night so it answers the moment you call it, timers and reminders ring on
time, and you never wait for the speech models to load. This guide covers where to run it, how to
keep it running, and how to look after it from another computer.

**The one rule: never publish Rafiq's ports.** The panel can run commands, read your email and
switch the computer off. It listens on `127.0.0.1` only and must stay that way: no port forwarding
on the router, no `host = "0.0.0.0"`, no reverse proxy open to the internet. To reach it from
another machine, use an SSH tunnel or a private network such as Tailscale or WireGuard (see below).
`rafiq status` says `no  panel private` if the panel is reachable from the network.

## Where to run it

| Machine | Good for | Mind |
|---|---|---|
| **This PC** | the simplest: microphone, speakers and every PC skill already work | it only answers while the PC is on |
| **A mini-PC** (Intel N100 or similar) | the best always-on home for voice: 6 to 10 W, fast enough for Whisper `small` | add a USB microphone or conference speaker |
| **A NAS** (Synology, TrueNAS, Unraid) | already on all the time | usually no sound card; run it as a typed and panel assistant (`rafiq ask`) or with a USB speakerphone; ARM NAS boxes are slow for Whisper |
| **A Raspberry Pi 5** (8 GB) | quiet and cheap; with a speakerphone it sits in the living room | use `whisper_model = "base"` or `"tiny"`, or the Claude brain; Ollama is too slow on a Pi |
| **A VPS** | always reachable, Claude or Ollama on a bigger machine | no microphone or speaker at all, it can't control your PC, and your keys and email live on someone else's computer |

Hearing and speaking happen where the microphone and speaker are. A server in a cupboard can't hear
the living room; put the speakerphone where you talk, on the machine Rafiq runs on.

**With the desktop agent on the main PC** (issue #27, not built yet), the always-on machine will be
the brain and your main PC the body: Rafiq opens apps and changes the volume on the PC while running
somewhere else. Until then, PC skills act on the machine Rafiq runs on.

## Install on the server

The same as on a desktop (see the README), in your home folder so the service below finds it:

```bash
git clone https://github.com/nexika/rafiq.git ~/rafiq && cd ~/rafiq
python3 -m venv .venv && .venv/bin/pip install -e ".[google,offline-voice]"
.venv/bin/rafiq doctor
```

On a Pi or a NAS, set a lighter model in `~/.config/rafiq/config.toml`:

```toml
[speech]
whisper_model = "base"
```

Keys for the service go in `~/.config/rafiq/env`, readable only by you:

```bash
printf 'ANTHROPIC_API_KEY=...\nAZURE_SPEECH_KEY=...\nAZURE_SPEECH_REGION=...\n' > ~/.config/rafiq/env
chmod 600 ~/.config/rafiq/env
```

## Keep it running: a systemd user service

Rafiq ships a unit file, `rafiq/rafiq.service`. It starts Rafiq at boot, restarts it if it stops,
and reads the keys above. It runs as you, not as root, so it uses your PipeWire sound and your
settings.

```bash
mkdir -p ~/.config/systemd/user
cp ~/rafiq/rafiq/rafiq.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rafiq
sudo loginctl enable-linger "$USER"     # start at boot and keep running when you log out
```

The unit also caps Rafiq's memory. Rafiq runs in about 150 MB. Past `MemoryHigh=1500M` systemd slows
it down, and past `MemoryMax=2G` it restarts Rafiq alone, so a runaway Rafiq can't fill the computer.

If the checkout isn't `~/rafiq`, edit the `ExecStart=` line first. Logs:
`journalctl --user -u rafiq -f`.

On a NAS without systemd, run `~/rafiq/.venv/bin/rafiq run --no-panel` from the NAS's task
scheduler at boot, or in a `tmux` session.

## Look after it

From an SSH session on the server:

```bash
rafiq status            # running? service active? version, panel private, disk space
rafiq update --check    # is there a new version?
rafiq update            # pull it, reinstall, restart the service if it was running
```

`rafiq status` exits with 1 when something needs attention, so a cron job or a monitoring tool can
watch it. `rafiq update` refuses to run when the checkout has local changes, and only fast-forwards,
so it never merges or throws away your work. `rafiq update --no-restart` installs without
restarting, for a moment when Rafiq is busy.

Restart and stop are systemd's: `systemctl --user restart rafiq`, `systemctl --user stop rafiq`.

**Backup** is one folder: `~/.config/rafiq` holds the settings, keys, your wake-word recordings,
alarms and the Google token. Copy it with your usual backup (it contains secrets; keep the copy
private):

```bash
tar czf rafiq-backup-$(date +%F).tgz -C ~/.config rafiq
```

## Reach the panel from another computer

An SSH tunnel brings the server's panel to your laptop without opening any port:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@server
```

Then, on the server, `rafiq panel --print` shows a one-time address; open it on the laptop within two
minutes. It becomes that tab's session, so the page itself never carries the panel's token. The panel
only accepts the host names `127.0.0.1` and `localhost`, which is what the tunnel gives it.

With Tailscale or WireGuard you can SSH to the server from anywhere, phone included, the same way.
Don't change the panel's `host` to the private network's address: anything else on that network
could then reach it.
