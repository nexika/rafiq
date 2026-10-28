import copy
import json
import threading
import urllib.error
import urllib.request

import pytest

from rafiq import brain as brain_mod
from rafiq import config, lang
from rafiq.assistant import Events
from rafiq.brain import Brain
from rafiq.panel import serve
from rafiq.reader import Reader, Segment
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
    brain.cfg["brain"]["routing"] = False      # "cherche" alone would offer only the web tools
    brain.on_event = lambda kind, data: events.append((kind, data))
    brain.think("cherche", "fr")
    waits = [d for k, d in events if k == "waiting"]
    assert waits == [{"name": "web_search", "text": "Je cherche.", "tag": "fr", "step_id": 1}]
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


def calls_to(name, count=1):
    return {"role": "assistant", "tool_calls": [{"function": {"name": name, "arguments": {}}}] * count}


def test_the_defaults_match_jarvis_six_rounds_twelve_calls_two_minutes():
    assert config.load()["brain"]["limits"] == {"max_rounds": 6, "max_tool_calls": 12, "max_seconds": 120}


def test_too_many_rounds_stops_with_a_message_in_the_users_language(ollama):
    script, requests = ollama
    script += [calls_to("get_time")] * 3
    brain = make_brain()
    brain.cfg["brain"]["limits"]["max_rounds"] = 2
    assert brain.think("quelle heure", "fr") == brain_mod.lang_mod.phrase("limit", "fr")
    assert len(requests) == 2


def test_too_many_tool_calls_stops_before_running_the_extra_ones(ollama):
    script, requests = ollama
    log = []
    script += [calls_to("get_time", 2), calls_to("power", 2)]
    brain = make_brain(log=log)
    brain.cfg["brain"]["limits"]["max_tool_calls"] = 3
    assert brain.think("shut down twice", "ar-EG") == brain_mod.lang_mod.phrase("limit", "ar-EG")
    assert log == []            # the batch that would pass the cap is not run at all
    assert len(requests) == 2


def test_running_out_of_time_stops_the_request(ollama):
    script, requests = ollama
    now = [1000.0]

    def slow_time():
        now[0] += 50
        return "Saturday 10 October 2026, 15:40"

    script += [calls_to("get_time")] * 4
    cfg = config.load()
    cfg["brain"]["mode"] = "ollama"
    cfg["brain"]["limits"]["max_seconds"] = 120
    brain = Brain(cfg, {"get_time": Tool("get_time", "time", slow_time)}, lambda a, t: True, clock=lambda: now[0])
    assert brain.think("what time is it", "en") == brain_mod.lang_mod.phrase("limit", "en")
    assert len(requests) == 3   # at 0 s, 50 s and 100 s; at 150 s the time is up


def test_the_model_request_never_waits_past_the_time_left(monkeypatch):
    now, timeouts = [1000.0], []

    def post(url, json=None, timeout=None):
        timeouts.append(timeout)
        now[0] += 90
        return FakeResponse({"message": calls_to("get_time")})

    import httpx

    monkeypatch.setattr(httpx, "post", post)
    cfg = config.load()
    cfg["brain"]["mode"] = "ollama"
    brain = Brain(cfg, tools_with([]), lambda a, t: True, clock=lambda: now[0])
    brain.think("what time is it", "en")
    assert timeouts == [120, 30]


def test_a_limit_does_not_fall_back_to_the_other_backend(ollama, monkeypatch):
    """Claude already spent the request's budget looping; starting over on Ollama would double it."""
    script, requests = ollama
    brain = make_brain()
    brain.cfg["brain"]["mode"] = "auto"
    brain.cfg["anthropic_api_key"] = "sk-test"

    def looping(*args):
        raise brain_mod.LimitReached("too many tool rounds")

    monkeypatch.setattr(brain, "_claude_turn", looping)
    assert brain.think("hello", "en") == brain_mod.lang_mod.phrase("limit", "en")
    assert requests == []


def test_system_prompt_has_no_time_or_language():
    text = brain_mod.system_prompt(config.load())
    assert "2026" not in text and "{" not in text


# ---------------------------------------------------------------------- panel

class FakeAssistant:
    def __init__(self):
        self.events, self.jobs, self.state, self.mode = Events(), [], "idle", "everyday"
        import queue

        self.pending_answer = queue.Queue()
        self.speaker = type("S", (), {"stop": lambda self: None})()
        self.reader = Reader(interrupt=lambda: None)

    def submit(self, kind, **data):
        self.jobs.append((kind, data))

    def set_project(self, path):
        self.jobs.append(("project", {"path": path}))
        return "error: not a folder" if path == "/nowhere" else path
    def set_muted(self, muted=None):
        self.jobs.append(("mute", {"muted": muted}))


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


def test_the_dictate_button_and_shortcut_are_queued_for_the_assistant(panel):
    assistant, port = panel
    assert request(port, "/api/dictate", {})[0] == 200
    assert assistant.jobs == [("dictate", {})]


def test_the_project_folder_is_set_from_the_panel(panel):
    assistant, port = panel
    assert request(port, "/api/project", {"path": "/home/me/code"})[0] == 200
    assert request(port, "/api/project", {"path": "/nowhere"})[0] == 400
    assert request(port, "/api/project", {"path": 3})[0] == 400
    assert assistant.jobs == [("project", {"path": "/home/me/code"}), ("project", {"path": "/nowhere"})]


def test_the_morning_paper_timers_offer_reaches_the_running_rafiq(panel):
    assistant, port = panel
    assert request(port, "/api/paper", {})[0] == 200
    assert assistant.jobs == [("paper", {})]


def test_the_mute_button_and_rafiq_mute_reach_the_assistant(panel):
    assistant, port = panel
    assert request(port, "/api/mute", {})[0] == 200
    assert request(port, "/api/mute", {"muted": False})[0] == 200
    assert request(port, "/api/mute", {"muted": "yes"})[0] == 400
    assert assistant.jobs == [("mute", {"muted": None}), ("mute", {"muted": False})]


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


def test_the_panel_pauses_resumes_skips_and_stops_the_reading(panel):
    assistant, port = panel
    assistant.reader.load([Segment("One.", "en"), Segment("Two.", "en"), Segment("Three.", "en")])
    assert request(port, "/api/reading", {"action": "pause"})[0] == 200
    assert assistant.reader.state == "paused"
    assert request(port, "/api/reading", {"action": "skip"})[0] == 200
    assert assistant.reader.index == 1
    assert request(port, "/api/reading", {"action": "resume"})[0] == 200
    assert assistant.reader.active and assistant.jobs == [("read", {})]
    assert request(port, "/api/stop", {})[0] == 200
    assert not assistant.reader.active
    assert assistant.events.history[-1]["state"] == "idle"   # the panel hides its reading controls


def test_an_unknown_reading_action_is_refused(panel):
    _, port = panel
    assert request(port, "/api/reading", {"action": "explode"})[0] == 400


def test_the_panel_will_not_listen_beyond_this_computer():
    with pytest.raises(ValueError):
        serve(FakeAssistant(), "0.0.0.0", 0, "t" * 40)


def test_show_again_is_queued_for_the_assistant(panel):
    assistant, port = panel
    assert request(port, "/api/show", {})[0] == 200
    assert assistant.jobs == [("show_again", {})]


def test_a_mode_chosen_in_the_panel_is_queued_for_the_assistant(panel):
    assistant, port = panel
    assert request(port, "/api/mode", {"mode": "offline"})[0] == 200
    assert assistant.jobs == [("mode", {"mode": "offline"})]


@pytest.mark.parametrize("body", [{"mode": "turbo"}, {"mode": 3}, {}])
def test_an_unknown_mode_from_the_panel_is_refused(panel, body):
    assistant, port = panel
    assert request(port, "/api/mode", body)[0] == 400
    assert assistant.jobs == []


# ---------------------------------------------------------------------- steps on the event stream

def steps_of(events):
    return [d for k, d in events if k == "step"]


def test_each_tool_call_is_a_numbered_step_that_runs_then_ends(ollama):
    script, _ = ollama
    events = []
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "get_time", "arguments": {}}},
                                                    {"function": {"name": "web_search", "arguments": {}}}]},
               {"role": "assistant", "content": "Done."}]
    brain = make_brain()
    brain.cfg["brain"]["routing"] = False
    brain.on_event = lambda kind, data: events.append((kind, data))
    brain.think("what time is it, and search", "en")
    assert [(s["step_id"], s["name"], s["status"]) for s in steps_of(events)] == [
        (1, "get_time", "queued"), (2, "web_search", "queued"),
        (1, "get_time", "running"), (1, "get_time", "done"),
        (2, "web_search", "running"), (2, "web_search", "done")]
    done = [s for s in steps_of(events) if s["status"] == "done"]
    assert all(s["ended"] >= s["started"] for s in done)


def test_the_tool_events_name_their_step(ollama):
    script, _ = ollama
    events = []
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "get_time", "arguments": {}}}]},
               {"role": "assistant", "content": "Noon."}]
    brain = make_brain()
    brain.on_event = lambda kind, data: events.append((kind, data))
    brain.think("what time is it", "en")
    assert {d["step_id"] for k, d in events if k in ("tool_start", "tool")} == {1}


def test_steps_start_again_from_one_on_the_next_request(ollama):
    script, _ = ollama
    events = []
    for _ in range(2):
        script += [{"role": "assistant", "tool_calls": [{"function": {"name": "get_time", "arguments": {}}}]},
                   {"role": "assistant", "content": "Noon."}]
    brain = make_brain()
    brain.on_event = lambda kind, data: events.append((kind, data))
    brain.think("what time is it", "en")
    brain.think("and now?", "en")
    assert {s["step_id"] for s in steps_of(events)} == {1}


def test_a_step_waiting_for_a_yes_is_asking_and_a_no_ends_it_as_declined(ollama):
    script, _ = ollama
    events = []
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "power", "arguments": "{}"}}]},
               {"role": "assistant", "content": "Okay, I didn't."}]
    brain = make_brain(confirm=lambda action, tag: False)
    brain.on_event = lambda kind, data: events.append((kind, data))
    brain.think("shut down", "en")
    assert [s["status"] for s in steps_of(events)] == ["queued", "asking", "declined"]


def test_a_step_whose_tool_fails_ends_as_failed(ollama):
    script, _ = ollama
    events = []
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "broken", "arguments": {}}}]},
               {"role": "assistant", "content": "Sorry."}]
    brain = make_brain()

    def broken():
        raise OSError("disk gone")

    brain.tools["broken"] = Tool("broken", "fails", broken)
    brain.cfg["brain"]["routing"] = False
    brain.cfg["brain"]["ollama_tools"] = "all"     # the test's own tool is not in the local set
    brain.on_event = lambda kind, data: events.append((kind, data))
    brain.think("break", "en")
    assert steps_of(events)[-1]["status"] == "failed"


def test_a_private_step_shows_neither_arguments_nor_result(ollama):
    script, _ = ollama
    events = []
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "read_mail",
                                                                  "arguments": {"query": "bank"}}}]},
               {"role": "assistant", "content": "Two emails."}]
    brain = make_brain()
    brain.tools["read_mail"] = Tool("read_mail", "mail", lambda query="": "secret statement",
                                    params={"query": {"type": "string"}}, level="private", private=True)
    brain.cfg["brain"]["routing"] = False
    brain.cfg["brain"]["ollama_tools"] = "all"     # the test's own tool is not in the local set
    brain.on_event = lambda kind, data: events.append((kind, data))
    brain.think("my mail", "en")
    for step in steps_of(events):
        assert "bank" not in str(step) and "secret" not in str(step)


def test_a_second_broken_call_still_ends_every_step_of_the_reply(ollama):
    script, _ = ollama
    events = []
    broken = {"function": {"name": "get_time", "arguments": "not json"}}
    script += [{"role": "assistant", "tool_calls": [broken, broken, {"function": {"name": "get_time",
                                                                                   "arguments": {}}}]}]
    brain = make_brain()
    brain.on_event = lambda kind, data: events.append((kind, data))
    brain.think("what time is it", "en")
    last = {s["step_id"]: s for s in steps_of(events)}
    assert {i: s["status"] for i, s in last.items()} == {1: "failed", 2: "failed", 3: "failed"}
    assert all("ended" in s for s in last.values())


def test_a_step_with_broken_arguments_keeps_its_start_and_has_an_end(ollama):
    script, _ = ollama
    events = []
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "get_time", "arguments": "not json"}}]},
               {"role": "assistant", "content": "Noon."}]
    brain = make_brain()
    brain.on_event = lambda kind, data: events.append((kind, data))
    brain.think("what time is it", "en")
    queued, failed = steps_of(events)[:2]
    assert failed["status"] == "failed" and failed["started"] == queued["started"] and "ended" in failed


def test_a_declined_private_tool_hides_its_arguments(ollama):
    script, _ = ollama
    events = []
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "close_tab",
                                                                  "arguments": {"tab": "my bank"}}}]},
               {"role": "assistant", "content": "Okay."}]
    brain = make_brain(confirm=lambda action, tag: False)
    brain.tools["close_tab"] = Tool("close_tab", "close a tab", lambda tab="": "closed",
                                    params={"tab": {"type": "string"}}, confirm="close a tab", level="private",
                                    private=True)
    brain.cfg["brain"]["routing"] = False
    brain.cfg["brain"]["ollama_tools"] = "all"     # the test's own tool is not in the local set
    brain.on_event = lambda kind, data: events.append((kind, data))
    brain.think("close my bank tab", "en")
    assert "my bank" not in str([d for k, d in events if k == "tool"])


def test_a_tool_can_reach_the_voice_of_the_request_it_runs_in(ollama):
    script, _ = ollama
    seen = []
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "peek", "arguments": {}}}]},
               {"role": "assistant", "content": "Done."}]
    brain = make_brain()
    brain.tools["peek"] = Tool("peek", "peek", lambda: seen.append(brain.voice) or "ok")
    brain.cfg["brain"]["routing"] = False
    brain.cfg["brain"]["ollama_tools"] = "all"
    brain.think("peek", "en", on_speech=lambda text, tag: None)
    assert seen[0] is not None and brain.voice is None


# ---------------------------------------------------------------------- stop

def test_stop_runs_no_further_step_and_says_it_stopped(ollama):
    script, _ = ollama
    events, ran = [], []
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "first", "arguments": {}}},
                                                    {"function": {"name": "second", "arguments": {}}}]},
               {"role": "assistant", "content": "should not be reached"}]
    brain = make_brain()

    def first():
        ran.append("first")
        brain.stop()                     # the user pressed stop while it ran
        return "ok"

    brain.tools["first"] = Tool("first", "first", first)
    brain.tools["second"] = Tool("second", "second", lambda: ran.append("second") or "ok")
    brain.cfg["brain"]["routing"] = False
    brain.cfg["brain"]["ollama_tools"] = "all"
    brain.on_event = lambda kind, data: events.append((kind, data))
    answer = brain.think("do both", "fr")
    assert ran == ["first"] and answer == lang.phrase("request_stopped", "fr")
    last = {d["step_id"]: d["status"] for k, d in events if k == "step"}
    assert last == {1: "done", 2: "stopped"}


def test_a_stop_is_forgotten_once_the_next_request_starts(ollama):
    script, _ = ollama
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "get_time", "arguments": {}}}]},
               {"role": "assistant", "content": "Noon."}]
    events = []
    brain = make_brain()
    brain.on_event = lambda kind, data: events.append((kind, data))
    brain.stop()
    brain.clear_stop()                   # what the assistant does when the next request's run opens
    assert brain.think("what time is it", "en") == "Noon."
    assert [d["status"] for k, d in events if k == "step"][-1] == "done"


def test_a_stop_pressed_before_the_brain_starts_ends_the_request_at_once(ollama):
    """Pressed while Rafiq was still transcribing: the stop belongs to this request."""
    script, requests = ollama
    script += [{"role": "assistant", "content": "the whole answer"}]
    brain = make_brain()
    brain.stop()
    assert brain.think("delete all my reminders", "en") == lang.phrase("request_stopped", "en")
    assert requests == []


def test_stop_during_a_question_runs_nothing_even_after_a_yes(ollama):
    script, _ = ollama
    events, ran = [], []
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "power", "arguments": "{}"}}]},
               {"role": "assistant", "content": "should not be reached"}]
    brain = make_brain(log=ran)

    def confirm(action, tag):
        brain.stop()                     # stop pressed while Rafiq asked; then a yes came in anyway
        return True

    brain.confirm = confirm
    brain.on_event = lambda kind, data: events.append((kind, data))
    assert brain.think("shut down", "en") == lang.phrase("request_stopped", "en")
    assert ran == [] and [d["status"] for k, d in events if k == "step"][-1] == "stopped"


def test_the_panel_s_stop_also_stops_the_request(panel):
    assistant, port = panel
    stopped = []
    assistant.brain = type("B", (), {"stop": lambda self: stopped.append(True)})()
    assert request(port, "/api/stop", {})[0] == 200
    assert stopped == [True]


def test_the_panel_s_stop_answers_no_to_a_question_waiting(panel):
    assistant, port = panel
    assistant.confirming = True
    assert request(port, "/api/stop", {})[0] == 200
    assert assistant.pending_answer.get_nowait() is False
