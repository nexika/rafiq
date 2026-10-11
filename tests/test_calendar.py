"""Google Calendar tools against a fake Calendar API: no network, no account."""

import pytest

from rafiq.tools import REGISTRY, load_all


class _Call:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeCalendar:
    """Just the parts of googleapiclient's calendar v3 service the tools use."""

    def __init__(self, calendars, events, calendar_list_error=None):
        self.calendars, self.events_by_calendar = calendars, events
        self.calendar_list_error = calendar_list_error
        self.listed, self.deleted = [], []

    def calendarList(self):  # noqa: N802  (the Google API's name)
        return self

    def events(self):
        return self

    def list(self, **params):
        if "calendarId" not in params:
            return _Call(self.calendar_list_error or {"items": self.calendars})
        self.listed.append(params["calendarId"])
        return _Call({"items": self.events_by_calendar.get(params["calendarId"], [])})

    def get(self, calendarId, eventId):  # noqa: N803
        found = [e for e in self.events_by_calendar.get(calendarId, []) if e["id"] == eventId]
        return _Call(found[0] if found else LookupError("not found"))

    def delete(self, calendarId, eventId):  # noqa: N803
        self.deleted.append((calendarId, eventId))
        return _Call("")


@pytest.fixture
def google(monkeypatch):
    load_all({"google": {"enabled": True}})
    from rafiq.tools import google as module

    def use(fake):
        monkeypatch.setattr(module, "service", lambda name, version: fake)
        return fake
    return use


CALENDARS = [{"id": "me@example.com", "summary": "me@example.com", "primary": True},
             {"id": "fr.french#holiday@group.v.calendar.google.com", "summary": "Holidays in France",
              "accessRole": "reader"},
             {"id": "team@group.calendar.google.com", "summary": "Team"}]
EVENTS = {"me@example.com": [{"id": "ev1", "summary": "Dentist", "start": {"dateTime": "2026-10-10T15:00:00+02:00"},
                              "end": {"dateTime": "2026-10-10T16:00:00+02:00"}}],
          "fr.french#holiday@group.v.calendar.google.com": [
              {"id": "h1", "summary": "Armistice", "start": {"date": "2026-10-10"}, "end": {"date": "2026-10-11"}}],
          "team@group.calendar.google.com": [
              {"id": "t1", "summary": "Standup", "start": {"dateTime": "2026-10-10T09:30:00+02:00"},
               "end": {"dateTime": "2026-10-10T09:45:00+02:00"}}]}


def test_calendar_events_reads_every_calendar_in_time_order(google):
    fake = google(FakeCalendar(CALENDARS, EVENTS))
    text = REGISTRY["calendar_events"].fn(days=1)
    assert set(fake.listed) == {c["id"] for c in CALENDARS}
    assert text.index("Armistice") < text.index("Standup") < text.index("Dentist")
    assert "(Holidays in France)" in text and "(Team)" in text
    assert "id ev1" in text  # primary events carry their id, so they can be deleted


def test_calendar_events_falls_back_to_primary_without_the_calendar_list(google):
    fake = google(FakeCalendar(CALENDARS, {"primary": EVENTS["me@example.com"]},
                               calendar_list_error=PermissionError("insufficient scopes")))
    text = REGISTRY["calendar_events"].fn(days=1)
    assert fake.listed == ["primary"] and "Dentist" in text


def test_calendar_delete_confirms_with_the_event_title_and_time(google):
    google(FakeCalendar(CALENDARS, {"primary": EVENTS["me@example.com"]}))
    action = REGISTRY["calendar_delete"].action({"id": "ev1"})
    assert "Dentist" in action and "2026-10-10 at 15:00" in action


def test_calendar_delete_confirmation_survives_an_unknown_event(google):
    google(FakeCalendar(CALENDARS, {}))
    assert "nope" in REGISTRY["calendar_delete"].action({"id": "nope"})


def test_calendar_delete_removes_the_event_from_the_primary_calendar(google):
    fake = google(FakeCalendar(CALENDARS, {"primary": EVENTS["me@example.com"]}))
    assert REGISTRY["calendar_delete"].fn(id="ev1").startswith("deleted")
    assert fake.deleted == [("primary", "ev1")]
