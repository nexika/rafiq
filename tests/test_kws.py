import io
import wave
from pathlib import Path

import numpy as np
import pytest

from rafiq import kws

rng = np.random.default_rng(7)


def word(n=20):
    return rng.normal(size=(n, 32))


def test_the_same_sequence_matches_perfectly():
    t = word()
    assert kws.sdtw(t, t) == pytest.approx(0.0)


def test_the_name_is_found_after_a_greeting_near_the_start():
    t, hey = word(), word(8)
    assert kws.sdtw(t, np.concatenate([hey, t, word(30)])) == pytest.approx(0.0)


def test_a_match_must_start_near_the_beginning():
    t = word()
    late = np.concatenate([word(60), t])
    assert kws.sdtw(t, late, max_start=50) > 0.5
    assert kws.sdtw(t, late, max_start=70) == pytest.approx(0.0)


def test_said_slower_still_matches():
    t = word()
    slow = np.repeat(t, 2, axis=0)            # every frame twice: half the speed
    assert kws.sdtw(t, slow) < 0.05


def test_a_different_word_does_not_match():
    assert kws.sdtw(word(), word(40)) > 0.8


def test_the_threshold_lies_between_the_name_and_ordinary_speech():
    base = word()
    names = [base + rng.normal(scale=0.2, size=base.shape) for _ in range(4)]
    others = [word(40) for _ in range(3)]
    spotter, report = kws.calibrate(names, others)
    assert report["name_spread"] < report["threshold"] < report["ordinary_closest"]
    assert spotter.score(base + rng.normal(scale=0.2, size=base.shape)) < spotter.threshold
    assert spotter.score(word(40)) > spotter.threshold


def test_too_few_recordings_are_refused():
    with pytest.raises(ValueError):
        kws.calibrate([word(), word()], [])


def test_a_spotter_survives_a_restart(tmp_path):
    base = word()
    spotter, _ = kws.calibrate([base + rng.normal(scale=0.1, size=base.shape) for _ in range(3)], [word(30)])
    spotter.save(tmp_path)
    again = kws.Spotter.load(tmp_path)
    assert again.threshold == pytest.approx(spotter.threshold)
    assert all(np.allclose(a, b) for a, b in zip(again.templates, spotter.templates, strict=True))
    assert kws.Spotter.load(tmp_path / "missing") is None


# ---------------------------------------------------------------------- with real models, when present

DATA = Path.home() / ".config" / "rafiq" / "data"
VOICE = DATA / "piper" / "en_US-ryan-medium.onnx"
MEL = DATA / "oww" / "melspectrogram.onnx"


@pytest.mark.skipif(not (VOICE.is_file() and MEL.is_file()), reason="needs a Piper voice and the mel model")
def test_synthetic_voice_wakes_on_the_name_and_not_on_look_alikes():
    from piper import PiperVoice
    from piper.config import SynthesisConfig
    from scipy.signal import resample_poly

    from rafiq.audio import SileroVAD
    from rafiq.enroll import fit, trim

    voice, vad, mel = PiperVoice.load(str(VOICE)), SileroVAD(), kws.Mel(MEL)

    def say(text, speed=1.0):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            voice.synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=speed))
        buf.seek(0)
        with wave.open(buf) as w:
            rate = w.getframerate()
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype("float32") / 32768
        audio = resample_poly(audio, 16000, rate).astype("float32")
        return np.concatenate([np.zeros(4000, "float32"), audio, np.zeros(8000, "float32")])

    names = [trim(say(f, s), vad) for f in ("Rafiq.", "Hey Rafiq.") for s in (0.9, 1.0, 1.15)]
    others = [trim(say(t), vad) for t in ("What's the weather like today?", "Remind me to call my friend.")]
    spotter, _ = fit(names, others, mel)
    for text in ("Rafiq, what time is it?", "Hey Rafiq, open Firefox."):
        assert spotter.score(mel(trim(say(text), vad)[:40000])) <= spotter.threshold, text
    for text in ("He's a terrific guy.", "I was just talking about dinner.", "Remind me later."):
        assert spotter.score(mel(trim(say(text), vad)[:40000])) > spotter.threshold, text
