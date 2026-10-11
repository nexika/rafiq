from datetime import date, datetime

import pytest

from rafiq import paper
from rafiq.__main__ import main

DAY = date(2026, 10, 12)

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Daily Tech</title>
<item><title>Chips get smaller</title><link>https://example.com/chips</link>
<description>&lt;p&gt;A new process &lt;b&gt;halves&lt;/b&gt; the size of transistors.&lt;/p&gt;</description></item>
<item><title>Second story</title><link>https://example.com/2</link><description>Short.</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom News</title>
<entry><title>Rain in Cairo</title><link href="https://example.org/rain"/>
<summary>The first rain of the season fell on Cairo.</summary></entry>
</feed>"""


class FakeSources:
    def __init__(self, feeds=None, events=None, reminders=None, weather=None, fail=()):
        self.feeds = feeds or {}
        self._events = events or []
        self._reminders = reminders or []
        self._weather = weather
        self.fail = set(fail)

    def weather(self):
        if "weather" in self.fail:
            raise OSError("offline")
        return self._weather

    def events(self, day):
        if "events" in self.fail:
            raise OSError("no calendar")
        return self._events

    def reminders(self, day):
        return self._reminders

    def feed(self, url):
        if url not in self.feeds:
            raise OSError("not found")
        return self.feeds[url]


def test_rss_items_keep_their_own_text_without_markup():
    items = paper.parse_feed(RSS)
    assert [a.title for a in items] == ["Chips get smaller", "Second story"]
    assert items[0].source == "Daily Tech"
    assert items[0].url == "https://example.com/chips"
    assert items[0].summary == "A new process halves the size of transistors."


def test_atom_entries_are_read():
    items = paper.parse_feed(ATOM)
    assert items == [paper.Article("Rain in Cairo", "https://example.org/rain",
                                   "The first rain of the season fell on Cairo.", "Atom News")]


def test_a_broken_feed_is_empty_not_an_error():
    assert paper.parse_feed("<html>not a feed") == []


def test_long_summaries_are_cut_at_a_word():
    text = "word " * 200
    cut = paper.shorten(text, 60)
    assert len(cut) <= 61 and cut.endswith("…") and not cut.endswith(" …")


def test_gather_builds_an_edition_from_the_config():
    cfg = {"paper": {"title": "My Paper", "languages": ["en"], "max_articles": 5,
                     "feeds": ["https://a/news.xml"], "tech_feeds": ["https://a/tech.xml"],
                     "priorities": ["Finish the report"],
                     "deadlines": [{"title": "Tax return", "date": "2026-10-15"},
                                   {"title": "Long past", "date": "2026-01-01"}]}}
    sources = FakeSources(
        feeds={"https://a/news.xml": ATOM, "https://a/tech.xml": RSS},
        events=[paper.Event("09:00", "Stand-up", "")],
        reminders=["Call mum"],
        weather=paper.Weather("Cairo", 31.0, 22.0, "clear sky", 0))
    edition = paper.gather(cfg, sources, DAY)
    assert edition.title == "My Paper" and edition.day == DAY
    assert [a.title for a in edition.news] == ["Rain in Cairo"]
    assert [a.title for a in edition.tech] == ["Chips get smaller", "Second story"]
    assert edition.events[0].title == "Stand-up"
    assert edition.reminders == ["Call mum"]
    assert edition.priorities == ["Finish the report"]
    assert edition.deadlines == [paper.Deadline("Tax return", date(2026, 10, 15))]
    assert edition.weather.city == "Cairo"
    assert edition.puzzle is not None


def test_a_failing_source_leaves_its_section_empty():
    cfg = {"paper": {"feeds": ["https://gone/feed.xml"]}}
    edition = paper.gather(cfg, FakeSources(fail={"weather", "events"}), DAY)
    assert edition.weather is None and edition.events == [] and edition.news == []
    assert any("weather" in note for note in edition.notes)


def test_density_follows_how_much_there_is():
    small = paper.demo_edition(DAY)
    small.news, small.tech, small.events = small.news[:1], [], []
    big = paper.demo_edition(DAY)
    big.news = big.news * 6
    assert paper.density(small) == "airy"
    assert paper.density(paper.demo_edition(DAY)) in ("normal", "airy")
    assert paper.density(big) == "compact"


def test_demo_edition_is_made_up_and_says_so():
    edition = paper.demo_edition(DAY)
    assert edition.demo and edition.news and edition.tech and edition.events and edition.puzzle
    assert "demo" in paper.render_html(edition).lower()


def test_html_is_a4_and_escapes_feed_text():
    edition = paper.demo_edition(DAY)
    edition.news[0] = paper.Article("<script>alert(1)</script>", "javascript:alert(1)", "x & y", "Evil")
    html = paper.render_html(edition)
    assert "size: A4" in html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert 'href="javascript:' not in html
    assert "x &amp; y" in html


def test_arabic_edition_is_right_to_left():
    edition = paper.demo_edition(DAY, languages=["ar"])
    html = paper.render_html(edition)
    assert 'dir="rtl"' in html and 'lang="ar"' in html
    assert "الطقس" in html


def test_bilingual_edition_shows_both_languages_in_headings():
    edition = paper.demo_edition(DAY, languages=["en", "ar"])
    html = paper.render_html(edition)
    assert 'dir="ltr"' in html
    assert "Weather" in html and "الطقس" in html


def test_sudoku_puzzle_has_a_valid_solution_and_blanks():
    puzzle = paper.sudoku(seed=7)
    rows = puzzle.solution
    for i in range(9):
        assert sorted(rows[i]) == list(range(1, 10))
        assert sorted(r[i] for r in rows) == list(range(1, 10))
        box = [rows[r][c] for r in range(i // 3 * 3, i // 3 * 3 + 3) for c in range(i % 3 * 3, i % 3 * 3 + 3)]
        assert sorted(box) == list(range(1, 10))
    blanks = sum(cell == 0 for row in puzzle.grid for cell in row)
    assert 35 <= blanks <= 55
    for r in range(9):
        for c in range(9):
            assert puzzle.grid[r][c] in (0, rows[r][c])
    assert paper.sudoku(seed=7) == puzzle


def test_reminders_come_from_the_scheduler_for_that_day():
    from rafiq.scheduler import Alarm

    morning = datetime(2026, 10, 12, 8, 30).astimezone().timestamp()
    tomorrow = datetime(2026, 10, 13, 8, 30).astimezone().timestamp()
    alarms = [Alarm("a", morning, "Water the plants", "reminder", "en"),
              Alarm("b", morning, "tea", "timer", "en"),
              Alarm("c", tomorrow, "Dentist", "reminder", "en")]
    assert paper.reminders_on(alarms, DAY) == ["08:30 Water the plants"]


class FakePrinter:
    def __init__(self):
        self.calls = []

    def print_pdf(self, html, out):
        self.calls.append((html, out))
        out.write_bytes(b"%PDF-1.7 fake")


def test_make_writes_html_and_hands_it_to_the_printer(tmp_path):
    printer = FakePrinter()
    out = paper.make(paper.demo_edition(DAY), tmp_path / "paper.pdf", printer)
    assert out.read_bytes().startswith(b"%PDF")
    assert "<html" in printer.calls[0][0]


def test_make_html_only_needs_no_printer(tmp_path):
    out = paper.make(paper.demo_edition(DAY), tmp_path / "paper.html", None)
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_missing_chromium_gives_a_clear_error(monkeypatch, tmp_path):
    import builtins

    real_import = builtins.__import__

    def no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_playwright)
    with pytest.raises(paper.PrintError, match="playwright install chromium"):
        paper.ChromiumPrinter().print_pdf("<html></html>", tmp_path / "x.pdf")


def test_cli_demo_html(tmp_path, capsys):
    out = tmp_path / "demo.html"
    assert main(["paper", "--demo", "--html", "--out", str(out)]) == 0
    assert out.is_file() and str(out) in capsys.readouterr().out
