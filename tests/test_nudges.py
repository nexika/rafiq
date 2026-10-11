from datetime import datetime, time, timedelta, timezone

import pytest

from rafiq import config
from rafiq.nudges import Nudger, OpenRouteService, Trip, in_quiet_hours, missing, settings

TZ = timezone(timedelta(hours=2))
NOON = datetime(2026, 10, 10, 12, 0, tzinfo=TZ)


class Clock:
    def __init__(self, when=NOON):
        self.when = when

    def __call__(self):
        return self.when


class FakeCalendar:
    """Events per calendar id, as Google returns them."""

    def __init__(self, events):
        self.events, self.asked = events, []

    def __call__(self, calendar_id, start, end):
        self.asked.append(calendar_id)
        return self.events.get(calendar_id, [])


class FakeRouter:
    def __init__(self, minutes=30):
        self.minutes, self.calls = minutes, []

    def travel_seconds(self, origin, destination, mode):
        self.calls.append((origin, destination, mode))
        return None if self.minutes is None else self.minutes * 60


def event(eid="e1", title="Dentist", start=NOON + timedelta(hours=1), location="12 Rue de Rivoli, Paris"):
    item = {"id": eid, "summary": title, "start": {"dateTime": start.isoformat()}}
    if location:
        item["location"] = location
    return item


def nudger(events, router=None, clock=None, **cfg):
    opts = settings({"nudges": {"enabled": True, "origin": "home", "buffer_minutes": 5, **cfg}})
    return Nudger(opts, FakeCalendar(events), router or FakeRouter(), clock or Clock(), user_tag="en")


def test_no_nudge_while_there_is_still_time():
    # Starts at 13:00, 30 min away + 5 min buffer: leave at 12:25.
    n = nudger({"primary": [event()]})
    assert n.check() == []


def test_one_nudge_when_it_is_time_to_leave_and_only_once():
    clock = Clock()
    n = nudger({"primary": [event()]}, clock=clock)
    clock.when = NOON + timedelta(minutes=25)
    said = n.check()
    assert len(said) == 1
    assert said[0].tag == "en" and "Dentist" in said[0].text and "30" in said[0].text and "13:00" in said[0].text
    clock.when += timedelta(minutes=2)
    assert n.check() == []


def test_events_without_a_location_or_already_started_are_skipped():
    clock = Clock(NOON + timedelta(minutes=50))
    router = FakeRouter()
    n = nudger({"primary": [event("a", location=""), event("b", start=NOON + timedelta(minutes=40))]},
               router=router, clock=clock)
    assert n.check() == [] and router.calls == []


def test_all_day_events_are_skipped():
    item = {"id": "x", "summary": "Holiday", "location": "Nice", "start": {"date": "2026-10-10"}}
    n = nudger({"primary": [item]}, clock=Clock(NOON + timedelta(minutes=59)))
    assert n.check() == []


def test_only_the_chosen_calendars_are_read():
    cal = FakeCalendar({"primary": [], "work": []})
    opts = settings({"nudges": {"enabled": True, "origin": "home", "calendars": ["work"]}})
    Nudger(opts, cal, FakeRouter(), Clock()).check()
    assert cal.asked == ["work"]


def test_the_travel_mode_is_passed_to_the_router():
    router = FakeRouter()
    nudger({"primary": [event()]}, router=router, mode="walking").check()
    assert router.calls == [("home", "12 Rue de Rivoli, Paris", "walking")]


def test_the_route_is_asked_once_per_event_not_every_minute():
    router, clock = FakeRouter(), Clock()
    n = nudger({"primary": [event()]}, router=router, clock=clock)
    for minute in range(5):
        clock.when = NOON + timedelta(minutes=minute)
        n.check()
    assert len(router.calls) == 1


def test_an_unknown_route_gives_no_nudge():
    n = nudger({"primary": [event()]}, router=FakeRouter(None), clock=Clock(NOON + timedelta(minutes=55)))
    assert n.check() == []


def test_quiet_hours_hold_the_nudge():
    early = datetime(2026, 10, 10, 6, 0, tzinfo=TZ)
    n = nudger({"primary": [event(start=early + timedelta(minutes=30))]}, clock=Clock(early),
               quiet_hours=["22:00", "07:00"])
    assert n.check() == []


@pytest.mark.parametrize("now, quiet, expected", [
    (time(23, 0), ["22:00", "07:00"], True),
    (time(6, 59), ["22:00", "07:00"], True),
    (time(7, 0), ["22:00", "07:00"], False),
    (time(13, 0), ["12:00", "14:00"], True),
    (time(15, 0), ["12:00", "14:00"], False),
    (time(3, 0), [], False),
])
def test_quiet_hours(now, quiet, expected):
    assert in_quiet_hours(now, quiet) is expected


def test_the_nudge_is_said_in_the_events_language():
    clock = Clock(NOON + timedelta(minutes=25))
    n = nudger({"primary": [event(title="موعد الطبيب")]}, clock=clock)
    said = n.check()
    assert said[0].tag == "ar" and "موعد الطبيب" in said[0].text


def test_the_nudge_is_said_in_the_users_language_when_the_event_does_not_tell():
    opts = settings({"nudges": {"enabled": True, "origin": "home", "buffer_minutes": 5}})
    n = Nudger(opts, FakeCalendar({"primary": [event(title="RDV 42")]}), FakeRouter(),
               Clock(NOON + timedelta(minutes=25)), user_tag="fr")
    assert n.check()[0].tag == "fr"


def test_nudges_are_off_by_default():
    assert settings(config.load())["enabled"] is False


def test_the_routing_key_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTESERVICE_API_KEY", "k-env")
    assert settings({"nudges": {"routing_key": "k-file"}})["routing_key"] == "k-env"


def test_openrouteservice_geocodes_both_places_then_asks_the_duration():
    asked = []

    def get(url, params):
        asked.append((url, params))
        if "geocode" in url:
            return {"features": [{"geometry": {"coordinates": [2.35, 48.85]}}]}
        return {"features": [{"properties": {"summary": {"duration": 1234.5}}}]}

    ors = OpenRouteService("secret", get=get)
    assert ors.travel_seconds("home", "Louvre", "cycling") == 1234.5
    assert "cycling-regular" in asked[-1][0]
    assert asked[-1][1]["start"] == "2.35,48.85"


def test_openrouteservice_says_none_for_a_place_it_cannot_find():
    ors = OpenRouteService("secret", get=lambda url, params: {"features": []})
    assert ors.travel_seconds("home", "Nowhere at all", "driving") is None


def test_trip_leave_time():
    trip = Trip("e", "Dentist", NOON, "x", 600)
    assert trip.leave_at(buffer_minutes=5) == NOON - timedelta(minutes=15)


def test_nudges_say_what_is_missing():
    ready = {"google": {"enabled": True}, "nudges": {"enabled": True, "origin": "home", "routing_key": "k"}}
    assert missing(ready) is None
    assert "Google" in missing({**ready, "google": {"enabled": False}})
    assert "origin" in missing({**ready, "nudges": {"enabled": True, "routing_key": "k"}})
    assert "OPENROUTESERVICE_API_KEY" in missing({**ready, "nudges": {"enabled": True, "origin": "home"}})
