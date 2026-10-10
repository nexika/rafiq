# A satellite on a Raspberry Pi

A satellite is a microphone and a speaker in another room. It hears "Rafiq" itself, sends what you said
to the Rafiq on your main computer, and plays the answer. Speech that does not start with the name
never leaves the satellite.

> The main computer's side (the satellite listener in `rafiq run`) is a separate piece of work
> ([#63](https://github.com/nexika/rafiq/issues/63)). Until it lands, the satellite connects and waits,
> retrying every 30 seconds.

## What you need

- A Raspberry Pi 4 or 5 (2 GB is enough) with Raspberry Pi OS Bookworm 64-bit, or any ARM64 or x86-64
  Linux computer.
- A USB speakerphone (Jabra Speak, Anker PowerConf, eMeet...): one box with a microphone that hears
  across the room and a speaker. A USB microphone and any speaker work too.
- The Pi and the main computer on the same network, or on the same Tailscale or WireGuard network.

## Install

```bash
sudo apt install python3-venv libportaudio2 git
git clone https://github.com/nexika/rafiq.git && cd rafiq
python3 -m venv .venv && .venv/bin/pip install -e ".[satellite]"
```

Check that the speakerphone is seen and pick it if it is not the default:

```bash
.venv/bin/python -m sounddevice      # lists the devices, > marks the default input, < the default output
```

## Settings

In `~/.config/rafiq/config.toml` on the Pi:

```toml
[satellite]
server = "ws://192.168.1.10:8766/satellite"   # the main computer
name = "kitchen"                              # the room; default: the Pi's host name
token = "..."                                 # given by the main computer; or RAFIQ_SATELLITE_TOKEN
input_device = ""                             # "" for the default, a number, or part of the name ("Jabra")
output_device = ""
```

`ws://` is accepted only for addresses on the private network (192.168.x.x, 10.x.x.x, 100.64.x.x for
Tailscale, `.local` names), because it sends the token in clear text. Anywhere else, use `wss://`.

## The wake word

The satellite uses your recordings of the name, like the main computer. Either record them on the Pi,
through the speakerphone (best, since it is the microphone that will hear you):

```bash
.venv/bin/rafiq enroll
```

or copy them from the main computer:

```bash
scp -r ~/.config/rafiq/data/wake pi@kitchen.local:.config/rafiq/data/
```

**Tune it** with `rafiq satellite --scores`: every utterance prints its score next to the threshold
(lower is closer to the name). Say the name from where you will usually stand, and some ordinary
sentences. If the name scores above the threshold, loosen it with `rafiq enroll --sensitivity 0.6`;
if ordinary speech scores below it, tighten it with `0.3`.

## Run it

```bash
.venv/bin/rafiq satellite
```

It connects, prints `connected to ... as kitchen`, and reconnects on its own when the main computer or
the network goes away (after 1, 2, 4... up to 30 seconds). If the main computer refuses the token, it
stops with exit code 2 instead of retrying.

## As a service

`rafiq satellite --unit` prints a systemd user unit with the right paths. Install it so the satellite
starts at boot, before anyone logs in:

```bash
mkdir -p ~/.config/systemd/user
.venv/bin/rafiq satellite --unit > ~/.config/systemd/user/rafiq-satellite.service
systemctl --user daemon-reload
systemctl --user enable --now rafiq-satellite
sudo loginctl enable-linger "$USER"          # start user services at boot without a login
journalctl --user -u rafiq-satellite -f      # what it is doing
```

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
3. Leave `output_device` empty, so the satellite plays through PipeWire's default output too.

Without PipeWire, two programs cannot open a USB sound card at the same time; ALSA's `dmix` plugin is
the fallback.

## The protocol

One WebSocket per satellite, protocol 1, described at the top of `rafiq/satellite.py`: JSON control
frames (`hello`, `welcome`/`refused`, `wake`, `end`, `listen`, `silence`, `play`, `play_end`) and
16-bit mono PCM in binary frames, 16 kHz from the satellite and at the rate `play` names from the
server.
