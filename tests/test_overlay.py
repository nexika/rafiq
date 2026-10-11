import subprocess
from pathlib import Path

import pytest

from rafiq import config, lang, overlay
from rafiq.assistant import Assistant, Events
from rafiq.overlay import Card, NotifyBackend, Overlay, card_for
from rafiq.practice import Practice


class FakeBackend:
    def __init__(self):
        self.shown = []

    def show(self, text, cards):
        self.shown.append((text, list(cards)))


def make_overlay():
    backend = FakeBackend()
    return Overlay(backend, spawn=lambda fn, *args: fn(*args)), backend


# ---------------------------------------------------------------------- cards

def test_the_weather_tool_makes_a_weather_card_from_its_first_line():
    result = "Cairo, Egypt: now 31°C (feels 33°C), clear sky\n2026-10-10: clear sky, 22 to 33°C"
    assert card_for("weather", result) == Card("weather", "Cairo, Egypt: now 31°C (feels 33°C), clear sky")


@pytest.mark.parametrize("name", ["set_timer", "set_reminder"])
def test_timers_and_reminders_make_a_timer_card(name):
    assert card_for(name, "set: tea, in 5 minutes (id 3)") == Card("timer", "tea, in 5 minutes (id 3)")


def test_a_music_tool_makes_a_music_card():
    assert card_for("play_music", "Playing Fairuz, Kifak Inta") == Card("music", "Playing Fairuz, Kifak Inta")


@pytest.mark.parametrize("name, result", [("weather", "error: no place called 'Xyz'"), ("calculate", "42"),
                                          ("set_timer", "declined")])
def test_errors_and_other_tools_make_no_card(name, result):
    assert card_for(name, result) is None


# ---------------------------------------------------------------------- the overlay

def test_an_answer_is_shown_with_the_cards_of_its_turn():
    shown, backend = make_overlay()
    shown.feed("tool", {"name": "weather", "args": {}, "result": "Paris, France: now 12°C, rain"})
    shown.feed("said", {"text": "It's twelve degrees and raining.", "tag": "en"})
    assert backend.shown == [("It's twelve degrees and raining.", [Card("weather", "Paris, France: now 12°C, rain")])]


def test_cards_do_not_carry_over_to_the_next_answer():
    shown, backend = make_overlay()
    shown.feed("tool", {"name": "set_timer", "args": {}, "result": "set: tea, in 5 minutes"})
    shown.feed("said", {"text": "Done.", "tag": "en"})
    shown.feed("said", {"text": "Anything else?", "tag": "en"})
    assert backend.shown[1] == ("Anything else?", [])


def test_a_said_event_can_bring_its_own_card():
    shown, backend = make_overlay()
    shown.feed("said", {"text": "Your timer is done: tea", "tag": "en", "cards": [{"kind": "timer", "text": "tea"}]})
    assert backend.shown == [("Your timer is done: tea", [Card("timer", "tea")])]


def test_show_again_brings_back_the_last_answer():
    shown, backend = make_overlay()
    shown.feed("tool", {"name": "weather", "args": {}, "result": "Rome, Italy: now 20°C"})
    shown.feed("said", {"text": "Twenty degrees.", "tag": "en"})
    assert shown.show_again() is True
    assert backend.shown[1] == backend.shown[0]


def test_show_again_with_nothing_said_yet_shows_nothing():
    shown, backend = make_overlay()
    assert shown.show_again() is False and backend.shown == []


def test_events_reach_watchers():
    events, seen = Events(), []
    events.watchers.append(lambda kind, data: seen.append((kind, data["text"])))
    events.publish("said", {"text": "hello"})
    assert seen == [("said", "hello")]


def test_a_failing_watcher_does_not_break_publishing():
    events = Events()
    events.watchers.append(lambda kind, data: 1 / 0)
    events.publish("said", {"text": "hello"})
    assert events.history[-1]["text"] == "hello"


# ---------------------------------------------------------------------- desktop notifications

class FakeRun:
    def __init__(self, outputs=None, error=None):
        self.calls, self.outputs, self.error = [], list(outputs or []), error

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(argv, 0, stdout=self.outputs.pop(0) if self.outputs else "(uint32 7,)\n")


def test_a_notification_replaces_the_previous_one_instead_of_stacking():
    run = FakeRun(["(uint32 7,)\n", "(uint32 7,)\n"])
    backend = NotifyBackend(seconds=8, run=run)
    backend.show("one", [])
    backend.show("two", [])
    first, second = run.calls[0][0], run.calls[1][0]
    assert first[0] == "gdbus" and "org.freedesktop.Notifications.Notify" in first
    assert first[first.index("org.freedesktop.Notifications.Notify") + 2] == "0"
    assert second[second.index("org.freedesktop.Notifications.Notify") + 2] == "7"
    assert first[-1] == "8000" and all(call[1].get("timeout") for call in run.calls)


def test_a_notification_quotes_the_text_and_escapes_markup():
    run = FakeRun()
    NotifyBackend(run=run).show("It's <b>5</b> \\ 6", [Card("weather", "Cairo: 31°C")])
    body = run.calls[0][0][run.calls[0][0].index("org.freedesktop.Notifications.Notify") + 5]
    assert body.startswith("'") and body.endswith("'")
    assert "It\\'s &lt;b&gt;5&lt;/b&gt; \\\\ 6" in body and "Cairo: 31°C" in body


def test_a_notification_is_transient_and_never_urgent():
    run = FakeRun()
    NotifyBackend(run=run).show("hi", [])
    hints = run.calls[0][0][-2]
    assert "'transient': <true>" in hints and "'urgency': <byte 0>" in hints


@pytest.mark.parametrize("error", [OSError("no gdbus"), subprocess.TimeoutExpired("gdbus", 3),
                                   subprocess.CalledProcessError(1, "gdbus")])
def test_a_failed_notification_is_not_an_error(error):
    NotifyBackend(run=FakeRun(error=error)).show("hi", [])


def test_the_display_is_off_when_asked():
    cfg = config.load()
    cfg["overlay"]["display"] = "off"
    assert overlay.pick_backend(cfg, which=lambda name: "/usr/bin/" + name) is None


def test_auto_uses_notifications_when_gdbus_is_there():
    assert isinstance(overlay.pick_backend(config.load(), which=lambda name: "/usr/bin/" + name), NotifyBackend)


def test_auto_without_gdbus_shows_nothing():
    assert overlay.pick_backend(config.load(), which=lambda name: None) is None


# ---------------------------------------------------------------------- "show it again" and silent mode

@pytest.mark.parametrize("text", ["show it again", "Show that again, please.", "montre-le encore",
                                  "muéstralo otra vez", "zeig das nochmal", "اعرضها مرة أخرى", "وريني تاني"])
def test_show_again_is_understood(text):
    assert lang.is_show_again(text)


@pytest.mark.parametrize("text", ["show me the weather", "play it again", "show it again and tell me the time "
                                  "in Tokyo and in Paris"])
def test_other_requests_are_not_show_again(text):
    assert not lang.is_show_again(text)


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def say(self, text, tag):
        self.said.append(text)


def bare_assistant(silent):
    assistant = Assistant.__new__(Assistant)
    assistant.practice = Practice(Path("practice.json"))   # no session running
    assistant.events, assistant.speaker, assistant.silent = Events(), FakeSpeaker(), silent
    return assistant


def test_silent_mode_answers_on_screen_only():
    assistant = bare_assistant(silent=True)
    assistant.say("Twenty degrees.", "en")
    assert assistant.speaker.said == [] and assistant.events.history[-1]["text"] == "Twenty degrees."


def test_without_silent_mode_the_answer_is_spoken():
    assistant = bare_assistant(silent=False)
    assistant.say("Twenty degrees.", "en")
    assert assistant.speaker.said == ["Twenty degrees."]


def test_show_it_again_does_not_go_to_the_brain():
    assistant = bare_assistant(silent=False)
    shown, backend = make_overlay()
    assistant.overlay = shown
    assistant.brain = None                     # would fail if asked
    assistant.events.publish("said", {"text": "Twenty degrees.", "tag": "en"})
    shown.feed("said", {"text": "Twenty degrees.", "tag": "en"})
    assistant.respond("show it again", "en")
    assert backend.shown[-1] == ("Twenty degrees.", []) and len(backend.shown) == 2


def test_show_it_again_with_nothing_to_show_says_so():
    assistant = bare_assistant(silent=False)
    assistant.overlay, assistant.brain = make_overlay()[0], None
    assistant.respond("show it again", "en")
    assert assistant.speaker.said == [lang.phrase("nothing_to_show", "en")]
