import numpy as np
import pytest

from rafiq.audio import (
    FRAME,
    RATE,
    Calibration,
    Endpointer,
    Microphone,
    Resampler,
    calibrate,
    describe_inputs,
    measure_noise,
    pick_rate,
    resolve_device,
    rms,
)


def noise(n, level, seed=0):
    rng = np.random.default_rng(seed)
    return [rng.normal(0, level, FRAME).astype("float32") for _ in range(n)]


def test_a_quiet_room_keeps_the_usual_thresholds():
    cal = calibrate(noise(30, 0.001), [0.02] * 30, threshold=0.5, end_seconds=0.7)
    assert cal.threshold == 0.5 and cal.end_seconds == 0.7
    assert cal.noise_rms == pytest.approx(0.001, rel=0.2)
    assert cal.rms_gate > cal.noise_rms


def test_a_noisy_room_raises_the_speech_threshold_and_waits_longer_for_the_end():
    probs = [0.1] * 20 + [0.45] * 10                     # the fan or the TV sometimes looks like speech
    cal = calibrate(noise(30, 0.05), probs, threshold=0.5, end_seconds=0.7)
    assert 0.5 < cal.threshold <= 0.9
    assert cal.end_seconds > 0.7
    assert cal.rms_gate > 0.05


def test_the_threshold_never_goes_out_of_reach():
    cal = calibrate(noise(30, 0.2), [0.99] * 30, threshold=0.5, end_seconds=0.7)
    assert cal.threshold <= 0.9


def test_calibration_with_nothing_heard_changes_nothing():
    cal = calibrate([], [], threshold=0.5, end_seconds=0.7)
    assert cal == Calibration(threshold=0.5, end_seconds=0.7, rms_gate=0.0, noise_rms=0.0)


def test_the_endpointer_ignores_frames_quieter_than_the_room():
    ep = Endpointer(start_frames=2)
    ep.apply(Calibration(threshold=0.5, end_seconds=0.7, rms_gate=0.05, noise_rms=0.02))
    assert all(ep.push(f, 0.9) is None for f in noise(10, 0.01))   # "speech" as quiet as the fan
    assert not ep.active
    for f in noise(3, 0.2):
        ep.push(f, 0.9)
    assert ep.active


def test_the_endpointer_takes_the_calibrated_end_silence():
    ep = Endpointer()
    ep.apply(Calibration(threshold=0.6, end_seconds=0.9, rms_gate=0.0, noise_rms=0.0))
    assert ep.threshold == 0.6 and ep.end_seconds == 0.9


class FakeMic:
    def __init__(self, frames):
        self.frames = list(frames)

    def drain(self):
        pass

    def read(self, timeout=0.5):
        return self.frames.pop(0) if self.frames else None


class FakeVAD:
    def __init__(self, prob):
        self.prob, self.resets = prob, 0

    def reset(self):
        self.resets += 1

    def __call__(self, frame):
        return self.prob


def test_measuring_the_room_reads_about_a_second_through_the_vad():
    vad = FakeVAD(0.3)
    frames, probs = measure_noise(FakeMic(noise(100, 0.01)), vad, seconds=1.0)
    assert len(frames) == len(probs) == round(RATE / FRAME)
    assert set(probs) == {0.3} and vad.resets >= 1


def test_measuring_stops_when_the_microphone_is_silent():
    frames, probs = measure_noise(FakeMic(noise(5, 0.01)), FakeVAD(0.1), seconds=1.0, timeout=0.01)
    assert len(frames) == 5


def test_16_khz_is_used_when_the_device_takes_it():
    assert pick_rate(lambda rate: True) == RATE


def test_another_rate_is_picked_when_16_khz_is_refused():
    assert pick_rate(lambda rate: rate in (44100, 48000)) == 48000
    assert pick_rate(lambda rate: rate == 44100) == 44100


def test_the_configured_rate_comes_first():
    assert pick_rate(lambda rate: True, preferred=44100) == 44100
    assert pick_rate(lambda rate: rate == 48000, preferred=44100) == 48000


def test_no_supported_rate_is_an_error():
    with pytest.raises(RuntimeError):
        pick_rate(lambda rate: False)


@pytest.mark.parametrize("rate", [16000, 48000, 44100, 22050])
def test_audio_from_the_device_comes_out_as_16_khz_frames(rate):
    r = Resampler(rate)
    seconds = 1.0
    t = np.arange(int(rate * seconds)) / rate
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype("float32")
    out = []
    for i in range(0, len(tone), r.blocksize):
        out += r.push(tone[i:i + r.blocksize])
    assert all(len(f) == FRAME and f.dtype == np.float32 for f in out)
    total = len(out) * FRAME
    assert abs(total - RATE * seconds) <= 2 * FRAME
    joined = np.concatenate(out)[FRAME:-FRAME]
    spectrum = np.abs(np.fft.rfft(joined))
    peak = np.argmax(spectrum) * RATE / len(joined)
    assert peak == pytest.approx(440, abs=5)


@pytest.mark.parametrize("rate", [48000, 44100])
def test_resampling_block_by_block_matches_resampling_it_all_at_once(rate):
    from scipy.signal import resample_poly

    r = Resampler(rate)
    rng = np.random.default_rng(1)
    sound = rng.normal(0, 0.1, rate).astype("float32")
    out = []
    for i in range(0, len(sound), r.blocksize):
        out += r.push(sound[i:i + r.blocksize])
    got = np.concatenate(out)
    want = resample_poly(sound, r.up, r.down)              # sample for sample: no clicks at block edges
    middle = slice(2 * FRAME, len(got) - 2 * FRAME)
    assert np.max(np.abs(got[middle] - want[middle])) < 1e-6


def test_the_microphone_resamples_what_the_device_sends():
    mic = Microphone(rate=48000)
    block = np.zeros((mic.resampler.blocksize, 1), dtype="float32")
    for _ in range(2):                                      # the first block waits for a little look-ahead
        mic._callback(block, len(block), None, None)
    frame = mic.read(timeout=0)
    assert frame is not None and len(frame) == FRAME


DEVICES = [
    {"name": "HDA Intel PCH: ALC257 Analog", "max_input_channels": 2, "default_samplerate": 44100.0},
    {"name": "HDMI 0", "max_input_channels": 0, "default_samplerate": 48000.0},
    {"name": "Blue Yeti USB", "max_input_channels": 1, "default_samplerate": 48000.0},
]


def test_the_device_is_chosen_by_index_or_part_of_its_name():
    assert resolve_device("", DEVICES) is None              # the system default
    assert resolve_device(2, DEVICES) == 2
    assert resolve_device("2", DEVICES) == 2
    assert resolve_device("yeti", DEVICES) == 2


def test_a_device_that_does_not_record_or_does_not_exist_is_an_error():
    with pytest.raises(ValueError):
        resolve_device("HDMI", DEVICES)
    with pytest.raises(ValueError):
        resolve_device("Rode", DEVICES)
    with pytest.raises(ValueError):
        resolve_device(1, DEVICES)


def test_doctor_lists_the_inputs_and_marks_the_chosen_one():
    lines = describe_inputs(DEVICES, chosen=2)
    assert len(lines) == 2
    assert lines[1].startswith("*") and "Blue Yeti USB" in lines[1] and "[2]" in lines[1]
    assert not lines[0].startswith("*")


def test_rms_of_a_frame():
    assert rms(np.full(FRAME, 0.5, dtype="float32")) == pytest.approx(0.5)


def test_the_assistant_fits_its_endpointer_to_the_room_and_says_so():
    from rafiq import config
    from rafiq.assistant import Assistant, Events

    assistant = object.__new__(Assistant)                   # no models, no microphone
    assistant.cfg = config.load()
    assistant.mic = FakeMic(noise(40, 0.05))
    assistant.mic.rate = 48000
    assistant.vad = FakeVAD(0.6)
    assistant.endpointer = Endpointer()
    assistant.events = Events()
    assistant.calibrate()
    assert assistant.endpointer.threshold > 0.5 and assistant.endpointer.rms_gate > 0
    event = assistant.events.history[-1]
    assert event["kind"] == "calibrated" and event["rate"] == 48000


class FakeSoundDevice:
    class default:
        device = (0, 1)

    def __init__(self, rates):
        self.rates = rates

    def query_devices(self):
        return DEVICES

    def check_input_settings(self, device=None, samplerate=None, **_kw):
        if samplerate not in self.rates:
            raise RuntimeError("Invalid sample rate")


def test_doctor_shows_the_microphone_its_rate_and_the_others(monkeypatch, capsys):
    import sys

    from rafiq import __main__ as cli
    from rafiq import config

    monkeypatch.setitem(sys.modules, "sounddevice", FakeSoundDevice({44100, 48000}))
    cfg = config.load()
    cfg["audio"]["device"] = "yeti"
    cli._check_microphone(cfg)
    out = capsys.readouterr().out
    assert "Blue Yeti USB at 48000 Hz, resampled to 16 kHz" in out
    assert "* [2] Blue Yeti USB" in out and "[0] HDA Intel" in out


def test_doctor_says_when_the_configured_microphone_is_missing(monkeypatch, capsys):
    import sys

    from rafiq import __main__ as cli
    from rafiq import config

    monkeypatch.setitem(sys.modules, "sounddevice", FakeSoundDevice({16000}))
    cfg = config.load()
    cfg["audio"]["device"] = "Rode"
    cli._check_microphone(cfg)
    out = capsys.readouterr().out
    assert "no  microphone" in out and "Rode" in out
