import json
from dataclasses import dataclass

import pytest
from test_brain_panel import ollama, panel, request  # noqa: F401  (fixtures)

from rafiq import config, lang
from rafiq.brain import Brain
from rafiq.permissions import ALWAYS, Approvals
from rafiq.tools import REGISTRY, Tool, load_all


@dataclass
class Ctx:
    remote: bool = False


def tools(log):
    def run(name):
        return lambda: log.append(name) or "done"

    return {"get_time": Tool("get_time", "time", run("get_time")),
            "open_in_browser": Tool("open_in_browser", "open", run("open_in_browser"), confirm="open the page",
                                    level=2),
            "power": Tool("power", "power off", run("power"), confirm="shut down the computer")}


def make_brain(confirm, log, approvals=None):
    cfg = config.load()
    cfg["brain"]["mode"] = "ollama"
    return Brain(cfg, tools(log), confirm, approvals=approvals or Approvals(config.data_dir() / "approvals.json"))


def call(script, name):
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": name, "arguments": {}}}]},
               {"role": "assistant", "content": "ok"}]


# ---------------------------------------------------------------------- levels

def test_a_tool_that_asks_is_level_3_unless_it_says_otherwise():
    assert Tool("a", "", print).level == 1
    assert Tool("b", "", print, confirm="do it").level == 3
    assert Tool("c", "", print, confirm="do it", level=2).level == 2


def test_rafiqs_tools_are_mapped_to_levels():
    load_all({"google": {"enabled": True}})
    for name in ("power", "email_send", "calendar_add"):
        assert REGISTRY[name].level == 3, name
    for name in ("open_in_browser", "cancel_timer"):
        assert REGISTRY[name].level == 2, name
        assert REGISTRY[name].action({"url": "https://example.com", "label": "tea"}), name
    for name in ("get_time", "weather", "web_search", "volume", "open_app", "lock_screen"):
        assert REGISTRY[name].level == 1, name


# ---------------------------------------------------------------------- "yes, always"

@pytest.mark.parametrize("text", [
    "yes, always", "always", "oui, toujours", "sí, siempre", "ja, immer", "نعم دائمًا", "أيوه دايمًا",
    "آه على طول", "ايوه علطول",
])
def test_yes_always_in_the_six_languages(text):
    assert lang.answer(text) == ALWAYS


@pytest.mark.parametrize("text, expected", [
    ("yes", True), ("no", False), ("maybe", None),
    ("no, not always", False),     # a no still wins
])
def test_answer_is_yes_or_no_otherwise(text, expected):
    assert lang.answer(text) is expected


def test_level_2_yes_always_is_remembered_and_not_asked_again(ollama):  # noqa: F811
    script, _ = ollama
    log, asked = [], []
    brain = make_brain(lambda action, tag, always=False: asked.append(always) or ALWAYS, log)
    call(script, "open_in_browser")
    call(script, "open_in_browser")
    brain.think("open it", "en")
    brain.think("open it again", "en")
    assert asked == [True] and log == ["open_in_browser", "open_in_browser"]
    assert Approvals(config.data_dir() / "approvals.json").has("open_in_browser")


def test_level_2_plain_yes_asks_again_next_time(ollama):  # noqa: F811
    script, _ = ollama
    log, asked = [], []
    brain = make_brain(lambda action, tag, always=False: asked.append(action) or True, log)
    call(script, "open_in_browser")
    call(script, "open_in_browser")
    brain.think("open it", "en")
    brain.think("open it again", "en")
    assert asked == ["open the page", "open the page"] and len(log) == 2


def test_level_3_is_asked_every_time_and_never_remembered(ollama):  # noqa: F811
    script, _ = ollama
    log, asked = [], []

    def confirm(action, tag, always=False):
        asked.append(always)
        return ALWAYS     # even if something answers "always", a level 3 action is not remembered

    brain = make_brain(confirm, log)
    call(script, "power")
    call(script, "power")
    brain.think("shut down", "en")
    brain.think("shut down", "en")
    assert asked == [False, False] and log == ["power", "power"]
    assert not Approvals(config.data_dir() / "approvals.json").has("power")


def test_level_3_cannot_be_approved_ahead_of_time(ollama):  # noqa: F811
    script, _ = ollama
    approvals = Approvals(config.data_dir() / "approvals.json")
    approvals.path.write_text(json.dumps({"power": 1.0}))     # written by hand: still asks
    log, asked = [], []
    brain = make_brain(lambda action, tag, always=False: asked.append(action) or False, log, Approvals(approvals.path))
    call(script, "power")
    brain.think("shut down", "en")
    assert asked == ["shut down the computer"] and log == []


# ---------------------------------------------------------------------- remote

def test_remotely_only_level_1_and_approved_level_2_run(ollama):  # noqa: F811
    script, _ = ollama
    log, asked = [], []
    brain = make_brain(lambda *a, **k: asked.append(a) or True, log)
    for name in ("get_time", "open_in_browser", "power"):
        call(script, name)
        brain.think("go", "en", Ctx(remote=True))
    assert log == ["get_time"] and asked == []
    brain.approvals.remember("open_in_browser")
    call(script, "open_in_browser")
    brain.think("go", "en", Ctx(remote=True))
    assert log == ["get_time", "open_in_browser"]


# ---------------------------------------------------------------------- the store

def test_approvals_are_stored_locally_and_revocable(tmp_path):
    path = tmp_path / "approvals.json"
    approvals = Approvals(path)
    approvals.remember("open_in_browser")
    assert Approvals(path).names() == ["open_in_browser"]
    assert (path.stat().st_mode & 0o777) == 0o600
    assert approvals.revoke("open_in_browser") and not approvals.revoke("open_in_browser")
    assert not Approvals(path).has("open_in_browser")


def test_a_broken_approvals_file_means_nothing_is_approved(tmp_path):
    path = tmp_path / "approvals.json"
    path.write_text("not json")
    assert Approvals(path).names() == []


# ---------------------------------------------------------------------- panel

def test_the_panel_revokes_an_approval(panel):  # noqa: F811
    assistant, port = panel
    assert request(port, "/api/revoke", {"tool": "open_in_browser"})[0] == 200
    assert assistant.revoked == ["open_in_browser"]
    assert request(port, "/api/revoke", {"tool": 3})[0] == 400


def test_the_panels_yes_always_button_answers_always(panel):  # noqa: F811
    assistant, port = panel
    assert request(port, "/api/confirm", {"yes": True, "always": True})[0] == 200
    assert assistant.pending_answer.get_nowait() == ALWAYS


# ---------------------------------------------------------------------- asking out loud

def quiet_assistant(heard):
    """An Assistant without microphone or speaker: it hears the given answers and keeps what it said."""
    import queue

    from rafiq.assistant import Assistant, Events

    a = object.__new__(Assistant)
    a.events, a.pending_answer, a.said = Events(), queue.Queue(), []
    a.say = lambda text, tag: a.said.append(text)
    a.set_state = lambda state: None
    a.next_utterance = lambda timeout: "audio"
    a.hear = lambda audio: (heard.pop(0), "en")
    return a


def test_yes_always_is_offered_out_loud_only_for_level_2():
    a = quiet_assistant(["yes, always", "yes, always"])
    assert a.confirm("open the page", "en", always=True) == ALWAYS
    assert a.said[-1] == "Should I open the page? Say yes, no, or yes always."
    assert a.confirm("shut down the computer", "en") is True
    assert a.said[-1] == "Should I shut down the computer? Say yes or no."


def test_the_panels_always_is_a_plain_yes_for_level_3():
    a = quiet_assistant([])
    # The queue is emptied before asking, so the click arrives while Rafiq listens.
    a.next_utterance = lambda timeout: a.pending_answer.put(ALWAYS)
    assert a.confirm("shut down the computer", "en") is True
