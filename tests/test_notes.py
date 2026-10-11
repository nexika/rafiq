import random
from datetime import datetime

import pytest

from rafiq import config
from rafiq.tools import load_all, notes


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "now", lambda: datetime(2026, 10, 10, 14, 32).astimezone())
    cfg = config.load()
    cfg["notes"]["folder"] = str(tmp_path / "vault")
    return type("Ctx", (), {"cfg": cfg, "tag": "en"})()


def test_notes_go_in_the_data_folder_by_default():
    assert notes.folder(config.load()) == config.data_dir() / "notes"


def test_a_note_is_a_dated_line_in_the_file_for_its_kind(ctx, tmp_path):
    assert notes.note_add("buy batteries", ctx=ctx).startswith("noted")
    notes.note_add("a lamp that follows the sun", "idea", ctx=ctx)
    assert (tmp_path / "vault" / "notes.md").read_text(encoding="utf-8") == \
        "# Notes\n\n- 2026-10-10 14:32 buy batteries\n"
    assert "- 2026-10-10 14:32 a lamp that follows the sun" in (tmp_path / "vault" / "ideas.md").read_text()


def test_notes_keep_any_language_and_stay_on_one_line(ctx, tmp_path):
    notes.note_add("اشتري  بطاريات\nبكرة", ctx=ctx)
    assert "- 2026-10-10 14:32 اشتري بطاريات بكرة\n" in (tmp_path / "vault" / "notes.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("text, kind", [("", "note"), ("   ", "idea"), ("x", "../../etc/passwd")])
def test_empty_notes_and_unknown_kinds_are_errors(ctx, text, kind):
    assert notes.note_add(text, kind, ctx=ctx).startswith("error")


def test_a_random_idea_comes_from_the_ideas_file(ctx, monkeypatch):
    for idea in ("solar lamp", "voice garden", "tea robot"):
        notes.note_add(idea, "idea", ctx=ctx)
    notes.note_add("buy batteries", ctx=ctx)
    picked = {notes.note_random(ctx=ctx, rng=random.Random(seed)) for seed in range(30)}
    assert picked == {"solar lamp (2026-10-10)", "voice garden (2026-10-10)", "tea robot (2026-10-10)"}


def test_a_random_idea_also_reads_bullets_written_by_hand(ctx, tmp_path):
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "ideas.md").write_text("# Ideas\n\nsome prose\n- a hand-written idea\n")
    assert notes.note_random(ctx=ctx) == "a hand-written idea"


def test_no_ideas_yet(ctx):
    assert notes.note_random(ctx=ctx) == "no idea notes yet"


def test_notes_of_a_day_across_kinds(ctx, tmp_path):
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "notes.md").write_text("# Notes\n\n- 2026-10-09 09:00 yesterday's note\n")
    notes.note_add("buy batteries", ctx=ctx)
    notes.note_add("tea robot", "idea", ctx=ctx)
    today = notes.notes_on(ctx=ctx)
    assert "14:32 note: buy batteries" in today and "14:32 idea: tea robot" in today
    assert "yesterday" not in today
    assert notes.notes_on("2026-10-09", ctx=ctx) == "09:00 note: yesterday's note"
    assert notes.notes_on("2026-10-01", ctx=ctx) == "no notes on 2026-10-01"
    assert notes.notes_on("tuesday", ctx=ctx).startswith("error")


def test_note_tools_are_registered_and_run_without_asking():
    tools = load_all({})
    for name in ("note_add", "note_random", "notes_on"):
        assert tools[name].action({"text": "x"}) is None
