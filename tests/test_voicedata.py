"""Recording Rafiq's own voice (#250): the microphone, the speaker and the keyboard are faked."""

import re
import wave

import numpy as np
import pytest

from rafiq import __main__ as cli
from rafiq import lang, voicedata

RATE = voicedata.RATE


def speech(seconds=1.0, peak=0.5, silence=0.5):
    """A tone between two stretches of silence, as a take with the key presses around it."""
    t = np.arange(int(seconds * RATE)) / RATE
    quiet = np.zeros(int(silence * RATE), dtype=np.float32)
    return np.concatenate([quiet, (peak * np.sin(2 * np.pi * 220 * t)).astype(np.float32), quiet])


class Session:
    """The keyboard answers in order; each recording hands back the next take."""

    def __init__(self, answers, takes=()):
        self.answers, self.takes, self.said, self.played, self.recorded = list(answers), list(takes), [], [], 0

    def ask(self, prompt=""):
        return self.answers.pop(0) if self.answers else "q"

    def record(self, ask):
        self.recorded += 1
        return self.takes.pop(0)

    def run(self, folder, sentences, tag="en"):
        extra = folder.parent / "list.txt"
        extra.write_text("\n".join(sentences), encoding="utf-8")
        return voicedata.run(tag, folder, extra, ask=self.ask, record=self.record, listen=self.played.append,
                             say=self.said.append)


@pytest.fixture
def no_phrases(monkeypatch):
    monkeypatch.setattr(lang, "PHRASES", {})


def test_a_kept_take_is_saved_as_piper_s_trainer_reads_it(tmp_path, no_phrases):
    folder = tmp_path / "voice"
    Session(["", ""], [speech()]).run(folder, ["The weather is mild today."])
    sid = voicedata.sentence_id("en", "The weather is mild today.")
    assert (folder / "metadata.csv").read_text(encoding="utf-8") == f"{sid}.wav|The weather is mild today.\n"
    with wave.open(str(folder / "wavs" / f"{sid}.wav"), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (22050, 1, 2)
        # The second of speech, with 0.2 s of the silence kept on each side.
        assert abs(w.getnframes() / RATE - 1.4) < 0.05


def test_a_second_session_starts_at_the_first_sentence_not_recorded(tmp_path, no_phrases):
    folder = tmp_path / "voice"
    Session(["", "", "q"], [speech()]).run(folder, ["One.", "Two."])
    later = Session(["", ""], [speech()])
    later.run(folder, ["One.", "Two."])
    assert later.recorded == 1 and any("[2/2] Two." in s for s in later.said)
    assert voicedata.recorded(folder) == {voicedata.sentence_id("en", "One."), voicedata.sentence_id("en", "Two.")}


def test_a_recording_follows_its_words_when_the_list_is_reordered(tmp_path, no_phrases):
    folder = tmp_path / "voice"
    Session(["", "", "q"], [speech()]).run(folder, ["One.", "Two."])
    later = Session(["", ""], [speech()])
    later.run(folder, ["Two.", "One."])
    assert later.recorded == 1 and any("Two." in s and "[2/2]" in s for s in later.said)


@pytest.mark.parametrize("bad, why", [(speech(peak=1.0), "clipped"), (speech(peak=0.02), "too quiet"),
                                      (np.zeros(RATE, dtype=np.float32), "nothing was heard")])
def test_a_take_that_can_t_be_trained_on_is_asked_for_again(tmp_path, no_phrases, bad, why):
    folder = tmp_path / "voice"
    session = Session(["", "", ""], [bad, speech()])
    session.run(folder, ["Again, please."])
    assert session.recorded == 2 and any(why in s for s in session.said) and len(voicedata.recorded(folder)) == 1


def test_play_lets_the_speaker_hear_the_take_before_keeping_it(tmp_path, no_phrases):
    session = Session(["", "p", ""], [speech()])
    session.run(tmp_path / "voice", ["Listen to this."])
    assert len(session.played) == 1 and len(voicedata.recorded(tmp_path / "voice")) == 1


def test_retry_throws_the_take_away(tmp_path, no_phrases):
    session = Session(["", "r", "", ""], [speech(), speech()])
    session.run(tmp_path / "voice", ["Once more."])
    assert session.recorded == 2 and len(list((tmp_path / "voice" / "wavs").glob("*.wav"))) == 1


def test_skip_moves_on_and_quit_keeps_what_was_kept(tmp_path, no_phrases):
    folder = tmp_path / "voice"
    session = Session(["s", "", "", "q"], [speech()])
    session.run(folder, ["Skipped.", "Kept.", "Never reached."])
    assert voicedata.recorded(folder) == {voicedata.sentence_id("en", "Kept.")}


def test_the_sentences_add_rafiq_s_own_phrases_without_placeholders(tmp_path, monkeypatch):
    monkeypatch.setattr(lang, "PHRASES", {"a": {"en": "Yes?"}, "b": {"en": "Should I {action}?"},
                                          "c": {"en": "From  the list."}, "d": {"fr": "Oui ?"}})
    extra = tmp_path / "list.txt"
    extra.write_text("# a comment\n\nFrom the list.\nA | pipe.\n", encoding="utf-8")
    assert voicedata.sentences("en", extra) == ["From the list.", "A pipe.", "Yes?"]


def test_a_language_without_a_list_says_where_to_put_one(tmp_path, no_phrases):
    said = []
    assert voicedata.run("xx", tmp_path / "voice", ask=lambda p="": pytest.fail("asked"), say=said.append) == 1
    assert "xx.txt" in said[0]


@pytest.mark.parametrize("tag", lang.TAGS)
def test_each_shipped_list_writes_numbers_as_said_and_has_enough_to_train_on(tag):
    path = voicedata.LISTS / f"{tag}.txt"
    lines = voicedata.sentences(tag, path)
    assert len(lines) >= 200
    assert not [s for s in lines if re.search(r"[0-9٠-٩|%&@#$]", s)]
    if tag.startswith("ar"):
        assert all(re.search(r"[؀-ۿ]", s) for s in lines)


def test_the_shipped_lists_are_installed_with_the_package():
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert "voice_sentences/*.txt" in pyproject["tool"]["setuptools"]["package-data"]["rafiq"]


def test_rafiq_record_voice_runs_the_recorder_for_the_language(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(voicedata, "run", lambda tag, folder=None, extra=None, **kw: seen.update(
        tag=tag, folder=folder, extra=extra) or 0)
    assert cli.main(["record-voice", "ar-EG", "--out", str(tmp_path)]) == 0
    assert seen == {"tag": "ar-EG", "folder": tmp_path, "extra": None}
