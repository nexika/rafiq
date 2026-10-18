"""`rafiq ask` goes through the running Rafiq when there is one, so Firefox's one session is shared (#191)."""

import argparse
import queue
import threading

import httpx
import pytest
from test_brain_panel import FakeAssistant, request

from rafiq import __main__ as cli
from rafiq.assistant import Assistant, Events
from rafiq.panel import serve


class AskingAssistant(FakeAssistant):
    def __init__(self, answer="It is noon."):
        super().__init__()
        self.answer, self.asked = answer, []

    def ask(self, text, tag=None, speak=False, timeout=None):
        self.asked.append((text, tag, speak))
        return self.answer


@pytest.fixture
def panel():
    assistant = AskingAssistant()
    server = serve(assistant, "127.0.0.1", 0, "t" * 40)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield assistant, server.server_address[1]
    server.shutdown()


def test_the_panel_answers_a_request_with_what_rafiq_said(panel):
    assistant, port = panel
    status, body = request(port, "/api/ask", {"text": "what time is it", "lang": "fr", "speak": True})
    assert status == 200 and '"answer": "It is noon."' in body
    assert assistant.asked == [("what time is it", "fr", True)]


def test_asking_without_the_token_is_refused(panel):
    assistant, port = panel
    assert request(port, "/api/ask", {"text": "shut down"}, token=None)[0] == 403
    assert request(port, "/api/ask", {"text": "shut down"}, host="evil.example")[0] == 403
    assert assistant.asked == []


def test_an_unknown_language_is_refused(panel):
    assistant, port = panel
    assert request(port, "/api/ask", {"text": "hi", "lang": "xx"})[0] == 400
    assert assistant.asked == []


def test_a_request_not_answered_in_time_says_so(panel):
    assistant, port = panel
    assistant.answer = None
    assert request(port, "/api/ask", {"text": "what time is it"})[0] == 504


def loop_assistant(said):
    """An Assistant with only what a typed job needs; respond() says `said`."""
    a = object.__new__(Assistant)
    a.events, a.jobs, a.silent, a.dictating, a.tag = Events(), queue.Queue(), False, False, "en"
    a.state = "idle"
    a.set_state = lambda state: None
    heard = []

    def respond(text, tag, source="voice"):
        heard.append((text, tag, a.silent))
        a.events.publish("said", {"text": said, "tag": tag})

    a.respond = respond
    return a, heard


def test_a_request_from_the_terminal_returns_what_rafiq_said_without_speaking_it():
    a, heard = loop_assistant("It is noon.")
    reply = queue.Queue()
    a.submit("text", text="what time is it", tag="en", speak=False, reply=reply)
    a._run_jobs()
    assert reply.get_nowait() == "It is noon."
    assert heard == [("what time is it", "en", True)] and a.silent is False


def test_a_request_from_the_terminal_with_speak_is_said_out_loud():
    a, heard = loop_assistant("It is noon.")
    reply = queue.Queue()
    a.submit("text", text="what time is it", tag="en", speak=True, reply=reply)
    a._run_jobs()
    assert heard[0][2] is False


def test_ask_waits_for_the_loop_and_gives_up_after_its_time():
    a, _ = loop_assistant("It is noon.")
    assert a.ask("what time is it", timeout=0.01) is None


def ask_args(*words, **kw):
    return argparse.Namespace(text=list(words), lang=kw.get("lang"), brain=kw.get("brain"), mode=None,
                              speak=kw.get("speak", False))


class Answer:
    def __init__(self, status, data=None):
        self.status_code, self.data = status, data or {}

    def json(self):
        return self.data


def no_local_brain(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the request was answered here, not by the running Rafiq")

    monkeypatch.setattr("rafiq.brain.Brain", refuse)


def local_brain(monkeypatch, answer="local answer"):
    class FakeBrain:
        def __init__(self, *a, **k):
            pass

        def think(self, text, tag, ctx=None, on_speech=None):
            return answer

    monkeypatch.setattr("rafiq.brain.Brain", FakeBrain)


def test_ask_goes_through_the_running_rafiq(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(httpx, "post", lambda url, **k: sent.append((url, k)) or Answer(200, {"answer": "It is noon."}))
    no_local_brain(monkeypatch)
    assert cli.cmd_ask(ask_args("what", "time", "is", "it", lang="en", speak=True)) == 0
    assert capsys.readouterr().out.strip() == "It is noon."
    url, kw = sent[0]
    assert url.endswith("/api/ask") and kw["json"] == {"text": "what time is it", "lang": "en", "speak": True}
    assert kw["headers"]["X-Rafiq-Token"]


def test_with_nothing_running_ask_answers_by_itself(monkeypatch, capsys):
    def refused(url, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", refused)
    local_brain(monkeypatch)
    assert cli.cmd_ask(ask_args("hello")) == 0
    assert capsys.readouterr().out.strip() == "local answer"


def test_an_older_rafiq_without_ask_is_answered_here(monkeypatch, capsys):
    monkeypatch.setattr(httpx, "post", lambda url, **k: Answer(400, {"error": "unknown request"}))
    local_brain(monkeypatch)
    assert cli.cmd_ask(ask_args("hello")) == 0
    assert capsys.readouterr().out.strip() == "local answer"


def test_a_request_the_running_rafiq_took_is_never_run_twice(monkeypatch, capsys):
    def slow(url, **k):
        raise httpx.ReadTimeout("no answer yet")

    monkeypatch.setattr(httpx, "post", slow)
    no_local_brain(monkeypatch)
    assert cli.cmd_ask(ask_args("turn", "off", "the", "lights")) == 1


def test_choosing_a_brain_answers_here(monkeypatch, capsys):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("asked the running Rafiq"))
    local_brain(monkeypatch)
    assert cli.cmd_ask(ask_args("hello", brain="ollama")) == 0
