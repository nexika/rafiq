import copy
import json
import threading
import urllib.error
import urllib.request

import pytest

from rafiq import config
from rafiq.phone import PhoneBridge, remote_tools, serve
from rafiq.scheduler import Scheduler
from rafiq.tools import REGISTRY, Tool, load_all, notes

TOKEN = "p" * 43


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


@pytest.fixture
def ollama(monkeypatch):
    """A scripted Ollama: each call returns the next message; the requests are kept."""
    script, requests = [], []

    def post(url, json=None, timeout=None):
        requests.append(copy.deepcopy(json))
        return FakeResponse({"message": script.pop(0)})

    import httpx

    monkeypatch.setattr(httpx, "post", post)
    return script, requests


def tools_with(log):
    def shutdown():
        log.append("shutdown ran")
        return "done"

    def lock():
        log.append("locked")
        return "locked"

    return {"power": Tool("power", "power off", shutdown, confirm="shut down the computer"),
            "lock_screen": Tool("lock_screen", "lock", lock),
            "get_time": Tool("get_time", "time", lambda: "15:40", safe=True)}


def make_bridge(tools=None, events=None):
    cfg = config.load()
    cfg["brain"]["mode"] = "ollama"
    scheduler = Scheduler(config.data_dir() / "alarms.json", lambda alarm: None)
    on_event = (lambda kind, data: events.append((kind, data))) if events is not None else None
    return PhoneBridge(cfg, tools if tools is not None else tools_with([]), scheduler, on_event=on_event)


# ---------------------------------------------------------------------- which tools the phone may use

def test_only_safe_tools_without_a_confirmation_are_allowed_from_the_phone():
    load_all({"google": {"enabled": True}})
    for name in ("get_time", "weather", "calculate", "set_timer", "add_note", "list_notes"):
        assert REGISTRY[name].safe, name
    for name in ("power", "email_send", "calendar_add", "open_app", "volume", "lock_screen",
                 "open_in_browser", "email_read"):
        assert not REGISTRY[name].safe, name
    for tool in REGISTRY.values():
        assert not (tool.safe and tool.confirm is not None), tool.name


def test_an_unsafe_tool_answers_do_that_at_home_and_does_not_run():
    log = []
    tools = remote_tools(tools_with(log))
    assert set(tools) == {"power", "lock_screen", "get_time"}    # the model still sees them, to explain
    ctx = type("Ctx", (), {"tag": "fr"})()
    for name in ("power", "lock_screen"):
        result = tools[name].run({}, ctx)
        assert "à la maison" in result and tools[name].confirm is None
    assert log == []
    assert tools["get_time"].run({}, ctx) == "15:40"


# ---------------------------------------------------------------------- notes

def test_notes_are_filed_listed_and_removed():
    assert notes.add_note("shopping", "milk").startswith("added")
    notes.add_note("shopping", "eggs")
    notes.add_note("tasks", "call the bank")
    assert "milk" in notes.list_notes("shopping") and "eggs" in notes.list_notes("shopping")
    assert "bank" not in notes.list_notes("shopping")
    assert notes.remove_note("shopping", "MILK").startswith("removed 1")
    assert "milk" not in notes.list_notes("shopping")
    assert notes.list_notes("ideas") == "the ideas list is empty"


def test_an_unknown_list_is_an_error():
    assert notes.add_note("secrets", "x").startswith("error")


# ---------------------------------------------------------------------- the inbox

def test_a_dictated_note_is_filed_in_its_list():
    events = []
    status, body = make_bridge(events=events).inbox({"kind": "note", "list": "shopping", "text": "oat milk"})
    assert status == 200 and body["ok"] and body["list"] == "shopping"
    assert "oat milk" in notes.list_notes("shopping")
    assert events[0][0] == "phone"


def test_a_note_without_a_list_goes_to_ideas():
    make_bridge().inbox({"kind": "note", "text": "a lamp that follows the sun"})
    assert "follows the sun" in notes.list_notes("ideas")


def test_a_shared_link_is_kept_with_its_text():
    make_bridge().inbox({"kind": "share", "text": "Read later", "url": "https://example.com/a"})
    assert "Read later https://example.com/a" in notes.list_notes("ideas")


def test_a_command_is_answered_with_only_safe_tools(ollama):
    script, requests = ollama
    log = []
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "power", "arguments": {}}}]},
               {"role": "assistant", "content": "Do that at home by voice."}]
    status, body = make_bridge(tools_with(log)).inbox({"text": "shut down the computer"})
    assert status == 200 and body["answer"] == "Do that at home by voice."
    assert log == []
    assert "at home by voice" in requests[1]["messages"][-1]["content"]


@pytest.mark.parametrize("data", [{}, {"text": "  "}, {"text": 3}, {"text": "x", "kind": "sing"}, []])
def test_a_bad_request_is_refused(data):
    assert make_bridge().inbox(data)[0] == 400


# ---------------------------------------------------------------------- over HTTP

@pytest.fixture
def phone(ollama):
    script, _ = ollama
    server = serve(make_bridge(), "127.0.0.1", 0, TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield script, server.server_address[1]
    server.shutdown()


def post(port, body, headers=None, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/inbox", method="POST", data=data)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_a_bearer_token_is_accepted_and_the_answer_comes_back(phone):
    script, port = phone
    script.append({"role": "assistant", "content": "It's twenty to four."})
    status, body = post(port, {"text": "what time is it"}, {"Authorization": f"Bearer {TOKEN}"})
    assert status == 200 and body["answer"] == "It's twenty to four."


def test_the_rafiq_token_header_is_accepted_too(phone):
    _, port = phone
    assert post(port, {"kind": "note", "text": "bread", "list": "shopping"}, {"X-Rafiq-Token": TOKEN})[0] == 200


def test_a_request_without_the_right_token_is_refused(phone):
    _, port = phone
    assert post(port, {"kind": "note", "text": "x"})[0] == 403
    assert post(port, {"kind": "note", "text": "x"}, {"Authorization": "Bearer " + "q" * 43})[0] == 403
    assert notes.list_notes("ideas") == "the ideas list is empty"


def test_bad_json_and_huge_bodies_are_refused(phone):
    _, port = phone
    auth = {"X-Rafiq-Token": TOKEN}
    assert post(port, None, auth, raw=b"{not json")[0] == 400
    assert post(port, None, auth, raw=b"x" * 20_000)[0] == 413


def test_the_phone_token_is_kept_and_private():
    token = config.phone_token()
    assert len(token) >= 32 and config.phone_token() == token
    assert (config.home() / "phone.token").stat().st_mode & 0o077 == 0
    assert token != config.panel_token()
