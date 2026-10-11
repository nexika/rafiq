import sys
import types
import wave

import numpy as np
import pytest

from rafiq import config
from rafiq.tts import Speaker


def speaker(**voice):
    cfg = config.load()
    cfg["voice"].update(voice)
    return Speaker(cfg)


def kokoro_files(home):
    folder = config.data_dir() / "kokoro"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "kokoro-v1.0.onnx").write_bytes(b"model")
    (folder / "voices-v1.0.bin").write_bytes(b"voices")


class Response:
    def __init__(self, content=b"mp3"):
        self.content = content

    def raise_for_status(self):
        pass


@pytest.fixture
def fake_kokoro(monkeypatch):
    calls = []

    class Kokoro:
        def __init__(self, model, voices):
            calls.append(("load", model, voices))

        def create(self, text, voice, speed, lang):
            calls.append(("create", text, voice, speed, lang))
            return np.zeros(2400, dtype="float32"), 24000

    monkeypatch.setitem(sys.modules, "kokoro_onnx", types.SimpleNamespace(Kokoro=Kokoro))
    return calls


def test_auto_uses_only_piper_without_keys_or_kokoro_files():
    assert speaker().engines() == ["piper"]


def test_auto_tries_elevenlabs_when_its_key_is_set(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    assert speaker().engines() == ["elevenlabs", "piper"]


def test_auto_keeps_azure_first_for_its_egyptian_voices(monkeypatch):
    monkeypatch.setenv("AZURE_SPEECH_KEY", "a")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    assert speaker().engines() == ["azure", "elevenlabs", "piper"]


def test_auto_tries_kokoro_before_piper_when_its_files_are_there(rafiq_home):
    kokoro_files(rafiq_home)
    assert speaker().engines() == ["kokoro", "piper"]


@pytest.mark.parametrize("engine", ["elevenlabs", "kokoro"])
def test_a_chosen_engine_falls_back_to_piper(engine):
    assert speaker(engine=engine).engines() == [engine, "piper"]


def test_elevenlabs_posts_the_text_with_the_flash_model(monkeypatch, tmp_path):
    import httpx

    sent = {}

    def post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, json=json, timeout=timeout)
        return Response(b"audio")

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "secret")
    path, errors = speaker(engine="elevenlabs").synthesize("hello", "en", tmp_path)
    assert errors == [] and path.suffix == ".mp3" and path.read_bytes() == b"audio"
    assert sent["url"].startswith("https://api.elevenlabs.io/v1/text-to-speech/")
    assert sent["headers"]["xi-api-key"] == "secret"
    assert sent["json"] == {"text": "hello", "model_id": "eleven_flash_v2_5", "language_code": "en"}
    assert sent["timeout"]


def test_elevenlabs_uses_the_voice_set_for_egyptian_arabic(monkeypatch, tmp_path):
    import httpx

    urls = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: urls.append(url) or Response())
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    s = speaker(engine="elevenlabs", elevenlabs_voices={"default": "base", "ar-EG": "masri"})
    s.synthesize("أهلا", "ar-EG", tmp_path)
    s.synthesize("bonjour", "fr", tmp_path)
    assert urls[0].endswith("/masri") and urls[1].endswith("/base")


def test_a_failing_elevenlabs_falls_back_and_never_shows_the_key(monkeypatch, tmp_path):
    import httpx

    def post(url, **kw):
        raise httpx.ConnectError("refused secret")

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "secret")
    s = speaker(engine="elevenlabs")
    s._piper_say = lambda text, tag, out: out.with_suffix(".wav")
    path, errors = s.synthesize("hi", "en", tmp_path)
    assert path.suffix == ".wav" and errors == ["elevenlabs: ConnectError"]


def test_kokoro_speaks_english_to_a_wav_file(rafiq_home, fake_kokoro, tmp_path):
    kokoro_files(rafiq_home)
    path, errors = speaker(engine="kokoro").synthesize("hello", "en", tmp_path)
    assert errors == [] and path.suffix == ".wav"
    with wave.open(str(path)) as wav:
        assert wav.getframerate() == 24000 and wav.getnframes() == 2400
    assert fake_kokoro[-1] == ("create", "hello", "am_michael", 1.0, "en-us")


def test_kokoro_loads_the_model_once(rafiq_home, fake_kokoro, tmp_path):
    kokoro_files(rafiq_home)
    s = speaker(engine="kokoro")
    s.synthesize("one", "en", tmp_path)
    s.synthesize("deux", "fr", tmp_path)
    assert [c[0] for c in fake_kokoro] == ["load", "create", "create"]
    assert fake_kokoro[-1][2:] == ("ff_siwis", 1.0, "fr-fr")


@pytest.mark.parametrize("tag", ["ar", "ar-EG", "de"])
def test_kokoro_leaves_languages_it_has_no_voice_for_to_piper(rafiq_home, fake_kokoro, tmp_path, tag):
    kokoro_files(rafiq_home)
    s = speaker(engine="kokoro")
    s._piper_say = lambda text, tag, out: out.with_suffix(".wav")
    path, errors = s.synthesize("text", tag, tmp_path)
    assert path is not None and errors == ["kokoro: RuntimeError"]
    assert fake_kokoro == []


def test_kokoro_without_its_files_is_an_error_not_a_crash(fake_kokoro, tmp_path):
    s = speaker(engine="kokoro")
    s._piper_say = lambda text, tag, out: (_ for _ in ()).throw(RuntimeError("no piper"))
    path, errors = s.synthesize("hello", "en", tmp_path)
    assert path is None and errors == ["kokoro: RuntimeError", "piper: RuntimeError"]
