import json
import queue
import threading
import tomllib
import urllib.error
import urllib.request

import numpy as np
import pytest

from rafiq import config
from rafiq.assistant import Events
from rafiq.audio import level
from rafiq.brain import Brain
from rafiq.panel import serve
from rafiq.tools import Tool

# ---------------------------------------------------------------------- settings the panel may change


def test_only_allowlisted_settings_are_shown_and_never_a_key():
    cfg = config.load()
    cfg["anthropic_api_key"] = "sk-secret"
    cfg["voice"]["azure_key"] = "az-secret"
    shown = config.settable(cfg)
    assert shown["brain.mode"] == "auto" and shown["voice.voices.ar-EG"] == "ar-EG-ShakirNeural"
    assert "secret" not in json.dumps(shown)
    assert not any("key" in name for name in shown)


def test_a_change_is_saved_to_the_config_file_and_applied_at_once(rafiq_home):
    cfg = config.load()
    config.save_settings({"brain.mode": "ollama", "voice.voices.ar-EG": "ar-EG-SalmaNeural"}, cfg)
    assert cfg["brain"]["mode"] == "ollama" and cfg["voice"]["voices"]["ar-EG"] == "ar-EG-SalmaNeural"
    saved = tomllib.loads((rafiq_home / "config.toml").read_text(encoding="utf-8"))
    assert saved == {"brain": {"mode": "ollama"}, "voice": {"voices": {"ar-EG": "ar-EG-SalmaNeural"}}}
    assert config.load()["brain"]["mode"] == "ollama"


def test_saving_keeps_the_rest_of_the_file_and_its_comments(rafiq_home):
    path = rafiq_home / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('user_name = "Loai"\n\n[brain]\nmode = "auto"        # auto: Claude when a key is set\n'
                    'ollama_model = "qwen2.5:7b"\n\n[place]\ncity = "Cairo"\n', encoding="utf-8")
    config.save_settings({"brain.mode": "claude", "voice.rate": "+10%"}, config.load())
    text = path.read_text(encoding="utf-8")
    assert 'mode = "claude"      # auto: Claude when a key is set' in text   # the comment keeps its column
    saved = tomllib.loads(text)
    assert saved["user_name"] == "Loai" and saved["place"]["city"] == "Cairo"
    assert saved["brain"] == {"mode": "claude", "ollama_model": "qwen2.5:7b"}
    assert saved["voice"]["rate"] == "+10%"


@pytest.mark.parametrize("changes", [
    {"anthropic_api_key": "sk-mine"},              # not on the allowlist
    {"voice.azure_key": "x"},
    {"brain.ollama_url": "http://evil.example"},
    {"brain.mode": "gpt"},                         # not one of the choices
    {"voice.rate": "fast"},
    {"brain.claude_model": 'x"\n[evil]'},          # would break out of the TOML string
    {"brain.mode": 3},
])
def test_anything_else_is_refused_and_nothing_is_written(rafiq_home, changes):
    cfg = config.load()
    with pytest.raises(ValueError):
        config.save_settings({"brain.mode": "ollama", **changes}, cfg)
    assert cfg["brain"]["mode"] == "auto"
    assert not (rafiq_home / "config.toml").exists()


# ---------------------------------------------------------------------- microphone level, tools and spend

def test_the_microphone_level_goes_from_silence_to_loud():
    assert level(np.zeros(512, dtype="float32")) == 0.0
    quiet = level(np.full(512, 0.01, dtype="float32"))
    loud = level(np.full(512, 0.5, dtype="float32"))
    assert 0.0 < quiet < loud <= 1.0


def test_passing_events_are_sent_but_not_kept_for_a_new_panel():
    events = Events()
    q = events.subscribe()
    events.publish("level", {"level": 0.4}, keep=False)
    assert q.get_nowait()["level"] == 0.4 and list(events.history) == []


class Block:
    def __init__(self, **data):
        self.__dict__.update(data)

    def model_dump(self):
        return dict(self.__dict__)


class FakeClaude:
    """Answers with a tool call, then with words; each reply reports the tokens it used."""
    def __init__(self):
        self.replies = [
            Block(content=[Block(type="tool_use", id="1", name="get_time", input={})],
                  usage=Block(input_tokens=1000, output_tokens=20)),
            Block(content=[Block(type="text", text="Twenty to four.")],
                  usage=Block(input_tokens=1200, output_tokens=10)),
        ]
        self.messages = self

    def create(self, **_kwargs):
        return self.replies.pop(0)

    def stream(self, **_kwargs):
        reply = self.replies.pop(0)
        texts = [b.text for b in reply.content if b.type == "text"]

        class Stream:
            text_stream = texts

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return reply

        return Stream()


def claude_brain(events, prices=None):
    cfg = config.load()
    cfg["brain"]["mode"] = "claude"
    cfg["anthropic_api_key"] = "sk-test"
    if prices is not None:
        cfg["brain"]["prices"] = prices
    tools = {"get_time": Tool("get_time", "time", lambda: "15:40")}
    brain = Brain(cfg, tools, lambda action, tag: True, on_event=lambda kind, data: events.append((kind, data)))
    brain._claude = FakeClaude()
    return brain


def test_the_panel_hears_which_tool_is_running_before_it_finishes():
    events = []
    claude_brain(events).think("what time is it", "en")
    kinds = [kind for kind, _ in events]
    assert kinds.index("tool_start") < kinds.index("tool")
    assert dict(events)["tool_start"] == {"name": "get_time"}


def test_claude_tokens_are_counted_and_priced_when_a_price_is_set():
    events = []
    brain = claude_brain(events, prices={"claude-haiku-5-5": {"input": 1.0, "output": 5.0}})
    brain.think("what time is it", "en")
    assert brain.spend() == {"input_tokens": 2200, "output_tokens": 30, "dollars": 0.00235}
    assert dict(events)["spend"]["input_tokens"] == 2200


def test_without_a_price_only_tokens_are_shown():
    brain = claude_brain([])
    brain.think("what time is it", "en")
    assert brain.spend() == {"input_tokens": 2200, "output_tokens": 30, "dollars": None}


# ---------------------------------------------------------------------- the panel's requests

class FakeAssistant:
    def __init__(self):
        self.events, self.jobs, self.state = Events(), [], "idle"
        self.pending_answer = queue.Queue()
        self.speaker = type("S", (), {"stop": lambda self: None})()
        self.cfg = config.load()
        self.cfg["anthropic_api_key"] = "sk-secret"

    def submit(self, kind, **data):
        self.jobs.append((kind, data))

    def status(self):
        return {"settings": config.settable(self.cfg), "choices": config.CHOICES,
                "spend": {"input_tokens": 0, "output_tokens": 0, "dollars": None}, "tasks": []}

    def change_settings(self, changes):
        config.save_settings(changes, self.cfg)


@pytest.fixture
def panel():
    assistant = FakeAssistant()
    server = serve(assistant, "127.0.0.1", 0, "t" * 40)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield assistant, server.server_address[1]
    server.shutdown()


def request(port, path, body=None, token="t" * 40):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Rafiq-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_the_status_carries_settings_but_no_key(panel):
    _, port = panel
    status, body = request(port, "/api/status")
    assert status == 200 and json.loads(body)["settings"]["brain.mode"] == "auto"
    assert "sk-secret" not in body


def test_the_status_needs_the_token(panel):
    _, port = panel
    assert request(port, "/api/status", token=None)[0] == 403


def test_the_panel_changes_an_allowlisted_setting(panel):
    assistant, port = panel
    assert request(port, "/api/settings", {"changes": {"brain.mode": "ollama"}})[0] == 200
    assert assistant.cfg["brain"]["mode"] == "ollama"


def test_the_panel_cannot_write_a_key(panel):
    assistant, port = panel
    status, body = request(port, "/api/settings", {"changes": {"anthropic_api_key": "sk-evil"}})
    assert status == 400 and "anthropic_api_key" in body
    assert assistant.cfg["anthropic_api_key"] == "sk-secret"


def test_the_voice_test_button_says_a_line_in_that_language(panel):
    assistant, port = panel
    assert request(port, "/api/voice-test", {"tag": "ar-EG"})[0] == 200
    assert request(port, "/api/voice-test", {"tag": "xx"})[0] == 400
    kind, data = assistant.jobs[0]
    assert kind == "say" and data["tag"] == "ar-EG" and data["text"]
    assert len(assistant.jobs) == 1
