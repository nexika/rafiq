import copy

import pytest

from rafiq import config, router
from rafiq.brain import Brain
from rafiq.tools import Tool

NAMES = ("get_time", "set_timer", "weather", "calendar_events", "email_send", "volume", "power", "open_app",
         "calculate", "convert_units", "web_search")
TOOLS = {name: Tool(name, name, lambda: "ok") for name in NAMES}


def offered(text):
    return set(router.route(text, TOOLS).tools)


@pytest.mark.parametrize("text", [
    "what's the weather like", "quel temps fait-il à Paris", "¿qué tiempo hace hoy?", "Wie ist das Wetter?",
    "ما حالة الطقس اليوم", "الجو عامل ايه النهارده",
])
def test_weather_in_every_language_gets_only_the_weather_tool(text):
    assert offered(text) == {"weather"}


@pytest.mark.parametrize("text", [
    "what time is it", "quelle heure est-il", "¿qué hora es?", "wie spät ist es", "كم الساعة", "الساعة كام",
    "set a timer for five minutes", "mets un minuteur de dix minutes", "stell einen Timer",
])
def test_time_and_timers_get_only_the_time_tools(text):
    assert offered(text) == {"get_time", "set_timer"}


@pytest.mark.parametrize("text", [
    "turn the volume up", "baisse le volume", "sube el volumen", "mach die Lautstärke leiser",
    "علي الصوت", "shut down the computer", "open firefox",
])
def test_computer_requests_get_only_the_computer_tools(text):
    assert offered(text) == {"volume", "power", "open_app"}


def test_calendar_and_email_are_their_own_domains():
    assert offered("what's on my calendar tomorrow") == {"calendar_events"}
    assert offered("send an email to Sara") == {"email_send"}
    assert offered("قراءة البريد") == {"email_send"}


def test_a_request_touching_two_domains_gets_both():
    assert offered("what's the weather and what time is it") == {"weather", "get_time", "set_timer"}


@pytest.mark.parametrize("text", [
    "hello", "thanks!", "good morning Rafiq", "merci beaucoup", "hola", "danke", "شكرا", "ازيك", "how are you",
])
def test_small_talk_gets_no_tools(text):
    route = router.route(text, TOOLS)
    assert route.name == "small talk" and route.tools == {}


@pytest.mark.parametrize("text", [
    "who won the match last night", "Paris", "tell me about the history of Cairo", "hello, who is the president",
])
def test_anything_unclear_keeps_every_tool(text):
    route = router.route(text, TOOLS)
    assert route.name == "all" and route.tools == TOOLS


def test_only_tools_that_are_loaded_are_offered():
    """Google is optional: with no calendar tools, a calendar request falls back to every tool."""
    tools = {k: v for k, v in TOOLS.items() if k != "calendar_events"}
    assert router.route("what's on my calendar", tools).tools == tools


def test_a_number_question_gets_the_math_tools():
    assert offered("what is 15 times 4") == {"calculate", "convert_units"}
    assert offered("convert 10 miles to km") == {"calculate", "convert_units"}


# ---------------------------------------------------------------------- in the brain

@pytest.fixture
def ollama(monkeypatch):
    script, requests = [], []

    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    def post(url, json=None, timeout=None):
        requests.append(copy.deepcopy(json))
        return Response({"message": script.pop(0)})

    import httpx

    monkeypatch.setattr(httpx, "post", post)
    return script, requests


def make_brain(routing=True, events=None):
    cfg = config.load()
    cfg["brain"]["mode"] = "ollama"
    cfg["brain"]["routing"] = routing
    on_event = (lambda kind, data: events.append((kind, data))) if events is not None else None
    return Brain(cfg, dict(TOOLS), lambda action, tag: True, on_event=on_event)


def tool_names(request):
    return {t["function"]["name"] for t in request["tools"]}


def test_the_brain_sends_only_the_routed_tools(ollama):
    script, requests = ollama
    events = []
    script += [{"role": "assistant", "content": "Sunny."}]
    make_brain(events=events).think("what's the weather", "en")
    assert tool_names(requests[0]) == {"weather"}
    assert ("brain", {"backend": "ollama", "route": "weather"}) in events


def test_small_talk_reaches_the_model_with_no_tools(ollama):
    script, requests = ollama
    script += [{"role": "assistant", "content": "Hi!"}]
    make_brain().think("hello", "en")
    assert requests[0]["tools"] == []


def test_a_tool_outside_the_route_is_not_run(ollama):
    script, requests = ollama
    script += [{"role": "assistant", "tool_calls": [{"function": {"name": "power", "arguments": {}}}]},
               {"role": "assistant", "content": "No."}]
    make_brain().think("what's the weather", "en")
    assert requests[1]["messages"][-1]["content"].startswith("error: there is no tool")


def test_routing_can_be_turned_off(ollama):
    script, requests = ollama
    script += [{"role": "assistant", "content": "Sunny."}]
    make_brain(routing=False).think("what's the weather", "en")
    assert tool_names(requests[0]) == set(NAMES)
