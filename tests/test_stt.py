import io
import wave

import httpx
import numpy as np
import pytest

from rafiq import config, stt
from rafiq.stt import AzureSTT, CloudSTTError, Heard, OpenAISTT, SpeechToText

AUDIO = np.zeros(16000, dtype=np.float32)


class FakeLocal:
    """Local Whisper: hears one language, or can't run at all."""

    def __init__(self, lang="en", broken=False):
        self.lang, self.broken, self.calls = lang, broken, []

    def language(self, audio):
        if self.broken:
            raise ImportError("No module named 'faster_whisper'")
        return self.lang, 0.9

    def transcribe(self, audio, lang=None, **kwargs):
        if lang is None:                # like Whisper: detect the language first
            lang, _ = self.language(audio)
        self.calls.append(lang)
        return Heard(text="local words", lang=lang, confidence=0.9)


class FakeCloud:
    def __init__(self, text="cloud words", fail=False):
        self.text, self.fail, self.calls = text, fail, []

    def transcribe(self, audio, lang=None):
        self.calls.append(lang)
        if self.fail:
            raise CloudSTTError("no network")
        return self.text


def speech(**cfg):
    return config._merge(config.DEFAULTS["speech"], cfg)


def test_by_default_every_language_stays_local_even_with_keys():
    local, azure = FakeLocal("ar"), FakeCloud()
    heard = SpeechToText(local, {"azure": azure}, speech()).transcribe(AUDIO)
    assert heard.text == "local words" and azure.calls == []


def test_a_language_can_be_sent_to_a_cloud_service():
    local, azure = FakeLocal("ar"), FakeCloud("عايز أعرف الجو")
    heard = SpeechToText(local, {"azure": azure}, speech(stt_by_language={"ar": "azure"})).transcribe(AUDIO)
    assert heard == Heard(text="عايز أعرف الجو", lang="ar", confidence=0.9)
    assert azure.calls == ["ar"] and local.calls == []


def test_other_languages_stay_local_when_one_goes_to_the_cloud():
    local, azure = FakeLocal("fr"), FakeCloud()
    heard = SpeechToText(local, {"azure": azure}, speech(stt_by_language={"ar": "azure"})).transcribe(AUDIO)
    assert heard.text == "local words" and local.calls == ["fr"] and azure.calls == []


def test_a_cloud_engine_for_every_language():
    local, openai = FakeLocal("de"), FakeCloud("hallo")
    heard = SpeechToText(local, {"openai": openai}, speech(stt_engine="openai")).transcribe(AUDIO)
    assert heard.text == "hallo" and openai.calls == ["de"]


def test_when_whisper_cannot_run_the_cloud_hears_and_the_text_tells_the_language():
    openai = FakeCloud("quel temps fait-il demain")
    heard = SpeechToText(FakeLocal(broken=True), {"openai": openai}, speech()).transcribe(AUDIO)
    assert openai.calls == [None]       # OpenAI finds the language itself
    assert heard.text == "quel temps fait-il demain" and heard.lang == "fr" and heard.confidence == 0.0


def test_when_whisper_cannot_run_azure_uses_the_conversations_language():
    azure = FakeCloud("الساعة كام")
    heard = SpeechToText(FakeLocal(broken=True), {"azure": azure}, speech()).transcribe(AUDIO, hint="ar-EG")
    assert azure.calls == ["ar-EG"] and heard.lang == "ar"


def test_whisper_that_failed_once_is_not_loaded_again():
    local, openai = FakeLocal(broken=True), FakeCloud()
    calls = []
    local.language = lambda audio: calls.append(1) or (_ for _ in ()).throw(OSError("model download failed"))
    stt_ = SpeechToText(local, {"openai": openai}, speech())
    stt_.transcribe(AUDIO)
    stt_.transcribe(AUDIO)
    assert len(calls) == 1 and len(openai.calls) == 2


def test_whisper_failing_without_any_cloud_service_raises():
    with pytest.raises(ImportError):
        SpeechToText(FakeLocal(broken=True), {}, speech()).transcribe(AUDIO)


def test_a_cloud_failure_falls_back_to_whisper():
    local, azure = FakeLocal("ar"), FakeCloud(fail=True)
    heard = SpeechToText(local, {"azure": azure}, speech(stt_by_language={"ar": "azure"})).transcribe(AUDIO)
    assert heard.text == "local words" and local.calls == ["ar"]


def test_a_chosen_engine_without_a_key_stays_local():
    local = FakeLocal("ar")
    heard = SpeechToText(local, {}, speech(stt_by_language={"ar": "openai"})).transcribe(AUDIO)
    assert heard.text == "local words"


@pytest.mark.parametrize("settings", [{"stt_engine": "local", "stt_by_language": {"ar": "azure"}},
                                      {"offline": True, "stt_by_language": {"ar": "azure"}},
                                      {"offline": True, "stt_engine": "azure"}])
def test_local_or_offline_never_sends_audio(settings):
    local, azure = FakeLocal("ar"), FakeCloud()
    SpeechToText(local, {"azure": azure}, speech(**settings)).transcribe(AUDIO)
    assert azure.calls == [] and local.calls == ["ar"]


def test_offline_with_broken_whisper_raises_instead_of_sending_audio():
    azure = FakeCloud()
    with pytest.raises(ImportError):
        SpeechToText(FakeLocal(broken=True), {"azure": azure}, speech(offline=True)).transcribe(AUDIO)
    assert azure.calls == []


def test_settings_find_the_keys_in_the_environment(monkeypatch):
    monkeypatch.setenv("AZURE_SPEECH_KEY", "az")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    cloud = stt.cloud_engines(config.load())
    assert isinstance(cloud["azure"], AzureSTT) and cloud["azure"].region == "westeurope"
    assert isinstance(cloud["openai"], OpenAISTT) and cloud["openai"].key == "sk"


def test_no_keys_no_cloud_engines():
    assert stt.cloud_engines(config.load()) == {}


def test_offline_has_no_cloud_engines(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    cfg = config.load()
    cfg["speech"]["offline"] = True
    assert stt.cloud_engines(cfg) == {}


def test_audio_is_sent_as_16_khz_mono_wav():
    data = stt.wav_bytes(np.array([0.0, 0.5, -1.0, 2.0], dtype=np.float32))
    with wave.open(io.BytesIO(data)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()) == (16000, 1, 2, 4)
        samples = np.frombuffer(wav.readframes(4), dtype=np.int16)
    assert list(samples) == [0, 16383, -32767, 32767]


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=httpx.Request("POST", "https://x"),
                                        response=httpx.Response(self.status_code))

    def json(self):
        return self.data


def test_azure_asks_for_the_egyptian_recogniser(monkeypatch):
    sent = {}

    def post(url, **kwargs):
        sent.update(url=url, **kwargs)
        return FakeResponse({"RecognitionStatus": "Success", "DisplayText": "عايز أعرف الجو."})

    monkeypatch.setattr(httpx, "post", post)
    text = AzureSTT("az", "westeurope", {"ar": "ar-EG"}).transcribe(AUDIO, "ar")
    assert text == "عايز أعرف الجو."
    assert sent["url"].startswith("https://westeurope.stt.speech.microsoft.com/")
    assert sent["params"]["language"] == "ar-EG"
    assert sent["headers"]["Ocp-Apim-Subscription-Key"] == "az"
    assert sent["content"][:4] == b"RIFF" and sent["timeout"]


@pytest.mark.parametrize("lang, locale", [("fr", "fr-FR"), ("ar-EG", "ar-EG"), (None, "en-US")])
def test_azure_locales(monkeypatch, lang, locale):
    sent = {}
    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.update(kw) or FakeResponse(
        {"RecognitionStatus": "NoMatch"}))
    assert AzureSTT("az", "westeurope", {"ar": "ar-EG"}).transcribe(AUDIO, lang) == ""
    assert sent["params"]["language"] == locale


@pytest.mark.parametrize("response", [FakeResponse({}, status=401), FakeResponse({"RecognitionStatus": "Error"})])
def test_azure_errors_are_cloud_errors(monkeypatch, response):
    monkeypatch.setattr(httpx, "post", lambda url, **kw: response)
    with pytest.raises(CloudSTTError):
        AzureSTT("az", "westeurope", {}).transcribe(AUDIO, "en")


def test_azure_network_errors_are_cloud_errors(monkeypatch):
    def post(url, **kw):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(CloudSTTError):
        AzureSTT("az", "westeurope", {}).transcribe(AUDIO, "en")


def test_openai_sends_the_model_and_language(monkeypatch):
    sent = {}

    def post(url, **kwargs):
        sent.update(url=url, **kwargs)
        return FakeResponse({"text": "what's the weather"})

    monkeypatch.setattr(httpx, "post", post)
    assert OpenAISTT("sk", "gpt-4o-mini-transcribe").transcribe(AUDIO, "ar-EG") == "what's the weather"
    assert sent["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert sent["headers"]["Authorization"] == "Bearer sk"
    assert sent["data"]["model"] == "gpt-4o-mini-transcribe" and sent["data"]["language"] == "ar"
    assert sent["files"]["file"][1][:4] == b"RIFF" and sent["timeout"]


def test_openai_without_a_language_lets_the_service_find_it(monkeypatch):
    sent = {}
    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.update(kw) or FakeResponse({"text": "hola"}))
    OpenAISTT("sk", "gpt-4o-mini-transcribe").transcribe(AUDIO, None)
    assert "language" not in sent["data"]


def test_openai_errors_are_cloud_errors(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, **kw: FakeResponse({}, status=500))
    with pytest.raises(CloudSTTError):
        OpenAISTT("sk", "gpt-4o-mini-transcribe").transcribe(AUDIO, "en")
