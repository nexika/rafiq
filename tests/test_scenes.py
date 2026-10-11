from types import SimpleNamespace

import pytest

from rafiq import config
from rafiq.assistant import Assistant, Context
from rafiq.tools import Tool, load_all
from rafiq.tools import scenes as scenes_mod

FILM = {"names": ["film mode", "mode film", "modo película", "Filmmodus", "وضع الفيلم"],
        "steps": [{"tool": "volume", "args": {"level": 40}},
                  {"tool": "lamp", "args": {"brightness": 10}, "hours": [18, 6]}]}


def fake_tools(log):
    def volume(level=None):
        log.append(("volume", level))
        return f"volume {level}%"

    def lamp(brightness=0):
        log.append(("lamp", brightness))
        return f"lamp {brightness}%"

    def shutdown(action=""):
        log.append(("power", action))
        return "done"

    def brief():
        return "Today: sunny, two meetings."

    return {"volume": Tool("volume", "", volume), "lamp": Tool("lamp", "", lamp),
            "power": Tool("power", "", shutdown, confirm="shut down the computer"),
            "daily_brief": Tool("daily_brief", "", brief)}


def make_ctx(scenes, log, confirm=lambda action, tag: True, tag="en"):
    cfg = config.load()
    cfg["scenes"] = scenes
    tools = fake_tools(log)
    return Context(cfg, scheduler=None, tag=tag, confirm=confirm, tools=tools)


def test_scenes_come_from_the_config_and_all_off_is_built_in():
    found = scenes_mod.scenes_from({**config.load(), "scenes": {"film": FILM}})
    assert set(found) == {"film"}
    assert set(scenes_mod.scenes_from(config.load())) == {"all_off"}
    film = found["film"]
    assert film.steps[0].tool == "volume" and film.steps[0].args == {"level": 40}
    assert film.steps[1].hours == (18, 6)


def test_a_step_without_a_tool_is_refused():
    with pytest.raises(ValueError, match="film"):
        scenes_mod.scenes_from({"scenes": {"film": {"names": ["film"], "steps": [{"args": {}}]}}})


@pytest.mark.parametrize("said", ["film mode", "Film mode.", "mode film !", "Modo película", "filmmodus",
                                  "وضع الفيلم", "film-mode"])
def test_a_scene_is_triggered_by_any_of_its_names(said):
    found = scenes_mod.scenes_from({"scenes": {"film": FILM}})
    assert scenes_mod.match(said, found).id == "film"


@pytest.mark.parametrize("said", ["turn everything off", "Éteins tout", "apaga todo", "alles aus",
                                  "أطفئ كل شيء", "اقفل كل حاجة", "طفي كل حاجه"])
def test_turn_everything_off_is_understood_in_six_languages(said):
    assert scenes_mod.match(said, scenes_mod.scenes_from(config.load())).id == "all_off"


def test_a_longer_request_is_left_to_the_brain():
    found = scenes_mod.scenes_from({"scenes": {"film": FILM}})
    assert scenes_mod.match("what is film mode about", found) is None
    assert scenes_mod.match("", found) is None


def test_steps_run_in_order_and_time_of_day_steps_only_in_their_hours():
    log = []
    ctx = make_ctx({"film": FILM}, log)
    scene = scenes_mod.scenes_from(ctx.cfg)["film"]
    scenes_mod.play(scene, ctx, hour=21)
    assert log == [("volume", 40), ("lamp", 10)]
    log.clear()
    outcomes = scenes_mod.play(scene, ctx, hour=14)
    assert log == [("volume", 40)]
    assert outcomes[1].result.startswith("skipped")


@pytest.mark.parametrize("hours, hour, runs", [((6, 12), 6, True), ((6, 12), 12, False), ((18, 6), 23, True),
                                               ((18, 6), 5, True), ((18, 6), 6, False), ((0, 24), 13, True)])
def test_hours_wrap_past_midnight(hours, hour, runs):
    assert scenes_mod.in_hours(hours, hour) is runs


def test_risky_steps_keep_their_confirmation():
    log, asked = [], []
    ctx = make_ctx(config.load()["scenes"], log, confirm=lambda action, tag: asked.append((action, tag)) or False,
                   tag="ar-EG")
    scene = scenes_mod.scenes_from(ctx.cfg)["all_off"]
    outcomes = scenes_mod.play(scene, ctx, hour=22)
    assert asked == [("shut down the computer", "ar-EG")]
    assert ("power", "shutdown") not in log
    assert outcomes[-1].result == "declined"


def test_risky_steps_do_not_run_when_no_one_can_be_asked():
    log = []
    ctx = make_ctx(config.load()["scenes"], log)
    ctx.confirm = None
    scenes_mod.play(scenes_mod.scenes_from(ctx.cfg)["all_off"], ctx, hour=22)
    assert log == []


def test_a_missing_tool_or_a_failing_step_does_not_stop_the_scene():
    log = []
    ctx = make_ctx({"x": {"names": ["x"], "steps": [{"tool": "spotify_pause"}, {"tool": "boom"},
                                                    {"tool": "volume", "args": {"level": 5}}]}}, log)
    ctx.tools["boom"] = Tool("boom", "", lambda: 1 / 0)
    outcomes = scenes_mod.play(scenes_mod.scenes_from(ctx.cfg)["x"], ctx, hour=10)
    assert outcomes[0].result.startswith("skipped") and "spotify_pause" in outcomes[0].result
    assert outcomes[1].result.startswith("error: ZeroDivisionError")
    assert log == [("volume", 5)]


def test_a_scene_cannot_start_another_scene():
    log = []
    ctx = make_ctx({"x": {"names": ["x"], "steps": [{"tool": "run_scene", "args": {"name": "x"}}]}}, log)
    ctx.tools["run_scene"] = load_all({})["run_scene"]
    outcomes = scenes_mod.play(scenes_mod.scenes_from(ctx.cfg)["x"], ctx, hour=10)
    assert outcomes[0].result.startswith("skipped")


def test_the_run_scene_tool_finds_a_scene_by_name_or_says_which_exist():
    log = []
    ctx = make_ctx({"film": FILM}, log)
    tools = load_all({"scenes": {"film": FILM}})
    assert "film mode" in tools["run_scene"].description
    assert "volume: volume 40%" in tools["run_scene"].run({"name": "Film mode"}, ctx)
    assert log[0] == ("volume", 40)
    missing = tools["run_scene"].run({"name": "party"}, ctx)
    assert missing.startswith("error") and "film mode" in missing


def test_the_first_start_of_each_day_is_noticed_once(tmp_path):
    from datetime import date

    path = tmp_path / "day_start.json"
    assert scenes_mod.first_start_today(path, date(2026, 10, 10)) is True
    assert scenes_mod.first_start_today(path, date(2026, 10, 10)) is False
    assert scenes_mod.first_start_today(path, date(2026, 10, 11)) is True


# ---------------------------------------------------------------------- in the assistant

class FakeBrain:
    def __init__(self):
        self.asked = []

    def think(self, text, tag, ctx, on_speech=None):
        self.asked.append(text)
        return "brain answer"


def bare_assistant(scenes, log, confirm=lambda action, tag: True):
    """An Assistant without a microphone, speaker or models: only what respond() and the jobs use."""
    a = Assistant.__new__(Assistant)
    a.cfg = config.load()
    a.cfg["scenes"] = scenes
    a.ctx = Context(a.cfg, scheduler=None, confirm=confirm, tools=fake_tools(log))
    a.brain, a.said, a.tag, a.follow_up_until, a.silent = FakeBrain(), [], "en", 0.0, False
    a.say = lambda text, tag: a.said.append((text, tag))
    a.set_state = lambda state: None
    a.speaker = SimpleNamespace(stream=lambda: SimpleNamespace(put=lambda text, tag: None, close=lambda: True))
    a.events = type("E", (), {"publish": lambda self, kind, data: None})()
    return a


def test_saying_a_scene_name_runs_it_without_the_brain():
    log = []
    a = bare_assistant({"film": FILM}, log)
    a.respond("Mode film", "fr")
    assert a.brain.asked == [] and log[0] == ("volume", 40)
    assert a.said == [("C'est fait.", "fr")]


def test_other_requests_still_go_to_the_brain():
    a = bare_assistant({"film": FILM}, [])
    a.respond("what time is it", "en")
    assert a.brain.asked == ["what time is it"]


def test_a_step_marked_speak_is_read_aloud():
    morning = {"names": ["good morning"], "steps": [{"tool": "daily_brief", "speak": True}]}
    a = bare_assistant({"morning": morning}, [])
    a.play_scene(scenes_mod.scenes_from(a.cfg)["morning"], "en")
    assert a.said == [("Today: sunny, two meetings.", "en")]


def test_the_day_start_scene_is_queued_on_the_first_start_only():
    a = bare_assistant({"morning": {"names": ["good morning"], "steps": []}}, [])
    a.cfg["day_start"] = {"scene": "morning"}
    jobs = []
    a.submit = lambda kind, **data: jobs.append((kind, data))
    a.start_the_day()
    a.start_the_day()
    assert jobs == [("scene", {"name": "morning"})]
