import queue
import subprocess
import threading

import numpy as np
import pytest

from rafiq import lang
from rafiq.assistant import Assistant
from rafiq.audio import FRAME, EchoGate, echo_cancel_source
from rafiq.bargein import BargeIn


def frame(level):
    """A frame whose RMS is `level`."""
    return np.full(FRAME, level, dtype="float32")


@pytest.mark.parametrize("text", ["stop", "Stop!", "Rafiq, stop.", "attends", "Attends !", "espera", "Warte.",
                                  "توقف", "استنى", "استني", "stopp", "arrête"])
def test_a_stop_word_in_any_language_stops(text):
    assert lang.is_stop(text)


@pytest.mark.parametrize("text", ["", "what time is it", "the bus stops here",
                                  "and then you should stop at the second light on the left"])
def test_other_speech_does_not_stop(text):
    assert not lang.is_stop(text)


def test_the_echo_alone_is_not_loud_enough():
    gate = EchoGate(factor=1.8, min_level=0.01)
    assert not any(gate.loud(frame(0.05)) for _ in range(40))


def test_a_voice_well_above_the_echo_is_loud_enough():
    gate = EchoGate(factor=1.8, min_level=0.01)
    for _ in range(40):
        gate.loud(frame(0.05))
    assert gate.loud(frame(0.2))
    assert not gate.loud(frame(0.07))       # louder, but not 1.8 times


def test_a_quiet_room_needs_the_minimum_level():
    gate = EchoGate(factor=1.8, min_level=0.01)
    for _ in range(40):
        gate.loud(frame(0.0))
    assert not gate.loud(frame(0.005))
    assert gate.loud(frame(0.05))


def run(barge, levels):
    return [barge.push(frame(level)) for level in levels]


def test_saying_stop_over_rafiq_stops_it_once():
    heard, stopped = [], []
    barge = BargeIn(lambda audio: heard.append(audio) or "stop", lambda: stopped.append(True))
    out = run(barge, [0.05] * 40 + [0.3] * 15 + [0.05] * 30)
    assert stopped == [True] and out.count(True) == 1 and len(heard) == 1


def test_rafiqs_own_voice_is_never_transcribed():
    heard = []
    barge = BargeIn(lambda audio: heard.append(audio) or "stop", lambda: None)
    assert not any(run(barge, [0.05, 0.08, 0.04, 0.06] * 50))
    assert heard == []


def test_loud_speech_that_is_not_a_stop_word_does_not_stop():
    stopped = []
    barge = BargeIn(lambda audio: "and the weather tomorrow is", lambda: stopped.append(True))
    run(barge, [0.05] * 40 + [0.3] * 15 + [0.05] * 30)
    assert stopped == []


def test_the_echo_cancelled_source_is_found():
    out = ("55\talsa_input.pci-0000_00_1f.3.analog-stereo\tPipeWire\ts32le 2ch 48000Hz\tRUNNING\n"
           "71\techo-cancel-source\tPipeWire\tfloat32le 1ch 48000Hz\tSUSPENDED\n"
           "72\techo-cancel-sink.monitor\tPipeWire\tfloat32le 1ch 48000Hz\tSUSPENDED\n")

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["pactl", "list"] and kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    assert echo_cancel_source(fake_run) == "echo-cancel-source"


def test_no_echo_cancel_module_or_no_pactl_means_none():
    def plain(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="55\talsa_input.analog\tPipeWire\t-\tRUNNING\n", stderr="")

    def missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    assert echo_cancel_source(plain) is None
    assert echo_cancel_source(missing) is None



def test_a_stale_listener_does_not_stop_the_next_speech():
    """Whisper still busy when stop() gave up waiting: that thread must not act once it wakes up."""
    busy, release, stopped = threading.Event(), threading.Event(), []

    def slow(audio):
        busy.set()
        release.wait(5)
        return "stop"

    frames = iter([frame(0.05)] * 40 + [frame(0.3)] * 15 + [frame(0.05)] * 30)
    barge = BargeIn(slow, lambda: stopped.append(True))
    barge.start(lambda timeout: next(frames, None))
    assert busy.wait(5)
    old = barge._thread
    barge._done.set()                    # what stop() leaves behind when its join times out
    barge._thread = None
    barge.start(lambda timeout: None)    # the next sentence starts listening
    release.set()
    old.join(5)
    barge.stop()
    assert stopped == []


class Silent:
    def publish(self, *_):
        pass

    def stop(self):
        pass


def test_saying_stop_over_a_confirmation_cancels_it():
    a = object.__new__(Assistant)
    a.events = a.speaker = Silent()
    a.pending_answer, a.interrupted = queue.Queue(), threading.Event()
    a.speech = None                              # no answer being spoken
    a.set_state = lambda state: None
    said, listened = [], []

    def say(text, tag):
        said.append(text)
        if len(said) == 1:
            a._interrupted()                     # the user says "stop" over the question

    a.say = say
    a.next_utterance = lambda timeout: listened.append(1) or np.zeros(16000, dtype="float32")
    a.hear = lambda audio: ("yes", "en")         # a stray "yes" afterwards must not count
    assert a.confirm("shut down the computer", "en") is False
    assert listened == [] and said[-1] == lang.phrase("cancelled", "en")
