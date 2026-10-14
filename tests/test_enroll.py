import numpy as np

from rafiq import enroll, kws
from rafiq.audio import RATE


def speech(peak: float, seconds: float = 0.7) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (peak * np.sin(2 * np.pi * 220 * t)).astype("float32")


def clipped(seconds: float = 0.7) -> np.ndarray:
    """Speech amplified past full scale, as a boosted laptop microphone records it."""
    return np.clip(speech(3.0, seconds), -1.0, 1.0)


def test_a_clipped_recording_is_too_loud():
    assert enroll.too_loud(clipped())


def test_a_clean_recording_is_not_too_loud():
    assert not enroll.too_loud(speech(0.44))


def test_a_single_full_scale_click_is_not_too_loud():
    clip = speech(0.3)
    clip[100] = 1.0
    assert not enroll.too_loud(clip)


class FakeMic:
    def start(self):
        pass

    def stop(self):
        pass


class FakeSpotter:
    def save(self, folder):
        pass


def test_a_clipped_take_is_asked_again_and_not_saved(monkeypatch):
    takes = iter([clipped(), speech(0.3), speech(0.3), speech(0.3)])
    monkeypatch.setattr(enroll, "record_one", lambda mic, vad, timeout=8.0: next(takes))
    monkeypatch.setattr(enroll.Microphone, "from_config", classmethod(lambda cls, cfg: FakeMic()))
    monkeypatch.setattr(enroll, "SileroVAD", lambda: None)
    monkeypatch.setattr(kws, "Mel", lambda: None)
    fitted = {}

    def fit(names, others, mel, request_clips=(), name_tags=()):
        fitted["names"] = names
        return FakeSpotter(), {"name_spread": 0.3, "ordinary_closest": None, "threshold": 0.4}

    monkeypatch.setattr(enroll, "fit", fit)
    monkeypatch.setattr(enroll, "REQUESTS", [])
    monkeypatch.setattr(enroll, "SENTENCES", [])
    said = []

    enroll.run(forms=["Rafiq"], takes=3, say=said.append, ask=lambda prompt: "")

    assert len(fitted["names"]) == 3
    assert not any(enroll.too_loud(clip) for clip in fitted["names"])
    assert any("too loud" in line for line in said)
    assert sorted(p.name for p in kws.folder().glob("*.wav")) == ["name-1.wav", "name-2.wav", "name-3.wav"]


def test_a_name_form_in_arabic_script_is_tagged_arabic():
    assert enroll.form_tag("يا رفيق") == "ar"


def test_a_name_form_in_latin_letters_says_nothing_about_the_language():
    assert enroll.form_tag("Rafiq") is None


def test_enroll_records_the_other_names_from_the_settings(rafiq_home, monkeypatch):
    rafiq_home.mkdir(parents=True, exist_ok=True)
    (rafiq_home / "config.toml").write_text('[wake]\nnames = ["Jarvis"]\n', encoding="utf-8")
    monkeypatch.setattr(enroll, "record_one", lambda mic, vad, timeout=8.0: speech(0.3))
    monkeypatch.setattr(enroll.Microphone, "from_config", classmethod(lambda cls, cfg: FakeMic()))
    monkeypatch.setattr(enroll, "SileroVAD", lambda: None)
    monkeypatch.setattr(kws, "Mel", lambda: None)
    monkeypatch.setattr(enroll, "fit", lambda *a, **k: (FakeSpotter(), {"name_spread": 0.3, "ordinary_closest": None,
                                                                        "threshold": 0.4}))
    monkeypatch.setattr(enroll, "REQUESTS", [])
    monkeypatch.setattr(enroll, "SENTENCES", [])
    prompts = []

    enroll.run(takes=1, say=lambda line: None, ask=prompts.append)

    assert [p for p in prompts if "Jarvis" in p] and [p for p in prompts if "يا رفيق" in p]


def enroll_with_report(monkeypatch, report, sentences):
    monkeypatch.setattr(enroll, "record_one", lambda mic, vad, timeout=8.0: speech(0.3))
    monkeypatch.setattr(enroll.Microphone, "from_config", classmethod(lambda cls, cfg: FakeMic()))
    monkeypatch.setattr(enroll, "SileroVAD", lambda: None)
    monkeypatch.setattr(kws, "Mel", lambda: None)
    monkeypatch.setattr(enroll, "fit", lambda *a, **k: (FakeSpotter(), report))
    monkeypatch.setattr(enroll, "REQUESTS", [])
    monkeypatch.setattr(enroll, "SENTENCES", sentences)
    said = []
    enroll.run(forms=["Rafiq"], takes=1, say=said.append, ask=lambda prompt: "")
    return "\n".join(said)


def test_enroll_names_the_sentence_that_came_as_close_as_the_name(monkeypatch):
    said = enroll_with_report(monkeypatch, {"name_spread": 0.562, "ordinary_closest": 0.518,
                                            "ordinary_closest_index": 1, "threshold": 0.49},
                              ["Remind me to call my friend later.", "What's the weather like today?"])
    assert "0.52" in said
    assert "What's the weather like today?" in said.split("close")[-1]


def test_enroll_says_nothing_when_ordinary_speech_is_far_from_the_name(monkeypatch):
    said = enroll_with_report(monkeypatch, {"name_spread": 0.3, "ordinary_closest": 0.6,
                                            "ordinary_closest_index": 0, "threshold": 0.43},
                              ["What's the weather like today?"])
    assert "enroll` again" not in said
