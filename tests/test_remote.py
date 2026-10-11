import json
import threading
import time

import httpx
import pytest

from rafiq import config, remote
from rafiq.brain import Brain
from rafiq.tools import Tool, load_all


def recording_actions(log):
    """Stand-ins for the agent's actions: they record what they were asked instead of touching the PC."""
    def make(name):
        def run(**args):
            log.append((name, args))
            return f"{name} done"
        return run
    actions = {name: make(name) for name in remote.ACTIONS}
    actions["confirm"] = lambda **args: log.append(("confirm", args)) or "yes"
    return actions


def make_agent(log, **kw):
    return remote.Agent("http://127.0.0.1:1", "desk", "t" * 43, recording_actions(log), **kw)


# ---------------------------------------------------------------------- the agent's allowlist

def test_agent_refuses_an_action_outside_the_allowlist():
    log = []
    agent = make_agent(log)
    assert agent.handle({"action": "shell", "args": {"command": "rm -rf ~"}}).startswith("error")
    assert agent.handle({"action": "__init__", "args": {}}).startswith("error")
    assert log == []


def test_agent_keeps_only_the_arguments_an_action_takes_with_the_right_types():
    log = []
    agent = make_agent(log)
    assert agent.handle({"action": "volume", "args": {"level": 30, "command": "id"}}) == "volume done"
    assert log == [("volume", {"level": 30})]
    assert agent.handle({"action": "volume", "args": {"level": "30; reboot"}}).startswith("error")
    assert agent.handle({"action": "power", "args": {"action": "format_disk"}}).startswith("error")
    assert agent.handle({"action": "open_app", "args": {}}).startswith("error")   # name is required
    assert agent.handle({"action": "volume", "args": "level=3"}).startswith("error")
    assert len(log) == 1


def test_agent_reports_an_action_that_fails_instead_of_stopping():
    agent = remote.Agent("http://127.0.0.1:1", "desk", "t" * 43, {"system_status": lambda: 1 / 0})
    assert agent.handle({"action": "system_status", "args": {}}).startswith("error: ZeroDivisionError")


@pytest.mark.parametrize("url, ok", [
    ("https://brain.example.com:8766", True),
    ("http://127.0.0.1:8766", True),
    ("http://localhost:8766", True),
    ("http://192.168.1.20:8766", True),
    ("http://100.101.12.3:8766", True),          # Tailscale
    ("http://brain.tail1234.ts.net:8766", True),
    ("http://brain.example.com:8766", False),    # the token would cross the internet in clear
    ("http://93.184.216.34:8766", False),
    ("ftp://192.168.1.20", False),
])
def test_agent_only_sends_its_token_over_tls_or_a_private_network(url, ok):
    assert remote.private_or_tls(url) is ok


# ---------------------------------------------------------------------- tokens

def test_a_token_per_agent_kept_only_as_a_hash():
    tokens = remote.AgentTokens(config.home() / "agents.json")
    desk = tokens.add("desk")
    laptop = tokens.add("laptop")
    assert desk != laptop and len(desk) >= 32
    assert tokens.check("desk", desk) and tokens.check("laptop", laptop)
    assert not tokens.check("desk", laptop)
    assert not tokens.check("nobody", desk)
    assert not tokens.check("desk", "")
    stored = (config.home() / "agents.json").read_text()
    assert desk not in stored and laptop not in stored
    assert remote.AgentTokens(config.home() / "agents.json").check("desk", desk)   # kept on disk
    tokens.remove("desk")
    assert not tokens.check("desk", desk)


def test_server_with_plain_http_only_listens_on_a_private_address():
    for host in ("127.0.0.1", "192.168.1.20", "100.101.12.3"):
        remote.check_listen(host, tls=False)
    for host in ("0.0.0.0", "93.184.216.34", "::"):
        with pytest.raises(ValueError):
            remote.check_listen(host, tls=False)
    remote.check_listen("0.0.0.0", tls=True)


# ---------------------------------------------------------------------- the brain's side

def test_pc_tools_run_on_the_agent_and_keep_their_confirmation():
    hub = remote.Hub()
    tools = remote.remote_tools(load_all({}), hub)
    assert isinstance(tools["volume"], remote.RemoteTool)
    assert tools["power"].action({"action": "shutdown"}) == "shut down the computer"
    assert not isinstance(tools["calculate"], remote.RemoteTool)   # thinking stays on the brain
    assert tools["volume"].run({"level": 20}, None).startswith("error: no desktop agent")


@pytest.fixture
def server():
    tokens = remote.AgentTokens(config.home() / "agents.json")
    token = tokens.add("desk")
    hub = remote.Hub(wait=0.2)
    asks = []

    def ask(text, tag):
        asks.append((text, tag))
        # What the brain does for "volume to 30": run the tool, which goes to the agent.
        return "Volume is now 30. " + hub.call("volume", {"level": 30})

    httpd = remote.serve(hub, tokens, ask, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield url, token, hub, asks
    httpd.shutdown()
    httpd.server_close()


def start_agent(url, token, log, name="desk"):
    agent = remote.Agent(url, name, token, recording_actions(log), wait=0.2)
    threading.Thread(target=agent.run_forever, daemon=True).start()
    return agent


def connected(hub, name="desk"):
    deadline = time.monotonic() + 5
    while name not in hub.agents() and time.monotonic() < deadline:
        time.sleep(0.01)
    return name in hub.agents()


def test_agent_connects_out_and_runs_what_the_brain_asks(server):
    url, token, hub, _ = server
    log = []
    agent = start_agent(url, token, log)
    try:
        assert connected(hub)
        assert hub.call("volume", {"level": 40}, timeout=5) == "volume done"
        assert log == [("volume", {"level": 40})]
        assert hub.agents() == ["desk"]
    finally:
        agent.stop()


def test_a_request_typed_on_the_agent_is_answered_by_the_brain(server):
    url, token, hub, asks = server
    log = []
    agent = start_agent(url, token, log)
    try:
        assert connected(hub)
        answer = agent.ask("volume to 30")
        assert answer == "Volume is now 30. volume done"
        assert asks == [("volume to 30", "en")]
        assert log == [("volume", {"level": 30})]
    finally:
        agent.stop()


def test_server_refuses_an_agent_without_its_token(server):
    url, token, hub, _ = server
    for headers in ({}, {"Authorization": "Bearer wrong", "X-Rafiq-Agent": "desk"},
                    {"Authorization": f"Bearer {token}", "X-Rafiq-Agent": "laptop"}):
        resp = httpx.post(f"{url}/agent/poll", headers=headers, json={}, timeout=5)
        assert resp.status_code == 403
    assert hub.agents() == []


def test_brain_confirms_on_the_agent_before_a_pc_action(monkeypatch):
    hub = remote.Hub()
    sent = []
    monkeypatch.setattr(hub, "call", lambda action, args, timeout=None: sent.append((action, args))
                        or ("no" if action == "confirm" else "done"))
    cfg = config.load()
    tools = remote.remote_tools({"power": Tool("power", "power", lambda action: "local!", {"action": {}},
                                               ("action",), confirm="shut down the computer")}, hub)
    brain = Brain(cfg, tools, remote.agent_confirm(hub))
    assert brain._run_tool("power", {"action": "shutdown"}, "en", None, tools).startswith("The user said no")
    assert sent == [("confirm", {"question": "Should I shut down the computer? Say yes or no.", "tag": "en"})]


def test_messages_are_plain_json():
    message = remote.Hub().message("volume", {"level": 3})
    assert set(json.loads(json.dumps(message))) == {"id", "action", "args"}


def test_agent_token_command_prints_a_token_the_server_accepts(capsys, monkeypatch):
    from rafiq.__main__ import main

    assert main(["agent-token", "desk"]) == 0
    token = capsys.readouterr().out.strip().splitlines()[-1]
    assert remote.AgentTokens(config.home() / "agents.json").check("desk", token)
    assert main(["agent-token", "desk", "--remove"]) == 0
    assert not remote.AgentTokens(config.home() / "agents.json").check("desk", token)
    assert main(["agent-token", "not a name"]) == 1
    monkeypatch.setenv("RAFIQ_AGENT_TOKEN", token)
    assert config.load()["agent"]["token"] == token


def test_agent_command_needs_a_server_name_and_token(capsys):
    from rafiq.__main__ import main

    assert main(["agent"]) == 1
    assert "RAFIQ_AGENT_TOKEN" in capsys.readouterr().err
