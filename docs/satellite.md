# A satellite on a Raspberry Pi

A satellite is a microphone and a speaker in another room. It hears "Rafiq" itself, sends what you said
to the Rafiq on your main computer (the hub), and speaks the answer. Speech that does not start with
the name never leaves the satellite.

## What you need

- A Raspberry Pi 4 or 5 (2 GB is enough) with Raspberry Pi OS Bookworm 64-bit, or any ARM64 or x86-64
  Linux computer.
- A USB speakerphone (Jabra Speak, Anker PowerConf, eMeet...): one box with a microphone that hears
  across the room and a speaker. A USB microphone and any speaker work too.
- The Pi and the main computer on the same network, or on the same Tailscale or WireGuard network.

## On the main computer

```bash
rafiq satellite token --name kitchen
```

prints a `[satellites.devices.kitchen]` block with a new token. Add it to the main computer's
`config.toml`, with `[satellites] host` set to its address on your network (see the README), and
restart `rafiq run`. It then listens for satellites on port 8766.

## Install on the Pi

```bash
sudo apt install python3-venv libportaudio2 ffmpeg git
git clone https://github.com/nexika/rafiq.git && cd rafiq
python3 -m venv .venv && .venv/bin/pip install -e ".[offline-voice]"
```

Check that the speakerphone is seen:

```bash
.venv/bin/python -m sounddevice      # lists the devices, > marks the default input, < the default output
```

## Settings

In `~/.config/rafiq/config.toml` on the Pi:

```toml
[satellite]
hub = "192.168.1.10:8766"       # the main computer
name = "kitchen"                # the name in its [satellites.devices]
token = "..."                   # the same token; or leave it out and set RAFIQ_SATELLITE_TOKEN

[audio]
device = "Jabra"                # the microphone: part of its name, or "" for the default
```

The answer plays on the default output. To make the speakerphone the default:
`wpctl status`, then `wpctl set-default <its number>`.

Requests travel as plain TCP, so keep satellites on your private network, or turn on TLS
(`tls_cert` and `tls_key` under the hub's `[satellites]`; `tls = true` and `tls_ca` under the Pi's
`[satellite]`).

## The wake word

The satellite uses your recordings of the name, like the main computer. Without them a small Whisper
model listens for the name instead, which is slower on a Pi. Record them on the Pi, through the
speakerphone (best, since it is the microphone that will hear you):

```bash
.venv/bin/rafiq enroll
```

or copy them from the main computer:

```bash
scp -r ~/.config/rafiq/data/wake pi@kitchen.local:.config/rafiq/data/
```

**Tune it** with `rafiq satellite scores`. It prints every utterance's score next to the threshold
(lower is closer to your recordings) and sends nothing. Say the name from where you usually stand,
then some ordinary sentences. If the name scores above the threshold, loosen it with
`rafiq enroll --sensitivity 0.6`. If ordinary speech scores below it, tighten it with `0.3`.

## Run it

```bash
.venv/bin/rafiq satellite run
```

It connects and prints `Connected; this is the kitchen.` If the main computer is not reachable yet, it
starts anyway and connects at the first request. After a lost connection it reconnects at the next
one. If the main computer refuses the name or the token, it stops with exit code 2 instead of
retrying.

## As a service

`rafiq satellite unit` prints a systemd user unit with the right paths. Install it so the satellite
starts at boot, before anyone logs in:

```bash
mkdir -p ~/.config/systemd/user
.venv/bin/rafiq satellite unit > ~/.config/systemd/user/rafiq-satellite.service
systemctl --user daemon-reload
systemctl --user enable --now rafiq-satellite
sudo loginctl enable-linger "$USER"          # start user services at boot without a login
journalctl --user -u rafiq-satellite -f      # what it is doing
```

It restarts on its own after a crash, but not after exit code 2 (refused): fix the settings, then
`systemctl --user restart rafiq-satellite`. To keep the token out of the config, put
`RAFIQ_SATELLITE_TOKEN=...` in a file and add `EnvironmentFile=` with that file's path under
`[Service]` (`systemctl --user edit rafiq-satellite`).

It is a user service, not a system one, so it shares your user's sound server (PipeWire) with other
programs.

## Sharing the speaker with Spotify

To use the same speakerphone as a Spotify Connect speaker, run a Spotify receiver such as
[raspotify](https://github.com/dtcooper/raspotify) (librespot) through PipeWire too, so both can play
at once:

1. Raspberry Pi OS Bookworm with the desktop uses PipeWire already. On Lite, install it:
   `sudo apt install pipewire pipewire-pulse wireplumber` and reboot.
2. Install raspotify, then run it as your user instead of as its own system user, with the PulseAudio
   backend (PipeWire answers to it): in `/etc/raspotify/conf` set `LIBRESPOT_BACKEND="pulseaudio"`,
   and `sudo systemctl edit raspotify` to add `User=<you>` and
   `Environment=XDG_RUNTIME_DIR=/run/user/<your uid>` under `[Service]`.
3. Keep the speakerphone as PipeWire's default output, so the satellite plays through it too.

Without PipeWire, two programs cannot open a USB sound card at the same time. ALSA's `dmix` plugin is
the fallback.

## The protocol

TCP (optionally TLS) frames, described at the top of `rafiq/satellite.py`: a `hello` with the name and
token, then each request as audio frames between `start` and `end`. The hub answers with `heard`, `ask`
(a yes or no question, answered on the satellite), `say` and `done`. Audio is 16 kHz mono 16-bit PCM.
