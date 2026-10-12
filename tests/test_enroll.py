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
