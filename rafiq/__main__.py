"""rafiq run | ask | say | enroll | listen | satellite | doctor | google-setup"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading

from . import config, lang


def cmd_run(args) -> int:
    from .assistant import Assistant
    from .panel import serve

    cfg = config.load()
    if args.brain:
        cfg["brain"]["mode"] = args.brain
    print("Loading the speech models (the first start downloads them)...", flush=True)
    assistant = Assistant(cfg)
    if not args.no_panel:
        host, port = cfg["panel"]["host"], int(cfg["panel"]["port"])
        server = serve(assistant, host, port, config.panel_token())
        threading.Thread(target=server.serve_forever, name="rafiq-panel", daemon=True).start()
        print(f"Panel: http://{host}:{port}/", flush=True)
    _start_hub(cfg, assistant)
    print('Say "Rafiq" (or "يا رفيق"). Ctrl+C stops.', flush=True)
    try:
        assistant.run()
    except KeyboardInterrupt:
        assistant.stop()
    return 0


def _start_hub(cfg: dict, assistant) -> None:
    """Serve the satellites listed in the settings, if any, with the assistant's models and tools."""
    from . import satellite
    from .brain import Brain

    if not satellite.devices(cfg):
        return
    hub = satellite.Hub(cfg, transcribe=assistant.transcribe, tools=assistant.tools, scheduler=assistant.scheduler,
                        make_brain=lambda tools, confirm: Brain(cfg, tools, confirm, on_event=assistant.events.publish),
                        on_event=assistant.events.publish)
    sats = cfg["satellites"]
    tls = satellite.server_tls(cfg)
    server = hub.serve(sats["host"], int(sats["port"]), tls)
    threading.Thread(target=server.serve_forever, name="rafiq-satellites", daemon=True).start()
    print(f"Satellites: {sats['host']}:{sats['port']} ({'TLS' if tls else 'no TLS: use a private network'}), "
          f"{', '.join(sorted(hub.devices))}", flush=True)


def _terminal_confirm(action: str, tag: str) -> bool:
    answer = input(lang.phrase("confirm", tag, action=action) + " ")
    return lang.yes_or_no(answer) is True


def cmd_ask(args) -> int:
    """Type a request instead of saying it: the same brain and tools, answers printed (and spoken with
    --speak). Useful without a microphone and for testing."""
    from .assistant import Context
    from .brain import Brain
    from .scheduler import Scheduler
    from .tools import load_all
    from .tts import Speaker

    cfg = config.load()
    if args.brain:
        cfg["brain"]["mode"] = args.brain
    text = " ".join(args.text)
    tag = args.lang or lang.dialect(lang.guess(text), text)

    def show(kind, data):
        if kind == "tool":
            print(f"  [{data['name']} {data['args']}] -> {str(data['result'])[:200]}", file=sys.stderr)
        elif kind == "brain":
            print(f"  ({data['backend']})", file=sys.stderr)
        elif kind == "error":
            print(f"  error: {data}", file=sys.stderr)

    def say_alarm(alarm):
        print(lang.phrase("timer_done" if alarm.kind == "timer" else "reminder", alarm.tag, label=alarm.label))

    scheduler = Scheduler(config.data_dir() / "alarms.json", say_alarm)
    brain = Brain(cfg, load_all(cfg), _terminal_confirm, on_event=show)
    answer = brain.think(text, tag, Context(cfg, scheduler, tag))
    print(answer)
    if args.speak:
        Speaker(cfg).say(answer, lang.script_language(answer, tag))
    return 0


def cmd_say(args) -> int:
    from .tts import Speaker

    speaker = Speaker(config.load())
    ok = speaker.say(" ".join(args.text), args.lang)
    return 0 if ok else 1


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok ' if ok else 'no '} {label}" + (f": {detail}" if detail else ""))
    return ok


def cmd_doctor(_args) -> int:
    import httpx

    cfg = config.load()
    print(f"Rafiq settings: {config.home() / 'config.toml'}")
    try:
        import sounddevice as sd

        device = sd.query_devices(kind="input")
        _check("microphone", True, device["name"])
    except Exception as exc:
        _check("microphone", False, type(exc).__name__)
    _check("audio player (ffplay)", bool(shutil.which("ffplay")))
    brain = cfg["brain"]
    _check("Claude API key", bool(cfg.get("anthropic_api_key")),
           brain["claude_model"] if cfg.get("anthropic_api_key") else "set ANTHROPIC_API_KEY to use Claude")
    try:
        tags = httpx.get(f"{brain['ollama_url']}/api/tags", timeout=3).json().get("models", [])
        names = [m["name"] for m in tags]
        _check("Ollama", brain["ollama_model"] in names,
               f"{brain['ollama_model']} " + ("installed" if brain["ollama_model"] in names else
                                              f"missing (ollama pull {brain['ollama_model']})"))
    except Exception:
        _check("Ollama", False, f"not reachable at {brain['ollama_url']}")
    from .tts import Speaker

    speaker = Speaker(cfg)
    _check("Azure voices (Egyptian Arabic)", bool(speaker.key and speaker.region),
           "set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION" if not speaker.key else speaker.region)
    for tag in ("en", "fr", "es", "de", "ar"):
        path = speaker.piper_path(tag)
        _check(f"offline voice {tag}", path is not None, path.name if path else "missing")
    google = config.home() / "google" / "token.json"
    _check("Google (calendar, email)", google.is_file() and cfg["google"].get("enabled", False),
           "connected" if google.is_file() else "run: rafiq google-setup")
    print(f"  voice engines in order: {', '.join(speaker.engines())}")
    if brain["mode"] == "auto":
        order = (["claude"] if cfg.get("anthropic_api_key") else []) + ["ollama"]
    else:
        order = [brain["mode"]]
    print(f"  brain in order: {', '.join(order)}")
    return 0


def cmd_listen(_args) -> int:
    """Make the running Rafiq listen now, without the name: bind this to a keyboard shortcut."""
    import httpx

    cfg = config.load()
    host, port = cfg["panel"]["host"], int(cfg["panel"]["port"])
    try:
        resp = httpx.post(f"http://{host}:{port}/api/listen", json={}, timeout=5,
                          headers={"X-Rafiq-Token": config.panel_token()})
    except httpx.HTTPError:
        print("Rafiq is not running (start it with: rafiq run)", file=sys.stderr)
        return 1
    return 0 if resp.status_code == 200 else 1


def cmd_enroll(args) -> int:
    """Record the name (and a few ordinary sentences) so Rafiq knows your voice saying it."""
    from . import enroll

    if args.sensitivity is not None:
        report = enroll.recalibrate(args.sensitivity)
        print(f"Rafiq now wakes below {report['threshold']:.2f} (sensitivity {args.sensitivity}).")
        return 0
    return enroll.run(forms=args.forms or None, takes=args.takes)


def cmd_satellite(args) -> int:
    """This computer as a microphone and speaker in another room, a simulator, or a new device's token."""
    import secrets

    from . import satellite

    cfg = config.load()
    if args.action == "run":
        return satellite.run_device(cfg)
    if args.action == "token":
        name = args.name or "kitchen"
        print(f"On the main computer, in {config.home() / 'config.toml'}:\n")
        print(f'[satellites.devices.{name}]\ntoken = "{secrets.token_urlsafe(32)}"\nroom = "{args.room or name}"')
        print('risky = "ask"                   # or "block": no actions that need a yes from this room')
        print("\nOn the satellite: [satellite] hub, name and the same token.")
        return 0
    # simulate: send a 16 kHz mono WAV as if said to a satellite, print the answer and the time taken
    import wave

    import numpy as np

    sat = cfg["satellite"]
    with wave.open(args.wav, "rb") as wav:
        if wav.getframerate() != 16000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            print("The recording must be 16 kHz mono 16-bit WAV.", file=sys.stderr)
            return 1
        audio = satellite.from_pcm(wav.readframes(wav.getnframes()))
    host, port = satellite.parse_hub(args.hub or sat["hub"] or f"127.0.0.1:{cfg['satellites']['port']}")
    try:
        result = satellite.simulate(host, port, args.name or sat["name"], args.token or sat["token"],
                                    np.asarray(audio), yes=args.yes, tls=satellite.client_tls(cfg))
    except (OSError, satellite.ProtocolError) as exc:
        print(f"The hub did not answer: {exc}", file=sys.stderr)
        return 1
    print(f"room: {result.room}\nheard: {result.heard}")
    for text in result.said:
        print(f"said: {text}")
    print(f"answered in {result.seconds:.2f} s")
    return 0


def cmd_google_setup(_args) -> int:
    from .tools.google import setup

    print(setup())
    print("Then set [google] enabled = true in", config.home() / "config.toml")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rafiq", description="Rafiq (رفيق), a voice assistant in six languages")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="listen for 'Rafiq' and answer; opens the panel")
    run.add_argument("--no-panel", action="store_true")
    run.add_argument("--brain", choices=("auto", "claude", "ollama"))
    run.set_defaults(fn=cmd_run)
    ask = sub.add_parser("ask", help="type a request instead of saying it")
    ask.add_argument("text", nargs="+")
    ask.add_argument("--lang", choices=lang.TAGS)
    ask.add_argument("--brain", choices=("auto", "claude", "ollama"))
    ask.add_argument("--speak", action="store_true", help="also say the answer")
    ask.set_defaults(fn=cmd_ask)
    say = sub.add_parser("say", help="say a sentence, to try a voice")
    say.add_argument("text", nargs="+")
    say.add_argument("--lang", choices=lang.TAGS, default="en")
    say.set_defaults(fn=cmd_say)
    enroll = sub.add_parser("enroll", help="record yourself saying the name, for a reliable wake word")
    enroll.add_argument("--forms", nargs="+", help='the ways you call it (default: "Rafiq" "يا رفيق")')
    enroll.add_argument("--takes", type=int, default=3, help="recordings of each form (default 3)")
    enroll.add_argument("--sensitivity", type=float,
                        help="refit the saved recordings: 0 strict ... 1 loose (default 0.45)")
    enroll.set_defaults(fn=cmd_enroll)
    sub.add_parser("listen", help="make the running Rafiq listen now (for a keyboard shortcut)").set_defaults(
        fn=cmd_listen)
    sat = sub.add_parser("satellite", help="a microphone and speaker in another room")
    sat.add_argument("action", choices=("run", "simulate", "token"),
                     help="run: be a satellite; simulate: send a WAV to the hub; token: make a new device's token")
    sat.add_argument("wav", nargs="?", help="simulate: a 16 kHz mono WAV of a request")
    sat.add_argument("--name", help="the satellite's name (default: [satellite] name)")
    sat.add_argument("--room", help="token: the room it is in (default: its name)")
    sat.add_argument("--hub", help="simulate: host:port of the hub (default: [satellite] hub)")
    sat.add_argument("--token", help="simulate: the satellite's token (default: [satellite] token)")
    sat.add_argument("--yes", action="store_true", help="simulate: answer yes when asked to confirm")
    sat.set_defaults(fn=cmd_satellite)
    sub.add_parser("doctor", help="check the microphone, models, voices and keys").set_defaults(fn=cmd_doctor)
    sub.add_parser("google-setup", help="connect your Google calendar and Gmail").set_defaults(fn=cmd_google_setup)
    args = parser.parse_args(argv)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
