"""OpenAI as a third brain, and the model picker: fake servers stand in for every provider."""

import copy
import json

import httpx
import pytest
from test_brain_panel import FakeAssistant, request, tools_with

from rafiq import config
from rafiq.brain import Brain, BrainError
from rafiq.panel import serve


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("GET", "http://fake")
            raise httpx.HTTPStatusError("refused", request=req, response=httpx.Response(self.status_code, request=req))

    def json(self):
        return self.data


@pytest.fixture
def openai(monkeypatch):
    """A scripted OpenAI chat completions server: each call returns the next message."""
    script, requests = [], []

    def post(url, json=None, headers=None, timeout=None):
        requests.append({"url": url, "headers": headers, "body": copy.deepcopy(json)})
        return FakeResponse({"choices": [{"message": script.pop(0)}]})

    monkeypatch.setattr(httpx, "post", post)
    return script, requests


@pytest.fixture
def catalogue(monkeypatch):
    """Fake model lists: Anthropic and OpenAI answer with the models the key may use, Ollama with
    the models it has. A key other than "good" is refused."""
    seen = []

    def get(url, headers=None, timeout=None, params=None):
        seen.append({"url": url, "headers": headers or {}})
        headers = headers or {}
        if url.endswith("/api/tags"):
            return FakeResponse({"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.2:3b"}]})
        if "anthropic" in url:
            if headers.get("x-api-key") != "good":
                return FakeResponse({}, 401)
            return FakeResponse({"data": [{"id": "claude-haiku-5-5"}, {"id": "claude-opus-5-5"}]})
        if headers.get("Authorization") != "Bearer good":
            return FakeResponse({}, 401)
        return FakeResponse({"data": [{"id": "gpt-5-mini"}, {"id": "gpt-5"}]})

    monkeypatch.setattr(httpx, "get", get)
    return seen


def make_brain(mode="openai", **keys):
    cfg = config.load()
    cfg["brain"]["mode"] = mode
    cfg.update(keys)
    return Brain(cfg, tools_with([]), lambda action, tag: True)


# ---------------------------------------------------------------------- OpenAI turns

def test_openai_answers_and_its_tool_calls_run(openai):
    script, requests = openai
    script += [{"role": "assistant", "content": None, "tool_calls": [
                   {"id": "call_1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}]},
               {"role": "assistant", "content": "It's twenty to four."}]
    brain = make_brain(openai_api_key="sk-test")
    assert brain.think("what time is it", "en") == "It's twenty to four."
    first, second = requests
    assert first["url"] == "https://api.openai.com/v1/chat/completions"
    assert first["headers"]["Authorization"] == "Bearer sk-test"
    assert first["body"]["model"] == brain.cfg["brain"]["openai_model"]
    assert first["body"]["messages"][0]["role"] == "system"
    assert {t["function"]["name"] for t in first["body"]["tools"]} == {"power", "get_time"}
    assert second["body"]["messages"][-1] == {"role": "tool", "tool_call_id": "call_1",
                                              "content": "Saturday 10 October 2026, 15:40"}


def test_openai_tool_calls_that_change_something_are_confirmed(openai):
    script, _ = openai
    asked = []
    script += [{"role": "assistant", "tool_calls": [
                   {"id": "c", "type": "function", "function": {"name": "power", "arguments": "not json"}}]},
               {"role": "assistant", "content": "Okay, I didn't."}]
    cfg = config.load()
    cfg["brain"]["mode"], cfg["openai_api_key"] = "openai", "sk-test"
    brain = Brain(cfg, tools_with([]), lambda action, tag: asked.append(action) or False)
    assert brain.think("shut down", "en") == "Okay, I didn't."
    assert asked == ["shut down the computer"]


def test_openai_url_can_point_at_a_compatible_server(openai):
    script, requests = openai
    script += [{"role": "assistant", "content": "hi"}]
    brain = make_brain(openai_api_key="k")
    brain.cfg["brain"]["openai_url"] = "http://127.0.0.1:9999/v1/"
    brain.think("hello", "en")
    assert requests[0]["url"] == "http://127.0.0.1:9999/v1/chat/completions"


def test_auto_mode_tries_claude_then_openai_then_ollama():
    assert make_brain("auto").backends() == ["ollama"]
    assert make_brain("auto", openai_api_key="k").backends() == ["openai", "ollama"]
    assert make_brain("auto", anthropic_api_key="a", openai_api_key="k").backends() == ["claude", "openai", "ollama"]
    assert make_brain("openai").backends() == ["openai"]


def test_when_openai_fails_auto_mode_falls_back_to_ollama(monkeypatch):
    def post(url, json=None, headers=None, timeout=None):
        if "openai" in url:
            raise httpx.ConnectError("refused")
        return FakeResponse({"message": {"role": "assistant", "content": "from ollama"}})

    monkeypatch.setattr(httpx, "post", post)
    events = []
    brain = make_brain("auto", openai_api_key="k")
    brain.on_event = lambda kind, data: events.append((kind, data))
    assert brain.think("hello", "en") == "from ollama"
    assert ("brain", {"backend": "ollama"}) in events


def test_the_openai_key_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert config.load()["openai_api_key"] == "sk-env"


# ---------------------------------------------------------------------- the model picker

def test_models_lists_what_each_key_can_use(catalogue):
    brain = make_brain(anthropic_api_key="good", openai_api_key="good")
    assert brain.models("openai") == ["gpt-5", "gpt-5-mini"]
    assert brain.models("claude") == ["claude-haiku-5-5", "claude-opus-5-5"]
    assert brain.models("ollama") == ["llama3.2:3b", "qwen2.5:7b"]


def test_a_refused_key_is_reported(catalogue):
    with pytest.raises(BrainError, match="refused"):
        make_brain(openai_api_key="bad").models("openai")


def test_a_backend_without_a_key_is_reported_without_asking_the_network(catalogue):
    with pytest.raises(BrainError, match="no API key"):
        make_brain().models("claude")
    assert catalogue == []


def test_choosing_a_model_the_key_can_use_switches_to_it(catalogue):
    brain = make_brain("auto", openai_api_key="good")
    brain.choose("openai", "gpt-5")
    assert brain.cfg["brain"]["mode"] == "openai" and brain.cfg["brain"]["openai_model"] == "gpt-5"
    assert brain.backends() == ["openai"]


def test_choosing_auto_goes_back_to_trying_each_backend(catalogue):
    brain = make_brain("openai", openai_api_key="good")
    brain.choose("auto", "")
    assert brain.backends() == ["openai", "ollama"]


def test_choosing_a_model_the_key_cannot_use_changes_nothing(catalogue):
    brain = make_brain("auto", openai_api_key="good")
    before = copy.deepcopy(brain.cfg["brain"])
    with pytest.raises(BrainError, match="gpt-9"):
        brain.choose("openai", "gpt-9")
    with pytest.raises(BrainError, match="unknown"):
        brain.choose("gemini", "x")
    assert brain.cfg["brain"] == before


def test_status_names_the_mode_and_each_backends_model():
    status = make_brain("auto", openai_api_key="k").status()
    assert status["mode"] == "auto"
    assert status["ready"] == {"claude": False, "openai": True, "ollama": True}
    assert status["models"]["openai"] == config.DEFAULTS["brain"]["openai_model"]


# ---------------------------------------------------------------------- the picker in the panel

@pytest.fixture
def panel(catalogue):
    assistant = FakeAssistant()
    assistant.brain = make_brain("auto", openai_api_key="good")
    server = serve(assistant, "127.0.0.1", 0, "t" * 40)
    port = server.server_address[1]
    import threading

    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield assistant, port
    server.shutdown()


def test_the_panel_shows_the_brain(panel):
    _, port = panel
    status, body = request(port, "/api/brain", {})
    assert status == 200 and json.loads(body)["mode"] == "auto"


def test_the_panel_lists_models(panel):
    _, port = panel
    status, body = request(port, "/api/models", {"backend": "openai"})
    assert status == 200 and json.loads(body)["models"] == ["gpt-5", "gpt-5-mini"]


def test_the_panel_picks_a_model_after_checking_the_key(panel):
    assistant, port = panel
    status, body = request(port, "/api/model", {"backend": "openai", "model": "gpt-9"})
    assert status == 400 and "gpt-9" in json.loads(body)["error"]
    assert request(port, "/api/model", {"backend": "openai", "model": "gpt-5"})[0] == 200
    assert assistant.brain.cfg["brain"]["openai_model"] == "gpt-5"
    assert ("brain", {"mode": "openai", "model": "gpt-5"}) in [
        (e["kind"], {k: e[k] for k in ("mode", "model")}) for e in assistant.events.history if e["kind"] == "brain"]


def test_the_picker_needs_the_token(panel):
    assistant, port = panel
    assert request(port, "/api/model", {"backend": "openai", "model": "gpt-5"}, token=None)[0] == 403
    assert assistant.brain.cfg["brain"]["mode"] == "auto"
