import queue
import threading

import numpy as np
from test_brain_panel import FakeAssistant

from rafiq.assistant import EVENTS_VERSION, Assistant, Events
from rafiq.panel import serve


def kept(events):
    return list(events.history)


def test_every_event_carries_the_stream_version():
    events = Events()
    events.publish("state", {"state": "idle"})
    assert kept(events)[0]["version"] == EVENTS_VERSION


def test_every_event_of_one_run_carries_the_same_run_id():
    events = Events()
    with events.run():
        events.publish("heard", {"text": "what time is it", "tag": "en"})
        events.publish("said", {"text": "Noon.", "tag": "en"})
    ids = {e.get("run_id") for e in kept(events)}
    assert len(ids) == 1 and None not in ids


def test_a_run_says_when_it_starts_and_ends():
    events = Events()
    with events.run():
        events.publish("said", {"text": "Noon.", "tag": "en"})
    runs = [e["status"] for e in kept(events) if e["kind"] == "run"]
    assert runs == ["started", "done"]


def test_a_run_that_raises_ends_as_failed():
    events = Events()
    try:
        with events.run():
            raise RuntimeError("the brain fell over")
    except RuntimeError:
        pass
    assert [e["status"] for e in kept(events) if e["kind"] == "run"] == ["started", "failed"]


def test_a_run_inside_a_run_is_the_same_run():
    events = Events()
    with events.run():
        with events.run():
            events.publish("said", {"text": "Noon.", "tag": "en"})
    assert len({e["run_id"] for e in kept(events)}) == 1
    assert [e["status"] for e in kept(events) if e["kind"] == "run"] == ["started", "done"]


def test_two_requests_get_different_run_ids():
    events = Events()
    for text in ("one", "two"):
        with events.run():
            events.publish("said", {"text": text, "tag": "en"})
    said = [e["run_id"] for e in kept(events) if e["kind"] == "said"]
    assert said[0] != said[1]


def test_an_event_from_another_thread_during_a_run_has_no_run_id():
    """The panel publishes from its own thread; a persona change is not part of the request."""
    events = Events()
    with events.run():
        t = threading.Thread(target=events.publish, args=("persona", {"persona": "butler"}))
        t.start()
        t.join()
    persona = next(e for e in kept(events) if e["kind"] == "persona")
    assert "run_id" not in persona


def test_an_event_that_names_its_own_run_keeps_it():
    events = Events()
    with events.run():
        events.publish("task", {"id": "ab12cd", "run_id": "task-ab12cd"})
    task = next(e for e in kept(events) if e["kind"] == "task")
    assert task["run_id"] == "task-ab12cd"


def test_a_watcher_is_still_called_with_the_event_data_alone():
    """The answer overlay reads kind and data; the run fields must not change what it gets."""
    events, seen = Events(), []
    events.watchers.append(lambda kind, data: seen.append((kind, data)))
    with events.run():
        events.publish("said", {"text": "Noon.", "tag": "en"})
    assert ("said", {"text": "Noon.", "tag": "en"}) in seen


# ---------------------------------------------------------------------- requests become runs

def loop_assistant():
    a = object.__new__(Assistant)
    a.events, a.jobs, a.silent, a.dictating, a.tag = Events(), queue.Queue(), False, False, "en"
    a.set_state = lambda state: None
    a.respond = lambda text, tag, source="voice": a.events.publish("said", {"text": "Noon.", "tag": tag})
    return a


def test_a_typed_request_and_its_answer_are_one_run():
    a = loop_assistant()
    a.submit("text", text="what time is it", tag="en")
    a._run_jobs()
    typed = next(e for e in kept(a.events) if e["kind"] == "typed")
    said = next(e for e in kept(a.events) if e["kind"] == "said")
    assert typed["run_id"] and typed["run_id"] == said["run_id"]


def test_a_spoken_follow_up_and_its_answer_are_one_run():
    a = loop_assistant()
    a.follow_up_until = float("inf")
    a.wake_names = ["rafiq"]

    def hear(audio, languages=None):
        a.events.publish("heard", {"text": "and tomorrow?", "tag": "en"})
        return "and tomorrow?", "en"

    a.hear = hear
    a.handle_idle_utterance(np.zeros(16000, dtype=np.float32))
    heard = next(e for e in kept(a.events) if e["kind"] == "heard")
    said = next(e for e in kept(a.events) if e["kind"] == "said")
    assert heard["run_id"] and heard["run_id"] == said["run_id"]


# ---------------------------------------------------------------------- the stream

class StreamingAssistant(FakeAssistant):
    cfg: dict = {}

    def privacy(self):
        return {"brain": "here", "voice": "here"}


def test_the_events_a_panel_gets_when_it_connects_carry_the_version_too():
    import json
    import urllib.request

    assistant = StreamingAssistant()
    assistant.events.publish("said", {"text": "Noon.", "tag": "en"})
    server = serve(assistant, "127.0.0.1", 0, "t" * 40)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/events?token={'t' * 40}", timeout=5) as resp:
            lines = []
            while len(lines) < 5:
                line = resp.readline().decode()
                if line.startswith("data: "):
                    lines.append(json.loads(line[6:]))
    finally:
        server.shutdown()
    assert [e["kind"] for e in lines] == ["said", "state", "privacy", "mode", "persona"]
    assert all(e["version"] == EVENTS_VERSION for e in lines)


def test_a_failed_run_says_why():
    events = Events()
    try:
        with events.run():
            raise RuntimeError("the brain fell over")
    except RuntimeError:
        pass
    end = [e for e in kept(events) if e["kind"] == "run"][-1]
    assert end["status"] == "failed" and end["error"] == "RuntimeError: the brain fell over"


def test_a_thread_given_the_run_context_still_starts_without_a_run():
    """Free-threaded Python can hand a new thread its parent's context; a run belongs to its own thread."""
    import contextvars

    events = Events()
    with events.run():
        context = contextvars.copy_context()
        t = threading.Thread(target=context.run, args=(events.publish, "persona", {"persona": "butler"}))
        t.start()
        t.join()
    assert "run_id" not in next(e for e in kept(events) if e["kind"] == "persona")
