"""The morning paper: a printable A4 page of the day, gathered from your sources and printed to PDF.

Sources (weather, calendar, reminders, RSS feeds) are gathered into a typed Edition, rendered to HTML and
CSS, and printed with Chromium (Playwright). A source that fails leaves its section empty and adds a
note; the paper is still made. Article summaries are the feed's own text, never invented.

Feed text is untrusted: it is escaped in the HTML, links are kept only when they are http or https, and
the XML parser does not load external entities.
"""

from __future__ import annotations

import html
import logging
import random
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

RTL = {"ar", "ar-EG"}

LABELS = {
    "weather": {"en": "Weather", "fr": "Météo", "es": "Tiempo", "de": "Wetter", "ar": "الطقس"},
    "calendar": {"en": "Today", "fr": "Aujourd'hui", "es": "Hoy", "de": "Heute", "ar": "اليوم"},
    "news": {"en": "News", "fr": "Actualités", "es": "Noticias", "de": "Nachrichten", "ar": "الأخبار"},
    "tech": {"en": "Tech", "fr": "Tech", "es": "Tecnología", "de": "Technik", "ar": "التقنية"},
    "priorities": {"en": "Priorities", "fr": "Priorités", "es": "Prioridades", "de": "Prioritäten",
                   "ar": "الأولويات"},
    "reminders": {"en": "Reminders", "fr": "Rappels", "es": "Recordatorios", "de": "Erinnerungen",
                  "ar": "التذكيرات"},
    "deadlines": {"en": "Deadlines", "fr": "Échéances", "es": "Plazos", "de": "Fristen", "ar": "المواعيد النهائية"},
    "puzzle": {"en": "Puzzle", "fr": "Jeu", "es": "Pasatiempo", "de": "Rätsel", "ar": "لغز"},
    "demo": {"en": "Demo edition: made-up data", "fr": "Édition de démonstration : données inventées",
             "es": "Edición de demostración: datos inventados", "de": "Demo-Ausgabe: erfundene Daten",
             "ar": "نسخة تجريبية: بيانات مختلقة"},
    "days_left": {"en": "in {n} d", "fr": "dans {n} j", "es": "en {n} d", "de": "in {n} T", "ar": "بعد {n} يوم"},
}


# ---------------------------------------------------------------------- the edition

@dataclass
class Article:
    title: str
    url: str
    summary: str
    source: str


@dataclass
class Event:
    time: str           # "09:00", or "" for all day
    title: str
    location: str


@dataclass
class Weather:
    city: str
    high: float
    low: float
    sky: str
    rain_chance: int


@dataclass
class Deadline:
    title: str
    due: date


@dataclass
class Puzzle:
    grid: list[list[int]]       # 0 is a blank
    solution: list[list[int]]


@dataclass
class Edition:
    day: date
    title: str
    languages: list[str]
    weather: Weather | None = None
    events: list[Event] = field(default_factory=list)
    news: list[Article] = field(default_factory=list)
    tech: list[Article] = field(default_factory=list)
    priorities: list[str] = field(default_factory=list)
    reminders: list[str] = field(default_factory=list)
    deadlines: list[Deadline] = field(default_factory=list)
    puzzle: Puzzle | None = None
    notes: list[str] = field(default_factory=list)     # sources that could not be read
    demo: bool = False


# ---------------------------------------------------------------------- feeds

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in node if _local(c.tag) == name), None)


def _text(node: ET.Element, name: str) -> str:
    found = _child(node, name)
    return "".join(found.itertext()).strip() if found is not None else ""


def plain(text: str) -> str:
    """Feed HTML to one line of plain text."""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.") + "…"


def parse_feed(xml: str) -> list[Article]:
    """The items of an RSS 2.0 or Atom feed; a feed that does not parse has none."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    if _local(root.tag) == "rss":
        channel = _child(root, "channel")
        if channel is None:
            return []
        source = _text(channel, "title")
        return [Article(plain(_text(item, "title")), _text(item, "link"), plain(_text(item, "description")), source)
                for item in channel if _local(item.tag) == "item"]
    if _local(root.tag) == "feed":
        source = _text(root, "title")
        out = []
        for entry in root:
            if _local(entry.tag) != "entry":
                continue
            links = [c for c in entry if _local(c.tag) == "link"]
            link = next((c.get("href", "") for c in links if c.get("rel", "alternate") == "alternate"), "")
            summary = _text(entry, "summary") or _text(entry, "content")
            out.append(Article(plain(_text(entry, "title")), link, plain(summary), source))
        return out
    return []


# ---------------------------------------------------------------------- sources

class Sources(Protocol):
    def weather(self) -> Weather | None: ...
    def events(self, day: date) -> list[Event]: ...
    def reminders(self, day: date) -> list[str]: ...
    def feed(self, url: str) -> str: ...


def reminders_on(alarms, day: date) -> list[str]:
    """The reminders (not timers) due on that local day, as "HH:MM label"."""
    out = []
    for alarm in alarms:
        due = datetime.fromtimestamp(alarm.due).astimezone()
        if alarm.kind == "reminder" and due.date() == day:
            out.append(f"{due:%H:%M} {alarm.label}")
    return out


class LiveSources:
    """The real sources: Open-Meteo, Google Calendar (when connected), saved reminders, public feeds."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def weather(self) -> Weather | None:
        from .tools.everyday import WEATHER_CODES, _get_json, geocode

        city = self.cfg.get("place", {}).get("city", "")
        if not city:
            return None
        place = geocode(city, _get_json)
        if not place:
            return None
        daily = _get_json("https://api.open-meteo.com/v1/forecast", params={
            "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto", "forecast_days": 1,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})["daily"]
        return Weather(place["name"], daily["temperature_2m_max"][0], daily["temperature_2m_min"][0],
                       WEATHER_CODES.get(daily["weather_code"][0], "unknown"),
                       int(daily["precipitation_probability_max"][0] or 0))

    def events(self, day: date) -> list[Event]:
        if not self.cfg.get("google", {}).get("enabled"):
            return []
        from .tools.google import service

        start = datetime.combine(day, datetime.min.time()).astimezone()
        items = service("calendar", "v3").events().list(
            calendarId="primary", timeMin=start.isoformat(), timeMax=(start + timedelta(days=1)).isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=25).execute().get("items", [])
        out = []
        for item in items:
            when = item.get("start", {}).get("dateTime", "")
            out.append(Event(when[11:16] if when else "", item.get("summary", ""), item.get("location", "")))
        return out

    def reminders(self, day: date) -> list[str]:
        from . import config
        from .scheduler import Scheduler

        return reminders_on(Scheduler(config.data_dir() / "alarms.json", lambda _a: None).pending(), day)

    def feed(self, url: str) -> str:
        from .tools.web import fetch

        return fetch(url)[1]


# ---------------------------------------------------------------------- gathering

def _languages(cfg: dict) -> list[str]:
    langs = cfg.get("paper", {}).get("languages") or cfg.get("speech", {}).get("languages", ["en"])[:1]
    return [tag for tag in langs if tag] or ["en"]


def _articles(urls: list[str], sources: Sources, limit: int, notes: list[str]) -> list[Article]:
    out: list[Article] = []
    for url in urls:
        try:
            out.extend(parse_feed(sources.feed(url)))
        except Exception as exc:  # one dead feed must not stop the paper
            logger.warning("feed %s: %s", url, exc)
            notes.append(f"feed {url}: {type(exc).__name__}")
    return [a for a in out if a.title][:limit]


def _deadlines(items: list[dict], day: date, horizon: int = 30) -> list[Deadline]:
    out = []
    for item in items:
        try:
            due = date.fromisoformat(str(item["date"]))
        except (KeyError, ValueError):
            continue
        if day <= due <= day + timedelta(days=horizon):
            out.append(Deadline(str(item.get("title", "")), due))
    return sorted(out, key=lambda d: d.due)


def gather(cfg: dict, sources: Sources, day: date) -> Edition:
    conf = cfg.get("paper", {})
    edition = Edition(day, conf.get("title") or "Rafiq", _languages(cfg))
    limit = int(conf.get("max_articles", 6))
    for name, fetch_section in (("weather", sources.weather), ("calendar", lambda: sources.events(day)),
                                ("reminders", lambda: sources.reminders(day))):
        try:
            value = fetch_section()
        except Exception as exc:  # a missing account or no network: that section stays empty
            logger.warning("%s: %s", name, exc)
            edition.notes.append(f"{name}: {type(exc).__name__}")
            continue
        if name == "weather":
            edition.weather = value
        elif name == "calendar":
            edition.events = list(value)
        else:
            edition.reminders = list(value)
    edition.news = _articles(conf.get("feeds", []), sources, limit, edition.notes)
    edition.tech = _articles(conf.get("tech_feeds", []), sources, limit, edition.notes)
    edition.priorities = [str(p) for p in conf.get("priorities", [])]
    edition.deadlines = _deadlines(conf.get("deadlines", []), day)
    edition.puzzle = sudoku(seed=day.toordinal())
    return edition


def demo_edition(day: date, languages: list[str] | None = None) -> Edition:
    """A full paper of made-up data, to see the layout without any account or feed."""
    return Edition(
        day, "Rafiq", languages or ["en"], demo=True,
        weather=Weather("Alexandria", 29.0, 21.0, "partly cloudy", 10),
        events=[Event("09:00", "Team stand-up", "Video call"), Event("12:30", "Lunch with Sara", "Corniche café"),
                Event("17:00", "Football practice", "Club pitch")],
        news=[Article("City opens a new tram line", "https://example.com/tram",
                      "The line links the old town to the university, with a tram every six minutes at rush hour.",
                      "Demo Daily"),
              Article("Harvest festival this weekend", "https://example.com/harvest",
                      "Local farmers bring dates, mangoes and olive oil to the main square from Friday.", "Demo Daily"),
              Article("Library extends its opening hours", "https://example.com/library",
                      "Reading rooms now stay open until ten in the evening during exam season.", "Demo Daily"),
              Article("Sea temperature hits a record", "https://example.com/sea",
                      "Measurements this week were the warmest recorded in October.", "Demo Daily")],
        tech=[Article("A phone battery that lasts a week", "https://example.com/battery",
                      "A prototype cell stores twice the energy in the same space.", "Demo Tech"),
              Article("Open-source speech model learns Arabic dialects", "https://example.com/speech",
                      "The model was trained on recordings from twelve countries.", "Demo Tech"),
              Article("Browsers agree on a new image format", "https://example.com/image",
                      "Pictures load faster with no visible loss of quality.", "Demo Tech")],
        priorities=["Send the quarterly report", "Book the car service"],
        reminders=["08:30 Water the plants", "19:00 Call mum"],
        deadlines=[Deadline("Tax return", day + timedelta(days=3)),
                   Deadline("Passport renewal", day + timedelta(days=12))],
        puzzle=sudoku(seed=day.toordinal()),
    )


# ---------------------------------------------------------------------- puzzle

def sudoku(seed: int, blanks: int = 45) -> Puzzle:
    """A 9x9 sudoku from a shuffled base pattern (always valid). The same seed gives the same puzzle."""
    rng = random.Random(seed)

    def shuffled_groups() -> list[int]:
        groups = rng.sample(range(3), 3)
        return [g * 3 + i for g in groups for i in rng.sample(range(3), 3)]

    rows, cols, digits = shuffled_groups(), shuffled_groups(), rng.sample(range(1, 10), 9)
    solution = [[digits[(3 * (r % 3) + r // 3 + c) % 9] for c in cols] for r in rows]
    grid = [row[:] for row in solution]
    for cell in rng.sample(range(81), blanks):
        grid[cell // 9][cell % 9] = 0
    return Puzzle(grid, solution)


# ---------------------------------------------------------------------- layout

def density(edition: Edition) -> str:
    """airy, normal or compact: the type size and how much of each summary fits on one page."""
    count = (len(edition.news) + len(edition.tech) + len(edition.events) + len(edition.reminders)
             + len(edition.priorities) + len(edition.deadlines))
    if count <= 8:
        return "airy"
    return "normal" if count <= 24 else "compact"


SUMMARY_CHARS = {"airy": 400, "normal": 240, "compact": 120}
FONT_PT = {"airy": 11.5, "normal": 10, "compact": 8.5}


def _label(key: str, languages: list[str], **values) -> str:
    table = LABELS[key]
    seen = []
    for tag in languages:
        text = table.get(tag, table.get(tag.split("-")[0], table["en"])).format(**values)
        if text not in seen:
            seen.append(text)
    return " · ".join(seen)


def _e(text) -> str:
    return html.escape(str(text), quote=True)


def _article(a: Article, limit: int) -> str:
    title = _e(a.title)
    if a.url.lower().startswith(("http://", "https://")):
        title = f'<a href="{_e(a.url)}">{title}</a>'
    summary = f"<p>{_e(shorten(a.summary, limit))}</p>" if a.summary else ""
    source = f'<span class="src">{_e(a.source)}</span>' if a.source else ""
    return f"<article><h3>{title}</h3>{summary}{source}</article>"


def _list(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>"


CSS = """
@page { size: A4; margin: 12mm; }
* { box-sizing: border-box; }
body { margin: 0; font-family: "Noto Serif", "Noto Naskh Arabic", Georgia, serif; font-size: var(--pt);
       color: #111; background: #fff; line-height: 1.35; }
header { border-bottom: 3px double #111; margin-bottom: 4mm; display: flex; justify-content: space-between;
         align-items: baseline; }
header h1 { font-size: 2.6em; margin: 0; letter-spacing: .02em; }
.demo { border: 1px solid #111; padding: 1mm 2mm; font-size: .85em; text-transform: uppercase; }
main { columns: 2; column-gap: 7mm; }
section { break-inside: avoid-column; margin-bottom: 4mm; }
section.wide { column-span: all; }
h2 { font-size: 1.15em; border-bottom: 1px solid #111; margin: 0 0 1.5mm; text-transform: uppercase; }
h3 { font-size: 1.02em; margin: 1.5mm 0 .5mm; }
article p { margin: 0; }
a { color: inherit; text-decoration: none; }
.src, .notes { color: #555; font-size: .8em; }
ul { margin: 0; padding-inline-start: 4mm; }
table.sudoku { border-collapse: collapse; margin: 2mm auto; }
table.sudoku td { width: 7mm; height: 7mm; border: 1px solid #999; text-align: center; font-size: 1.1em; }
table.sudoku tr:nth-child(3n) td { border-bottom: 2px solid #111; }
table.sudoku td:nth-child(3n) { border-inline-end: 2px solid #111; }
table.sudoku { border: 2px solid #111; }
"""


def render_html(edition: Edition) -> str:
    langs = edition.languages or ["en"]
    primary = langs[0]
    direction = "rtl" if primary in RTL else "ltr"
    mode = density(edition)
    limit = SUMMARY_CHARS[mode]
    sections = []
    if edition.weather:
        w = edition.weather
        sections.append(f"<section><h2>{_e(_label('weather', langs))}</h2><p><b>{_e(w.city)}</b>: {_e(w.sky)}, "
                        f"{w.low:.0f}–{w.high:.0f}°C, {w.rain_chance}% ☂</p></section>")
    if edition.events:
        rows = [f"{e.time} {e.title}" + (f" ({e.location})" if e.location else "") for e in edition.events]
        sections.append(f"<section><h2>{_e(_label('calendar', langs))}</h2>{_list(rows)}</section>")
    for key, items in (("priorities", edition.priorities), ("reminders", edition.reminders)):
        if items:
            sections.append(f"<section><h2>{_e(_label(key, langs))}</h2>{_list(items)}</section>")
    if edition.deadlines:
        rows = [f"{d.due.isoformat()} {d.title} ({_label('days_left', langs, n=(d.due - edition.day).days)})"
                for d in edition.deadlines]
        sections.append(f"<section><h2>{_e(_label('deadlines', langs))}</h2>{_list(rows)}</section>")
    for key, articles in (("news", edition.news), ("tech", edition.tech)):
        if articles:
            body = "".join(_article(a, limit) for a in articles)
            sections.append(f"<section><h2>{_e(_label(key, langs))}</h2>{body}</section>")
    if edition.puzzle:
        cells = "".join("<tr>" + "".join(f"<td>{n or ''}</td>" for n in row) + "</tr>" for row in edition.puzzle.grid)
        sections.append(f'<section class="wide"><h2>{_e(_label("puzzle", langs))}</h2>'
                        f'<table class="sudoku" dir="ltr">{cells}</table></section>')
    demo = f'<span class="demo">{_e(_label("demo", langs))}</span>' if edition.demo else ""
    notes = f'<p class="notes">{_e("; ".join(edition.notes))}</p>' if edition.notes else ""
    return (f'<!doctype html>\n<html lang="{_e(primary)}" dir="{direction}"><head><meta charset="utf-8">'
            f"<title>{_e(edition.title)} {edition.day.isoformat()}</title>"
            f"<style>:root {{ --pt: {FONT_PT[mode]}pt; }}{CSS}</style></head>"
            f'<body class="{mode}"><header><h1>{_e(edition.title)}</h1>{demo}'
            f"<span>{edition.day.isoformat()}</span></header><main>{''.join(sections)}</main>{notes}</body></html>")


# ---------------------------------------------------------------------- printing

class PrintError(RuntimeError):
    pass


class Printer(Protocol):
    def print_pdf(self, html: str, out: Path) -> None: ...


class ChromiumPrinter:
    """Prints with headless Chromium through Playwright (pip install playwright; playwright install chromium)."""

    def print_pdf(self, html: str, out: Path) -> None:
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError as exc:
            raise PrintError("printing needs Chromium: pip install playwright && playwright install chromium") from exc
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                try:
                    page = browser.new_page()
                    page.set_content(html, wait_until="load")
                    page.pdf(path=str(out), format="A4", print_background=True, prefer_css_page_size=True)
                finally:
                    browser.close()
        except Error as exc:
            raise PrintError(f"Chromium could not print ({exc}); try: playwright install chromium") from exc


def make(edition: Edition, out: Path, printer: Printer | None) -> Path:
    """Write the paper: HTML when `out` ends in .html or there is no printer, else a PDF."""
    out.parent.mkdir(parents=True, exist_ok=True)
    page = render_html(edition)
    if printer is None or out.suffix.lower() == ".html":
        out.write_text(page, encoding="utf-8")
    else:
        printer.print_pdf(page, out)
    return out
