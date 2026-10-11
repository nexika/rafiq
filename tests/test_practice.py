import copy
import json

import pytest

from rafiq import brain as brain_mod
from rafiq import config
from rafiq.assistant import Context
from rafiq.practice import Practice
from rafiq.tools import load_all
from rafiq.tts import Speaker


def make_practice(tmp_path, events=None):
    log = events if events is not None else []
    return Practice(tmp_path / "practice.json", on_event=lambda kind, data: log.append((kind, data)))


def ctx_with(practice, tag="en"):
    return Context(config.load(), scheduler=None, tag=tag, practice=practice)


def run(name, args, ctx):
    return load_all({})[name].run(args, ctx)


def test_practice_starts_with_a_language_level_and_topic(tmp_path):
    practice = make_practice(tmp_path)
    assert not practice.active
    practice.start("fr", "a2", "food", home="en")
    assert practice.active and (practice.tag, practice.level, practice.topic) == ("fr", "A2", "food")


@pytest.mark.parametrize("tag, level", [("klingon", "A1"), ("fr", "Z9")])
def test_an_unknown_language_or_level_is_refused(tmp_path, tag, level):
    with pytest.raises(ValueError):
        make_practice(tmp_path).start(tag, level, "")


def test_the_request_note_tells_the_model_how_to_practise(tmp_path):
    practice = make_practice(tmp_path)
    assert practice.note() == ""
    practice.start("de", "B1", "travel", home="en")
    note = practice.note()
    assert "German" in note and "B1" in note and "travel" in note and "practice_correct" in note


def test_the_note_goes_into_the_request_but_not_the_system_prompt(tmp_path, monkeypatch):
    """The system prompt stays the same, so a local model keeps its cached reading of it."""
    requests = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"role": "assistant", "content": "Salut !"}}

    import httpx

    def post(url, json=None, timeout=None):
        requests.append(copy.deepcopy(json))
        return Resp()

    monkeypatch.setattr(httpx, "post", post)
    cfg = config.load()
    cfg["brain"]["mode"] = "ollama"
    practice = make_practice(tmp_path)
    practice.start("fr", "A1", "", home="en")
    brain_mod.Brain(cfg, {}, lambda a, t: True).think("bonjour", "fr", ctx_with(practice, "fr"))
    assert "practice_correct" in requests[0]["messages"][-1]["content"]
    assert requests[0]["messages"][0]["content"] == brain_mod.system_prompt(cfg)


def test_beginners_hear_a_slower_voice(tmp_path):
    practice = make_practice(tmp_path)
    assert practice.rate() is None
    practice.start("es", "A1", "", home="en")
    slow_a1 = practice.rate()
    practice.start("es", "A2", "", home="en")
    assert int(slow_a1.strip("%")) < int(practice.rate().strip("%")) < 0     # A1 slower than A2
    practice.start("es", "B2", "", home="en")
    assert practice.rate() is None


def test_corrections_reach_the_panel_and_the_end_lists_them(tmp_path):
    events = []
    practice = make_practice(tmp_path, events)
    practice.start("fr", "A2", "food", home="en")
    practice.correct("je suis faim", "j'ai faim", "hunger uses avoir")
    assert ("correction", {"said": "je suis faim", "correct": "j'ai faim", "note": "hunger uses avoir",
                           "tag": "fr"}) in events
    summary = practice.end()
    assert not practice.active
    assert "j'ai faim" in summary and "hunger uses avoir" in summary
    assert events[-1][0] == "practice" and events[-1][1]["state"] == "end"


def test_common_mistakes_are_kept_locally_across_sessions(tmp_path):
    for _ in range(2):
        practice = make_practice(tmp_path)
        practice.start("fr", "A2", "", home="en")
        practice.correct("je suis faim", "j'ai faim")
        practice.correct("la problème", "le problème")
        practice.correct("je suis faim", "j'ai faim")
        practice.end()
    common = make_practice(tmp_path).common("fr")
    assert common[0]["correct"] == "j'ai faim" and common[0]["count"] == 4
    assert common[1]["count"] == 2
    assert make_practice(tmp_path).common("de") == []
    assert "j'ai faim" in make_practice(tmp_path).history_summary("fr")


def test_the_history_stays_short(tmp_path):
    practice = make_practice(tmp_path)
    practice.start("fr", "B1", "", home="en")
    for i in range(80):
        practice.correct(f"faute {i}", f"correct {i}")
    practice.end()
    data = json.loads((tmp_path / "practice.json").read_text())
    assert len(data["fr"]) <= 30


def test_a_damaged_history_file_is_started_over(tmp_path):
    (tmp_path / "practice.json").write_text("{not json")
    practice = make_practice(tmp_path)
    assert practice.common("fr") == []
    practice.start("fr", "A1", "", home="en")
    practice.correct("a", "b")
    practice.end()
    assert make_practice(tmp_path).common("fr")[0]["correct"] == "b"


# ---------------------------------------------------------------------- tools

def test_the_tools_start_correct_and_end_a_session(tmp_path):
    practice = make_practice(tmp_path)
    ctx = ctx_with(practice)
    started = run("practice_start", {"language": "French", "level": "A1", "topic": "family"}, ctx)
    assert practice.active and practice.tag == "fr" and practice.home == "en"
    assert "French" in started
    assert run("practice_correct", {"said": "il a 5 ans", "correct": "il a cinq ans"}, ctx).startswith("noted")
    ended = run("practice_end", {}, ctx)
    assert "il a cinq ans" in ended and not practice.active


def test_the_tools_report_bad_input_to_the_model(tmp_path):
    ctx = ctx_with(make_practice(tmp_path))
    assert run("practice_start", {"language": "fr", "level": "D7"}, ctx).startswith("error")
    assert run("practice_correct", {"said": "a", "correct": "b"}, ctx).startswith("error")
    assert run("practice_end", {}, ctx).startswith("error")
    assert run("practice_start", {"language": "fr"}, Context(config.load(), None)).startswith("error")


def test_practice_tools_run_without_asking():
    tools = load_all({})
    for name in ("practice_start", "practice_correct", "practice_end"):
        assert tools[name].action({}) is None


# ---------------------------------------------------------------------- voice

def test_the_speaker_uses_the_practice_rate_over_the_setting(monkeypatch):
    sent = []

    class Resp:
        content = b"mp3"

        def raise_for_status(self):
            pass

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, headers=None, content=None, timeout=None: sent.append(content)
                        or Resp())
    cfg = config.load()
    speaker = Speaker(cfg)
    speaker.key, speaker.region = "k", "westeurope"
    speaker.rate = "-25%"
    path, errors = speaker.synthesize("Bonjour", "fr", config.data_dir())
    assert path and not errors and b'rate="-25%"' in sent[0]
