"""Egyptian Arabic mixed with English words: "افتحلي الـ settings"."""

import tomllib
from pathlib import Path

import numpy as np
import pytest

from rafiq import lang, stt

SENTENCES = tomllib.loads((Path(__file__).parent / "data" / "mixed_sentences.toml").read_text(encoding="utf-8"))
CASES = [(s["said"], heard) for s in SENTENCES["sentence"] for heard in s["heard"] + [s["said"]]]


@pytest.mark.parametrize("said, heard", CASES)
def test_english_words_written_in_arabic_script_come_back_in_latin_letters(said, heard):
    assert lang.normalize_arabic(lang.latinize(heard)) == lang.normalize_arabic(said)


@pytest.mark.parametrize("said", [s["said"] for s in SENTENCES["sentence"]])
def test_mixed_sentences_are_heard_as_egyptian(said):
    assert lang.dialect("ar", said) == "ar-EG"


@pytest.mark.parametrize("text", [
    "ما هو الطقس اليوم في القاهرة", "الساعة كام دلوقتي", "عايز أعرف الجو النهارده", "يا رفيق", "",
])
def test_plain_arabic_is_left_alone(text):
    assert lang.latinize(text) == text


def test_word_error_rate():
    assert stt.word_error_rate("افتحلي الـ settings", "افتحلي الـ settings") == 0.0
    assert stt.word_error_rate("افتحلي الـ settings", "افتحلي السيتنجز") == pytest.approx(2 / 3)
    assert stt.word_error_rate("ابعت email لسارة", "") == 1.0
    # spelling that varies in speech-to-text output is not an error
    assert stt.word_error_rate("ابعت لسارة", "إبعت لساره") == 0.0


class FakeSegment:
    def __init__(self, text):
        self.text, self.no_speech_prob = text, 0.0
        self.avg_logprob, self.compression_ratio = -0.2, 1.2


class FakeModel:
    def __init__(self, text, lang="ar"):
        self.text, self.lang, self.calls = text, lang, []

    def detect_language(self, audio):
        return None, None, [(self.lang, 0.9)]

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return [FakeSegment(self.text)], None


def transcriber(model, mixed=True):
    t = stt.Transcriber("small", mixed=mixed)
    t._model = model
    return t


def test_arabic_is_transcribed_with_a_prompt_that_keeps_english_words_in_latin_letters():
    model = FakeModel("افتحلي السيتنجز")
    heard = transcriber(model).transcribe(np.zeros(16000, dtype=np.float32))
    assert model.calls[0]["initial_prompt"] == stt.MIXED_PROMPT
    assert heard.text == "افتحلي الـ settings"
    assert heard.lang == "ar"


def test_other_languages_and_the_wake_listener_get_no_mixed_prompt():
    model = FakeModel("open the settings", lang="en")
    transcriber(model).transcribe(np.zeros(16000, dtype=np.float32))
    assert model.calls[0]["initial_prompt"] is None
    model = FakeModel("يا رفيق")
    transcriber(model).transcribe(np.zeros(16000, dtype=np.float32), prompt=stt.WAKE_PROMPT)
    assert model.calls[0]["initial_prompt"] == stt.WAKE_PROMPT


def test_mixed_handling_can_be_turned_off():
    model = FakeModel("افتحلي السيتنجز")
    heard = transcriber(model, mixed=False).transcribe(np.zeros(16000, dtype=np.float32))
    assert model.calls[0]["initial_prompt"] is None
    assert heard.text == "افتحلي السيتنجز"


def test_a_folder_of_recordings_is_scored_against_its_transcripts(tmp_path):
    from rafiq.enroll import save_wav

    for name, text in (("settings", "افتحلي الـ settings"), ("email", "ابعت email لسارة")):
        save_wav(tmp_path / f"{name}.wav", np.zeros(8000, dtype=np.float32))
        (tmp_path / f"{name}.txt").write_text(text + "\n", encoding="utf-8")
    save_wav(tmp_path / "no-transcript.wav", np.zeros(8000, dtype=np.float32))
    results = stt.score(tmp_path, transcriber(FakeModel("افتحلي السيتنجز")))
    assert [(r.name, r.heard) for r in results] == [("email", "افتحلي الـ settings"),
                                                     ("settings", "افتحلي الـ settings")]
    assert results[0].error == 1.0
    assert results[1].error == 0.0
