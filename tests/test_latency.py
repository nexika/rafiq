import json
import threading
from pathlib import Path

import pytest

from rafiq import config, lang, timing
from rafiq.brain import Brain
from rafiq.practice import Practice
from rafiq.tools import REGISTRY, Tool, load_all
from rafiq.tools.web import SEARCHING
from rafiq.tts import Sentences, Speaker


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


@pytest.fixture
def ollama(monkeypatch):
    script = []

    def post(url, json=None, timeout=None):
        return FakeResponse({"message": script.pop(0)})

    import httpx

    monkeypatch.setattr(httpx, "post", post)
    return script


def slow_and_fast_tools(log):
    def search(query=""):
        log.append("search ran")
        return "results"

    def clock():
        log.append("clock ran")
        return "15:40"

    return {"web_search": Tool("web_search", "search", search, slow=True, waiting_phrase=SEARCHING),
            "get_time": Tool("get_time", "time", clock)}


def make_brain(mode="ollama", log=None):
    cfg = config.load()
    cfg["brain"]["mode"] = mode
    cfg["anthropic_api_key"] = "test"
    return Brain(cfg, slow_and_fast_tools(log if log is not None else []), lambda action, tag: True)


def call(name, args=None):
    return {"role": "assistant", "tool_calls": [{"function": {"name": name, "arguments": args or {}}}]}


# ---------------------------------------------------------------------- sentences

def test_sentences_come_out_as_soon_as_they_end():
    split = Sentences()
    assert split.push("Hello the") == []
    assert split.push("re. How are") == ["Hello there."]
    assert split.push(" you? Fine") == ["How are you?"]
    assert split.flush() == "Fine"
    assert split.flush() == ""


def test_arabic_question_marks_end_a_sentence_and_decimals_do_not():
    split = Sentences()
    assert split.push("إزيك؟ الجو ") == ["إزيك؟"]
    assert split.push("It is 3.5 degrees. ") == ["الجو It is 3.5 degrees."]


# ---------------------------------------------------------------------- waiting phrase

def test_a_slow_tool_is_announced_before_it_runs_in_the_users_language(ollama):
    log, heard = [], []
    ollama += [call("web_search", {"query": "news"}), {"role": "assistant", "content": "Nothing new."}]
    brain = make_brain(log=log)
    brain.think("quoi de neuf", "fr", on_speech=lambda text, tag: heard.append((text, tag, list(log))))
    assert heard[0] == (SEARCHING["fr"], "fr", [])      # said before the search ran
    assert heard[-1][0] == "Nothing new."


def test_a_fast_tool_is_not_announced(ollama):
    heard = []
    ollama += [call("get_time"), {"role": "assistant", "content": "Twenty to four."}]
    make_brain().think("what time is it", "en", on_speech=lambda text, tag: heard.append(text))
    assert heard == ["Twenty to four."]


def test_the_waiting_phrase_is_said_once_per_request(ollama):
    heard = []
    ollama += [call("web_search", {"query": "a"}), call("web_search", {"query": "b"}),
               {"role": "assistant", "content": "Done."}]
    make_brain().think("search twice", "en", on_speech=lambda text, tag: heard.append(text))
    assert heard == [SEARCHING["en"], "Done."]


def test_the_fallback_waiting_phrase_exists_in_the_six_languages():
    assert set(lang.PHRASES["wait"]) == set(lang.TAGS)


def test_slow_tools_are_marked_and_have_a_waiting_phrase():
    load_all({"google": {"enabled": True}})
    for name in ("web_search", "read_page", "weather", "calendar_events", "email_list", "email_read"):
        assert REGISTRY[name].slow, name
        assert set(REGISTRY[name].waiting_phrase) == set(lang.TAGS), name
    for name in ("get_time", "calculate", "set_timer", "volume"):
        assert not REGISTRY[name].slow, name


# ---------------------------------------------------------------------- streamed answers

class Block:
    def __init__(self, type, text="", id="", name="", input=None):
        self.type, self.text, self.id, self.name, self.input = type, text, id, name, input

    def model_dump(self):
        return {"type": self.type, "text": self.text}


class FakeStream:
    def __init__(self, chunks, content, log):
        self.chunks, self.content, self.log = chunks, content, log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        for chunk in self.chunks:
            self.log.append(("chunk", chunk))
            yield chunk

    def get_final_message(self):
        return type("Message", (), {"content": self.content})()


class FakeClaude:
    """Each call to messages.stream plays the next scripted reply."""
    def __init__(self, replies, log):
        self.replies, self.log = replies, log
        self.messages = self

    def stream(self, **kwargs):
        chunks, content = self.replies.pop(0)
        return FakeStream(chunks, content, self.log)


def test_claude_speaks_the_first_sentence_while_the_rest_is_still_coming():
    log = []
    brain = make_brain("claude")
    text = "It is sunny. Take a hat!"
    brain._claude = FakeClaude([(["It is sun", "ny. Take", " a hat!"], [Block("text", text)])], log)
    answer = brain.think("weather?", "en", on_speech=lambda text, tag: log.append(("said", text)))
    assert answer == text
    assert log == [("chunk", "It is sun"), ("chunk", "ny. Take"), ("said", "It is sunny."),
                   ("chunk", " a hat!"), ("said", "Take a hat!")]


def test_claude_text_before_a_tool_replaces_the_waiting_phrase():
    log, heard = [], []
    brain = make_brain("claude", log=log)
    brain._claude = FakeClaude([
        (["Let me look."], [Block("text", "Let me look."), Block("tool_use", id="1", name="web_search",
                                                                  input={"query": "x"})]),
        (["Found it."], [Block("text", "Found it.")]),
    ], [])
    assert brain.think("search x", "en", on_speech=lambda text, tag: heard.append(text)) == "Found it."
    assert heard == ["Let me look.", "Found it."] and log == ["search ran"]


def test_ollama_answers_are_spoken_sentence_by_sentence(ollama):
    heard = []
    ollama += [{"role": "assistant", "content": "أهلاً! أنا رفيق."}]
    answer = make_brain().think("إزيك", "ar-EG", on_speech=lambda text, tag: heard.append((text, tag)))
    assert answer == "أهلاً! أنا رفيق."
    assert heard == [("أهلاً!", "ar-EG"), ("أنا رفيق.", "ar-EG")]


def test_without_a_listener_nothing_is_spoken_and_the_answer_is_returned(ollama):
    ollama += [call("web_search", {"query": "a"}), {"role": "assistant", "content": "Done."}]
    assert make_brain().think("search", "en") == "Done."


# ---------------------------------------------------------------------- speaking a stream

def fake_speaker(log, synth_gate=None):
    speaker = Speaker(config.load(), on_start=lambda: log.append("start"), on_end=lambda: log.append("end"))

    def synthesize(text, tag, folder, name="say"):
        log.append(("synth", text))
        return folder / f"{name}.wav", []

    def play(path):
        log.append(("play", path.name))
        if synth_gate is not None:
            synth_gate.wait(2)

    speaker.synthesize, speaker._play = synthesize, play
    return speaker


def test_a_stream_plays_sentences_in_order_with_one_start_and_one_end():
    log = []
    stream = fake_speaker(log).stream()
    stream.put("One.", "en")
    stream.put("Two.", "en")
    assert stream.close() is True
    plays = [entry for entry in log if entry[0] == "play"]
    assert plays == [("play", "say1.wav"), ("play", "say2.wav")]
    assert log[0] == ("synth", "One.") and log.count("start") == 1 and log[-1] == "end"


def test_the_next_sentence_is_made_while_the_first_plays():
    log, gate = [], threading.Event()
    stream = fake_speaker(log, synth_gate=gate).stream()
    stream.put("One.", "en")
    stream.put("Two.", "en")
    for _ in range(200):
        if ("synth", "Two.") in log:
            break
        threading.Event().wait(0.01)
    assert ("synth", "Two.") in log and ("play", "say2.wav") not in log
    gate.set()
    stream.close()


def test_an_empty_stream_never_takes_the_speaker():
    log = []
    stream = fake_speaker(log).stream()
    stream.put("  ", "en")
    assert stream.close() is True and log == []


# ---------------------------------------------------------------------- timing log

def test_timings_are_logged_without_the_content(ollama):
    ollama += [call("web_search", {"query": "secret plans"}), {"role": "assistant", "content": "Private answer."}]
    make_brain().think("my secret question", "en")
    text = timing.path().read_text()
    assert "secret" not in text and "Private" not in text
    stages = [json.loads(line)["stage"] for line in text.splitlines()]
    assert stages.count("model") == 2 and stages.count("tool") == 1


def test_the_summary_gives_the_median_of_each_stage():
    for seconds in (0.1, 0.3, 0.2):
        timing.record("stt", seconds)
    timing.record("model", 1.0, backend="claude")
    assert timing.summary() == {"stt": (200, 3), "model": (1000, 1)}
    assert timing.line() == "speech-to-text 200 ms, model 1000 ms (last 4 timings, medians)"


def test_the_summary_line_says_when_nothing_was_timed_yet():
    assert "no timings yet" in timing.line()


# ---------------------------------------------------------------------- the assistant

def test_the_assistant_speaks_the_answer_as_it_comes_and_shows_it_once(ollama):
    from rafiq.assistant import Assistant, Events

    log = []
    ollama += [{"role": "assistant", "content": "One. Two."}]
    assistant = Assistant.__new__(Assistant)       # no microphone or models
    assistant.practice = Practice(Path("practice.json"))   # no session running
    assistant.cfg = config.load()
    assistant.cfg["speech"]["follow_up_seconds"] = 0
    assistant.events, assistant.speech, assistant.tag, assistant.silent = Events(), None, "en", False
    assistant.ctx = type("Ctx", (), {"tag": "en"})()
    assistant.speaker = fake_speaker(log)
    assistant.brain = make_brain()
    assistant.publish_spending = lambda tag: None
    assistant.respond("count", "en")
    assert [e for e in log if e[0] == "play"] == [("play", "say1.wav"), ("play", "say2.wav")]
    said = [e for e in assistant.events.history if e["kind"] == "said"]
    assert [e["text"] for e in said] == ["One. Two."] and assistant.speech is None
