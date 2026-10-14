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


def test_align_says_where_the_match_ends():
    t = word(20)
    score, end = kws.align(t, np.concatenate([word(5), t, word(30)]))
    assert score == pytest.approx(0.0) and end == 24


def test_the_name_is_cut_out_of_a_request_recording():
    t = word(20)
    request = np.concatenate([t, word(40)])          # "Rafiq" then the request
    cut = kws.name_in_context([word(20), t], request)
    assert len(cut) == 20 and np.allclose(cut, t)


def test_the_threshold_lies_between_the_name_and_ordinary_speech():
    base = word()
    names = [base + rng.normal(scale=0.2, size=base.shape) for _ in range(4)]
    others = [word(40) for _ in range(3)]
    spotter, report = kws.calibrate(names, others)
    assert report["name_spread"] < report["threshold"] < report["ordinary_closest"]
    assert spotter.score(base + rng.normal(scale=0.2, size=base.shape)) < spotter.threshold
    assert spotter.score(word(40)) > spotter.threshold


def overlapping():
    """Loose name takes, and one ordinary sentence closer to them than they are to each other."""
    base = word()
    names = [base + rng.normal(scale=0.6, size=base.shape) for _ in range(4)]
    return names, [base + rng.normal(scale=0.05, size=base.shape), word(40)]


def test_ordinary_speech_as_close_as_the_name_keeps_the_threshold_below_it():
    names, others = overlapping()
    spotter, report = kws.calibrate(names, others)
    closest = spotter.score(others[0])
    assert closest <= report["name_spread"]              # the case from #175
    assert spotter.threshold < closest


def test_the_report_gives_the_real_closest_sentence():
    names, others = overlapping()
    spotter, report = kws.calibrate(names, others)
    assert report["ordinary_closest"] == pytest.approx(min(spotter.score(o) for o in others))
    assert report["ordinary_closest_index"] == 0


def test_the_loosest_sensitivity_still_stays_below_every_ordinary_sentence():
    base = word()
    names = [base + rng.normal(scale=0.2, size=base.shape) for _ in range(4)]
    spotter, report = kws.calibrate(names, [word(40) for _ in range(3)], sensitivity=1.0)
    assert spotter.threshold < report["ordinary_closest"]


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
        with wave.open(buf, "wb") as w:   # no random variation: the same audio every run
            voice.synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=speed, noise_scale=0.0,
                                                                     noise_w_scale=0.0))
        buf.seek(0)
        with wave.open(buf) as w:
            rate = w.getframerate()
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype("float32") / 32768
        audio = resample_poly(audio, 16000, rate).astype("float32")
        return np.concatenate([np.zeros(4000, "float32"), audio, np.zeros(8000, "float32")])

    names = [trim(say(f, s), vad) for f in ("Rafiq.", "Hey Rafiq.") for s in (0.9, 1.0, 1.15)]
    requests = [trim(say(t), vad) for t in ("Rafiq, turn up the volume.", "Hey Rafiq, tell me a joke.")]
    others = [trim(say(t), vad) for t in ("What's the weather like today?", "Remind me to call my friend.")]
    spotter, _ = fit(names, others, mel, request_clips=requests)
    for text in ("Rafiq, what time is it?", "Hey Rafiq, open Firefox."):
        assert spotter.score(mel(trim(say(text), vad)[:40000])) <= spotter.threshold, text
    for text in ("He's a terrific guy.", "I was just talking about dinner.", "Remind me later."):
        assert spotter.score(mel(trim(say(text), vad)[:40000])) > spotter.threshold, text


# ---------------------------------------------------------------------- the language of the name


def test_the_closest_recording_tells_the_language_the_name_was_called_in():
    arabic, latin = word(), word()
    spotter = kws.Spotter([arabic, latin], np.zeros(32), np.ones(32), threshold=1.0, tags=["ar", None])
    assert spotter.best(arabic)[1] == "ar"
    assert spotter.best(latin)[1] is None


def test_the_language_of_each_recording_survives_a_restart(tmp_path):
    base = word()
    names = [base + rng.normal(scale=0.1, size=base.shape) for _ in range(3)]
    spotter, _ = kws.calibrate(names, [word(30)], name_tags=["ar", "ar", None])
    spotter.save(tmp_path)
    assert kws.Spotter.load(tmp_path).tags == ["ar", "ar", None]


def test_recordings_saved_before_languages_were_kept_still_load(tmp_path):
    spotter, _ = kws.calibrate([word() for _ in range(3)], [word(30)])
    spotter.save(tmp_path)
    (tmp_path / "calibration.json").write_text('{"threshold": 0.5}')
    again = kws.Spotter.load(tmp_path)
    assert again.tags == [None, None, None]
    assert again.best(again.templates[0])[1] is None


def wake_with(tag, conversation):
    from rafiq.assistant import Assistant

    a = object.__new__(Assistant)
    a.spotter = type("S", (), {"threshold": 1.0, "best": lambda self, frames: (0.2, tag)})()
    a.mel = lambda audio: audio
    a.events = type("E", (), {"publish": lambda self, *args: None})()
    a.name_seconds, a.tag = 0.8, conversation
    greeted = []
    a._listen_for_request = lambda greet=True: greeted.append(a.tag)
    a._spot_with_recordings(np.zeros(16000, dtype="float32"))
    return greeted[0]


def test_the_name_called_in_arabic_is_answered_in_arabic():
    assert wake_with("ar", "en") == "ar"


def test_an_egyptian_conversation_stays_egyptian_when_called_in_arabic():
    assert wake_with("ar", "ar-EG") == "ar-EG"


def test_the_name_said_in_latin_letters_keeps_the_conversation_language():
    assert wake_with(None, "fr") == "fr"


def test_the_whisper_wake_listener_answers_to_another_name_from_the_settings():
    from rafiq.assistant import Assistant

    a = object.__new__(Assistant)
    a.follow_up_until, a.spotter, a.wake_names, a.tag = 0.0, None, ("Jarvis",), "en"
    prompts = []

    class WakeSTT:
        def transcribe(self, audio, beam_size, hotwords, prompt):
            prompts.append(prompt)
            return type("H", (), {"text": "Jarvis, what time is it?", "lang": "en"})()

    a.wake_stt = WakeSTT()
    a.events = type("E", (), {"publish": lambda self, *args: None})()
    a.hear = lambda audio: ("Jarvis, what time is it?", "en")
    asked = []
    a.respond = lambda text, tag: asked.append(text)

    a.handle_idle_utterance(np.zeros(16000 * 3, dtype="float32"))

    assert asked == ["what time is it?"]
    assert "Jarvis" in prompts[0]
