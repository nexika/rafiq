import queue
import time
from types import SimpleNamespace

import numpy as np
import pytest

from rafiq import assistant as assistant_mod
from rafiq import config, lang
from rafiq.__main__ import main


class FakeMic:
    def __init__(self, **_kwargs):
        self.frames, self.muted = queue.Queue(), False

    def read(self, timeout=0.5):
        try:
            return self.frames.get_nowait()
        except queue.Empty:
            return None

    def drain(self):
        pass


class FakeEndpointer:
    """Every frame is a whole utterance, so a test can say something by queueing one frame."""
    active = False

    def __init__(self, **_kwargs):
        self.frames = 0

    def push(self, frame, prob):
        self.frames += 1
        return np.zeros(16000)

    def reset(self):
        pass


class FakeSpeaker:
    def __init__(self, cfg, on_start=None, on_end=None):
        self.said = []

    def say(self, text, tag):
        self.said.append(text)
        return True

    def stop(self):
        pass


class FakeBrain:
    def __init__(self, cfg, tools, confirm, on_event=None):
        self.asked = []

    def think(self, text, tag, ctx=None):
        self.asked.append(text)
        return "an answer"


@pytest.fixture
def rafiq(monkeypatch):
    """An Assistant with no microphone, models or voice: what it hears is set with `heard`."""
    heard = []
    monkeypatch.setattr(assistant_mod, "Microphone", FakeMic)
    monkeypatch.setattr(assistant_mod, "SileroVAD", lambda: (lambda frame: 1.0))
    monkeypatch.setattr(assistant_mod, "Endpointer", FakeEndpointer)
    monkeypatch.setattr(assistant_mod, "Speaker", FakeSpeaker)
    monkeypatch.setattr(assistant_mod, "Brain", FakeBrain)
    monkeypatch.setattr(assistant_mod, "load_all", lambda cfg: {})
    monkeypatch.setattr(assistant_mod.kws.Spotter, "load", classmethod(lambda cls, *a, **k: None))
    a = assistant_mod.Assistant(config.load())
    a.stt.transcribe = lambda audio, **_kwargs: SimpleNamespace(text=heard.pop(0), lang="en")
    a.set_state("idle")
    return a, heard


@pytest.mark.parametrize("text", [
    "go to sleep", "Go to sleep.", "go to sleep please", "sleep", "mets-toi en veille", "Mets toi en veille !",
    "نام", "نام يا رفيق", "روح نام", "vete a dormir", "geh schlafen",
])
def test_go_to_sleep_in_every_language(text):
    assert lang.is_sleep(text)


@pytest.mark.parametrize("text", [
    "set a sleep timer", "I can't sleep", "how long should I sleep", "", "what time is it", "نام كويس امبارح؟",
])
def test_requests_that_mention_sleep_are_not_go_to_sleep(text):
    assert not lang.is_sleep(text)


def test_go_to_sleep_closes_the_follow_up_window_without_thinking(rafiq):
    a, _ = rafiq
    a.respond("what time is it", "en")
    assert a.follow_up_until > time.monotonic()
    a.respond("go to sleep", "en")
    assert a.follow_up_until == 0.0
    assert a.brain.asked == ["what time is it"]


def test_go_to_sleep_cancels_a_pending_confirmation(rafiq):
    a, heard = rafiq
    heard.append("نام")
    a.mic.frames.put(np.zeros(512))
    a.follow_up_until = time.monotonic() + 6
    assert a.confirm("shut down the computer", "ar-EG") is False
    assert a.follow_up_until == 0.0
    assert any(e["kind"] == "sleep" for e in a.events.history)


def test_muted_rafiq_ignores_what_it_hears_while_idle(rafiq):
    a, _ = rafiq
    a.set_muted(True)
    a.mic.frames.put(np.zeros(512))
    assert a.next_utterance(timeout=0.3, idle=True) is None
    assert a.endpointer.frames == 0


def test_muting_closes_the_follow_up_window_and_shows_in_the_panel(rafiq):
    a, _ = rafiq
    a.follow_up_until = time.monotonic() + 6
    assert a.set_muted(True) is True
    assert a.follow_up_until == 0.0
    assert a.state == "muted"
    a.set_state("idle")                      # after a job: still muted
    assert a.state == "muted"
    assert a.set_muted(None) is False        # a toggle
    assert a.state == "idle"


def test_the_talk_button_still_works_while_muted(rafiq):
    a, heard = rafiq
    a.set_muted(True)
    heard.append("what time is it")
    a.mic.frames.put(np.zeros(512))
    a._listen_for_request(greet=False)
    assert a.brain.asked == ["what time is it"]
    assert a.muted


def test_muting_cancels_a_pending_confirmation(rafiq):
    a, _ = rafiq
    a.confirming = True
    a.set_muted(True)
    assert a.pending_answer.get_nowait() is False


def test_rafiq_mute_asks_the_running_rafiq(monkeypatch):
    import httpx

    sent = []

    def post(url, json=None, timeout=None, headers=None):
        sent.append((url, json))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(httpx, "post", post)
    assert main(["mute"]) == 0
    assert main(["mute", "off"]) == 0
    assert [s[0].rsplit("/", 1)[1] for s in sent] == ["mute", "mute"]
    assert [s[1] for s in sent] == [{}, {"muted": False}]


def test_rafiq_mute_says_when_rafiq_is_not_running(monkeypatch, capsys):
    import httpx

    def down(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", down)
    assert main(["mute"]) == 1
    assert "not running" in capsys.readouterr().err
