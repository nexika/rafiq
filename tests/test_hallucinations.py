from types import SimpleNamespace

import numpy as np
import pytest

from rafiq import hallucinations
from rafiq.stt import Transcriber


# What Whisper wrote, word for word, for silence, fan noise or music in each of the six languages.
@pytest.mark.parametrize("text", [
    " Thanks for watching!", " Thank you for watching.", " Thank you.", " you", " Bye.",
    " Please subscribe to my channel.", " Subtitles by the Amara.org community",
    " Sous-titrage ST' 501", " Sous-titrage Société Radio-Canada", " Merci d'avoir regardé cette vidéo !",
    " Sous-titres réalisés para la communauté d'Amara.org",
    " ¡Gracias por ver el video!", " Subtítulos realizados por la comunidad de Amara.org", " ¡Suscríbete!",
    " Untertitel im Auftrag des ZDF, 2017", " Untertitel der Amara.org-Community", " Vielen Dank fürs Zuschauen!",
    " ترجمة نانسي قنقر", " اشتركوا في القناة", " شكرا للمشاهدة", " لا تنسوا الاشتراك في القناة",
    " متنساش تعمل لايك واشتراك", " شكرًا على المشاهدة",
    " [Music]", " (applause)", " ♪ ♪", " ...", "",
    " Thanks for watching! Thanks for watching!",
])
def test_whispers_stock_phrases_are_hallucinations(text):
    assert hallucinations.is_hallucination(text)


@pytest.mark.parametrize("text", [
    "What's the weather in Cairo?", "Thank you, what time is it?", "Merci, quelle heure est-il ?",
    "¿Qué tiempo hace hoy?", "Wie spät ist es?", "الساعة كام دلوقتي", "ما هو الطقس اليوم",
    "yes", "oui", "نعم", "Play the video I was watching",
])
def test_real_requests_are_kept(text):
    assert not hallucinations.is_hallucination(text)


def test_a_credit_line_is_dropped_even_after_real_words():
    assert hallucinations.is_hallucination("Quelle heure est-il ? Sous-titrage ST' 501")


def test_every_language_has_a_list():
    assert set(hallucinations.PHRASES) == {"en", "fr", "es", "de", "ar", "ar-EG"}


def seg(text, no_speech=0.1, logprob=-0.3, ratio=1.2):
    return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob, compression_ratio=ratio)


@pytest.mark.parametrize("segment, kept", [
    (seg("What time is it?"), True),
    (seg("What time is it?", no_speech=0.9), False),                 # Whisper is sure there is no speech
    (seg("What time is it?", no_speech=0.7, logprob=-1.2), False),   # likely silence, and an unsure guess
    (seg("What time is it?", no_speech=0.7, logprob=-0.4), True),    # likely silence, but a confident guess
    (seg("the the the the the the the", ratio=2.8), False),          # a loop of the same words
])
def test_segments_from_silence_or_loops_are_dropped(segment, kept):
    assert hallucinations.is_speech(segment) is kept


class FakeModel:
    def __init__(self, segments):
        self.segments = segments

    def transcribe(self, audio, **options):
        return iter(self.segments), None


def transcriber(segments):
    stt = Transcriber()
    stt._model = FakeModel(segments)
    return stt


def test_a_transcription_of_noise_comes_back_empty():
    heard = transcriber([seg(" Sous-titrage ST' 501")]).transcribe(np.zeros(16000, np.float32), lang="fr")
    assert heard.text == ""


def test_an_invented_segment_after_a_request_is_left_out():
    heard = transcriber([seg(" What time is it?"), seg(" Thanks for watching!")]).transcribe(
        np.zeros(16000, np.float32), lang="en")
    assert heard.text == "What time is it?"


def test_a_silent_segment_is_left_out():
    heard = transcriber([seg(" Open Firefox."), seg(" Thank you.", no_speech=0.7, logprob=-1.5)]).transcribe(
        np.zeros(16000, np.float32), lang="en")
    assert heard.text == "Open Firefox."
