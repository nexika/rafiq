import queue

import numpy as np
import pytest

from rafiq import voiceid
from rafiq.assistant import Assistant, Events

rng = np.random.default_rng(3)


def voice(center, n, spread=0.05):
    """n embeddings of one speaker: a point on the sphere with a little noise around it."""
    return [center + rng.normal(scale=spread, size=center.shape) for _ in range(n)]


ME, SOMEONE = rng.normal(size=64), rng.normal(size=64)
ME, SOMEONE = ME / np.linalg.norm(ME), SOMEONE / np.linalg.norm(SOMEONE)


def test_fbank_gives_80_bins_every_10_ms_with_the_mean_removed():
    audio = rng.normal(scale=0.1, size=16000).astype("float32")
    feats = voiceid.fbank(audio)
    assert feats.shape == (98, 80)                     # Kaldi's frames: 25 ms windows, 10 ms apart
    assert np.allclose(feats.mean(axis=0), 0, atol=1e-4)


def test_fbank_of_a_tone_peaks_in_the_bin_of_its_pitch():
    t = np.arange(16000) / 16000
    low, high = (voiceid.fbank(0.3 * np.sin(2 * np.pi * f * t), cmn=False)[10] for f in (300, 3000))
    assert np.argmax(low) < 30 < np.argmax(high)


def test_my_voice_matches_and_another_does_not():
    vp, report = voiceid.build(voice(ME, 6))
    assert vp.matches(vp.score(voice(ME, 1)[0]))
    assert not vp.matches(vp.score(voice(SOMEONE, 1)[0]))
    assert report["recordings"] == 6


def test_the_threshold_never_drops_below_the_floor():
    vp, _ = voiceid.build(voice(ME, 4, spread=2.0))    # recordings too unlike each other
    assert vp.threshold >= voiceid.FLOOR


def test_a_voice_print_needs_three_recordings():
    with pytest.raises(ValueError):
        voiceid.build(voice(ME, 2))


def test_the_voice_print_survives_a_save_and_load(tmp_path):
    vp, _ = voiceid.build(voice(ME, 5))
    vp.save(tmp_path)
    again = voiceid.VoicePrint.load(tmp_path)
    assert again.threshold == pytest.approx(vp.threshold)
    assert np.allclose(again.centroid, vp.centroid)


def test_no_voice_print_loads_as_none(tmp_path):
    assert voiceid.VoicePrint.load(tmp_path) is None


def test_measure_counts_false_rejects_and_false_accepts():
    report = voiceid.measure(voice(ME, 8), voice(SOMEONE, 5) + [ME])
    assert report["false_rejects"] == 0 and report["mine"] == 8
    assert report["false_accepts"] == 1 and report["others"] == 6


# ---------------------------------------------------------------- the confirmation question


class FakeEmbedder:
    def __init__(self, embedding):
        self.embedding = embedding

    def __call__(self, audio):
        return self.embedding


def assistant(heard: str, embedding=None, panel=None):
    """An Assistant with no microphone, speaker or models: it hears `heard` once, in a voice whose
    embedding is `embedding`, and the panel's button gives `panel` (None: nobody clicks)."""
    a = Assistant.__new__(Assistant)
    a.events, a.pending_answer, a.said = Events(), queue.Queue(), []
    a.voice_print = voiceid.build(voice(ME, 6))[0] if embedding is not None else None
    a.embedder = FakeEmbedder(embedding) if embedding is not None else None
    a.say = lambda text, tag: a.said.append(text)
    a.set_state = lambda state: None
    utterances = [np.zeros(8000, "float32")]

    def next_utterance(timeout):
        if utterances:
            return utterances.pop()
        if panel is not None and a.said and "panel" in a.said[-1]:
            a.pending_answer.put(panel)
        return None

    a.next_utterance = next_utterance
    a.hear = lambda audio: (heard, "en")
    return a


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr("rafiq.assistant.CONFIRM_SECONDS", 0.3)


def test_without_a_voice_print_any_yes_confirms(quick):
    assert assistant("yes").confirm("shut down", "en") is True


def test_a_yes_in_the_enrolled_voice_confirms(quick):
    assert assistant("yes", embedding=voice(ME, 1)[0]).confirm("shut down", "en") is True


def test_a_yes_in_another_voice_does_not_confirm(quick):
    a = assistant("yes", embedding=SOMEONE)
    assert a.confirm("shut down", "en") is False
    assert any("panel" in s for s in a.said)


def test_after_an_unsure_voice_the_panels_yes_still_confirms(quick):
    assert assistant("yes", embedding=SOMEONE, panel=True).confirm("shut down", "en") is True


def test_a_no_cancels_whoever_says_it(quick):
    assert assistant("no", embedding=SOMEONE).confirm("shut down", "en") is False


def test_the_voice_check_is_shown_in_the_panel(quick):
    a = assistant("yes", embedding=SOMEONE)
    a.confirm("shut down", "en")
    checks = [e for e in a.events.history if e["kind"] == "voice"]
    assert checks and checks[0]["match"] is False and "score" in checks[0] and "threshold" in checks[0]


def test_the_voice_print_is_built_from_the_enroll_recordings(tmp_path):
    from rafiq import kws
    from rafiq.enroll import save_wav

    for i in range(5):
        save_wav(kws.folder() / f"name-{i}.wav", np.full(1600, 0.1 * (i + 1), "float32"))
    stranger = tmp_path / "others"
    stranger.mkdir()
    save_wav(stranger / "video.wav", np.full(1600, -0.5, "float32"))

    def embed(audio):    # quiet clips are me, the negative one a stranger
        return voice(ME, 1)[0] if audio.mean() > 0 else SOMEONE

    report = voiceid.from_recordings(stranger, embedder=embed)
    assert report["mine"] == 5 and report["false_rejects"] == 0
    assert report["others"] == 1 and report["false_accepts"] == 0
    assert voiceid.VoicePrint.load() is not None


def test_no_voice_print_without_recordings():
    with pytest.raises(ValueError):
        voiceid.from_recordings(embedder=lambda audio: ME)


class FakeSession:
    def __init__(self, out):
        self.out, self.frames = out, None

    def run(self, _names, inputs):
        self.frames = inputs["feats"].shape[1]
        return [np.array([self.out], "float32")]


def test_a_short_yes_is_stretched_to_a_second_before_it_is_embedded():
    e = voiceid.Embedder.__new__(voiceid.Embedder)
    e.session = FakeSession([3.0, 4.0])
    assert np.allclose(e(rng.normal(scale=0.1, size=4000)), [0.6, 0.8])
    assert e.session.frames >= voiceid.MIN_FRAMES


def test_a_broken_embedding_matches_nobody():
    e = voiceid.Embedder.__new__(voiceid.Embedder)
    e.session = FakeSession([np.nan, 1.0])
    emb = e(rng.normal(scale=0.1, size=16000))
    assert not np.any(emb)
    vp = voiceid.VoicePrint(voiceid.unit(np.ones(len(emb))), voiceid.FLOOR)
    assert not vp.matches(vp.score(emb))
