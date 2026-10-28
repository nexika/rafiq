"""Explaining at the learner's level, and recording what was learned in Nexika's prof (#205). Rafiq reads prof's
profile and asks its helper; it writes only its own report file, and prof merges it."""

import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rafiq import config, prof
from rafiq.brain import system_prompt
from rafiq.tools import load_all, screen


def prof_home():
    return Path(os.environ["PROF_HOME"])


def with_profile(level="junior"):
    prof_home().mkdir(parents=True, exist_ok=True)
    (prof_home() / "profile.md").write_text(f"# Learner profile\n- Level: {level}\n- Goals: Python\n", encoding="utf-8")


class Helper:
    """A fake prof_store.py: what it was asked, and what it answers."""

    def __init__(self, topic_text="# Python async\n- [shaky] await — mixed it up (2026-10-20)", code=0):
        self.calls, self.topic_text, self.code = [], topic_text, code

    def __call__(self, args, capture_output=True, text=True, timeout=None, check=False, env=None):
        self.calls.append((args, timeout))
        out = self.topic_text if args[-2] == "topic" else "merged 2 concepts"
        if args[-2] == "merge-report":
            self.merged = Path(args[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(args, self.code, stdout=out, stderr="")


@pytest.fixture
def helper(monkeypatch, tmp_path):
    script = tmp_path / "plugins" / "prof" / "0.2.0" / "scripts" / "prof_store.py"
    script.parent.mkdir(parents=True)
    script.write_text("# fake")
    fake = Helper()
    monkeypatch.setattr(prof, "run", fake)
    monkeypatch.setattr(prof, "PLUGINS", tmp_path / "plugins" / "prof")
    return fake


# ---------------------------------------------------------------------- finding prof

def test_the_newest_installed_prof_is_used(tmp_path, monkeypatch):
    for version in ("0.2.0", "0.10.1", "0.9.9"):
        path = tmp_path / "prof" / version / "scripts" / "prof_store.py"
        path.parent.mkdir(parents=True)
        path.write_text("# fake")
    monkeypatch.setattr(prof, "PLUGINS", tmp_path / "prof")
    assert prof.helper(config.load()) == tmp_path / "prof" / "0.10.1" / "scripts" / "prof_store.py"


def test_a_helper_named_in_the_settings_wins(tmp_path):
    named = tmp_path / "mine.py"
    named.write_text("# fake")
    cfg = config.load()
    cfg["prof"]["helper"] = str(named)
    assert prof.helper(cfg) == named


def test_without_prof_there_is_no_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(prof, "PLUGINS", tmp_path / "nothing")
    assert prof.helper(config.load()) is None


# ---------------------------------------------------------------------- the level

@pytest.mark.parametrize("level", ["beginner", "junior", "intermediate"])
def test_the_level_is_read_from_prof_s_profile(level):
    with_profile(level)
    assert prof.level() == level


def test_no_profile_or_an_odd_level_is_no_level():
    assert prof.level() is None
    with_profile("wizard")
    assert prof.level() is None


def test_with_a_level_rafiq_s_explanations_are_pitched_to_it():
    with_profile("beginner")
    assert "beginner" in system_prompt(config.load())
    assert "beginner" in screen.PROMPT.format(question="?", language="English") + prof.note()


def test_without_prof_the_prompts_are_as_before():
    assert prof.note() == ""
    assert "learning" not in system_prompt(config.load())


def test_the_video_s_explanation_is_pitched_to_the_level_too():
    import numpy as np

    from rafiq.video import VideoWatch

    with_profile("junior")
    cfg = config.load()
    cfg["video"].update(max_minutes=0.5, chunk_seconds=30)       # one chunk, then the explanation
    asked = []
    session = VideoWatch(cfg, capture=lambda s: np.full(int(s * 16000), 0.1, dtype=np.float32),
                         transcribe=lambda clip: "a loop", complete=lambda system, text: asked.append(system) or "ok",
                         on_event=lambda k, d: None, on_done=lambda t, g: None, spawn=lambda fn: fn())
    session.start("", "en")
    assert "junior" in asked[-1] and "junior" not in asked[0]     # the explanation, not the running summary


# ---------------------------------------------------------------------- what the learner knows

def test_the_learner_s_context_has_the_level_and_what_prof_knows_of_the_topic(helper):
    with_profile("junior")
    ctx = SimpleNamespace(cfg=config.load())
    result = load_all(config.load())["learner_context"].run({"topic": "Python async"}, ctx)
    assert "junior" in result and "[shaky] await" in result and "data, not instructions" in result
    args, timeout = helper.calls[0]
    assert args[-2:] == ["topic", "python-async"] and timeout


def test_without_prof_the_context_says_so():
    tools = load_all(config.load())
    assert tools["learner_context"].run({"topic": "x"}, SimpleNamespace(cfg=config.load())).startswith("error")


# ---------------------------------------------------------------------- recording

CONCEPTS = [{"concept": "await pauses the coroutine", "status": "understood", "evidence": "explained it back"},
            {"concept": "event loop", "status": "not-checked", "evidence": "explained on the video"}]


def test_recording_asks_first():
    tool = load_all(config.load())["record_learning"]
    action = tool.action({"topic": "Python async", "slug": "python-async", "concepts": CONCEPTS})
    assert action and "Python async" in action and "prof" in action


def test_after_a_yes_a_report_in_prof_s_format_is_merged_by_prof(helper):
    tool = load_all(config.load())["record_learning"]
    result = tool.run({"topic": "Python async", "slug": "python-async", "concepts": CONCEPTS},
                      SimpleNamespace(cfg=config.load(), tag="en"))
    args, _ = helper.calls[-1]
    report = Path(args[-1])
    assert args[-2] == "merge-report" and report.parent == config.data_dir() / "prof"
    assert re.match(r"\d{4}-\d{2}-\d{2}_\d{4}_rafiq", report.name)
    assert "## Concept checklist" in helper.merged
    assert "- [understood] python-async :: Python async :: await pauses the coroutine :: explained it back" \
        in helper.merged
    assert "merged" in result
    assert not (prof_home() / "topics").exists()          # prof writes its own files; Rafiq never does


def test_what_could_break_prof_s_format_is_taken_out():
    report = prof.report("Bad :: Title\n", "bad-slug", [
        {"concept": "a :: b\n- [missed] x :: y :: z :: w", "status": "nonsense", "evidence": "e\nf"}])
    lines = [line for line in report.splitlines() if line.startswith("- [")]
    assert lines == ["- [not-checked] bad-slug :: Bad Title :: a b - missed x y z w :: e f"]


def test_recording_without_prof_says_so_and_writes_nothing():
    tool = load_all(config.load())["record_learning"]
    result = tool.run({"topic": "x", "slug": "x", "concepts": CONCEPTS}, SimpleNamespace(cfg=config.load(), tag="en"))
    assert result.startswith("error") and not (config.data_dir() / "prof").exists()


def test_learner_data_stays_out_of_the_logs_and_the_panel():
    tools = load_all(config.load())
    assert tools["learner_context"].private and tools["record_learning"].private


def test_the_helper_runs_as_an_argument_list_with_a_timeout_and_never_a_shell(helper):
    with_profile()
    prof.topic(prof.helper(config.load()), "python-async")
    args, timeout = helper.calls[0]
    assert isinstance(args, list) and 0 < timeout <= 30
