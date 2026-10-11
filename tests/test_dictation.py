import subprocess

import pytest

from rafiq import dictation
from rafiq.dictation import Keyboard, Session, TypingFailed


@pytest.mark.parametrize("text", [
    "dictate", "Dictate.", "start dictating", "dictation", "dicte", "dictée", "commence la dictée", "dicta",
    "empieza a dictar", "diktieren", "Diktat starten", "إملاء", "ابدأ الإملاء", "اكتب ورايا",
])
def test_the_start_phrase_in_every_language(text):
    assert dictation.wants_dictation(text)


@pytest.mark.parametrize("text", ["what is dictation", "dictate an email to Sara", "how do I spell dictate",
                                  "what time is it", ""])
def test_a_request_about_dictation_is_not_the_start_phrase(text):
    assert not dictation.wants_dictation(text)


@pytest.mark.parametrize("text, before", [
    ("stop dictating", ""),
    ("Stop dictating.", ""),
    ("and that is all. Stop dictating", "and that is all."),
    ("arrête la dictée", ""),
    ("deja de dictar", ""),
    ("Diktat beenden", ""),
    ("توقف عن الإملاء", ""),
    ("شكرا. أوقف الإملاء", "شكرا."),
    ("بطل إملاء", ""),
])
def test_the_stop_phrase_ends_dictation_and_keeps_what_came_before(text, before):
    assert dictation.stop_command(text) == (True, before)


def test_text_without_the_stop_phrase_is_kept_whole():
    assert dictation.stop_command("please stop the music") == (False, "please stop the music")


@pytest.mark.parametrize("text, lang, expected", [
    ("hello comma how are you question mark", "en", "Hello, how are you?"),
    ("Hello, comma, world.", "en", "Hello, world."),
    ("one full stop two", "en", "One. Two"),
    ("first line new line second", "en", "First line\nsecond"),
    ("bonjour virgule ça va point d'interrogation", "fr", "Bonjour, ça va?"),
    ("hola coma qué tal punto", "es", "Hola, qué tal."),
    ("hallo Komma wie geht's Fragezeichen", "de", "Hallo, wie geht's?"),
    ("مرحبا فاصلة كيف حالك علامة استفهام", "ar", "مرحبا، كيف حالك؟"),
    ("أهلا فاصلة إزيك", "ar-EG", "أهلا، إزيك"),
])
def test_spoken_punctuation_becomes_marks(text, lang, expected):
    assert dictation.punctuate(text, lang) == expected


def test_a_session_joins_chunks_with_spaces_and_stops_on_the_phrase():
    session = Session()
    assert session.add("hello comma", "en") == (False, "Hello,")
    assert session.add("how are you question mark", "en") == (False, " how are you?")
    assert session.add("fine new line", "en") == (False, " Fine\n")
    assert session.add("thanks stop dictating", "en") == (True, "thanks")


def test_a_session_types_nothing_for_silence():
    assert Session().add("", "en") == (False, "")


class Runner:
    """Stands in for subprocess.run: records each command and fails the ones it is told to."""
    def __init__(self, failing=()):
        self.calls, self.failing = [], set(failing)

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs.get("input")))
        if args[0] in self.failing:
            return subprocess.CompletedProcess(args, 1, "", "no")
        return subprocess.CompletedProcess(args, 0, "", "")


def keyboard(session_type, runner, installed=("ydotool", "xdotool", "wtype", "wl-copy", "xclip")):
    env = {"XDG_SESSION_TYPE": session_type}
    return Keyboard(env=env, which=lambda name: f"/usr/bin/{name}" if name in installed else None, run=runner)


def test_x11_types_with_xdotool_as_an_argument_never_a_shell():
    runner = Runner()
    assert keyboard("x11", runner).type("hello; rm -rf ~") == "xdotool"
    args, _ = runner.calls[0]
    assert args[:2] == ["xdotool", "type"] and args[-1] == "hello; rm -rf ~"


def test_wayland_types_latin_text_with_wtype_first():
    runner = Runner()
    assert keyboard("wayland", runner).type("hello") == "wtype"


def test_wayland_without_wtype_types_latin_text_with_ydotool():
    runner = Runner()
    assert keyboard("wayland", runner, installed=("ydotool", "wl-copy")).type("hello") == "ydotool"


def test_arabic_on_ydotool_is_pasted_through_the_clipboard():
    """ydotool sends key codes, so it cannot type Arabic: the text goes on the clipboard instead."""
    runner = Runner()
    assert keyboard("wayland", runner, installed=("ydotool", "wl-copy")).type("مرحبا") == "paste"
    assert runner.calls[0] == (["wl-copy"], "مرحبا")
    assert runner.calls[1][0][:2] == ["ydotool", "key"]


def test_when_typing_fails_the_text_is_pasted():
    runner = Runner(failing={"xdotool"})
    kb = keyboard("x11", runner, installed=("xdotool", "xclip"))
    with pytest.raises(TypingFailed):   # the paste needs xdotool too
        kb.type("bonjour")
    runner = Runner(failing={"wtype"})
    assert keyboard("wayland", runner, installed=("wtype", "wl-copy", "ydotool")).type("é") == "paste"


def test_without_any_typing_tool_it_says_what_to_install():
    with pytest.raises(TypingFailed, match="ydotool|wtype"):
        keyboard("wayland", Runner(), installed=()).type("hello")


# ---------------------------------------------------------------------- in the assistant

class FakeKeyboard:
    def __init__(self, fail=False):
        self.typed, self.fail = [], fail

    def type(self, text):
        if self.fail:
            raise TypingFailed("could not type: install xdotool and xclip")
        self.typed.append(text)
        return "xdotool"


@pytest.fixture
def assistant():
    """An Assistant without microphone, models or speaker: each utterance is the text it would be heard as."""
    import queue
    from pathlib import Path

    from rafiq import config
    from rafiq.assistant import Assistant, Context, Events
    from rafiq.practice import Practice
    from rafiq.stt import Heard

    a = Assistant.__new__(Assistant)
    a.cfg = config.load()
    a.events, a.jobs, a.tag, a.state = Events(), queue.Queue(), "en", "idle"
    a.ctx = Context(a.cfg, None)
    a.keyboard, a.dictating, a.follow_up_until = FakeKeyboard(), False, 0.0
    a._stop = __import__("threading").Event()
    a.stt_lock, a.muted = __import__("threading").Lock(), False
    a.practice = Practice(Path("practice.json"))   # no session running
    a.spoken, a.utterances = [], []
    a.say = lambda text, tag: a.spoken.append((text, tag))
    a.brain = type("B", (), {"think": lambda self, *args: pytest.fail("dictation went to the brain")})()

    def next_utterance(timeout):
        a._run_jobs()
        return a.utterances.pop(0) if a.utterances else None

    a.next_utterance = next_utterance
    a.stt = type("T", (), {"transcribe": lambda self, audio, **kw: Heard(audio[0], audio[1], 1.0)})()
    return a


def states(a):
    return [e["state"] for e in a.events.history if e["kind"] == "state"]


def test_saying_dictate_types_what_follows_until_stop(assistant):
    assistant.utterances = [("hello comma", "en"), ("مرحبا فاصلة", "ar"), ("done period stop dictating", "en")]
    assistant.respond("dictate", "en")
    assert assistant.keyboard.typed == ["Hello,", " مرحبا،", " done."]
    assert "dictating" in states(assistant) and assistant.dictating is False
    assert [e["on"] for e in assistant.events.history if e["kind"] == "dictation"] == [True, False]
    assert len(assistant.spoken) == 2          # "dictating" when it starts, "stopped" when it ends


def test_the_shortcut_starts_dictation_and_pressed_again_stops_it(assistant):
    assistant.submit("dictate")
    assistant.utterances = [("one", "en")]
    stops_after_one = assistant.next_utterance

    def next_utterance(timeout):
        audio = stops_after_one(timeout)
        if audio is None:
            assistant.submit("dictate")          # the second press
            return stops_after_one(timeout)
        return audio

    assistant.next_utterance = next_utterance
    assistant._run_jobs()
    assert assistant.keyboard.typed == ["One"] and assistant.dictating is False
    assert states(assistant)[-1] == "idle"


def test_when_typing_fails_dictation_stops_and_says_why(assistant):
    assistant.keyboard = FakeKeyboard(fail=True)
    assistant.utterances = [("hello", "en"), ("more", "en")]
    assistant.respond("dictate", "en")
    assert assistant.dictating is False and assistant.utterances == [("more", "en")]
    assert any("xdotool" in e.get("error", "") for e in assistant.events.history if e["kind"] == "error")
