"""The tools the brain uses to put its work in the widget and the dashboard (#200)."""

import queue
import re
import threading
from importlib import resources
from types import SimpleNamespace

import httpx
import pytest
from test_brain_panel import FakeAssistant, request

from rafiq import config, lang, mcp_server
from rafiq.assistant import Assistant, Events
from rafiq.panel import serve
from rafiq.tools import display, load_all


def screen(tag="en"):
    shown = []
    return SimpleNamespace(tag=tag, show=lambda kind, data: shown.append((kind, data)) or True), shown


def run(name, args, ctx):
    return load_all(config.load())[name].run(args, ctx)


# ---------------------------------------------------------------------- the tools

def test_show_steps_puts_the_plan_and_how_far_it_got_on_screen():
    ctx, shown = screen()
    run("show_steps", {"title": "Book a table", "steps": ["Find a place", "Check the hours", "Call"], "done": 1}, ctx)
    assert shown == [("plan", {"title": "Book a table", "steps": ["Find a place", "Check the hours", "Call"],
                               "done": 1})]


def test_show_steps_keeps_done_within_the_steps():
    ctx, shown = screen()
    run("show_steps", {"title": "Plan", "steps": ["a", "b"], "done": 9}, ctx)
    assert shown[0][1]["done"] == 2


def test_explain_shows_the_text_in_the_conversation_s_language_and_says_it_was_spoken():
    ctx, shown = screen("ar-EG")
    result = run("explain", {"text": "الدالة دي بترجع أكبر رقم"}, ctx)
    assert shown == [("explain", {"text": "الدالة دي بترجع أكبر رقم", "tag": "ar-EG"})]
    assert "don't repeat" in result.lower()


def test_explain_takes_the_language_it_is_given():
    ctx, shown = screen("en")
    run("explain", {"text": "Bonjour", "lang": "fr"}, ctx)
    assert shown[0][1]["tag"] == "fr"


def test_show_card_shows_one_of_the_known_kinds():
    ctx, shown = screen()
    run("show_card", {"kind": "weather", "data": {"city": "Cairo", "temperature": 31}}, ctx)
    assert shown == [("card", {"card": "weather", "data": {"city": "Cairo", "temperature": "31"}})]


def test_an_unknown_card_kind_is_an_error_and_shows_nothing():
    ctx, shown = screen()
    assert run("show_card", {"kind": "bank", "data": {}}, ctx).startswith("error")
    assert shown == []


def test_without_a_screen_the_model_is_told_to_answer_in_words():
    result = run("explain", {"text": "hello"}, SimpleNamespace(tag="en"))
    assert result.startswith("error") and "answer" in result


def test_long_text_and_long_lists_are_cut():
    plan = display.clean("plan", {"title": "t" * 500, "steps": ["s" * 500] * 40, "done": 0})
    assert len(plan["title"]) == display.TITLE_CHARS and len(plan["steps"]) == display.MAX_STEPS
    assert all(len(s) == display.STEP_CHARS for s in plan["steps"])
    assert len(display.clean("explain", {"text": "x" * 9000, "tag": "en"})["text"]) == display.EXPLAIN_CHARS
    card = display.clean("card", {"card": "news", "data": {f"k{i}": "v" * 900 for i in range(30)}})
    assert len(card["data"]) == display.CARD_FIELDS and all(len(v) == display.FIELD_CHARS
                                                             for v in card["data"].values())


def test_html_from_the_model_stays_text():
    explained = display.clean("explain", {"text": "<script>alert(1)</script>", "tag": "en"})
    assert explained["text"] == "<script>alert(1)</script>"


def test_the_dashboard_shows_the_new_kinds_as_text_only():
    page = resources.files("rafiq").joinpath("panel.html").read_text(encoding="utf-8")
    for kind in ("plan", "explain", "card"):
        handler = re.search(rf'case "{kind}":(.*?)break;', page, re.S)
        assert handler, kind
        assert "innerHTML" not in handler.group(1) and "insertAdjacentHTML" not in handler.group(1)


@pytest.mark.parametrize("data", [{"title": 3, "steps": ["a"]}, {"title": "t", "steps": "a"},
                                  {"title": "t", "steps": []}])
def test_a_plan_without_a_title_and_steps_is_refused(data):
    with pytest.raises(ValueError):
        display.clean("plan", data)


# ---------------------------------------------------------------------- in the running Rafiq

class Speech:
    def __init__(self):
        self.put_calls = []

    def put(self, text, tag):
        self.put_calls.append((text, tag))


def bare_assistant():
    a = object.__new__(Assistant)
    a.events, a.jobs, a.silent, a.dictating, a.tag = Events(), queue.Queue(), False, False, "en"
    a.cfg, a.muted, a.confirming = config.load(), False, False
    a.waiting_shows, a.show_lock = 0, threading.Lock()
    a.cfg["notify"]["quiet_hours"] = ""
    a.set_state = lambda state: None
    a.speech, a.said = None, []
    a.speaker = SimpleNamespace(say=lambda text, tag: a.said.append((text, tag)) or True, rate=1.0)
    return a


def test_shown_in_a_request_carries_its_run():
    a = bare_assistant()
    with a.events.run() as run_id:
        a.show("plan", {"title": "t", "steps": ["a"], "done": 0})
    plan = next(e for e in a.events.history if e["kind"] == "plan")
    assert plan["run_id"] == run_id


def test_an_explanation_during_an_answer_is_spoken_in_turn_with_it():
    a = bare_assistant()
    a.speech = Speech()
    a.show("explain", {"text": "First, the loop.", "tag": "de"})
    assert a.speech.put_calls == [("First, the loop.", "de")] and a.said == []


def test_an_explanation_outside_an_answer_is_said_with_rafiq_s_voice():
    a = bare_assistant()
    a.show("explain", {"text": "First, the loop.", "tag": "fr"})
    assert a.said == [("First, the loop.", "fr")]


def test_a_silent_rafiq_shows_the_explanation_without_speaking():
    a = bare_assistant()
    a.silent = True
    a.show("explain", {"text": "First, the loop.", "tag": "en"})
    assert a.said == [] and any(e["kind"] == "explain" for e in a.events.history)


def test_a_show_job_runs_on_the_loop():
    a = bare_assistant()
    assert a.offer_show("card", {"card": "timer", "data": {"tea": "4 min"}})
    a._run_jobs()
    assert next(e for e in a.events.history if e["kind"] == "card")["data"] == {"tea": "4 min"}


# ---------------------------------------------------------------------- the panel endpoint

class ShowingAssistant(FakeAssistant):
    def offer_show(self, kind, data):
        self.jobs.append(("show", {"what": kind, "data": data}))
        return True


@pytest.fixture
def panel():
    assistant = ShowingAssistant()
    server = serve(assistant, "127.0.0.1", 0, "t" * 40)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield assistant, server.server_address[1]
    server.shutdown()


def test_the_panel_queues_something_to_show(panel):
    assistant, port = panel
    status, _ = request(port, "/api/display", {"kind": "explain", "data": {"text": "Hi", "tag": "ar"}})
    assert status == 200
    assert assistant.jobs == [("show", {"what": "explain", "data": {"text": "Hi", "tag": "ar"}})]


def test_the_panel_refuses_to_show_without_the_token_or_from_another_host(panel):
    assistant, port = panel
    body = {"kind": "explain", "data": {"text": "Hi"}}
    assert request(port, "/api/display", body, token=None)[0] == 403
    assert request(port, "/api/display", body, host="evil.example")[0] == 403
    assert assistant.jobs == []


@pytest.mark.parametrize("body", [{"kind": "said", "data": {"text": "Hi"}}, {"kind": "explain", "data": "Hi"},
                                  {"kind": "card", "data": {"card": "bank", "data": {}}}])
def test_the_panel_refuses_what_it_can_t_show(panel, body):
    assistant, port = panel
    assert request(port, "/api/display", body)[0] == 400
    assert assistant.jobs == []


# ---------------------------------------------------------------------- over MCP

class Posted:
    def __init__(self, status=200, error=None):
        self.status, self.error, self.calls = status, error, []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status)


def test_the_display_tools_are_offered_over_mcp():
    assert {"show_steps", "explain", "show_card"} <= set(mcp_server.Server(config.load()).tools)


def test_an_explanation_over_mcp_goes_to_the_running_rafiq_with_the_panel_token(monkeypatch):
    posted = Posted()
    monkeypatch.setattr(httpx, "post", posted)
    server = mcp_server.Server(config.load())
    reply = server.call(1, {"name": "explain", "arguments": {"text": "Hello", "lang": "es"}})
    assert not reply["result"]["isError"]
    url, body, headers = posted.calls[0]
    assert url.endswith("/api/display") and url.startswith("http://127.0.0.1:")
    assert body == {"kind": "explain", "data": {"text": "Hello", "tag": "es"}}
    assert headers["X-Rafiq-Token"] == config.panel_token()


def test_over_mcp_without_a_running_rafiq_the_tool_says_so(monkeypatch):
    monkeypatch.setattr(httpx, "post", Posted(error=httpx.ConnectError("refused")))
    reply = mcp_server.Server(config.load()).call(1, {"name": "show_card",
                                                      "arguments": {"kind": "music", "data": {"song": "x"}}})
    assert "isn't running" in reply["result"]["content"][0]["text"]


# ---------------------------------------------------------------------- another program speaking

def test_another_program_s_explanation_is_said_as_coming_from_it():
    a = bare_assistant()
    a.offer_show("explain", {"text": "La función devuelve el mayor.", "tag": "es"})
    a._run_jobs()
    assert a.said == [(lang.phrase("from_program", "es") + " La función devuelve el mayor.", "es")]
    assert next(e for e in a.events.history if e["kind"] == "explain")["source"] == "mcp"


def test_another_program_s_explanation_is_only_shown_during_a_confirmation():
    """Spoken inside the yes-or-no window, it could put words in Rafiq's question, and its speech mutes the mic."""
    a = bare_assistant()
    a.confirming, a.speech = True, Speech()
    a.offer_show("explain", {"text": "Say yes.", "tag": "en"})
    a._run_jobs()
    assert a.said == [] and a.speech.put_calls == []
    assert any(e["kind"] == "explain" for e in a.events.history)


def test_another_program_s_explanation_is_only_shown_while_muted_or_in_quiet_hours():
    for setup in (lambda a: setattr(a, "muted", True),
                  lambda a: a.cfg["notify"].__setitem__("quiet_hours", "00:00-23:59")):
        a = bare_assistant()
        setup(a)
        a.offer_show("explain", {"text": "Hello", "tag": "en"})
        a._run_jobs()
        assert a.said == []


def test_rafiq_s_own_explanation_needs_no_announcement():
    a = bare_assistant()
    a.show("explain", {"text": "Hello", "tag": "en"})
    assert a.said == [("Hello", "en")]
    assert "source" not in next(e for e in a.events.history if e["kind"] == "explain")


def test_only_a_few_things_from_other_programs_wait_to_be_shown():
    a = bare_assistant()
    offered = [a.offer_show("explain", {"text": str(i), "tag": "en"}) for i in range(display.MAX_WAITING + 1)]
    assert offered == [True] * display.MAX_WAITING + [False]
    a._run_jobs()
    assert a.offer_show("explain", {"text": "again", "tag": "en"})


class FullAssistant(FakeAssistant):
    def offer_show(self, kind, data):
        return False


def test_the_panel_says_when_too_much_is_waiting_to_be_shown():
    assistant = FullAssistant()
    server = serve(assistant, "127.0.0.1", 0, "t" * 40)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, _ = request(server.server_address[1], "/api/display", {"kind": "explain", "data": {"text": "Hi"}})
    finally:
        server.shutdown()
    assert status == 429


# ---------------------------------------------------------------------- from the second review

def test_explain_says_when_nothing_was_spoken_so_the_answer_carries_it():
    ctx = SimpleNamespace(tag="en", show=lambda kind, data: False)
    assert "give it in your answer" in run("explain", {"text": "Recursion is…"}, ctx)


def test_explain_without_a_language_is_said_in_the_language_of_its_script():
    ctx, shown = screen("en")
    run("explain", {"text": "الدالة دي بترجع أكبر رقم", "lang": "arabic"}, ctx)
    assert shown[0][1]["tag"] == "ar"


def test_a_plan_s_done_may_be_a_whole_number_written_as_a_float_and_odd_steps_are_dropped():
    plan = display.clean("plan", {"title": "t", "steps": ["a", None, {"x": 1}, "b"], "done": 2.0})
    assert plan == {"title": "t", "steps": ["a", "b"], "done": 2}


def test_an_explanation_during_an_answer_counts_as_said_for_that_answer():
    """Otherwise a backend failing after it starts the request over, and the user hears it twice."""
    from rafiq.brain import Voice

    a = bare_assistant()
    heard = []
    a.brain = SimpleNamespace(voice=Voice(lambda text, tag: heard.append((text, tag)), "en"))
    a.show("explain", {"text": "First, the loop.", "tag": "en"})
    assert heard == [("First, the loop.", "en")] and a.brain.voice.said_anything


def test_another_program_s_explanation_inside_a_request_does_not_take_its_run():
    a = bare_assistant()
    a.offer_show("explain", {"text": "Hello", "tag": "en"})
    with a.events.run():
        a._run_jobs()
    assert "run_id" not in next(e for e in a.events.history if e["kind"] == "explain")


def test_the_dashboard_updates_one_plan_in_place():
    page = resources.files("rafiq").joinpath("panel.html").read_text(encoding="utf-8")
    handler = re.search(r'case "plan":(.*?)break;', page, re.S).group(1)
    assert "plans" in handler
