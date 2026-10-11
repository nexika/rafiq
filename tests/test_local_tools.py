import copy

import pytest

from rafiq import config, lang, toolcheck
from rafiq.brain import Brain
from rafiq.tools import LOCAL_TOOLS, Tool, load_all, local_tools


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


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


def timer_tools(log):
    def set_timer(seconds, label=""):
        log.append(seconds)
        return f"timer set for {seconds} seconds"

    return {"set_timer": Tool("set_timer", "timer", set_timer, {"seconds": {"type": "integer"}}, ("seconds",)),
            "list_apps": Tool("list_apps", "apps", lambda: "firefox")}


def make_brain(tools, **brain):
    cfg = config.load()
    cfg["brain"].update(mode="ollama", **brain)
    return Brain(cfg, tools, lambda action, tag: True)


def call(name, arguments):
    return {"role": "assistant", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


# ---------------------------------------------------------------------- the smaller tool set

def test_the_local_set_is_smaller_and_only_names_real_tools():
    tools = load_all({"google": {"enabled": True}})
    assert set(LOCAL_TOOLS) <= set(tools)
    assert len(local_tools(tools, "local")) < len(tools)


def test_the_local_set_leaves_out_tools_that_are_not_loaded():
    tools = load_all({"google": {"enabled": False}})
    assert set(local_tools(tools, "local")) <= set(tools)


@pytest.mark.parametrize("choice, names", [
    ("all", {"set_timer", "list_apps"}), ("local", {"set_timer"}), (["list_apps", "nope"], {"list_apps"}),
])
def test_the_setting_picks_the_tools(choice, names):
    assert set(local_tools(timer_tools([]), choice)) == names


def test_ollama_gets_the_local_set_by_default(ollama):
    script, requests = ollama
    script += [{"role": "assistant", "content": "hi"}]
    make_brain(timer_tools([])).think("hello", "en")
    assert [t["function"]["name"] for t in requests[0]["tools"]] == ["set_timer"]


def test_all_tools_can_still_be_sent_to_ollama(ollama):
    script, requests = ollama
    script += [{"role": "assistant", "content": "hi"}]
    make_brain(timer_tools([]), ollama_tools="all").think("hello", "en")
    assert len(requests[0]["tools"]) == 2


# ---------------------------------------------------------------------- one retry on a broken call

@pytest.mark.parametrize("broken", ['{"seconds": 30', "[30]", {}, {"secs": 30}])
def test_a_broken_tool_call_is_not_run_and_the_model_is_asked_again(ollama, broken):
    script, requests = ollama
    log = []
    script += [call("set_timer", broken), call("set_timer", {"seconds": 30}),
               {"role": "assistant", "content": "Timer set."}]
    assert make_brain(timer_tools(log)).think("timer thirty seconds", "en") == "Timer set."
    assert log == [30]
    retry = requests[1]["messages"][-1]
    assert retry["role"] == "tool" and retry["content"].startswith("error:")


def test_a_second_broken_call_in_the_same_turn_gives_up(ollama):
    script, _ = ollama
    log = []
    script += [call("set_timer", "{oops"), call("set_timer", "{oops again")]
    answer = make_brain(timer_tools(log)).think("timer", "en")
    assert log == [] and answer == lang.phrase("brain_down", "en")


def test_string_arguments_that_are_valid_json_still_run(ollama):
    script, _ = ollama
    log = []
    script += [call("set_timer", '{"seconds": 5}'), {"role": "assistant", "content": "Done."}]
    make_brain(timer_tools(log)).think("timer", "en")
    assert log == [5]


# ---------------------------------------------------------------------- the accuracy check

def test_toolcheck_scores_the_tool_and_the_language():
    replies = iter([call("get_time", {}), {"role": "assistant", "content": "Bonjour !"},
                    {"role": "assistant", "content": "Hello!"}])
    requests = [toolcheck.Request("what time is it", "en", "get_time"),
                toolcheck.Request("salut", "fr", None),
                toolcheck.Request("إزيك", "ar-EG", None)]
    sent = []

    def ask(payload):
        sent.append(payload)
        return next(replies)

    tools = {"get_time": Tool("get_time", "time", lambda: "now")}
    results = toolcheck.run(config.load(), tools, requests, ask)
    assert [(r.right_tool, r.right_language) for r in results] == [(True, True), (True, True), (True, False)]
    assert toolcheck.score(results) == (3, 3, 2)
    assert all(p["tools"][0]["function"]["name"] == "get_time" for p in sent)


def test_toolcheck_counts_a_missed_tool():
    tools = {"get_time": Tool("get_time", "time", lambda: "now")}
    results = toolcheck.run(config.load(), tools, [toolcheck.Request("what time is it", "en", "get_time")],
                            lambda payload: {"role": "assistant", "content": "It is noon."})
    assert results[0].called == [] and not results[0].right_tool


def test_the_fixed_requests_only_expect_real_tools():
    tools = load_all({"google": {"enabled": True}})
    assert len(toolcheck.REQUESTS) >= 15
    assert {r.tool for r in toolcheck.REQUESTS if r.tool} <= set(tools)
