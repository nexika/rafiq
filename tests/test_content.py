from datetime import date

import pytest

from rafiq.tools import REGISTRY, content, load_all

TODAY = date(2026, 10, 10)


@pytest.fixture
def pipeline(tmp_path):
    return content.Pipeline(tmp_path / "content.json")


def test_a_new_video_starts_as_an_idea_and_is_saved(pipeline, tmp_path):
    item = pipeline.add("Morning routine vlog", due="2026-10-20", today=TODAY)
    assert item.stage == "idea" and item.due == "2026-10-20"
    again = content.Pipeline(tmp_path / "content.json")
    assert [i.title for i in again.items()] == ["Morning routine vlog"]


def test_advance_moves_one_stage_at_a_time_through_to_published(pipeline):
    item = pipeline.add("Desk tour", today=TODAY)
    stages = [pipeline.advance(item.id, today=TODAY).stage for _ in range(4)]
    assert stages == ["script", "shoot", "edit", "published"]
    with pytest.raises(ValueError):
        pipeline.advance(item.id, today=TODAY)


def test_a_video_can_jump_to_a_named_stage_and_is_found_by_words_in_its_title(pipeline):
    pipeline.add("Desk tour", today=TODAY)
    assert pipeline.advance("desk", stage="edit", today=TODAY).stage == "edit"
    with pytest.raises(ValueError):
        pipeline.advance("desk", stage="filming", today=TODAY)


def test_an_ambiguous_or_unknown_name_is_refused(pipeline):
    pipeline.add("Desk tour", today=TODAY)
    pipeline.add("Desk setup on a budget", today=TODAY)
    with pytest.raises(LookupError):
        pipeline.find("desk")
    with pytest.raises(LookupError):
        pipeline.find("cooking")


def test_late_items_are_past_their_date_and_not_published(pipeline):
    pipeline.add("Late one", due="2026-10-01", today=TODAY)
    pipeline.add("On time", due="2026-10-30", today=TODAY)
    done = pipeline.add("Done long ago", due="2026-09-01", today=TODAY)
    pipeline.advance(done.id, stage="published", today=TODAY)
    pipeline.add("No date", today=TODAY)
    assert [i.title for i in pipeline.late(TODAY)] == ["Late one"]


def test_a_bad_due_date_or_stage_is_refused(pipeline):
    with pytest.raises(ValueError):
        pipeline.add("Vlog", due="next tuesday", today=TODAY)
    with pytest.raises(ValueError):
        pipeline.add("Vlog", stage="uploaded", today=TODAY)
    with pytest.raises(ValueError):
        pipeline.add("   ", today=TODAY)


def test_remove_deletes_a_video(pipeline):
    item = pipeline.add("Desk tour", today=TODAY)
    assert pipeline.remove(item.id).title == "Desk tour"
    assert pipeline.items() == []


def test_a_damaged_file_starts_an_empty_pipeline(tmp_path):
    path = tmp_path / "content.json"
    path.write_text("{not json")
    assert content.Pipeline(path).items() == []


@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr(content, "today", lambda: TODAY)


def test_the_tools_are_registered_and_change_nothing_outside_rafiq():
    tools = load_all({})
    for name in ("content_add", "content_advance", "content_list", "content_remove"):
        assert name in tools and REGISTRY[name].action({}) is None, name


def test_tools_track_a_video_and_list_the_late_ones_first(today):
    assert content.content_add("Desk tour", due="2026-10-05").startswith("added")
    content.content_add("Morning vlog")
    assert "now at script" in content.content_advance("desk")
    text = content.content_list()
    assert text.splitlines()[0].startswith("late: ")
    assert "Desk tour" in text.splitlines()[0] and "Morning vlog" in text
    assert "Desk tour" not in content.content_list(stage="idea")


def test_tools_report_errors_in_words_the_model_can_pass_on(today):
    assert content.content_list() == "no videos in the pipeline"
    assert content.content_advance("nothing").startswith("error")
    assert content.content_add("Vlog", due="soon").startswith("error")
    assert content.content_list(stage="uploaded").startswith("error")
    content.content_add("Desk tour")
    assert content.content_remove("desk").startswith("removed")
