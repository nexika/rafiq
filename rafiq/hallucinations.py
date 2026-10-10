"""What Whisper writes when nobody spoke: stock phrases it learned from subtitled videos.

On silence, fan noise or music Whisper often "hears" a sign-off ("Thanks for watching!") or a subtitle
credit ("Sous-titrage ST' 501", "ترجمة نانسي قنقر"). None of it may become a request. A segment is
dropped when Whisper itself thinks there was no speech or when it repeats itself in a loop, and what is
left is dropped when it is one of these phrases.
"""

from __future__ import annotations

import re

from .lang import words

# Whole utterances: dropped only when they are all that was heard, so "Thank you, what time is it?"
# still gets an answer. Never put a yes or a no here: confirmations are short too.
PHRASES = {
    "en": {"thanks for watching", "thank you for watching", "thank you so much for watching", "thank you",
           "thank you very much", "thanks", "you", "bye", "bye bye", "please subscribe",
           "please subscribe to my channel", "subscribe to my channel", "like and subscribe",
           "don't forget to like and subscribe", "see you in the next video", "see you next time",
           "i'll see you in the next video", "the end"},
    "fr": {"merci d'avoir regardé", "merci d'avoir regardé cette vidéo", "merci de m'avoir regardé", "merci",
           "merci beaucoup", "abonnez-vous", "abonnez-vous à la chaîne", "n'oubliez pas de vous abonner",
           "à bientôt", "à la prochaine", "sous-titrage", "sous-titres"},
    "es": {"gracias por ver", "gracias por ver el video", "gracias por ver el vídeo", "muchas gracias por ver",
           "gracias", "muchas gracias", "suscríbete", "suscríbete al canal", "no olvides suscribirte",
           "nos vemos en el próximo video", "nos vemos en el próximo vídeo", "hasta la próxima", "subtítulos"},
    "de": {"vielen dank fürs zuschauen", "danke fürs zuschauen", "vielen dank für's zuschauen", "danke",
           "vielen dank", "bis zum nächsten mal", "abonniert den kanal", "tschüss", "untertitel"},
    "ar": {"شكرا للمشاهدة", "شكرا لكم على المشاهدة", "شكرا على المشاهدة", "شكرا", "شكرا لكم",
           "اشتركوا في القناة", "اشترك في القناة", "لا تنسوا الاشتراك في القناة", "موسيقى", "ترجمة"},
    "ar-EG": {"متنساش تعمل لايك واشتراك", "متنسوش تشتركوا في القناة", "متنساش تشترك في القناة",
              "اشترك في القناة وفعل الجرس", "شكرا ليكم", "شكرا على المشاهده"},
}

# Subtitle credits: dropped wherever they appear, since no one says them to an assistant.
CREDITS = {
    "en": {"amara.org", "subtitles by", "transcribed by", "captioned by"},
    "fr": {"sous-titrage st' 501", "sous-titrage société radio-canada", "sous-titres réalisés par",
           "sous-titres réalisés para"},
    "es": {"subtítulos realizados por", "subtitulado por", "subtítulos por"},
    "de": {"untertitel im auftrag des zdf", "untertitelung des zdf", "untertitel der amara", "untertitel von",
           "copyright wdr"},
    "ar": {"ترجمة نانسي قنقر", "نانسي قنقر"},
    "ar-EG": {"ترجمة نانسي قنقر"},
}

# Whisper's own limits (the defaults of openai-whisper and faster-whisper's temperature fallback).
NO_SPEECH = 0.8          # sure there was no speech
MAYBE_NO_SPEECH = 0.6    # probably none, so drop the text if Whisper was also unsure of it
LOW_LOGPROB = -1.0
LOOP_RATIO = 2.4         # text that compresses this well is the same words over and over

_ASIDES = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*")   # [Music], (applause), *laughs*


def _key(text: str) -> tuple[str, ...]:
    return tuple(words(text))


_WHOLE = {_key(p) for table in PHRASES.values() for p in table}
_ANYWHERE = {_key(p) for table in CREDITS.values() for p in table}


def _contains(found: tuple[str, ...], part: tuple[str, ...]) -> bool:
    n = len(part)
    return any(found[i:i + n] == part for i in range(len(found) - n + 1))


def _repeats(found: tuple[str, ...], part: tuple[str, ...]) -> bool:
    n = len(part)
    return len(found) % n == 0 and all(found[i:i + n] == part for i in range(0, len(found), n))


def is_hallucination(text: str) -> bool:
    """True when the text is only noise, a stock phrase (said once or over and over), or holds a
    subtitle credit."""
    found = _key(_ASIDES.sub(" ", text))
    if not found:
        return True
    if any(_contains(found, credit) for credit in _ANYWHERE):
        return True
    return any(_repeats(found, phrase) for phrase in _WHOLE)


def is_speech(segment) -> bool:
    """Whether a Whisper segment is worth keeping: not silence by Whisper's own measure, not a loop."""
    if segment.no_speech_prob >= NO_SPEECH:
        return False
    if segment.no_speech_prob >= MAYBE_NO_SPEECH and segment.avg_logprob < LOW_LOGPROB:
        return False
    return segment.compression_ratio <= LOOP_RATIO
