import copy
import json
import threading
import urllib.error
import urllib.request

import pytest

from rafiq import brain as brain_mod
from rafiq import config
from rafiq.assistant import Events
from rafiq.brain import Brain
from rafiq.panel import serve
from rafiq.tools import Tool


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def tools_with(log):
    def shutdown():
        log.append("shutdown ran")
        return "done"

    def clock():
        return "Saturday 10 October 2026, 15:40"

    return {"power": Tool("power", "power off", shutdown, confirm="shut down the computer"),
            "get_time": Tool("get_time", "time", clock, card="facts"),
            "web_search": Tool("web_search", "search", lambda: "results", slow=True,
                               waiting_phrase={"en": "Let me look.", "fr": "Je cherche."})}


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


def make_brain(confirm=lambda action, tag: True, log=None):
    cfg = config.load()
    cfg["brain"]["mode"] = "ollama"
    return Brain(cfg, tools_with(log if log is not None else []), confirm)


def test_a_tool_call_runs_and_its_result_goes_back_to_the_model(ollama):
    script, requests = ollama
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "get_time", "arguments": {}}}]},
               {"role": "assistant", "content": "It's twenty to four."}]
    assert make_brain().think("what time is it", "en") == "It's twenty to four."
    assert requests[1]["messages"][-1] == {"role": "tool", "tool_name": "get_time",
                                           "content": "Saturday 10 October 2026, 15:40"}


def test_a_declined_confirmation_does_not_run_the_action(ollama):
    script, _ = ollama
    log, asked = [], []
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "power", "arguments": "{}"}}]},
               {"role": "assistant", "content": "Okay, I didn't."}]
    brain = make_brain(confirm=lambda action, tag: asked.append((action, tag)) or False, log=log)
    brain.think("shut down", "ar-EG")
    assert asked == [("shut down the computer", "ar-EG")] and log == []


def test_a_slow_tool_announces_a_waiting_phrase_and_each_result_carries_its_card(ollama):
    script, _ = ollama
    events = []
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "web_search", "arguments": {}}},
                                                    {"function": {"name": "get_time", "arguments": {}}}]},
               {"role": "assistant", "content": "Done."}]
    brain = make_brain()
    brain.on_event = lambda kind, data: events.append((kind, data))
    brain.think("cherche", "fr")
    waits = [d for k, d in events if k == "waiting"]
    assert waits == [{"name": "web_search", "text": "Je cherche.", "tag": "fr"}]
    cards = {d["name"]: d["card"] for k, d in events if k == "tool"}
    assert cards == {"web_search": "text", "get_time": "facts"}


def test_an_unknown_tool_is_reported_to_the_model_not_run(ollama):
    script, requests = ollama
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "rm_rf", "arguments": {}}}]},
               {"role": "assistant", "content": "I can't."}]
    make_brain().think("delete everything", "en")
    assert requests[1]["messages"][-1]["content"].startswith("error: there is no tool")


def test_the_prompt_prefix_is_the_same_in_every_language(ollama):
    """So Ollama reuses its cached reading of the system prompt and tools when the language changes."""
    script, requests = ollama
    script += [{"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"}]
    brain = make_brain()
    brain.think("hello", "en")
    brain.think("إزيك", "ar-EG")
    assert requests[0]["messages"][0] == requests[1]["messages"][0]
    assert requests[0]["tools"] == requests[1]["tools"]
    assert "Egyptian Arabic" in requests[1]["messages"][-1]["content"]


def test_memory_starts_over_when_the_language_changes(ollama):
    script, requests = ollama
    script += [{"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"},
               {"role": "assistant", "content": "c"}]
    brain = make_brain()
    brain.think("hello", "en")
    brain.think("and you?", "en")
    brain.think("et toi ?", "fr")
    assert len(requests[1]["messages"]) == 4      # system, the first turn's two messages, the new one
    assert len(requests[2]["messages"]) == 2      # system and the new one: no English turns


def test_when_every_backend_fails_rafiq_says_so_in_the_users_language(monkeypatch):
    import httpx

    def down(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", down)
    assert make_brain().think("bonjour", "fr") == "Je n'arrive pas à réfléchir pour le moment."


def test_system_prompt_has_no_time_or_language():
    text = brain_mod.system_prompt(config.load())
    assert "2026" not in text and "{" not in text


# ---------------------------------------------------------------------- panel

class FakeAssistant:
    def __init__(self):
        self.events, self.jobs, self.state = Events(), [], "idle"
        import queue

        self.pending_answer = queue.Queue()
        self.speaker = type("S", (), {"stop": lambda self: None})()

    def submit(self, kind, **data):
        self.jobs.append((kind, data))


@pytest.fixture
def panel():
    assistant = FakeAssistant()
    server = serve(assistant, "127.0.0.1", 0, "t" * 40)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield assistant, port
    server.shutdown()


def request(port, path, body=None, token="t" * 40, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Rafiq-Token", token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_the_page_carries_its_token(panel):
    _, port = panel
    status, page = request(port, "/")
    assert status == 200 and "t" * 40 in page and "__TOKEN__" not in page


def test_typed_text_is_queued_for_the_assistant(panel):
    assistant, port = panel
    assert request(port, "/api/text", {"text": "what time is it"})[0] == 200
    assert assistant.jobs == [("text", {"text": "what time is it"})]


def test_a_request_without_the_token_is_refused(panel):
    assistant, port = panel
    assert request(port, "/api/text", {"text": "shut down"}, token=None)[0] == 403
    assert request(port, "/api/text", {"text": "shut down"}, token="x" * 40)[0] == 403
    assert assistant.jobs == []


def test_another_host_name_is_refused(panel):
    """A web site that rebinds its own name to 127.0.0.1 still sends its name as the Host."""
    assistant, port = panel
    assert request(port, "/", host=f"evil.example:{port}")[0] == 403
    assert request(port, "/api/listen", {}, host="evil.example")[0] == 403
    assert assistant.jobs == []


def test_the_panel_will_not_listen_beyond_this_computer():
    with pytest.raises(ValueError):
        serve(FakeAssistant(), "0.0.0.0", 0, "t" * 40)
