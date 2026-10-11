"""Calendar nudges: before an event with a place, Rafiq says when to leave, from the travel time.

Off by default. Turn on in config.toml ([nudges] enabled = true) with Google connected, a starting
address (origin) and an OpenRouteService key (free at openrouteservice.org; OPENROUTESERVICE_API_KEY in
the environment, or routing_key in the file). One nudge per event, in the event's language or yours,
never during quiet hours. The calendar and the router are passed in, so tests need neither.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta

import httpx

from . import lang

logger = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "calendars": ["primary"],        # Google calendar ids to watch; "primary" is your own
    "origin": "",                    # where you leave from, an address
    "mode": "driving",               # driving, cycling or walking
    "routing_key": "",
    "buffer_minutes": 5,             # leave this much earlier than the trip itself
    "look_ahead_hours": 4,
    "quiet_hours": ["22:00", "07:00"],
    "language": "",                  # empty: the event's language, else the last one you spoke
}
REFRESH = timedelta(minutes=5)       # how often the calendar is read again
RETRY = timedelta(minutes=10)        # how soon an unknown route is asked again


def settings(cfg: dict) -> dict:
    opts = {**DEFAULTS, **(cfg.get("nudges") or {})}
    if os.environ.get("OPENROUTESERVICE_API_KEY"):
        opts["routing_key"] = os.environ["OPENROUTESERVICE_API_KEY"]
    return opts


def missing(cfg: dict) -> str | None:
    """Why nudges cannot run with these settings, or None when they can."""
    opts = settings(cfg)
    if not cfg.get("google", {}).get("enabled"):
        return "connect Google first (rafiq google-setup, then [google] enabled = true)"
    if not opts["origin"].strip():
        return "set [nudges] origin, the address you leave from"
    if not opts["routing_key"]:
        return "set OPENROUTESERVICE_API_KEY (free at openrouteservice.org)"
    return None


def in_quiet_hours(now: time, quiet: list[str]) -> bool:
    """Whether a time of day falls in [start, end); the range may run past midnight."""
    if len(quiet or []) != 2:
        return False
    start, end = (time.fromisoformat(t) for t in quiet)
    now = now.replace(tzinfo=None)
    return start <= now < end if start <= end else now >= start or now < end


@dataclass
class Trip:
    event_id: str
    title: str
    start: datetime
    location: str
    seconds: float

    def leave_at(self, buffer_minutes: float) -> datetime:
        return self.start - timedelta(seconds=self.seconds, minutes=buffer_minutes)


@dataclass
class Nudge:
    text: str
    tag: str


class OpenRouteService:
    """Travel time between two addresses (openrouteservice.org, a free key)."""

    URL = "https://api.openrouteservice.org"
    PROFILES = {"driving": "driving-car", "cycling": "cycling-regular", "walking": "foot-walking"}

    def __init__(self, key: str, get: Callable[[str, dict], dict] | None = None):
        self.key, self.get = key, get or _get_json
        self.places: dict[str, str | None] = {}

    def _place(self, address: str) -> str | None:
        if address not in self.places:
            data = self.get(f"{self.URL}/geocode/search", {"api_key": self.key, "text": address, "size": 1})
            features = data.get("features") or []
            lon, lat = features[0]["geometry"]["coordinates"][:2] if features else (None, None)
            self.places[address] = None if lon is None else f"{lon},{lat}"
        return self.places[address]

    def travel_seconds(self, origin: str, destination: str, mode: str) -> float | None:
        try:
            start, end = self._place(origin), self._place(destination)
            if not start or not end:
                return None
            profile = self.PROFILES.get(mode, "driving-car")
            data = self.get(f"{self.URL}/v2/directions/{profile}",
                            {"api_key": self.key, "start": start, "end": end})
            return float(data["features"][0]["properties"]["summary"]["duration"])
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning("no route to %s: %s", destination, type(exc).__name__)
            return None


def _get_json(url: str, params: dict) -> dict:
    resp = httpx.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


class Nudger:
    """Reads the chosen calendars every few minutes and says "time to leave" once per event.

    calendar(calendar_id, start, end) returns Google Calendar event dicts; router.travel_seconds(origin,
    destination, mode) returns seconds or None; clock() returns an aware datetime."""

    def __init__(self, opts: dict, calendar: Callable, router, clock: Callable[[], datetime] | None = None,
                 user_tag: str = "en"):
        self.opts, self.calendar, self.router = opts, calendar, router
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.user_tag = user_tag
        self.events: list[dict] = []
        self.read_at: datetime | None = None
        self.routes: dict[str, tuple[float | None, datetime]] = {}
        self.done: set[str] = set()
        self._stop = threading.Event()

    def _read(self, now: datetime) -> None:
        if self.read_at and now - self.read_at < REFRESH:
            return
        end = now + timedelta(hours=float(self.opts["look_ahead_hours"]))
        events = []
        for calendar_id in self.opts["calendars"]:
            events += self.calendar(calendar_id, now, end)
        self.events, self.read_at = events, now

    def _route(self, key: str, location: str, now: datetime) -> float | None:
        known = self.routes.get(key)
        if known and (known[0] is not None or now - known[1] < RETRY):
            return known[0]
        seconds = self.router.travel_seconds(self.opts["origin"], location, self.opts["mode"])
        self.routes[key] = (seconds, now)
        return seconds

    def _tag(self, title: str) -> str:
        if self.opts.get("language"):
            return self.opts["language"]
        tag = lang.guess(title, self.user_tag)
        if tag == "ar" and self.user_tag.startswith("ar"):
            return self.user_tag
        return tag

    def check(self) -> list[Nudge]:
        """The nudges due now; each event is nudged once."""
        now = self.clock()
        self._read(now)
        if in_quiet_hours(now.time(), self.opts["quiet_hours"]):
            return []
        nudges = []
        for item in self.events:
            begin = (item.get("start") or {}).get("dateTime")
            location = (item.get("location") or "").strip()
            if not begin or not location:          # all-day events and events without a place
                continue
            start = datetime.fromisoformat(begin)
            key = f"{item.get('id')}@{begin}"      # a moved event is nudged again
            if key in self.done or start <= now:
                continue
            seconds = self._route(key, location, now)
            if seconds is None:
                continue
            trip = Trip(item.get("id", ""), item.get("summary") or "", start, location, seconds)
            if now < trip.leave_at(float(self.opts["buffer_minutes"])):
                continue
            self.done.add(key)
            tag = self._tag(trip.title)
            nudges.append(Nudge(lang.phrase("leave_now", tag, title=trip.title, time=f"{start:%H:%M}",
                                            minutes=str(max(1, round(seconds / 60)))), tag))
        return nudges

    def start(self, on_nudge: Callable[[Nudge], None], every: float = 60.0) -> None:
        def loop():
            while not self._stop.wait(every):
                try:
                    for nudge in self.check():
                        on_nudge(nudge)
                except Exception as exc:     # the calendar or the network may fail; try next time
                    logger.warning("calendar nudges: %s: %s", type(exc).__name__, exc)

        threading.Thread(target=loop, name="rafiq-nudges", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
