"""rafiq run | ask | say | doctor | toolcheck | google-setup | install-service | uninstall-service"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading

from . import config, lang, modes


def cmd_run(args) -> int:
    from .assistant import Assistant
    from .panel import serve

    cfg = config.load()
    if args.brain:
        cfg["brain"]["mode"] = args.brain
    if args.mode:
        modes.switch(cfg, args.mode)
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
    if args.mode:
        modes.switch(cfg, args.mode)
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


def _check_microphone(cfg: dict) -> None:
    """The microphone Rafiq will use, the sample rate it opens it at, and the others to choose from."""
    from .audio import RATE, describe_inputs, pick_rate, rate_supported, resolve_device

    try:
        import sounddevice as sd

        devices = list(sd.query_devices())
        setting = cfg["audio"].get("device")
        try:
            index = resolve_device(setting, devices)
        except ValueError as exc:
            _check("microphone", False, f"{exc} (set [audio] device)")
            index = None
        else:
            if index is None:
                index = sd.default.device[0] if sd.default.device[0] >= 0 else None
            rate = pick_rate(lambda r: rate_supported(sd, index, r), preferred=int(cfg["audio"].get("rate") or 0))
            name = devices[index]["name"] if index is not None else "system default"
            _check("microphone", True, f"{name} at {rate} Hz" + ("" if rate == RATE else ", resampled to 16 kHz"))
        print("  microphones (choose one with [audio] device = <number or part of the name>):")
        for line in describe_inputs(devices, index):
            print(f"    {line}")
    except Exception as exc:
        _check("microphone", False, type(exc).__name__)


def cmd_doctor(_args) -> int:
    import httpx

    cfg = config.load()
    print(f"Rafiq settings: {config.home() / 'config.toml'}")
    _check_microphone(cfg)
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
    _check("ElevenLabs voices", bool(speaker.eleven_key),
           speaker.cfg.get("elevenlabs_model", "") if speaker.eleven_key else "set ELEVENLABS_API_KEY")
    kokoro = speaker.kokoro_paths()
    _check("Kokoro offline voices (en, fr, es)", kokoro is not None,
           "found" if kokoro else "missing (see README)")
    for tag in ("en", "fr", "es", "de", "ar"):
        path = speaker.piper_path(tag)
        _check(f"offline voice {tag}", path is not None, path.name if path else "missing")
    google = config.home() / "google" / "token.json"
    _check("Google (calendar, email)", google.is_file() and cfg["google"].get("enabled", False),
           "connected" if google.is_file() else "run: rafiq google-setup")
    print(f"  mode: {modes.current(cfg)}")
    from .stt import cloud_engines

    cloud = cloud_engines(cfg)
    speech = cfg["speech"]
    offline = speech.get("offline", False) or modes.offline(cfg)
    _check("cloud speech to text", bool(cloud) or offline,
           "off (offline)" if offline else ", ".join(cloud) or
           "set AZURE_SPEECH_KEY or OPENAI_API_KEY for languages Whisper hears badly")
    if speech.get("stt_by_language"):
        print("  speech to text by language: "
              + ", ".join(f"{k} {v}" for k, v in speech["stt_by_language"].items()))
    print(f"  voice engines in order: {', '.join(speaker.engines())}")
    if modes.offline(cfg):
        order = ["ollama"]
    elif brain["mode"] == "auto":
        order = (["claude"] if cfg.get("anthropic_api_key") else []) + ["ollama"]
    else:
        order = [brain["mode"]]
    print(f"  brain in order: {', '.join(order)}")
    return 0


def cmd_toolcheck(_args) -> int:
    """Send a fixed list of requests to the local model, once with every tool and once with the set it
    gets, and print how often it picked the right tool and answered in the right language."""
    import httpx

    from . import toolcheck
    from .tools import load_all, local_tools

    cfg = config.load()
    tools = load_all(cfg)
    ask = toolcheck.ollama_ask(cfg)
    choice = cfg["brain"].get("ollama_tools", "local")
    print(f"{cfg['brain']['ollama_model']}, {len(toolcheck.REQUESTS)} requests")
    for label, subset in (("all tools", tools), (f"ollama_tools = {choice!r}", local_tools(tools, choice))):
        try:
            results = toolcheck.run(cfg, subset, toolcheck.REQUESTS, ask)
        except httpx.HTTPError as exc:
            print(f"Ollama is not answering at {cfg['brain']['ollama_url']}: {type(exc).__name__}", file=sys.stderr)
            return 1
        for r in results:
            if not (r.right_tool and r.right_language):
                print(f"  [{r.request.tag}] {r.request.text!r}: wanted {r.request.tool}, got "
                      f"{', '.join(r.called) or 'no tool'} {r.answer[:60]!r}")
        total, tool_ok, lang_ok = toolcheck.score(results)
        print(f"{label} ({len(subset)}): right tool {tool_ok}/{total}, right language {lang_ok}/{total}")
    return 0


def _ask_running(path: str, body: dict) -> int:
    """Send a request to the running Rafiq through its panel."""
    import httpx

    cfg = config.load()
    host, port = cfg["panel"]["host"], int(cfg["panel"]["port"])
    try:
        resp = httpx.post(f"http://{host}:{port}{path}", json=body, timeout=5,
                          headers={"X-Rafiq-Token": config.panel_token()})
    except httpx.HTTPError:
        print("Rafiq is not running (start it with: rafiq run)", file=sys.stderr)
        return 1
    return 0 if resp.status_code == 200 else 1


def cmd_listen(_args) -> int:
    """Make the running Rafiq listen now, without the name: bind this to a keyboard shortcut."""
    return _ask_running("/api/listen", {})


def cmd_mute(args) -> int:
    """Turn listening off and on: bind this to a keyboard shortcut too."""
    return _ask_running("/api/mute", {} if args.state is None else {"muted": args.state == "on"})


def cmd_enroll(args) -> int:
    """Record the name (and a few ordinary sentences) so Rafiq knows your voice saying it."""
    from . import enroll

    if args.sensitivity is not None:
        report = enroll.recalibrate(args.sensitivity)
        print(f"Rafiq now wakes below {report['threshold']:.2f} (sensitivity {args.sensitivity}).")
        return 0
    return enroll.run(forms=args.forms or None, takes=args.takes)


def cmd_google_setup(_args) -> int:
    from .tools.google import setup

    print(setup())
    print("Then set [google] enabled = true in", config.home() / "config.toml")
    return 0


def cmd_install_service(_args) -> int:
    """Start Rafiq at every login, as a systemd user service."""
    from . import service

    try:
        path = service.install()
    except service.ServiceError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Rafiq now starts at login ({path}) and is running.")
    print("Logs: journalctl --user -u rafiq -f")
    print(f"Exported keys are not seen by the service: put ANTHROPIC_API_KEY=... lines in {service.env_file()}")
    return 0


def cmd_uninstall_service(_args) -> int:
    from . import service

    try:
        removed = service.uninstall()
    except service.ServiceError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("Rafiq no longer starts at login." if removed else "Rafiq was not set to start at login.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rafiq", description="Rafiq (رفيق), a voice assistant in six languages")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="listen for 'Rafiq' and answer; opens the panel")
    run.add_argument("--no-panel", action="store_true")
    run.add_argument("--brain", choices=("auto", "claude", "ollama"))
    run.add_argument("--mode", choices=modes.MODES, help="everyday (fast), quality or offline")
    run.set_defaults(fn=cmd_run)
    ask = sub.add_parser("ask", help="type a request instead of saying it")
    ask.add_argument("text", nargs="+")
    ask.add_argument("--lang", choices=lang.TAGS)
    ask.add_argument("--brain", choices=("auto", "claude", "ollama"))
    ask.add_argument("--mode", choices=modes.MODES, help="everyday (fast), quality or offline")
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
    mute = sub.add_parser("mute", help="stop or start listening (for a keyboard shortcut)")
    mute.add_argument("state", nargs="?", choices=("on", "off"), help="on: stop listening; default: toggle")
    mute.set_defaults(fn=cmd_mute)
    sub.add_parser("doctor", help="check the microphone, models, voices and keys").set_defaults(fn=cmd_doctor)
    sub.add_parser("toolcheck", help="measure how well the local model picks tools").set_defaults(
        fn=cmd_toolcheck)
    sub.add_parser("google-setup", help="connect your Google calendar and Gmail").set_defaults(fn=cmd_google_setup)
    sub.add_parser("install-service", help="start Rafiq at every login (systemd user service)").set_defaults(
        fn=cmd_install_service)
    sub.add_parser("uninstall-service", help="stop starting Rafiq at login").set_defaults(fn=cmd_uninstall_service)
    args = parser.parse_args(argv)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
