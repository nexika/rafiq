"""rafiq run | ask | say | show | paper | agent | agent-token | satellite | gestures | doctor | status | update |
phone | mcp | toolcheck | google-setup | spotify-setup | instagram-setup | hue-setup | install-service |
uninstall-service | wake-pc"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
from pathlib import Path

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
        assistant.gestures.url = f"http://{host}:{server.server_address[1]}/api/gesture"
        print(f"Panel: http://{host}:{port}/", flush=True)
    _start_hub(cfg, assistant)
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


def _start_hub(cfg: dict, assistant) -> None:
    """Serve the satellites listed in the settings, if any, with the assistant's models and tools."""
    from . import satellite
    from .brain import Brain

    if not satellite.devices(cfg):
        return
    hub = satellite.Hub(cfg, transcribe=assistant.transcribe, tools=assistant.tools, scheduler=assistant.scheduler,
                        make_brain=lambda tools, confirm: Brain(cfg, tools, confirm, on_event=assistant.events.publish,
                                                              facts=assistant.facts),
                        on_event=assistant.events.publish)
    sats = cfg["satellites"]
    tls = satellite.server_tls(cfg)
    server = hub.serve(sats["host"], int(sats["port"]), tls)
    threading.Thread(target=server.serve_forever, name="rafiq-satellites", daemon=True).start()
    print(f"Satellites: {sats['host']}:{sats['port']} ({'TLS' if tls else 'no TLS: use a private network'}), "
          f"{', '.join(sorted(hub.devices))}", flush=True)


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


def cmd_paper(args) -> int:
    """Make today's morning paper: a PDF (or HTML with --html) of the weather, calendar, feeds and more."""
    from datetime import date

    from . import paper

    if args.paper_command:
        return cmd_paper_print(args)
    cfg = config.load()
    day = date.today()
    if args.demo:
        edition = paper.demo_edition(day, args.lang or None)
    else:
        if args.lang:
            cfg["paper"]["languages"] = args.lang
        edition = paper.gather(cfg, paper.LiveSources(cfg), day)
    suffix = ".html" if args.html else ".pdf"
    out = Path(args.out) if args.out else config.data_dir() / "paper" / f"{day.isoformat()}{suffix}"
    try:
        paper.make(edition, out, None if args.html else paper.ChromiumPrinter())
    except paper.PrintError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(out)
    return 0


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
    import importlib.util

    _check("hand gestures (mediapipe, opencv)", all(importlib.util.find_spec(m) for m in ("mediapipe", "cv2")),
           "" if cfg["gestures"].get("enabled") else "off; pip install rafiq[gestures], then rafiq gestures demo")
    spotify = config.home() / "spotify" / "token.json"
    _check("Spotify", spotify.is_file() and cfg["spotify"].get("enabled", False),
           "connected" if spotify.is_file() else "run: rafiq spotify-setup")
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


def cmd_status(_args) -> int:
    """Is Rafiq up, and is it safe? Works over SSH on a server without a screen."""
    from . import hosting

    checks = hosting.status(config.load())
    for check in checks:
        _check(check.label, check.ok, check.detail)
    return 0 if all(check.ok for check in checks) else 1


def cmd_update(args) -> int:
    from . import hosting

    code, lines = hosting.update(check=args.check, restart=not args.no_restart)
    print("\n".join(lines))
    return code


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


def cmd_gestures(args) -> int:
    """Hand gestures: on and off ask the running Rafiq (for a keyboard shortcut); demo and calibrate open
    the camera in a window."""
    if args.action in ("demo", "calibrate"):
        from . import gestures, tracker

        options = gestures.settings(config.load())
        return tracker.main([f"--{args.action}", "--hold", str(options["hold_seconds"]),
                             "--cooldown", str(options["cooldown_seconds"]), "--camera", str(options["camera"])])
    import httpx

    cfg = config.load()
    host, port = cfg["panel"]["host"], int(cfg["panel"]["port"])
    try:
        resp = httpx.post(f"http://{host}:{port}/api/gestures", json={"on": args.action == "on"}, timeout=5,
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


def cmd_show(_args) -> int:
    """Show the running Rafiq's last answer on screen again (also: say "show it again")."""
    return _ask_running("/api/show", {})


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


def cmd_paper_print(args) -> int:
    """The morning paper on paper: print it now (asking first), have the running Rafiq offer it, or install
    the systemd timer that does that every morning."""
    from . import paper_print as paper

    if args.paper_command == "print":
        result = paper.MorningPaper(config.load()).print_today(lambda action: _terminal_confirm(action, "en"))
        print(result)
        return 0 if result.startswith("printed") else 1
    if args.paper_command == "offer":
        return _ask_running("/api/paper", {})
    try:
        units = paper.timer_units(args.at, sys.executable)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    folder = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in units.items():
        (folder / name).write_text(text)
    print(f"Wrote {folder / paper.TIMER}. Turn it on with:\n"
          f"  systemctl --user daemon-reload && systemctl --user enable --now {paper.TIMER}")
    return 0


def cmd_wake_pc(args) -> int:
    """Start a computer that is off, with Wake-on-LAN (what a satellite does when the main computer is off)."""
    from .fallback import wake_pc

    try:
        wake_pc(args.mac, args.broadcast, args.port)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wake-on-LAN packet sent to {args.mac}.")
    return 0


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


def cmd_spotify_setup(_args) -> int:
    from .tools.spotify import setup

    print(setup(config.load()))
    print("Then set [spotify] enabled = true in", config.home() / "config.toml")
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
    news = sub.add_parser("paper", help="make today's morning paper (an A4 PDF), print it, or schedule that")
    news.add_argument("paper_command", nargs="?", choices=("print", "offer", "timer"),
                      help="print: print today's paper now (asks first); offer: have the running Rafiq offer to "
                           "print it (what the timer runs); timer: install a systemd user timer that offers it")
    news.add_argument("--at", default="07:00", help="with timer: time of day, HH:MM (default 07:00)")
    news.add_argument("--demo", action="store_true", help="made-up data, to see the layout")
    news.add_argument("--html", action="store_true", help="write HTML instead of printing a PDF")
    news.add_argument("--out", help="where to save it (default: the data folder, paper/<date>.pdf)")
    news.add_argument("--lang", nargs="+", choices=lang.TAGS, help="languages, e.g. --lang en ar")
    news.set_defaults(fn=cmd_paper)
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
    sub.add_parser("show", help="show the last answer on screen again").set_defaults(fn=cmd_show)
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
    mute = sub.add_parser("mute", help="stop or start listening (for a keyboard shortcut)")
    mute.add_argument("state", nargs="?", choices=("on", "off"), help="on: stop listening; default: toggle")
    mute.set_defaults(fn=cmd_mute)
    gestures = sub.add_parser("gestures", help="webcam hand gestures: on, off, demo (camera with landmarks), "
                              "calibrate (tune the timing)")
    gestures.add_argument("action", choices=("on", "off", "demo", "calibrate"))
    gestures.set_defaults(fn=cmd_gestures)
    sub.add_parser("doctor", help="check the microphone, models, voices and keys").set_defaults(fn=cmd_doctor)
    sub.add_parser("status", help="is Rafiq running, its version, and is the panel private").set_defaults(
        fn=cmd_status)
    update = sub.add_parser("update", help="pull the new version, reinstall it and restart the service")
    update.add_argument("--check", action="store_true", help="only say whether there is a new version")
    update.add_argument("--no-restart", action="store_true", help="don't restart the rafiq service")
    update.set_defaults(fn=cmd_update)
    sub.add_parser("phone", help="show how to set up a phone shortcut for the phone bridge").set_defaults(
        fn=cmd_phone)
    sub.add_parser("mcp", help="offer the harmless tools to MCP clients such as Claude Desktop (stdio)").set_defaults(
        fn=cmd_mcp)
    sub.add_parser("toolcheck", help="measure how well the local model picks tools").set_defaults(
        fn=cmd_toolcheck)
    sub.add_parser("google-setup", help="connect your Google calendar and Gmail").set_defaults(fn=cmd_google_setup)
    wake = sub.add_parser("wake-pc", help="start a computer that is off, with Wake-on-LAN")
    wake.add_argument("mac", help="its network card address, such as aa:bb:cc:dd:ee:ff")
    wake.add_argument("--broadcast", default="255.255.255.255", help="the network's broadcast address")
    wake.add_argument("--port", type=int, default=9)
    wake.set_defaults(fn=cmd_wake_pc)
    sub.add_parser("spotify-setup", help="connect your Spotify account").set_defaults(fn=cmd_spotify_setup)
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
