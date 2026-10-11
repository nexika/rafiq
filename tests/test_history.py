import json
from types import SimpleNamespace

import pytest

from rafiq import lang
from rafiq.assistant import Events
from rafiq.history import History, privacy

DAY = 86400


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


def make(tmp_path, clock, days=7):
    return History(tmp_path / "history.json", retention_days=days, clock=clock)


def test_a_request_and_its_answer_are_kept_on_disk(tmp_path, clock):
    make(tmp_path, clock).add("what time is it", "Twenty to four.", "en", "voice")
    [entry] = make(tmp_path, clock).entries()
    assert (entry.heard, entry.said, entry.tag, entry.source) == ("what time is it", "Twenty to four.", "en", "voice")


def test_only_this_user_can_read_the_file(tmp_path, clock):
    make(tmp_path, clock).add("hello", "hi", "en", "voice")
    assert (tmp_path / "history.json").stat().st_mode & 0o777 == 0o600


def test_entries_come_newest_first_and_search_matches_what_was_heard_or_said(tmp_path, clock):
    history = make(tmp_path, clock)
    history.add("weather in Cairo", "Sunny, 31 degrees.", "en", "voice")
    clock.now += 60
    history.add("الساعة كام دلوقتي؟", "الساعة أربعة إلا تلت.", "ar-EG", "voice")
    clock.now += 60
    history.add("set a timer", "Ten minutes, starting now.", "en", "typed")
    assert [e.heard for e in history.entries()][0] == "set a timer"
    assert [e.heard for e in history.entries("SUNNY")] == ["weather in Cairo"]
    assert [e.heard for e in history.entries("الساعة")] == ["الساعة كام دلوقتي؟"]


def test_one_entry_or_all_of_them_can_be_deleted(tmp_path, clock):
    history = make(tmp_path, clock)
    first = history.add("one", "1", "en", "voice")
    history.add("two", "2", "en", "voice")
    assert history.delete(first.id) == [first]
    assert [e.heard for e in make(tmp_path, clock).entries()] == ["two"]
    assert history.delete("nope") == []
    history.clear()
    assert make(tmp_path, clock).entries() == []


def test_forget_last_removes_only_the_latest(tmp_path, clock):
    history = make(tmp_path, clock)
    history.add("one", "1", "en", "voice")
    clock.now += 1
    history.add("two", "2", "en", "voice")
    assert history.forget_last().heard == "two"
    assert [e.heard for e in history.entries()] == ["one"]


def test_entries_older_than_the_retention_are_dropped(tmp_path, clock):
    history = make(tmp_path, clock, days=7)
    history.add("old", "a", "en", "voice")
    clock.now += 8 * DAY
    history.add("new", "b", "en", "voice")
    assert [e.heard for e in history.entries()] == ["new"]
    assert "old" not in (tmp_path / "history.json").read_text()


def test_a_retention_of_zero_keeps_nothing(tmp_path, clock):
    history = make(tmp_path, clock, days=0)
    history.add("secret", "ok", "en", "voice")
    assert history.entries() == [] and not (tmp_path / "history.json").exists()


def test_a_broken_file_starts_an_empty_history(tmp_path, clock):
    (tmp_path / "history.json").write_text("{not json")
    assert make(tmp_path, clock).entries() == []


def test_the_file_is_plain_json(tmp_path, clock):
    make(tmp_path, clock).add("hi", "hello", "en", "voice")
    assert json.loads((tmp_path / "history.json").read_text())[0]["heard"] == "hi"


# ---------------------------------------------------------------------- forget by voice

@pytest.mark.parametrize("text", [
    "Forget what I just said.", "forget that", "Rafiq, forget that please", "delete that",
    "Oublie ce que je viens de dire", "oublie ça", "Olvida lo que acabo de decir", "olvida eso",
    "Vergiss, was ich gerade gesagt habe", "vergiss das", "انسى اللي قلته", "انس ما قلته", "امسح اللي قلته",
])
def test_forget_requests_are_recognised_in_every_language(text):
    assert lang.is_forget(text)


@pytest.mark.parametrize("text", [
    "forget that meeting on Friday", "what did I just say", "delete that file in Downloads", "oublie pas le pain",
    "",
])
def test_other_requests_are_not_taken_for_forget(text):
    assert not lang.is_forget(text)


# ---------------------------------------------------------------------- the panel's events

def test_forgotten_texts_leave_the_panels_event_history_and_open_panels_are_told():
    events = Events()
    panel = events.subscribe()
    events.publish("heard", {"text": "Rafiq, my bank pin"})
    events.publish("said", {"text": "I can't help with that."})
    events.publish("heard", {"text": "weather"})
    events.drop({"my bank pin", "I can't help with that."})
    assert [e["text"] for e in events.history if "text" in e] == ["weather"]
    assert not any(e["kind"] == "forgotten" for e in events.history)   # the texts are not replayed
    told = [panel.get_nowait() for _ in range(4)][-1]
    assert told["kind"] == "forgotten" and "Rafiq, my bank pin" in told["texts"]


# ---------------------------------------------------------------------- what leaves this computer

def test_ollama_and_piper_are_fully_offline():
    status = privacy(["ollama"], ["piper"])
    assert status == {"brain": "ollama", "voice": "piper", "cloud": [], "offline": True}


def test_claude_or_a_cloud_voice_means_requests_leave_the_computer():
    assert privacy(["claude", "ollama"], ["piper"])["cloud"] == ["claude"]
    status = privacy(["ollama"], ["azure", "piper"])
    assert status["cloud"] == ["azure"] and not status["offline"]
    assert privacy(["ollama"], ["edge", "piper"])["cloud"] == ["edge"]


# ---------------------------------------------------------------------- the assistant

class FakeBrain:
    def __init__(self):
        from rafiq.brain import Memory

        self.memory, self.asked = Memory(), []

    def think(self, text, tag, ctx=None, on_speech=None):
        self.asked.append(text)
        answer = f"answer to {text}"
        self.memory.add(text, answer, 0.0, tag)
        return answer

    def backends(self):
        return ["claude", "ollama"]


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def say(self, text, tag):
        self.said.append(text)

    def engines(self):
        return ["piper"]

    def stream(self):
        return SimpleNamespace(put=lambda text, tag: None, close=lambda: True)

    def stop(self):
        pass


def make_assistant(tmp_path, clock):
    import queue

    from rafiq.assistant import Assistant, Context
    from rafiq.practice import Practice
    from rafiq.reader import Reader

    a = Assistant.__new__(Assistant)
    a.cfg = {"speech": {"follow_up_seconds": 6}}
    a.events, a.brain, a.speaker = Events(), FakeBrain(), FakeSpeaker()
    a.history = make(tmp_path, clock)
    a.ctx, a.tag, a.state, a.jobs = Context({}, None), "en", "idle", []
    a.pending_answer = queue.Queue()
    a.practice, a.silent = Practice(tmp_path / "practice.json"), False
    a.reader = Reader(interrupt=lambda: None)
    a.publish_spending = lambda tag: None
    return a


def test_each_answered_request_is_kept_with_where_it_came_from(tmp_path, clock):
    a = make_assistant(tmp_path, clock)
    a.respond("weather in Cairo", "en")
    a.respond("set a timer", "en", source="typed")
    assert [(e.heard, e.said, e.source) for e in a.history.entries()] == [
        ("set a timer", "answer to set a timer", "typed"), ("weather in Cairo", "answer to weather in Cairo", "voice")]


def test_forget_by_voice_deletes_the_last_request_everywhere_without_asking_the_brain(tmp_path, clock):
    a = make_assistant(tmp_path, clock)
    a.respond("weather in Cairo", "en")
    clock.now += 1
    a.respond("my bank pin is 1234", "en")
    a.events.publish("heard", {"text": "my bank pin is 1234"})
    a.respond("Forget what I just said", "en")
    assert [e.heard for e in a.history.entries()] == ["weather in Cairo"]
    assert a.brain.asked == ["weather in Cairo", "my bank pin is 1234"]
    assert [t.content for t in a.brain.memory.turns] == ["weather in Cairo", "answer to weather in Cairo"]
    assert not any(e.get("text") == "my bank pin is 1234" for e in a.events.history)
    assert a.speaker.said[-1] == lang.phrase("forgotten", "en")


def test_forget_with_nothing_kept_says_so(tmp_path, clock):
    a = make_assistant(tmp_path, clock)
    a.respond("oublie ça", "fr")
    assert a.speaker.said == [lang.phrase("nothing_to_forget", "fr")]


def test_the_assistant_reports_whether_requests_leave_the_computer(tmp_path, clock):
    status = make_assistant(tmp_path, clock).privacy()
    assert status["cloud"] == ["claude"] and not status["offline"]


# ---------------------------------------------------------------------- the panel

@pytest.fixture
def panel(tmp_path, clock):
    import threading

    from rafiq.panel import serve

    assistant = make_assistant(tmp_path, clock)
    server = serve(assistant, "127.0.0.1", 0, "t" * 40)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield assistant, server.server_address[1]
    server.shutdown()


def call(port, path, body=None, token="t" * 40):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("X-Rafiq-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def test_the_panel_lists_and_searches_the_history_with_the_privacy_status(panel, clock):
    assistant, port = panel
    assistant.respond("weather in Cairo", "en")
    clock.now += 1
    assistant.respond("set a timer", "en")
    status, body = call(port, "/api/history?token=" + "t" * 40)
    assert status == 200 and [e["heard"] for e in body["entries"]] == ["set a timer", "weather in Cairo"]
    assert body["privacy"]["cloud"] == ["claude"]
    _, body = call(port, "/api/history?q=cairo&token=" + "t" * 40)
    assert [e["heard"] for e in body["entries"]] == ["weather in Cairo"]


def test_the_history_needs_the_token(panel):
    assistant, port = panel
    assistant.respond("secret", "en")
    assert call(port, "/api/history")[0] == 403
    assert call(port, "/api/history?token=" + "x" * 40)[0] == 403
    assert call(port, "/api/history/clear", {}, token="x" * 40)[0] == 403
    assert len(assistant.history.entries()) == 1


def test_the_panel_deletes_one_entry_or_all(panel, clock):
    assistant, port = panel
    assistant.respond("one", "en")
    clock.now += 1
    assistant.respond("two", "en")
    first = assistant.history.entries()[-1]
    assert call(port, "/api/history/delete", {"id": first.id})[0] == 200
    assert [e.heard for e in assistant.history.entries()] == ["two"]
    assert [t.content for t in assistant.brain.memory.turns] == ["two", "answer to two"]
    assert call(port, "/api/history/clear", {})[0] == 200
    assert assistant.history.entries() == [] and assistant.brain.memory.turns == []
