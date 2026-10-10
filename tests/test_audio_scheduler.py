import numpy as np

from rafiq.audio import FRAME, Endpointer
from rafiq.scheduler import Scheduler


def frames(n, value=0.0):
    return [np.full(FRAME, value, dtype="float32") for _ in range(n)]


def test_an_utterance_starts_after_a_few_speech_frames_and_ends_after_silence():
    ep = Endpointer(start_frames=3, end_seconds=0.2, pre_seconds=0.1)
    out = [ep.push(f, 0.0) for f in frames(10)]                 # silence
    out += [ep.push(f, 0.9) for f in frames(20, 0.5)]           # speech
    assert ep.active and not any(o is not None for o in out)
    tail = [ep.push(f, 0.0) for f in frames(10)]                # silence again
    done = [o for o in tail if o is not None]
    assert len(done) == 1 and not ep.active
    # the speech, a little before it, and the silence that ended it
    assert len(done[0]) >= 20 * FRAME


def test_a_single_click_is_not_speech():
    ep = Endpointer(start_frames=3)
    assert all(ep.push(f, p) is None for f, p in zip(frames(6), [0.9, 0.1, 0.9, 0.1, 0.9, 0.1], strict=True))
    assert not ep.active


def test_an_utterance_is_cut_at_the_maximum_length():
    ep = Endpointer(start_frames=1, max_seconds=1.0)
    results = [ep.push(f, 0.9) for f in frames(100, 0.5)]
    assert any(r is not None for r in results)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_alarms_fire_once_when_due_and_survive_a_restart(tmp_path):
    clock, fired = Clock(), []
    path = tmp_path / "alarms.json"
    s = Scheduler(path, fired.append, clock=clock)
    s.add(1060, "tea", "timer", "en")
    s.add(5000, "call mum", "reminder", "ar-EG")
    assert s.tick() == []
    restarted = Scheduler(path, fired.append, clock=clock)
    assert [a.label for a in restarted.pending()] == ["tea", "call mum"]
    clock.t = 1061
    assert [a.label for a in restarted.tick()] == ["tea"]
    assert restarted.tick() == []
    assert fired[0].tag == "en" and [a.label for a in restarted.pending()] == ["call mum"]


def test_cancel_by_label_words(tmp_path):
    s = Scheduler(tmp_path / "a.json", lambda a: None, clock=Clock())
    s.add(2000, "Pasta water", "timer", "en")
    s.add(2000, "Laundry", "timer", "en")
    assert [a.label for a in s.cancel(label="pasta")] == ["Pasta water"]
    assert [a.label for a in s.pending()] == ["Laundry"]
