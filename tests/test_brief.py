from datetime import date, datetime, timedelta

from rafiq import config
from rafiq.assistant import Assistant, Context
from rafiq.scheduler import Scheduler
from rafiq.tools import REGISTRY, Tool, brief, load_all


def fake_tools(calls, failing=()):
    def make(name, text):
        def run(**kwargs):
            calls.append((name, kwargs))
            if name in failing:
                raise RuntimeError("Google is not set up")
            return text
        return Tool(name, name, lambda days=None, query=None, max_results=None: run(**{
            k: v for k, v in {"days": days, "query": query, "max_results": max_results}.items() if v is not None}))

    return {"get_time": make("get_time", "Saturday 10 October 2026, 07:30"),
            "weather": make("weather", "Cairo, Egypt: now 24°C, clear sky"),
            "calendar_events": make("calendar_events", "- 2026-10-10T13:00 to 14:00: lunch with Sara"),
            "email_list": make("email_list", "- id 1: from Anna, 'Tickets'")}


def context(tmp_path, city="Cairo"):
    cfg = config.load()
    cfg["place"]["city"] = city
    return Context(cfg, Scheduler(tmp_path / "alarms.json", lambda alarm: None))


def test_the_brief_gathers_time_weather_calendar_and_new_email(tmp_path):
    calls = []
    text = brief.gather(fake_tools(calls), context(tmp_path))
    assert "07:30" in text and "24°C" in text and "lunch with Sara" in text and "Tickets" in text
    assert [name for name, _ in calls] == ["get_time", "weather", "calendar_events", "email_list"]
    assert dict(calls)["calendar_events"] == {"days": 1}
    assert "is:unread" in dict(calls)["email_list"]["query"]


def test_the_brief_leaves_out_what_is_not_set_up_or_fails(tmp_path):
    calls = []
    tools = fake_tools(calls, failing=("email_list",))
    del tools["calendar_events"]                       # Google off: no calendar tool at all
    text = brief.gather(tools, context(tmp_path, city=""))
    assert "07:30" in text
    assert "Weather" not in text and "Calendar" not in text and "Email" not in text
    assert "weather" not in [name for name, _ in calls]   # no city: not even asked


def test_the_brief_names_what_is_due_today_but_not_later(tmp_path):
    ctx = context(tmp_path)
    end_of_day = datetime.now().astimezone().replace(hour=23, minute=59, second=0, microsecond=0)
    ctx.scheduler.add(end_of_day.timestamp(), "send the report", "reminder", "en")
    ctx.scheduler.add((end_of_day + timedelta(days=2)).timestamp(), "dentist", "reminder", "en")
    text = brief.gather({}, ctx)
    assert "send the report" in text and "dentist" not in text


def test_the_brief_is_a_tool_that_reads_without_asking():
    tools = load_all({"google": {"enabled": False}})
    assert "daily_brief" in tools and REGISTRY["daily_brief"].action({}) is None


def test_the_start_of_day_brief_is_claimed_once_a_day(tmp_path):
    path = tmp_path / "brief.json"
    today = date(2026, 10, 10)
    assert brief.claim_today(path, today) is True
    assert brief.claim_today(path, today) is False
    assert brief.claim_today(path, today + timedelta(days=1)) is True


def test_a_broken_state_file_does_not_stop_the_brief(tmp_path):
    path = tmp_path / "brief.json"
    path.write_text("{not json")
    assert brief.claim_today(path, date(2026, 10, 10)) is True


class FakeAssistant:
    """Only what start_of_day and the brief job use, without a microphone or a speaker."""
    def __init__(self, enabled, language="fr"):
        self.cfg = config.load()
        self.cfg["brief"].update({"at_first_start": enabled, "language": language})
        self.jobs, self.said = [], []

    def submit(self, kind, **data):
        self.jobs.append((kind, data))

    def respond(self, text, tag):
        self.said.append((text, tag))


def test_the_brief_plays_at_the_first_start_of_the_day_when_turned_on():
    on = FakeAssistant(enabled=True)
    Assistant.start_of_day(on)
    Assistant.start_of_day(on)
    assert on.jobs == [("brief", {})]
    off = FakeAssistant(enabled=False)
    Assistant.start_of_day(off)
    assert off.jobs == []


def test_the_brief_job_asks_the_brain_in_the_configured_language():
    assistant = FakeAssistant(enabled=True, language="ar-EG")
    Assistant.play_brief(assistant)
    assert assistant.said == [(brief.REQUEST, "ar-EG")]
