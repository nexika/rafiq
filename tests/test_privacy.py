import logging
import os
import queue
import time

import pytest

from rafiq import privacy
from rafiq.assistant import Assistant, Events


@pytest.mark.parametrize("text, hidden, mark", [
    ("write to loai.attar+news@gmail.com today", "loai.attar+news@gmail.com", "[email]"),
    ("the key is sk-ant-api03-Abc123def456ghi789jkl", "sk-ant-api03-Abc123def456ghi789jkl", "[key]"),
    ("token ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4", "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4", "[key]"),
    ("AWS AKIAIOSFODNN7EXAMPLE leaked", "AKIAIOSFODNN7EXAMPLE", "[key]"),
    ("password=hunter2 and more", "hunter2", "[key]"),
    ("Authorization: Bearer abc.def.ghi", "abc.def.ghi", "[key]"),
    ("id 3f8a9c2e7b1d4f6a8c0e2b4d6f8a0c2e9", "3f8a9c2e7b1d4f6a8c0e2b4d6f8a0c2e9", "[key]"),
    ("call +33 6 12 34 56 78 now", "+33 6 12 34 56 78", "[phone]"),
    ("call 06 12 34 56 78 now", "06 12 34 56 78", "[phone]"),
    ("call (555) 123-4567 now", "(555) 123-4567", "[phone]"),
    ("you paid $1,250.00 for it", "1,250.00", "[amount]"),
    ("the bill is 42,50 € this month", "42,50", "[amount]"),
    ("balance: 300 EUR", "300 EUR", "[amount]"),
    ("it costs 20 dollars", "20 dollars", "[amount]"),
    ("الفاتورة 150 جنيه", "150 جنيه", "[amount]"),
])
def test_private_data_is_replaced_by_a_mark(text, hidden, mark):
    out = privacy.redact(text)
    assert hidden not in out and mark in out


@pytest.mark.parametrize("text", [
    "It's twenty to four.",
    "Saturday 10 October 2026, 15:40",
    "2026-10-10 15:40:12",
    "set a timer for 10 minutes",
    "it's 21 degrees in Cairo",
    "volume 40%",
])
def test_ordinary_text_is_left_alone(text):
    assert privacy.redact(text) == text


def test_redacting_nothing_gives_nothing():
    assert privacy.redact("") == ""


def test_log_lines_are_redacted_with_their_arguments(tmp_path):
    logger = privacy.start_logging(tmp_path, days=30)
    try:
        logging.getLogger("rafiq.test").info("mail from %s about %s", "boss@example.com", "$99")
        for handler in logger.handlers:
            handler.flush()
        text = (tmp_path / "rafiq.log").read_text(encoding="utf-8")
    finally:
        privacy.stop_logging()
    assert "mail from [email] about [amount]" in text
    assert "boss@example.com" not in text and "$99" not in text


def test_logs_older_than_the_limit_are_deleted(tmp_path):
    old, recent, other = tmp_path / "rafiq.log.2026-08-01", tmp_path / "rafiq.log.2026-10-01", tmp_path / "keep.txt"
    for path in (old, recent, other):
        path.write_text("x")
    now = time.time()
    os.utime(old, (now - 40 * 86400, now - 40 * 86400))
    os.utime(other, (now - 40 * 86400, now - 40 * 86400))
    os.utime(recent, (now - 5 * 86400, now - 5 * 86400))
    assert privacy.prune_logs(tmp_path, days=30, now=now) == [old]
    assert not old.exists() and recent.exists() and other.exists()


def test_events_are_logged_redacted(tmp_path):
    privacy.start_logging(tmp_path, days=30)
    try:
        Events().publish("heard", {"text": "email jane@doe.org", "tag": "en"})
        for handler in logging.getLogger("rafiq").handlers:
            handler.flush()
        text = (tmp_path / "rafiq.log").read_text(encoding="utf-8")
    finally:
        privacy.stop_logging()
    assert "heard" in text and "[email]" in text and "jane@doe.org" not in text


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def say(self, text, tag):
        self.said.append((text, tag))


def bare_assistant():
    """An Assistant without a microphone, models or speaker: only what running jobs needs."""
    a = Assistant.__new__(Assistant)
    a.events, a.jobs, a.speaker, a.state = Events(), queue.Queue(), FakeSpeaker(), "idle"
    a.silent, a.muted = False, False
    return a


def test_a_background_result_is_said_redacted():
    a = bare_assistant()
    a.announce("Done: sent the invoice of 300 EUR to jane@doe.org", "en")
    a._run_jobs()
    assert a.speaker.said == [("Done: sent the invoice of [amount] to [email]", "en")]


def test_an_alarm_is_said_as_it_was_set():
    """A reminder is your own words, said back to you: not redacted."""
    a = bare_assistant()
    a.submit("say", text="Reminder: pay 300 EUR", tag="en")
    a._run_jobs()
    assert a.speaker.said == [("Reminder: pay 300 EUR", "en")]
