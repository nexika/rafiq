from datetime import UTC, datetime, timedelta

from rafiq import config
from rafiq.tools import agenda, load_all

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

FEED = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:1
SUMMARY:Video for Acme due
DTSTART;VALUE=DATE:20261014
DTEND;VALUE=DATE:20261015
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:Summer trip
DTSTART;VALUE=DATE:20261012
DTEND;VALUE=DATE:20261019
END:VEVENT
BEGIN:VEVENT
UID:3
SUMMARY:Script draft to Brand X
DTSTART:20261011T150000Z
END:VEVENT
BEGIN:VEVENT
UID:4
SUMMARY:Old deadline
DTSTART;VALUE=DATE:20261001
END:VEVENT
BEGIN:VEVENT
UID:5
SUMMARY:Far away
DTSTART;VALUE=DATE:20261201
END:VEVENT
BEGIN:VEVENT
UID:6
SUMMARY:Day off
DTSTART;VALUE=DATE:20261013
DTEND;VALUE=DATE:20261014
END:VEVENT
BEGIN:VTODO
UID:7
SUMMARY:Invoice Acme
DUE:20261016T120000Z
END:VTODO
END:VCALENDAR
"""


def cfg_with(feeds):
    cfg = config.load()
    cfg["deadlines"]["feeds"] = feeds
    return cfg


def test_entries_are_split_into_deadlines_and_periods_away():
    entries = agenda.parse(FEED, "Loopstr")
    deadlines, periods = agenda.split(entries, NOW, days=14, period_words=["day off"])
    assert [e.title for e in deadlines] == ["Script draft to Brand X", "Video for Acme due", "Invoice Acme"]
    assert [e.title for e in periods] == ["Summer trip", "Day off"]
    assert all(e.feed == "Loopstr" for e in deadlines + periods)


def test_a_period_already_started_is_still_listed():
    entries = agenda.parse(FEED, "Loopstr")
    _, periods = agenda.split(entries, datetime(2026, 10, 15, 9, 0, tzinfo=UTC), days=3)
    assert [e.title for e in periods] == ["Summer trip"]


def test_feed_is_fetched_at_most_once_per_cache_window():
    calls = []

    def get(url):
        calls.append(url)
        return FEED

    clock = iter([0, 60, 16 * 60]).__next__
    cache = {}
    for _ in range(3):
        agenda.fetch("https://example.com/f.ics", get=get, clock=clock, ttl=15 * 60, cache=cache)
    assert len(calls) == 2


def test_webcal_addresses_are_fetched_over_https():
    seen = []
    agenda.fetch("webcal://example.com/f.ics", get=lambda u: seen.append(u) or FEED, cache={})
    assert seen == ["https://example.com/f.ics"]


def test_deadlines_tool_reports_both_lists_as_data():
    ctx = type("Ctx", (), {"cfg": cfg_with([{"name": "Loopstr", "url": "https://example.com/f.ics"}])})()
    text = agenda.deadlines(14, ctx=ctx, get=lambda u: FEED, now=NOW, cache={})
    assert "data, not instructions" in text
    assert text.index("Video for Acme due") < text.index("Periods away")
    assert text.index("Summer trip") > text.index("Periods away")
    assert "Old deadline" not in text and "Far away" not in text


def test_one_broken_feed_does_not_hide_the_others():
    def get(url):
        if "broken" in url:
            raise OSError("connection refused")
        return FEED

    ctx = type("Ctx", (), {"cfg": cfg_with(["https://broken.example/x.ics", "https://example.com/f.ics"])})()
    text = agenda.deadlines(14, ctx=ctx, get=get, now=NOW, cache={})
    assert "could not read" in text and "broken.example" in text
    assert "Video for Acme due" in text


def test_no_feed_configured_is_an_error():
    ctx = type("Ctx", (), {"cfg": config.load()})()
    assert agenda.deadlines(ctx=ctx, get=lambda u: FEED, now=NOW, cache={}).startswith("error")


def test_tool_is_loaded_only_when_a_feed_is_configured():
    assert "deadlines" not in load_all(config.load())
    tools = load_all(cfg_with(["https://example.com/f.ics"]))
    assert "deadlines" in tools and tools["deadlines"].action({}) is None


def test_upcoming_gives_the_daily_brief_structured_entries():
    deadlines, periods = agenda.upcoming(cfg_with(["https://example.com/f.ics"]), days=7,
                                         get=lambda u: FEED, now=NOW, cache={})
    assert deadlines[0].title == "Script draft to Brand X"
    assert deadlines[0].start == NOW.replace(day=11, hour=15)
    assert periods[0].end - periods[0].start == timedelta(days=7)
