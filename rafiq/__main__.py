"""rafiq setup | run | ask | say | dictate | notify | hooks | show | paper | agent | agent-token | satellite |
gestures | doctor | status | update | phone | mobile | mcp | toolcheck | google-setup | chatgpt-login |
chatgpt-logout | spotify-setup | instagram-setup | hue-setup | browser | install-service | uninstall-service |
wake-pc | backup | stt-score | enroll | voiceid | listen"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from . import config, lang, modes, privacy, sock


def cmd_run(args) -> int:
    from . import service
    from .assistant import Assistant
    from .panel import serve

    service.load_env()         # the launchd agent's keys, from rafiq.env (#192); nothing where it isn't set
    cfg = config.load()
    if args.brain:
        cfg["brain"]["mode"] = args.brain
    privacy.start_logging(config.data_dir() / "logs", int(cfg["privacy"]["log_days"]))
    if args.mode:
        modes.switch(cfg, args.mode)
    _stop_on_sigterm()
    if args.server:
        return run_server(cfg)
    if cfg["mobile"].get("enabled"):
        _check_mobile(cfg)                  # before the models load: a setting to fix stops at once
    print("Loading the speech models (the first start downloads them)...", flush=True)
    assistant = Assistant(cfg)
    if not args.no_panel:
        host, port = cfg["panel"]["host"], int(cfg["panel"]["port"])
        server = serve(assistant, host, port, config.panel_token())
        threading.Thread(target=server.serve_forever, name="rafiq-panel", daemon=True).start()
        _start_socket(server)
        assistant.gestures.url = f"http://{host}:{server.server_address[1]}/api/gesture"
        if sock.available():
            print("Panel: open it with `rafiq panel`", flush=True)
        else:
            _open_panel_here(server)
    _start_hub(cfg, assistant)
    if cfg["phone"].get("enabled"):
        _start_phone(cfg, assistant)
    if cfg["mobile"].get("enabled"):
        _start_mobile(cfg, assistant)
    print('Say "Rafiq" (or "يا رفيق"). Ctrl+C stops.', flush=True)
    try:
        assistant.run()
    except KeyboardInterrupt:
        assistant.stop()
    return 0


def _start_socket(tcp):
    """The panel's socket for Rafiq's own programs (#234); without it Rafiq still runs, and they say it isn't
    reachable."""
    from . import panel

    if not sock.available():               # Windows (#193): its programs say they can't reach Rafiq yet
        return None
    try:
        server = panel.serve_socket(tcp)
    except (OSError, ValueError) as exc:
        print(f"Rafiq's own programs (rafiq ask, rafiq mcp, the widget) can't reach it: {exc}", file=sys.stderr)
        return None
    threading.Thread(target=server.serve_forever, name="rafiq-panel-socket", daemon=True).start()
    return server


def _open_panel_here(server) -> None:
    """Where `rafiq panel` can't ask the running Rafiq for a key (Windows, #193), Rafiq started in a terminal opens
    its panel itself, once, with a key it makes. The logon task opens no browser at every login."""
    port = server.server_address[1]
    if not (sys.stdout and sys.stdout.isatty()):
        print(f"Panel: http://127.0.0.1:{port}, opened when you start `rafiq run` in a terminal", flush=True)
        return
    _open_browser(f"http://127.0.0.1:{port}/#key={server.RequestHandlerClass.new_key()}")
    print("Panel: opened in your browser (once; start `rafiq run` again in a terminal to open it again)", flush=True)


def _stop_on_sigterm() -> None:
    """`systemctl stop` sends SIGTERM, whose default skips atexit hooks (Firefox then keeps a session it refuses
    to share); end the way Ctrl+C does instead."""
    signal.signal(signal.SIGTERM, signal.default_int_handler)


def _start_mobile(cfg: dict, assistant):
    """Serve the phone companion's page for `tailscale serve`: on its socket, or on 127.0.0.1's port when the settings
    ask for it. One that can't start stops Rafiq: running on without it would leave `tailscale serve` pointing at
    whatever another account put there, with the paired phones' tokens (#238)."""
    from . import mobile

    if not sock.available():               # it needs a socket only this user can open, or /proc (#193)
        print(MOBILE_NOT_YET, file=sys.stderr, flush=True)
        return None
    port, listen = int(cfg["mobile"]["port"]), cfg["mobile"].get("listen", "socket")
    devices = mobile.Devices(config.home() / "mobile")
    name, ips = tailnet_self()
    own = mobile.OwnAddresses(lambda: tailnet_self()[1], known=ips)
    try:
        if listen == "socket":
            server = mobile.serve_socket(assistant, port, devices, tailnet=name, own_ips=own)
            where = f"{mobile.socket_path()}, for `tailscale serve --bg unix:{mobile.socket_path()}`"
        elif listen == "port":
            server = mobile.serve(assistant, "127.0.0.1", port, devices, tailnet=name, own_ips=own)
            where = f"http://127.0.0.1:{port}, for `tailscale serve` (another account here could take this port)"
        else:
            raise ValueError(f'[mobile] listen is "socket" or "port", not {listen!r}')
    except (ValueError, OSError) as exc:
        _mobile_cannot_start(exc)
    threading.Thread(target=server.serve_forever, name="rafiq-mobile", daemon=True).start()
    print(f"Phone page: {where} (rafiq mobile pair NAME)", flush=True)
    return server


# A setting to fix (sysexits' EX_CONFIG): the systemd unit doesn't start Rafiq again on it, which would only load
# the speech models and stop, every few seconds, for good.
EXIT_CONFIG = 78


def _mobile_cannot_start(exc: Exception):
    from . import service

    print(f"The phone page can't start: {exc}. Fix it, or set [mobile] enabled = false.", file=sys.stderr)
    service.refresh()           # a unit from before #238 would restart this every 5 s; systemd reloads it first
    sys.exit(EXIT_CONFIG)


MOBILE_NOT_YET = "The phone page is not on Windows yet; Rafiq runs without it ([mobile] enabled is ignored)."


def _check_mobile(cfg: dict) -> None:
    from . import mobile

    if not sock.available():
        return
    try:
        mobile.check_start(cfg)
    except (ValueError, OSError) as exc:
        _mobile_cannot_start(exc)


def _start_phone(cfg: dict, assistant):
    """Serve the phone bridge on a private address; when the address is refused, Rafiq runs without it."""
    from . import phone

    try:
        host, port = phone.address(cfg["phone"]["host"]), int(cfg["phone"]["port"])
    except ValueError as exc:
        print(f"Phone bridge off: {exc}", flush=True)
        return None
    bridge = phone.PhoneBridge(cfg, assistant.tools, assistant.scheduler, on_event=assistant.events.publish)
    try:
        server = phone.serve(bridge, host, port, config.phone_token())
    except (ValueError, OSError) as exc:          # the port is taken, or the address can't be bound
        print(f"Phone bridge off: {exc}", flush=True)
        return None
    threading.Thread(target=server.serve_forever, name="rafiq-phone", daemon=True).start()
    print(f"Phone bridge: http://{host}:{port}/api/inbox", flush=True)
    return server


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
    from .budget import Budget
    from .facts import Facts
    from .scheduler import Scheduler
    from .tools import load_all
    from .tts import Speaker

    cfg = config.load()
    text = " ".join(args.text)
    if not (args.brain or args.mode):
        answered = _ask_through_running(cfg, text, args.lang, args.speak)
        if answered is not None:
            return answered
    if args.brain:
        cfg["brain"]["mode"] = args.brain
    if args.mode:
        modes.switch(cfg, args.mode)
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
    budget = Budget(cfg)
    brain = Brain(cfg, tools, _terminal_confirm, on_event=show, facts=facts, budget=budget)
    stream = Speaker(cfg, budget=budget).stream() if args.speak else None
    try:
        answer = brain.think(text, tag, Context(cfg, scheduler, tag, facts=facts, presence=watcher,
                                                confirm=_terminal_confirm, tools=tools, budget=budget),
                             on_speech=stream.put if stream else None)
        print(answer)
    finally:
        if stream:
            stream.close()
    return 0


def _ask_through_running(cfg: dict, text: str, tag: str | None, speak: bool) -> int | None:
    """Have the running Rafiq answer, so its tools are shared (Firefox allows one session). None when no Rafiq
    took the request: nothing runs there, or an older one has no /api/ask. A request it took is never sent
    again here, even when the answer comes too late."""
    import httpx

    from .panel import ASK_SECONDS

    host, port = cfg["panel"]["host"], int(cfg["panel"]["port"])
    if not sock.available():
        from . import hosting

        # Windows (#193): this can't reach the running Rafiq yet, and answering here next to it would save timers
        # and facts that it overwrites from its own copy. Only an answer here when none runs; the probe sends no
        # token.
        if hosting.panel_up(host, port):
            print("Rafiq is running, and `rafiq ask` can't reach it on Windows yet: say it to Rafiq, or type it in "
                  "the panel.", file=sys.stderr)
            return 1
        return None
    try:
        resp = sock.post(f"http://{host}:{port}/api/ask", json={"text": text, "lang": tag, "speak": speak},
                          headers={"X-Rafiq-Token": config.panel_token()},
                          timeout=httpx.Timeout(ASK_SECONDS + 10, connect=2))
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return None
    except httpx.HTTPError:
        print("The running Rafiq took the request but didn't answer in time.", file=sys.stderr)
        return 1
    if resp.status_code == 504:
        print("The running Rafiq didn't answer in time.", file=sys.stderr)
        return 1
    if resp.status_code != 200:
        return None
    print(resp.json().get("answer", ""))
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


def _check(label: str, ok: bool, detail: str = "", fix: str = "", optional: bool = False) -> bool:
    mark = "ok " if ok else "-- " if optional else "no "
    print(f"  {mark} {label}" + (f": {detail}" if detail else ""))
    if fix:
        print(f"        {'tip' if ok or optional else 'fix'}: {fix}")
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
    from . import doctor

    cfg = config.load()
    print(f"Rafiq settings: {config.home() / 'config.toml'}")
    _check_microphone(cfg)
    _check("audio player (ffplay)", bool(shutil.which("ffplay")))
    from . import desktop
    from .dictation import Keyboard

    if desktop.system() == "windows":
        _check("dictation (typing into windows)", True, "not on Windows yet", optional=True)
    elif desktop.system() != "mac":         # on macOS, System Events: "Typing and keys (Accessibility)" below
        typing = ("wtype", "ydotool", "wl-copy") if Keyboard().wayland else ("xdotool", "xclip")
        found = [t for t in typing if shutil.which(t)]
        _check("dictation (typing into windows)", bool(found), ", ".join(found) or "install " + " or ".join(typing))
    print("Integrations:")
    for check in doctor.integrations(cfg):
        _check(check.label, check.ok, check.detail, check.fix, check.optional)
    brain = cfg["brain"]
    from . import chatgpt

    plan = chatgpt.status()
    _check("ChatGPT plan", plan["signed_in"] and plan["plan"],
           ("signed in, plan usage allowed" if plan["plan"] else "signed in, but plan usage wasn't allowed: "
            "run rafiq chatgpt-login") if plan["signed_in"] else "run rafiq chatgpt-login (Plus or Pro)", optional=True)
    from . import mobile

    phones = mobile.Devices(config.home() / "mobile").names()
    _check("phone page", bool(cfg["mobile"].get("enabled") and phones),
           (f"on, {len(phones)} phone(s) paired" if phones else "on, no phone paired: run rafiq mobile pair NAME")
           if cfg["mobile"].get("enabled") else "off; set [mobile] enabled = true, then rafiq mobile pair NAME",
           optional=True)
    _check("OpenAI API key", bool(cfg.get("openai_api_key")),
           brain["openai_model"] if cfg.get("openai_api_key") else "set OPENAI_API_KEY to use OpenAI")
    from .tts import Speaker

    speaker = Speaker(cfg)
    _check("ElevenLabs voices", bool(speaker.eleven_key),
           speaker.cfg.get("elevenlabs_model", "") if speaker.eleven_key else "set ELEVENLABS_API_KEY")
    kokoro = speaker.kokoro_paths()
    _check("Kokoro offline voices (en, fr, es)", kokoro is not None,
           "found" if kokoro else "missing (see README)")
    for tag in ("en", "fr", "es", "de", "ar"):
        path = speaker.piper_path(tag)
        _check(f"offline voice {tag}", path is not None, path.name if path else "missing")
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
    from . import nudges

    if nudges.settings(cfg)["enabled"]:
        why = nudges.missing(cfg)
        _check("calendar nudges", why is None, why or f"from {nudges.settings(cfg)['origin']}")
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
    print("Models for this machine:")
    for line in doctor.advice_lines(cfg, doctor.machine()):
        print(line)
    return 0


def cmd_notify(args) -> int:
    """Tell the running Rafiq that a long job ended; it says so unless muted or in quiet hours."""
    return _ask_running("/api/notify", {"text": " ".join(args.text), "event": args.event or "",
                                        "lang": args.lang})


def _open_browser(url: str) -> None:
    import webbrowser

    webbrowser.open(url)


def cmd_panel(args) -> int:
    """Open the panel: a one-time key from the running Rafiq, after "#" so no server ever receives it, becomes the
    page's session; the page itself never carries the panel token (#222). --print shows the address instead,
    for an SSH tunnel."""
    import httpx

    cfg = config.load()
    host, port = cfg["panel"]["host"], int(cfg["panel"]["port"])
    try:
        resp = sock.post(f"http://{host}:{port}/api/panel-key", json={}, timeout=5,
                          headers={"X-Rafiq-Token": config.panel_token()})
    except httpx.HTTPError as exc:
        return _not_reached(exc)
    if resp.status_code != 200:
        print("The running Rafiq refused to open its panel; restart it (an older one has no rafiq panel).",
              file=sys.stderr)
        return 1
    # Always the address the panel binds, never the name "localhost": a browser tries ::1 first for that name,
    # where another account's program could be listening, read the key after "#" and redeem it.
    url = f"http://127.0.0.1:{port}/#key={resp.json()['key']}"
    if args.print:
        print(url)
        print("It works once, within two minutes.", file=sys.stderr)
    else:
        _open_browser(url)
    return 0


def _not_reached(exc: Exception) -> int:
    """Why the running Rafiq couldn't be asked: not running, or (on Windows, #193) no way to ask it yet."""
    if isinstance(exc, sock.Unavailable):
        print(exc, file=sys.stderr)
    else:
        print("Rafiq is not running (start it with: rafiq run)", file=sys.stderr)
    return 1


def cmd_hooks(args) -> int:
    from .notify import HOOKS

    print(HOOKS[args.kind], end="")
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


def cmd_dictate(_args) -> int:
    """Start typing what you say into the focused window, or stop: bind this to a keyboard shortcut."""
    return _ask_running("/api/dictate", {})


def _ask_running(path: str, body: dict) -> int:
    """Send a request to the running Rafiq through its panel."""
    import httpx

    cfg = config.load()
    host, port = cfg["panel"]["host"], int(cfg["panel"]["port"])
    try:
        resp = sock.post(f"http://{host}:{port}{path}", json=body, timeout=5,
                          headers={"X-Rafiq-Token": config.panel_token()})
    except httpx.HTTPError as exc:
        return _not_reached(exc)
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
        resp = sock.post(f"http://{host}:{port}/api/gestures", json={"on": args.action == "on"}, timeout=5,
                          headers={"X-Rafiq-Token": config.panel_token()})
    except httpx.HTTPError as exc:
        return _not_reached(exc)
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


def cmd_voiceid(args) -> int:
    """Build the voice print from the enroll recordings, so only your spoken yes confirms an action."""
    from . import voiceid

    if args.off:
        for name in ("voiceprint.npy", "voiceprint.json"):
            (voiceid.folder() / name).unlink(missing_ok=True)
        print("Voice ID is off: any spoken yes confirms again.")
        return 0
    try:
        report = voiceid.from_recordings(Path(args.others) if args.others else None)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Voice print saved from {report['recordings']} recordings; a yes needs a similarity of "
          f"{report['threshold']:.2f}.")
    print(f"  your own recordings refused: {report['false_rejects']} of {report['mine']}")
    if report["others"]:
        print(f"  other recordings accepted: {report['false_accepts']} of {report['others']}")
    else:
        print("  add --others DIR (.wav files of other people) to count false accepts too")
    return 0


def cmd_setup(args) -> int:
    from . import setup

    return setup.run(enroll=not args.no_enroll)


def cmd_stt_score(args) -> int:
    """Score speech-to-text on your own recordings (name.wav with name.txt), for each Whisper model, with
    and without the handling of English words inside Arabic."""
    from pathlib import Path

    from .stt import Transcriber, score

    folder = Path(args.folder)
    if not any(folder.glob("*.wav")):
        print(f"No .wav recordings in {folder}", file=sys.stderr)
        return 1
    languages = config.load()["speech"]["languages"]
    for model in args.models:
        for mixed in (False, True):
            results = score(folder, Transcriber(model, languages, mixed=mixed))
            if not results:
                print(f"No recording in {folder} has its text next to it (name.wav and name.txt)", file=sys.stderr)
                return 1
            mean = sum(r.error for r in results) / len(results)
            print(f"{model}, mixed {'on' if mixed else 'off'}: {mean:.0%} word errors over {len(results)} recordings")
            if args.verbose:
                for r in results:
                    print(f"  {r.error:4.0%} {r.name}: {r.heard}")
    return 0


def cmd_satellite(args) -> int:
    """This computer as a microphone and speaker in another room, a simulator, or a new device's token."""
    import secrets

    from . import satellite

    cfg = config.load()
    if args.action == "run":
        return satellite.run_device(cfg)
    if args.action == "scores":
        return satellite.print_scores(cfg)
    if args.action == "unit":
        print(satellite.unit(sys.executable), end="")
        return 0
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
    from . import phone as phone_mod

    cfg = config.load()
    phone = cfg["phone"]
    try:
        host = phone_mod.address(phone["host"])
    except ValueError as exc:
        print(f"The phone bridge can't listen there: {exc}")
        return 1
    print(f"Address:  http://{host}:{phone['port']}/api/inbox  (POST, JSON body)")
    print(f"Header:   Authorization: Bearer {config.phone_token()}")
    print('Body:     {"text": "film mode"}  or  {"kind": "note", "list": "shopping", "text": "milk"}')
    print('Reply:    {"ok": true, "answer": "..."}: have the shortcut speak or show "answer".')
    if not phone.get("enabled"):
        print("Turn it on: set [phone] enabled = true in", config.home() / "config.toml")
    if host in ("127.0.0.1", "localhost"):
        print('It only listens on this computer: set [phone] host = "tailscale" to reach it from the phone.')
    return 0


def tailnet_self(run=subprocess.run) -> tuple[str, tuple[str, ...]]:
    """This computer's name on the tailnet (desk.tail1234.ts.net), where `tailscale serve` answers, and its own
    Tailscale addresses, from which the phone page refuses what tailscaled brings (#241); ("", ()) if unknown."""
    try:
        done = run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)
        me = json.loads(done.stdout).get("Self", {}) if done.returncode == 0 else {}
        name, ips = me.get("DNSName", ""), me.get("TailscaleIPs") or []
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return "", ()
    name = name.rstrip(".") if isinstance(name, str) else ""
    return name, tuple(ip for ip in ips if isinstance(ip, str)) if isinstance(ips, list) else ()


def tailnet_name(run=subprocess.run) -> str:
    """This computer's name on the tailnet; "" if unknown."""
    return tailnet_self(run)[0]


def cmd_mobile(args) -> int:
    """Pair a phone with Rafiq's companion page, list the paired phones, or unpair one (#207)."""
    from . import mobile

    if not sock.available():
        print(MOBILE_NOT_YET, file=sys.stderr)
        return 1

    cfg = config.load()
    devices = mobile.Devices(config.home() / "mobile")
    if args.action == "devices":
        names = devices.names()
        print("\n".join(names) if names else "No phone is paired: run `rafiq mobile pair NAME`.")
        return 0
    if not args.name:
        print(f"Give the phone's name: rafiq mobile {args.action} \"Sam's iPhone\"", file=sys.stderr)
        return 1
    if args.action == "unpair":
        if devices.remove(args.name):
            print(f"{args.name} is unpaired: it can't reach Rafiq any more.")
            return 0
        print(f"No phone is paired as {args.name}. Paired: {', '.join(devices.names()) or 'none'}", file=sys.stderr)
        return 1
    try:
        code = devices.start_pairing(args.name)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    port = int(cfg["mobile"]["port"])
    name = tailnet_name()
    print(f"Pairing code for {args.name.strip()}: {code[:4]}-{code[4:]}  (good for 5 minutes)")
    print()
    if not cfg["mobile"].get("enabled"):
        print(f"1. Turn the phone page on: set [mobile] enabled = true in {config.home() / 'config.toml'}, then "
              "restart Rafiq.")
    else:
        print("1. The phone page is on.")
    if cfg["mobile"].get("listen", "socket") == "port":
        print(f"2. Once, so your tailnet reaches it over HTTPS: tailscale serve --bg http://127.0.0.1:{port}")
        print("   On a port, another account here could take it while Rafiq is stopped and collect your phone's")
        print('   token: set [mobile] listen = "socket" when your Tailscale can serve a socket.')
    else:
        print(f"2. Once, so your tailnet reaches it over HTTPS: sudo tailscale serve --bg unix:{mobile.socket_path()}")
        print(f"   Set up before with http://127.0.0.1:{port}? First: tailscale serve --https=443 off")
        print('   (A Tailscale that can\'t serve a socket says so: then set [mobile] listen = "port".)')
    print("   (Tailscale asks you to turn on HTTPS for your tailnet the first time. Never use `tailscale funnel`:")
    print("   that would put Rafiq on the internet.)")
    where = f"https://{name}/" if name else "the https://…ts.net address that `tailscale serve status` shows"
    print(f"3. On the phone, with Tailscale on, open {where} and type the code.")
    print("4. Keep it on the home screen: Share, then Add to Home Screen (iPhone, Safari), or the menu, then")
    print("   Install app (Android, Chrome).")
    return 0


def cmd_mcp(args) -> int:
    from . import mcp_server

    if getattr(args, "list", False):
        server = mcp_server.Server(config.load())
        for name, tool in sorted(server.tools.items()):
            where = "here" if name in server.local else ("asks first" if tool.level in ("act", "critical")
                                                       else "in the running Rafiq")
            print(f"{name:28} {tool.level:9} {where}")
        return 0
    return mcp_server.run()


def cmd_plugin(args) -> int:
    """Package Rafiq for the AI app the user has, under their nickname, and say how to install it."""
    from . import plugin

    cfg = config.load()
    slug, nick = plugin.name(cfg)
    if args.host == "claude-code":
        try:
            root = plugin.claude_code(cfg, Path(args.out) if args.out else config.home() / "plugin", args.notify)
        except plugin.NotOurs as exc:
            print(f"{exc}: give rafiq plugin an empty folder, or the one it made before", file=sys.stderr)
            return 1
        print(f"{nick} for Claude Code is in {root}. Install it with:\n"
              f"  claude plugin marketplace add {root}\n  claude plugin install {slug}@{slug}-local\n"
              "Then keep `rafiq run` running: it asks before anything changes and shows the work in its widget.")
    elif args.host == "claude-desktop":
        path = plugin.claude_desktop(cfg, Path(args.out) if args.out else config.home())
        print(f"{nick} for Claude Desktop: {path}\nOpen it with Claude Desktop (or drag it to Settings > "
              "Extensions) to install it, and keep `rafiq run` running.")
    else:
        print("# Add to ~/.codex/config.toml; the long tool_timeout_sec lets Codex wait for your yes.")
        print(plugin.codex(cfg), end="")
    hint = 'To let the app be the brain, set [brain] mode = "off": Rafiq then points what you say to it.'
    print(("# " if args.host == "codex" else "") + hint)    # Codex's lines may be pasted as they are
    return 0


def cmd_google_setup(_args) -> int:
    from .tools.google import setup

    print(setup())
    print("Then set [google] enabled = true in", config.home() / "config.toml")
    return 0


def cmd_backup(args) -> int:
    """Save settings, recordings and everything else Rafiq keeps to a private archive on this computer."""
    from . import maintain

    try:
        path = maintain.backup(args.to)
    except (maintain.MaintenanceError, OSError) as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Saved {path} (readable only by you; it holds your keys, keep it off public places).")
    return 0


def cmd_chatgpt_login(args) -> int:
    from . import chatgpt

    said = chatgpt.login(config.load(), new=getattr(args, "new", False))
    print(said)
    return 0 if said.startswith("Signed in") and chatgpt.signed_in() else 1


def cmd_chatgpt_logout(_args) -> int:
    from . import chatgpt

    print(chatgpt.logout())
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


def cmd_browser(args) -> int:
    """Start Rafiq's own browser, whose tabs Rafiq can list, read and switch."""
    from . import browser

    cfg = config.load()
    try:
        browser.launch(cfg, args.url or "")
        engine, _ = browser.find(cfg)
    except browser.BrowserError as exc:
        print(exc)
        return 1
    print(f"Rafiq's browser is starting, with its own profile in {browser.profile_dir(engine)}.")
    print("Sign in there only to the sites you want Rafiq to read; your everyday browser is not touched.")
    if not cfg["browser"].get("enabled"):
        print("Then set [browser] enabled = true in", config.home() / "config.toml")
    return 0


def cmd_hue_setup(args) -> int:
    from .tools import hue

    print(hue.setup(args.host or ""))
    print("Then set [hue] enabled = true in", config.home() / "config.toml")
    return 0


def cmd_install_service(_args) -> int:
    """Start Rafiq at every login: a systemd user service, a launchd agent, or a Windows logon task."""
    from . import service

    try:
        path = service.install()
    except service.ServiceError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Rafiq now starts at login ({path}) and is running.")
    from . import desktop

    if desktop.system() == "windows":
        print(f"Logs: Get-Content -Wait \"{config.home() / 'task.log'}\"")
        print(f"The task sees keys set with setx, or ANTHROPIC_API_KEY=... lines in {service.env_file()}")
        if os.environ.get("RAFIQ_HOME"):
            print("Set RAFIQ_HOME with setx too, or the task uses the folder in %APPDATA%")
        return 0
    if desktop.system() == "mac":
        print(f"Logs: tail -f {config.home() / 'launchd.log'}")
    else:
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


def _rerun_in_utf8(argv, popen=subprocess.Popen) -> int | None:
    """On Windows, `rafiq` from a terminal runs once more in Python's UTF-8 mode, as the logon task does (-X utf8):
    otherwise every file and pipe without an encoding is in the ANSI code page, where an Arabic reminder can't be
    saved and the clipboard's Arabic can't be read. None where nothing needs to run again."""
    from . import desktop

    if desktop.system() != "windows" or sys.flags.utf8_mode:
        return None
    env = {**os.environ, "PYTHONUTF8": "1"}       # this Rafiq's own Python children, too
    child = popen([sys.executable, "-X", "utf8", "-P", "-m", "rafiq", *(sys.argv[1:] if argv is None else argv)],
                  env=env)
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:             # Ctrl+C reaches the child too; wait for it to end its own way
            continue


def main(argv=None) -> int:
    rerun = _rerun_in_utf8(argv)
    if rerun is not None:
        return rerun
    parser = argparse.ArgumentParser(prog="rafiq", description="Rafiq (رفيق), a voice assistant in six languages")
    sub = parser.add_subparsers(dest="command", required=True)
    wizard = sub.add_parser("setup", help="first-run wizard: keys, voices, models, microphone, wake word")
    wizard.add_argument("--no-enroll", action="store_true", help="skip recording the wake word")
    wizard.set_defaults(fn=cmd_setup)
    run = sub.add_parser("run", help="listen for 'Rafiq' and answer; opens the panel")
    run.add_argument("--no-panel", action="store_true")
    run.add_argument("--brain", choices=("auto", "chatgpt", "claude", "openai", "ollama", "off"))
    run.add_argument("--mode", choices=modes.MODES, help="everyday (fast), quality or offline")
    run.add_argument("--server", action="store_true", help="the brain only, for `rafiq agent` on another PC")
    run.set_defaults(fn=cmd_run)
    ask = sub.add_parser("ask", help="type a request instead of saying it")
    ask.add_argument("text", nargs="+")
    ask.add_argument("--lang", choices=lang.TAGS)
    ask.add_argument("--brain", choices=("auto", "chatgpt", "claude", "openai", "ollama", "off"))
    ask.add_argument("--mode", choices=modes.MODES, help="everyday (fast), quality or offline")
    ask.add_argument("--speak", action="store_true", help="also say the answer")
    ask.set_defaults(fn=cmd_ask)
    panel_cmd = sub.add_parser("panel", help="open the running Rafiq's panel in the browser")
    panel_cmd.add_argument("--print", action="store_true", help="show its one-time address instead (SSH tunnel)")
    panel_cmd.set_defaults(fn=cmd_panel)
    say = sub.add_parser("say", help="say a sentence, to try a voice")
    say.add_argument("text", nargs="+")
    say.add_argument("--lang", choices=lang.TAGS, default="en")
    say.set_defaults(fn=cmd_say)
    notify = sub.add_parser("notify", help="have the running Rafiq say that a long job ended")
    notify.add_argument("text", nargs="+")
    event = notify.add_mutually_exclusive_group()
    event.add_argument("--done", dest="event", action="store_const", const="done",
                       help='say "Finished: TEXT" in your language')
    event.add_argument("--failed", dest="event", action="store_const", const="failed",
                       help='say "Failed: TEXT" in your language')
    notify.add_argument("--lang", choices=lang.TAGS, help="default: the language you last spoke")
    notify.set_defaults(fn=cmd_notify)
    hooks = sub.add_parser("hooks", help="print a ready-made hook that calls rafiq notify")
    hooks.add_argument("kind", choices=("claude-code", "shell"))
    hooks.set_defaults(fn=cmd_hooks)
    enroll = sub.add_parser("enroll", help="record yourself saying the name, for a reliable wake word")
    enroll.add_argument("--forms", nargs="+", help='the ways you call it (default: "Rafiq" "يا رفيق" and [wake] names)')
    enroll.add_argument("--takes", type=int, default=3, help="recordings of each form (default 3)")
    enroll.add_argument("--sensitivity", type=float,
                        help="refit the saved recordings: 0 strict ... 1 loose (default 0.45)")
    enroll.set_defaults(fn=cmd_enroll)
    voice = sub.add_parser("voiceid", help="only your voice can confirm actions (uses the enroll recordings)")
    voice.add_argument("--others", metavar="DIR", help="16 kHz .wav files of other voices, to count false accepts")
    voice.add_argument("--off", action="store_true", help="delete the voice print")
    voice.set_defaults(fn=cmd_voiceid)
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
    sub.add_parser("dictate", help="start or stop typing what you say (for a keyboard shortcut)").set_defaults(
        fn=cmd_dictate)
    score = sub.add_parser("stt-score", help="score speech-to-text on your recordings (name.wav + name.txt)")
    score.add_argument("folder")
    score.add_argument("--models", nargs="+", default=["small", "medium"], help="default: small medium")
    score.add_argument("-v", "--verbose", action="store_true", help="show what was heard in each recording")
    score.set_defaults(fn=cmd_stt_score)
    sub.add_parser("show", help="show the last answer on screen again").set_defaults(fn=cmd_show)
    sat = sub.add_parser("satellite", help="a microphone and speaker in another room")
    sat.add_argument("action", choices=("run", "simulate", "token", "scores", "unit"),
                     help="run: be a satellite; simulate: send a WAV to the hub; token: make a new device's "
                          "token; scores: print each utterance's wake score; unit: print a systemd user unit")
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
    sub.add_parser("doctor", help="check the microphone, voices, keys and integrations; suggest models").set_defaults(
        fn=cmd_doctor)
    sub.add_parser("status", help="is Rafiq running, its version, and is the panel private").set_defaults(
        fn=cmd_status)
    update = sub.add_parser("update", help="pull the new version, reinstall it and restart the service")
    update.add_argument("--check", action="store_true", help="only say whether there is a new version")
    update.add_argument("--no-restart", action="store_true", help="don't restart the rafiq service")
    update.set_defaults(fn=cmd_update)
    sub.add_parser("phone", help="show how to set up a phone shortcut for the phone bridge").set_defaults(
        fn=cmd_phone)
    mobile = sub.add_parser("mobile", help="pair a phone with Rafiq's companion page, list or unpair phones")
    mobile.add_argument("action", choices=("pair", "devices", "unpair"))
    mobile.add_argument("name", nargs="?", default="", help="the phone's name, such as \"Sam's iPhone\"")
    mobile.set_defaults(fn=cmd_mobile)
    mcp = sub.add_parser("mcp", help="offer Rafiq's tools to MCP clients such as Claude Code and Claude Desktop")
    mcp.add_argument("--list", action="store_true", help="show the tools offered, and which ask first")
    mcp.set_defaults(fn=cmd_mcp)
    plug = sub.add_parser("plugin", help="package Rafiq, under your nickname, for Claude Code, Claude Desktop or Codex")
    plug.add_argument("host", choices=("claude-code", "claude-desktop", "codex"))
    plug.add_argument("--out", help="the folder to write it to")
    plug.add_argument("--notify", action="store_true",
                      help="Claude Code: have Rafiq say each time Claude stops (after every reply)")
    plug.set_defaults(fn=cmd_plugin)
    sub.add_parser("toolcheck", help="measure how well the local model picks tools").set_defaults(
        fn=cmd_toolcheck)
    sub.add_parser("google-setup", help="connect your Google calendar and Gmail").set_defaults(fn=cmd_google_setup)
    backup = sub.add_parser("backup", help="save settings and recordings to a private local archive")
    backup.add_argument("--to", type=Path, help="the folder for the archive (default: <settings>/backups)")
    backup.set_defaults(fn=cmd_backup)
    wake = sub.add_parser("wake-pc", help="start a computer that is off, with Wake-on-LAN")
    wake.add_argument("mac", help="its network card address, such as aa:bb:cc:dd:ee:ff")
    wake.add_argument("--broadcast", default="255.255.255.255", help="the network's broadcast address")
    wake.add_argument("--port", type=int, default=9)
    wake.set_defaults(fn=cmd_wake_pc)
    login = sub.add_parser("chatgpt-login", help="sign in with ChatGPT, so Rafiq runs on your ChatGPT plan")
    login.add_argument("--new", action="store_true", help="register again, to use another ChatGPT account")
    login.set_defaults(fn=cmd_chatgpt_login)
    sub.add_parser("chatgpt-logout", help="sign Rafiq out of ChatGPT").set_defaults(fn=cmd_chatgpt_logout)
    sub.add_parser("spotify-setup", help="connect your Spotify account").set_defaults(fn=cmd_spotify_setup)
    insta = sub.add_parser("instagram-setup", help="connect your Instagram Business or Creator accounts")
    insta.add_argument("token", help="a user token from the Graph API Explorer of your Meta app")
    insta.set_defaults(fn=cmd_instagram_setup)
    hue = sub.add_parser("hue-setup", help="pair with the Philips Hue bridge (press its link button)")
    hue.add_argument("--host", help="the bridge's address on your network (default: look for it)")
    hue.set_defaults(fn=cmd_hue_setup)
    web = sub.add_parser("browser", help="start Rafiq's own Chrome or Firefox, whose tabs Rafiq can read")
    web.add_argument("url", nargs="?", help="a web address to open")
    web.set_defaults(fn=cmd_browser)
    sub.add_parser("install-service", help="start Rafiq at every login (systemd user service)").set_defaults(
        fn=cmd_install_service)
    sub.add_parser("uninstall-service", help="stop starting Rafiq at login").set_defaults(fn=cmd_uninstall_service)
    args = parser.parse_args(argv)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from . import service

    service.detached_output()           # Windows' logon task has no console: what follows goes to task.log
    config.make_home_private()          # Windows only: its ACL is what keeps the keys in it the user's (#193)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
