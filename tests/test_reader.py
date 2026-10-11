import subprocess
import sys
import threading
import types

import pytest

from rafiq import reader
from rafiq.reader import Reader, Segment
from rafiq.tools import REGISTRY, load_all


def test_text_is_split_into_sentences():
    assert reader.sentences("First one. Second one!  Third?\n\nA new paragraph") == [
        "First one.", "Second one!", "Third?", "A new paragraph"]
    assert reader.sentences("مرحبا بك. كيف حالك؟ أنا بخير") == ["مرحبا بك.", "كيف حالك؟", "أنا بخير"]
    assert reader.sentences("   \n ") == []


def test_each_sentence_is_read_in_its_own_language():
    text = "Bonjour à tous, comment allez-vous ? هذا نص عربي طويل. The weather is nice today."
    assert reader.segments(text) == [
        Segment("Bonjour à tous, comment allez-vous ?", "fr"),
        Segment("هذا نص عربي طويل.", "ar"),
        Segment("The weather is nice today.", "en"),
    ]


def test_an_english_sentence_with_one_arabic_word_stays_english():
    assert reader.segments("The word is رفيق and it means friend.")[0].tag == "en"


def test_a_sentence_that_tells_nothing_keeps_the_language_before_it():
    assert [s.tag for s in reader.segments("Wie spät ist es heute? 42.")] == ["de", "de"]


def test_very_long_texts_are_cut():
    assert len(reader.segments("Word. " * 100_000)) < 100_000


def fake_run(outputs):
    """A subprocess runner where each command's first word maps to its output (None: not installed)."""
    calls = []

    def run(args):
        calls.append(args)
        out = outputs.get(args[0])
        if out is None:
            raise FileNotFoundError(args[0])
        return subprocess.CompletedProcess(args, 0, out, "")
    return run, calls


def test_the_selection_comes_from_wayland_first():
    run, calls = fake_run({"wl-paste": "selected words", "xclip": "x11 words"})
    assert reader.selection(run=run) == "selected words"
    assert calls[0][:2] == ["wl-paste", "--primary"]


def test_the_selection_falls_back_to_x11():
    run, _ = fake_run({"xclip": "x11 words"})
    assert reader.selection(run=run) == "x11 words"


def test_the_clipboard_is_not_the_primary_selection():
    run, calls = fake_run({"wl-paste": "copied"})
    assert reader.clipboard(run=run) == "copied"
    assert "--primary" not in calls[0]


def test_no_clipboard_tool_gives_empty_text():
    run, _ = fake_run({})
    assert reader.selection(run=run) == ""


def test_pdf_text_is_read_page_by_page(tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    page = types.SimpleNamespace
    fake = types.ModuleType("pypdf")
    fake.PdfReader = lambda path: types.SimpleNamespace(
        pages=[page(extract_text=lambda: "Page one."), page(extract_text=lambda: "Page two.")])
    monkeypatch.setitem(sys.modules, "pypdf", fake)
    assert reader.pdf_text(str(pdf)) == "Page one.\n\nPage two."


def test_a_missing_pdf_is_an_error(tmp_path):
    with pytest.raises(ValueError):
        reader.pdf_text(str(tmp_path / "nope.pdf"))


# ---------------------------------------------------------------------- reading, pausing, skipping


class FakeVoice:
    """Says sentences instantly, and lets a test act in the middle of one."""

    def __init__(self, during=None):
        self.said, self.stopped, self.during = [], 0, during or {}

    def say(self, text, tag):
        self.said.append((text, tag))
        if text in self.during:
            self.during.pop(text)()

    def stop(self):
        self.stopped += 1


def loaded(voice, *texts):
    r = Reader(interrupt=voice.stop)
    r.load([Segment(t, "en") for t in texts])
    return r


def test_reader_reads_every_sentence_in_order():
    voice = FakeVoice()
    r = loaded(voice, "One.", "Two.", "Three.")
    assert r.active
    r.run(voice.say)
    assert voice.said == [("One.", "en"), ("Two.", "en"), ("Three.", "en")]
    assert not r.active and r.state == "idle"


def test_pause_stops_the_voice_and_resume_starts_the_sentence_again():
    voice = FakeVoice()
    r = loaded(voice, "One.", "Two.", "Three.")
    voice.during["Two."] = r.pause
    r.run(voice.say)
    assert voice.stopped == 1 and r.state == "paused" and not r.active
    assert r.resume()
    r.run(voice.say)
    assert [t for t, _ in voice.said] == ["One.", "Two.", "Two.", "Three."]


def test_skip_moves_on_to_the_next_sentence():
    voice = FakeVoice()
    r = loaded(voice, "One.", "Two.", "Three.")
    voice.during["One."] = r.skip
    r.run(voice.say)
    assert [t for t, _ in voice.said] == ["One.", "Two.", "Three."]
    assert voice.stopped == 1


def test_stop_ends_the_reading():
    voice = FakeVoice()
    r = loaded(voice, "One.", "Two.", "Three.")
    voice.during["One."] = r.stop
    r.run(voice.say)
    assert [t for t, _ in voice.said] == ["One."]
    assert r.state == "idle" and not r.resume()


def test_resume_with_nothing_paused_does_nothing():
    r = Reader(interrupt=lambda: None)
    assert not r.resume() and not r.active


def test_progress_is_reported():
    voice, seen = FakeVoice(), []
    r = loaded(voice, "One.", "Two.")
    r.run(voice.say, on_progress=lambda i, n: seen.append((i, n)))
    assert seen == [(0, 2), (1, 2)]


def test_controls_from_another_thread_are_safe():
    voice = FakeVoice()
    r = loaded(voice, *[f"S{i}." for i in range(200)])
    t = threading.Thread(target=lambda: [r.skip() for _ in range(50)])
    t.start()
    r.run(voice.say)
    t.join()
    assert r.state == "idle"


# ---------------------------------------------------------------------- the tools


class Ctx:
    def __init__(self):
        self.reader = Reader(interrupt=lambda: None)
        self.tag = "en"


def test_read_aloud_loads_the_text_and_does_not_send_it_to_the_model(monkeypatch):
    load_all({})
    monkeypatch.setattr(reader, "selection", lambda: "A secret sentence. Another one.")
    ctx = Ctx()
    result = REGISTRY["read_aloud"].run({"source": "selection"}, ctx)
    assert ctx.reader.active and len(ctx.reader.segments) == 2
    assert "secret" not in result and "2 sentences" in result


def test_read_aloud_with_nothing_selected_is_an_error(monkeypatch):
    load_all({})
    monkeypatch.setattr(reader, "selection", lambda: "  ")
    ctx = Ctx()
    assert REGISTRY["read_aloud"].run({"source": "selection"}, ctx).startswith("error")
    assert not ctx.reader.active


def test_read_aloud_reads_a_page(monkeypatch):
    load_all({})
    monkeypatch.setattr(reader, "page_text", lambda url: "Article text. More text." if url == "https://a.b/c" else "")
    ctx = Ctx()
    assert "2 sentences" in REGISTRY["read_aloud"].run({"source": "page", "url": "https://a.b/c"}, ctx)


def test_read_aloud_needs_an_address_for_a_page_and_a_path_for_a_pdf():
    load_all({})
    assert REGISTRY["read_aloud"].run({"source": "page"}, Ctx()).startswith("error")
    assert REGISTRY["read_aloud"].run({"source": "pdf"}, Ctx()).startswith("error")


def test_the_reading_tool_pauses_resumes_and_stops():
    load_all({})
    ctx = Ctx()
    ctx.reader.load([Segment("One.", "en"), Segment("Two.", "en")])
    ctx.reader.pause()
    assert REGISTRY["reading"].run({"action": "resume"}, ctx) == "resumed" and ctx.reader.active
    assert REGISTRY["reading"].run({"action": "stop"}, ctx) == "stopped" and not ctx.reader.active
    assert REGISTRY["reading"].run({"action": "resume"}, ctx).startswith("error")
