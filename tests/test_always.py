"""'Yes, always': a tool that asks first can be approved once, except a few that always ask (#72)."""

import queue
import threading
from pathlib import Path

import httpx
import numpy as np
import pytest
from test_brain_panel import tools_with

from rafiq import config, lang
from rafiq.assistant import Assistant
from rafiq.brain import Brain
from rafiq.permissions import Approvals, levels
from rafiq.tools import Tool, check, load_all

ALWAYS_ASK = {"power", "email_send", "email_reply", "email_trash", "calendar_delete"}


def test_the_blocklist_is_exactly_the_tools_that_must_ask_every_time():
    tools = load_all({"google": {"enabled": True}})
    assert {n for n, t in tools.items() if t.always_asks} == ALWAYS_ASK
    assert all(tools[n].level == "critical" for n in ALWAYS_ASK)


def test_only_a_tool_that_asks_first_can_always_ask():
    with pytest.raises(ValueError):
        check(Tool("volume", "volume", lambda: "", level="act", always_asks=True))


def sending(log):
    return {**tools_with(log),
            "email_send": Tool("email_send", "send", lambda: log.append("sent") or "sent", confirm="send the mail",
                               level="critical", always_asks=True)}


def scripted(monkeypatch, name, times):
    script = [{"role": "assistant", "tool_calls": [{"function": {"name": name, "arguments": {}}}]},
              {"role": "assistant", "content": "Done."}] * times

    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Resp({"message": script.pop(0)}))


def brain(tools, confirm, approvals):
    cfg = config.load()
    cfg["brain"].update(mode="ollama", routing=False, ollama_tools="all")   # every tool, every time
    return Brain(cfg, tools, confirm, approvals=approvals)


def test_always_on_a_blocklisted_tool_is_a_plain_yes(tmp_path, monkeypatch):
    scripted(monkeypatch, "email_send", 2)
    log, asked = [], []
    approvals = Approvals(tmp_path / "approvals.json")
    b = brain(sending(log), lambda action, tag, can_remember=True: asked.append(can_remember) or "always", approvals)
    b.think("send it", "en")
    b.think("send it again", "en")
    assert asked == [False, False] and log == ["sent", "sent"] and not approvals.allowed("email_send")


def test_an_old_approval_of_a_blocklisted_tool_is_ignored(tmp_path, monkeypatch):
    scripted(monkeypatch, "email_send", 1)
    log, asked = [], []
    approvals = Approvals(tmp_path / "approvals.json")
    approvals.remember("email_send")              # saved before the blocklist existed
    b = brain(sending(log), lambda action, tag, can_remember=True: asked.append(action) or True, approvals)
    b.think("send", "en")
    assert asked == ["send the mail"] and log == ["sent"]


def test_other_tools_that_ask_offer_always(tmp_path, monkeypatch):
    scripted(monkeypatch, "power", 1)
    asked = []
    brain(tools_with([]), lambda action, tag, can_remember=True: asked.append(can_remember) or True,
          Approvals(tmp_path / "approvals.json")).think("shut down", "en")
    assert asked == [True]


def test_the_permissions_page_shows_tools_that_ask_every_time(tmp_path):
    approvals = Approvals(tmp_path / "approvals.json")
    approvals.remember("email_send")
    rows = {r["name"]: r["level"] for r in levels(sending([]), approvals)}
    assert rows["email_send"] == "always_asks" and rows["power"] == "asks"


@pytest.mark.parametrize("text", ["yes, always", "Always.", "oui, toujours", "sí, siempre", "ja, immer",
                                  "نعم، دائما", "أيوه دايما", "ايوه على طول"])
def test_yes_always_in_each_language(text):
    assert lang.is_always(text)


@pytest.mark.parametrize("text", ["yes", "no, not always", "always no", "", "toujours pas"])
def test_not_yes_always(text):
    assert not lang.is_always(text)


class Events:
    def __init__(self):
        self.seen = []

    def publish(self, kind, data):
        self.seen.append((kind, data))


def asking(heard):
    a = object.__new__(Assistant)
    a.events, a.pending_answer, a.interrupted = Events(), queue.Queue(), threading.Event()
    a.speech, a.confirming, a.voice_print = None, False, None
    a.set_state = lambda state: None
    a.say = lambda text, tag: None
    a.next_utterance = lambda timeout: np.zeros(16000, dtype="float32")
    a.hear = lambda audio: (heard, "en")
    return a


def test_saying_yes_always_remembers_when_the_tool_allows_it():
    a = asking("yes, always")
    assert a.confirm("shut down the computer", "en", can_remember=True) == "always"
    assert ("confirm", {"action": "shut down the computer", "tag": "en", "always": True}) in a.events.seen


def test_saying_yes_always_to_a_blocklisted_tool_is_a_yes():
    a = asking("yes, always")
    assert a.confirm("send the mail", "en", can_remember=False) is True
    assert ("confirm", {"action": "send the mail", "tag": "en", "always": False}) in a.events.seen


def test_the_panel_hides_always_when_the_tool_cannot_be_remembered():
    page = (Path(__file__).parent.parent / "rafiq" / "panel.html").read_text(encoding="utf-8")
    assert "e.always" in page and "always_asks" in page


def test_a_bare_always_is_a_yes_always():
    assert asking("toujours").confirm("print the paper", "fr", can_remember=True) == "always"
