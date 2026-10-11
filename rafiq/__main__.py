"""rafiq run | ask | say | agent | agent-token | doctor | phone | mcp | toolcheck | google-setup |
instagram-setup | hue-setup | install-service | uninstall-service"""

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
    if args.server:
        return run_server(cfg)
    print("Loading the speech models (the first start downloads them)...", flush=True)
    assistant = Assistant(cfg)
    if not args.no_panel:
        host, port = cfg["panel"]["host"], int(cfg["panel"]["port"])
        server = serve(assistant, host, port, config.panel_token())
        threading.Thread(target=server.serve_forever, name="rafiq-panel", daemon=True).start()
        print(f"Panel: http://{host}:{port}/", flush=True)
    if cfg["phone"].get("enabled"):
        from .phone import PhoneBridge
        from .phone import serve as serve_phone

        bridge = PhoneBridge(cfg, assistant.tools, assistant.scheduler, on_event=assistant.events.publish)
        host, port = cfg["phone"]["host"], int(cfg["phone"]["port"])
        server = serve_phone(bridge, host, port, config.phone_token())
        threading.Thread(target=server.serve_forever, name="rafiq-phone", daemon=True).start()
        print(f"Phone bridge: http://{host}:{port}/api/inbox", flush=True)
    print('Say "Rafiq" (or "يا رفيق"). Ctrl+C stops.', flush=True)
    try:
        assistant.run()
    except KeyboardInterrupt:
        assistant.stop()
    return 0


def run_server(cfg: dict) -> int:
    """The brain without a microphone or a speaker: requests, the PC's actions, confirmations and
    alarms all go through `rafiq agent` on the PC."""
    from . import remote
    from .assistant import Context
    from .brain import Brain
    from .scheduler import Scheduler
    from .tools import load_all

    hub = remote.Hub()
    tokens = remote.AgentTokens(config.home() / "agents.json")

    def say_alarm(alarm):
        text = lang.phrase("timer_done" if alarm.kind == "timer" else "reminder", alarm.tag, label=alarm.label)
        hub.call("say", {"text": text, "tag": alarm.tag})

    def show(kind, data):
        if kind == "tool":
            print(f"  [{data['name']} {data['args']}] -> {str(data['result'])[:200]}", flush=True)
        elif kind == "error":
            print(f"  error: {data}", flush=True)

    scheduler = Scheduler(config.data_dir() / "alarms.json", say_alarm)
    tools = remote.remote_tools(load_all(cfg), hub)
    confirm = remote.agent_confirm(hub)
    brain = Brain(cfg, tools, confirm, on_event=show)
    one_at_a_time = threading.Lock()

    def ask(text: str, tag: str) -> str:
        with one_at_a_time:
            print(f"> {text}", flush=True)
            return brain.think(text, tag, Context(cfg, scheduler, tag, confirm=confirm, tools=tools))

    srv = cfg["server"]
    try:
        server = remote.serve(hub, tokens, ask, srv["host"], int(srv["port"]), srv["tls_cert"], srv["tls_key"])
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not tokens.names():
        print("No agent can connect yet: create a token with `rafiq agent-token NAME`.", flush=True)
    scheme = "https" if srv["tls_cert"] else "http"
    print(f"Brain server: {scheme}://{srv['host']}:{server.server_address[1]}/ (agents: "
          f"{', '.join(tokens.names()) or 'none'}). Ctrl+C stops.", flush=True)
    scheduler.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        server.server_close()
    return 0


def cmd_agent(args) -> int:
    """Lend this PC to a Rafiq server: run the actions it asks for, and type requests to it."""
    from . import remote
    from .tools import load_all

    cfg = config.load()
    agent_cfg = cfg["agent"]
    server, name, token = args.server or agent_cfg["server"], args.name or agent_cfg["name"], agent_cfg["token"]
    if not (server and name and token):
        print("Set [agent] server and name in the config, and the token in RAFIQ_AGENT_TOKEN "
              "(from `rafiq agent-token NAME` on the server).", file=sys.stderr)
        return 1
    tools = load_all(cfg)
    speaker = None
    if args.speak:
        from .tts import Speaker

        speaker = Speaker(cfg)

    def say(text: str, tag: str = "en") -> str:
        print(text, flush=True)
        if speaker:
            speaker.say(text, tag)
        return "said"

    def confirm(question: str, tag: str = "en") -> str:
        if speaker:
            speaker.say(question, tag)
        return "yes" if lang.yes_or_no(input(question + " ")) is True else "no"

    actions = {action: tools[action].fn for action in remote.ACTIONS}
    actions.update(say=say, confirm=confirm)
    try:
        agent = remote.Agent(server, name, token, actions, log=lambda text: print(f"  {text}", file=sys.stderr))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    poller = threading.Thread(target=agent.run_forever, name="rafiq-agent", daemon=True)
    poller.start()
    print(f"Lending this PC to {server} as {name!r}. Type a request (Ctrl+D or Ctrl+C stops).", flush=True)
    try:
        if not sys.stdin.isatty():
            poller.join()
            return 0
        while poller.is_alive():
            text = input("> ").strip()
            if text:
                try:
                    answer = agent.ask(text)
                except Exception as exc:
                    print(f"  error: {type(exc).__name__}", file=sys.stderr)
                    continue
                say(answer, lang.script_language(answer, lang.dialect(lang.guess(text), text)))
    except (EOFError, KeyboardInterrupt):
        pass
    agent.stop()
    return 0


def cmd_agent_token(args) -> int:
    """On the server: create (or remove) the token one PC uses to connect."""
    from . import remote

    tokens = remote.AgentTokens(config.home() / "agents.json")
    if args.remove:
        return 0 if tokens.remove(args.name) else 1
    try:
        token = tokens.add(args.name)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Token for {args.name} (shown once; on that PC: export RAFIQ_AGENT_TOKEN=...):\n{token}")
    return 0


def _terminal_confirm(action: str, tag: str) -> bool:
    answer = input(lang.phrase("confirm", tag, action=action) + " ")
    return lang.yes_or_no(answer) is True


def cmd_ask(args) -> int:
    """Type a request instead of saying it: the same brain and tools, answers printed (and spoken with
    --speak). Useful without a microphone and for testing."""
    from .assistant import Context
    from .brain import Brain
    from .facts import Facts
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
    # Only to turn presence on or off: the running Rafiq reads the saved setting at its next check.
    watcher = None
    if cfg["presence"].get("phone_ip"):
        from .presence import Presence

        watcher = Presence(config.data_dir() / "presence.json", lambda: False, lambda: None, lambda: None)
    facts = Facts(config.data_dir() / "memory.json")
    tools = load_all(cfg)
    brain = Brain(cfg, tools, _terminal_confirm, on_event=show, facts=facts)
    stream = Speaker(cfg).stream() if args.speak else None
    try:
        answer = brain.think(text, tag, Context(cfg, scheduler, tag, facts=facts, presence=watcher,
                                                confirm=_terminal_confirm, tools=tools),
                             on_speech=stream.put if stream else None)
        print(answer)
    finally:
        if stream:
            stream.close()
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
    _check("OpenAI API key", bool(cfg.get("openai_api_key")),
           brain["openai_model"] if cfg.get("openai_api_key") else "set OPENAI_API_KEY to use OpenAI")
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
    instagram = config.home() / "instagram" / "token.json"
    _check("Instagram statistics", instagram.is_file() and cfg["instagram"].get("enabled", False),
           "connected" if instagram.is_file() else "run: rafiq instagram-setup <token>")
    home_assistant = cfg["home_assistant"]
    _check("Home Assistant (Nest, Hue, plugs)", bool(home_assistant.get("url") and home_assistant.get("token")),
           home_assistant.get("url") or "set home_assistant.url and HOME_ASSISTANT_TOKEN")
    hue = config.home() / "hue.json"
    _check("Philips Hue", hue.is_file() and cfg["hue"].get("enabled", False),
           "paired" if hue.is_file() else "run: rafiq hue-setup")
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
    from .brain import Brain

    print(f"  brain in order: {', '.join(Brain(cfg, {}, lambda action, tag: False).backends())}")
    from . import timing

    print(f"  latency: {timing.line()}")
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


def cmd_phone(_args) -> int:
    """What to put in an iOS Shortcut or an Android HTTP Shortcut."""
    cfg = config.load()
    phone = cfg["phone"]
    print(f"Address:  http://{phone['host']}:{phone['port']}/api/inbox  (POST, JSON body)")
    print(f"Header:   Authorization: Bearer {config.phone_token()}")
    print('Body:     {"text": "film mode"}  or  {"kind": "note", "list": "shopping", "text": "milk"}')
    print('Reply:    {"ok": true, "answer": "..."}: have the shortcut speak or show "answer".')
    if not phone.get("enabled"):
        print("Turn it on: set [phone] enabled = true in", config.home() / "config.toml")
    if phone["host"] in ("127.0.0.1", "localhost", "::1"):
        print("It only listens on this computer: put a tunnel or a VPN in front of it to reach it from the phone.")
    return 0


def cmd_mcp(_args) -> int:
    from . import mcp_server

    return mcp_server.run()


def cmd_google_setup(_args) -> int:
    from .tools.google import setup

    print(setup())
    print("Then set [google] enabled = true in", config.home() / "config.toml")
    return 0


def cmd_instagram_setup(args) -> int:
    from .tools.instagram import setup

    print(setup(args.token))
    print("Then set [instagram] enabled = true in", config.home() / "config.toml")
    return 0


def cmd_hue_setup(args) -> int:
    from .tools import hue

    print(hue.setup(args.host or ""))
    print("Then set [hue] enabled = true in", config.home() / "config.toml")
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
    run.add_argument("--brain", choices=("auto", "claude", "openai", "ollama"))
    run.add_argument("--mode", choices=modes.MODES, help="everyday (fast), quality or offline")
    run.add_argument("--server", action="store_true", help="the brain only, for `rafiq agent` on another PC")
    run.set_defaults(fn=cmd_run)
    ask = sub.add_parser("ask", help="type a request instead of saying it")
    ask.add_argument("text", nargs="+")
    ask.add_argument("--lang", choices=lang.TAGS)
    ask.add_argument("--brain", choices=("auto", "claude", "openai", "ollama"))
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
    agent = sub.add_parser("agent", help="lend this PC (screen, speakers, apps) to a `rafiq run --server`")
    agent.add_argument("--server", help="the server's address, e.g. http://100.101.12.3:8766")
    agent.add_argument("--name", help="this agent's name, as given to `rafiq agent-token`")
    agent.add_argument("--speak", action="store_true", help="say answers and questions out loud")
    agent.set_defaults(fn=cmd_agent)
    token = sub.add_parser("agent-token", help="on the server: create the token a PC's agent connects with")
    token.add_argument("name")
    token.add_argument("--remove", action="store_true", help="remove that agent's token instead")
    token.set_defaults(fn=cmd_agent_token)
    sub.add_parser("listen", help="make the running Rafiq listen now (for a keyboard shortcut)").set_defaults(
        fn=cmd_listen)
    mute = sub.add_parser("mute", help="stop or start listening (for a keyboard shortcut)")
    mute.add_argument("state", nargs="?", choices=("on", "off"), help="on: stop listening; default: toggle")
    mute.set_defaults(fn=cmd_mute)
    sub.add_parser("doctor", help="check the microphone, models, voices and keys").set_defaults(fn=cmd_doctor)
    sub.add_parser("phone", help="show how to set up a phone shortcut for the phone bridge").set_defaults(
        fn=cmd_phone)
    sub.add_parser("mcp", help="offer the harmless tools to MCP clients such as Claude Desktop (stdio)").set_defaults(
        fn=cmd_mcp)
    sub.add_parser("toolcheck", help="measure how well the local model picks tools").set_defaults(
        fn=cmd_toolcheck)
    sub.add_parser("google-setup", help="connect your Google calendar and Gmail").set_defaults(fn=cmd_google_setup)
    insta = sub.add_parser("instagram-setup", help="connect your Instagram Business or Creator accounts")
    insta.add_argument("token", help="a user token from the Graph API Explorer of your Meta app")
    insta.set_defaults(fn=cmd_instagram_setup)
    hue = sub.add_parser("hue-setup", help="pair with the Philips Hue bridge (press its link button)")
    hue.add_argument("--host", help="the bridge's address on your network (default: look for it)")
    hue.set_defaults(fn=cmd_hue_setup)
    sub.add_parser("install-service", help="start Rafiq at every login (systemd user service)").set_defaults(
        fn=cmd_install_service)
    sub.add_parser("uninstall-service", help="stop starting Rafiq at login").set_defaults(fn=cmd_uninstall_service)
    args = parser.parse_args(argv)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
