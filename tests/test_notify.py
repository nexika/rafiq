import threading
from datetime import datetime

import pytest
from test_brain_panel import FakeAssistant, request

from rafiq import __main__ as cli
from rafiq import config, notify
from rafiq.assistant import Assistant, Events
from rafiq.panel import serve


def at(hour, minute=0):
    return datetime(2026, 10, 10, hour, minute)


@pytest.mark.parametrize("hours, now, quiet", [
    ("22:00-07:00", at(23), True),
    ("22:00-07:00", at(3, 30), True),
    ("22:00-07:00", at(7), False),
    ("22:00-07:00", at(12), False),
    ("13:00-14:00", at(13, 30), True),
    ("13:00-14:00", at(14, 1), False),
    ("", at(3), False),
])
def test_quiet_hours_can_wrap_past_midnight(hours, now, quiet):
    assert notify.in_quiet_hours(hours, now) is quiet


def test_bad_quiet_hours_are_refused():
    with pytest.raises(ValueError):
        notify.in_quiet_hours("late", at(3))


@pytest.mark.parametrize("event, tag, expected", [
    ("done", "en", "Finished: the benchmark"),
    ("failed", "fr", "Échec : the benchmark"),
    ("done", "ar-EG", "خلص: the benchmark"),
    ("", "de", "the benchmark"),
])
def test_the_message_is_worded_in_the_users_language(event, tag, expected):
    assert notify.message("the benchmark", event, tag) == expected


def test_a_notification_is_spoken_outside_quiet_hours():
    cfg = config.load()
    cfg["notify"]["quiet_hours"] = "22:00-07:00"
    assert notify.should_speak(cfg, muted=False, now=at(15)) is True
    assert notify.should_speak(cfg, muted=False, now=at(23)) is False
    assert notify.should_speak(cfg, muted=True, now=at(15)) is False


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def say(self, text, tag):
        self.said.append((text, tag))


def bare_assistant(muted=False):
    assistant = object.__new__(Assistant)
    assistant.cfg, assistant.events, assistant.speaker = config.load(), Events(), FakeSpeaker()
    assistant.tag, assistant.muted = "ar-EG", muted
    return assistant


def test_the_assistant_says_a_notification_in_the_conversations_language():
    assistant = bare_assistant()
    assistant.notify("CI", "failed", None, now=at(15))
    assert assistant.speaker.said == [("فشل: CI", "ar-EG")]
    assert assistant.events.history[-1]["kind"] == "notify"


def test_a_muted_assistant_shows_the_notification_without_saying_it():
    assistant = bare_assistant(muted=True)
    assistant.notify("tests", "done", "en", now=at(15))
    assert assistant.speaker.said == []
    event = assistant.events.history[-1]
    assert event["kind"] == "notify" and event["text"] == "Finished: tests" and event["spoken"] is False


@pytest.fixture
def panel():
    assistant = FakeAssistant()
    server = serve(assistant, "127.0.0.1", 0, "t" * 40)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield assistant, server.server_address[1]
    server.shutdown()


def test_the_panel_queues_a_notification(panel):
    assistant, port = panel
    assert request(port, "/api/notify", {"text": "benchmark", "event": "done"})[0] == 200
    assert assistant.jobs == [("notify", {"text": "benchmark", "event": "done", "lang": None})]


def test_the_panel_refuses_an_unknown_event(panel):
    assistant, port = panel
    assert request(port, "/api/notify", {"text": "x", "event": "exploded"})[0] == 400
    assert assistant.jobs == []


def test_rafiq_notify_posts_to_the_running_rafiq(monkeypatch):
    import httpx

    sent = []

    def post(url, json=None, timeout=None, headers=None):
        sent.append((url, json, headers))
        return type("R", (), {"status_code": 200})()

    monkeypatch.setattr(httpx, "post", post)
    assert cli.main(["notify", "--failed", "CI", "on", "main"]) == 0
    url, body, headers = sent[0]
    assert url.endswith("/api/notify") and body == {"text": "CI on main", "event": "failed", "lang": None}
    assert headers["X-Rafiq-Token"] == config.panel_token()


def test_rafiq_notify_says_when_rafiq_is_not_running(monkeypatch, capsys):
    import httpx

    def down(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", down)
    assert cli.main(["notify", "done"]) == 1
    assert "not running" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["claude-code", "shell"])
def test_ready_made_hooks_call_rafiq_notify(kind, capsys):
    assert cli.main(["hooks", kind]) == 0
    assert "rafiq notify" in capsys.readouterr().out
