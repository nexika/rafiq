"""rafiq run | ask | say | doctor | google-setup | satellite"""

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
    print('Say "Rafiq" (or "يا رفيق"). Ctrl+C stops.', flush=True)
    try:
        assistant.run()
    except KeyboardInterrupt:
        assistant.stop()
    return 0


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
    """Be a microphone and speaker in another room for the Rafiq running on the main computer."""
    import logging

    from . import kws, satellite

    if args.unit:
        print(satellite.service_unit(sys.executable), end="")
        return 0
    cfg = config.load()
    settings = cfg["satellite"]
    server = args.server or settings["server"]
    if not server:
        print("Set the main computer's address: [satellite] server = \"ws://...\" in "
              f"{config.home() / 'config.toml'} (or --server)", file=sys.stderr)
        return 1
    try:
        satellite.check_server(server)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    secret = satellite.token(cfg)
    if not secret:
        print("Set the satellite's token: RAFIQ_SATELLITE_TOKEN or [satellite] token", file=sys.stderr)
        return 1
    spotter = kws.Spotter.load()
    if spotter is None:
        print("Record the name on this device (rafiq enroll), or copy ~/.config/rafiq/data/wake from the "
              "main computer", file=sys.stderr)
        return 1
    try:
        import websockets  # noqa: F401
    except ImportError:
        print('The satellite needs the websockets package: pip install "rafiq[satellite]"', file=sys.stderr)
        return 1
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from .audio import RATE

    mel = kws.Mel()
    ears = satellite.MicrophoneEars(satellite.device(settings["input_device"]),
                                    cfg["speech"]["max_utterance_seconds"])

    def show(score, threshold):
        print(f"  heard: score {score:.3f}, wakes below {threshold:.3f}", flush=True)

    sat = satellite.Satellite(
        server, args.name or settings["name"] or satellite.default_name(), secret, satellite.WebSocketLink.open,
        ears, satellite.SpeakerPlayer(satellite.device(settings["output_device"])),
        lambda audio: spotter.score(mel(audio[:int(kws.MAX_SECONDS * RATE)])), spotter.threshold,
        name_seconds=max(len(t) for t in spotter.templates) * 0.02, on_score=show if args.scores else None)
    ears.start()
    try:
        return sat.run()
    except KeyboardInterrupt:
        sat.stop()
        return 0
    finally:
        ears.stop()


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
    sub.add_parser("doctor", help="check the microphone, models, voices and keys").set_defaults(fn=cmd_doctor)
    sat = sub.add_parser("satellite", help="be a microphone and speaker in another room (Raspberry Pi)")
    sat.add_argument("--server", help="the main computer, e.g. ws://192.168.1.10:8766/satellite")
    sat.add_argument("--name", help="this room's name (default: the host name)")
    sat.add_argument("--scores", action="store_true", help="print each utterance's wake score, to tune it")
    sat.add_argument("--unit", action="store_true", help="print a systemd user unit that runs the satellite")
    sat.set_defaults(fn=cmd_satellite)
    sub.add_parser("google-setup", help="connect your Google calendar and Gmail").set_defaults(fn=cmd_google_setup)
    args = parser.parse_args(argv)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
