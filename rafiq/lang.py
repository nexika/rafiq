"""Languages: which one the user spoke, Egyptian or standard Arabic, yes or no, and Rafiq's own phrases.

A language tag is one of "en", "fr", "es", "de", "ar" (Modern Standard Arabic) or "ar-EG" (Egyptian).
Whisper only says "ar"; the dialect is read from words that Egyptian uses and standard Arabic doesn't.
"""

from __future__ import annotations

import re
import unicodedata

TAGS = ("en", "fr", "es", "de", "ar", "ar-EG")
NAMES = {"en": "English", "fr": "French", "es": "Spanish", "de": "German", "ar": "Modern Standard Arabic",
         "ar-EG": "Egyptian Arabic"}

ARABIC = re.compile(r"[؀-ۿ]")
_DIACRITICS = re.compile(r"[ً-ْـ]")   # harakat and tatweel

# Words that mark Egyptian Arabic. Each is distinctive on its own; two or more settle it.
EGYPTIAN = {
    "ازاي", "إزاي", "عايز", "عايزة", "عاوز", "عاوزه", "عاوزة", "دلوقتي", "دلوقت", "كده", "كدا", "فين", "ليه",
    "مش", "بتاع", "بتاعة", "بتاعي", "اللي", "ايه", "إيه", "امتى", "إمتى", "النهارده", "النهاردة", "بكره",
    "بكرة", "خلاص", "اوي", "أوي", "حاجة", "حاجه", "شوية", "شويه", "ماشي", "طب", "بقى", "يعني", "دي", "ده",
    "زي", "عشان", "علشان", "ازيك", "إزيك", "إزيكم", "مفيش", "فيه", "معلش", "لسه", "هو", "قوي", "يلا", "ياريت",
    "اهو", "أهو", "برضه", "برضو", "تعالى", "هات", "بص", "شغل", "شغّل", "اقفل", "افتح", "استنى", "عاملين",
    "احسبلي", "قولي", "قوللي", "اعملي", "اعملّي", "شوفلي", "دورلي", "فكرني", "فكّرني", "كام", "بكام", "ازاى",
}
# Words shared with standard Arabic or other dialects: they count only next to a distinctive one.
_WEAK = {"يعني", "هو", "فيه", "شغل", "افتح", "يلا", "طب", "حاجة", "حاجه", "اللي", "ليه"}


_LETTERS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})


def normalize_arabic(text: str) -> str:
    """Spelling that varies in speech-to-text output: hamza on alef, ta marbuta, alef maqsura."""
    return _DIACRITICS.sub("", unicodedata.normalize("NFKC", text)).translate(_LETTERS)


def _norm(items: set[str]) -> frozenset[str]:
    return frozenset(normalize_arabic(w).lower() for w in items)


def words(text: str) -> list[str]:
    return re.findall(r"[\w']+", normalize_arabic(text).lower())


def egyptian_score(text: str) -> int:
    found = [w for w in words(text) if w in _EGYPTIAN]
    strong = [w for w in found if w not in _WEAK_N]
    return len(strong) * 2 + (len(found) - len(strong)) if strong else 0


def dialect(lang: str, text: str, previous: str | None = None) -> str:
    """The tag for what the user said. Arabic with Egyptian words is "ar-EG"; a short Arabic reply
    with no telling words keeps the dialect of the conversation so far."""
    if lang != "ar":
        return lang if lang in TAGS else "en"
    score = egyptian_score(text)
    if score >= 2:
        return "ar-EG"
    if score == 0 and previous == "ar-EG" and len(words(text)) <= 4:
        return "ar-EG"
    return "ar"


STOPWORDS = {
    "en": {"the", "is", "and", "what", "how", "you", "it", "to", "of", "my", "please", "can", "a", "in", "for",
           "this", "me", "open", "set", "time", "weather"},
    "fr": {"le", "la", "les", "est", "et", "quel", "quelle", "comment", "tu", "vous", "je", "de", "du", "des",
           "un", "une", "mon", "ma", "pour", "ouvre", "il", "fait", "quoi", "s'il", "plait", "plaît"},
    "es": {"el", "la", "los", "las", "es", "y", "qué", "que", "cómo", "como", "tú", "de", "del", "un", "una",
           "mi", "para", "por", "abre", "hace", "tiempo", "hoy", "está"},
    "de": {"der", "die", "das", "ist", "und", "was", "wie", "du", "ich", "zu", "von", "ein", "eine", "mein",
           "meine", "für", "bitte", "öffne", "mach", "wetter", "heute", "spät", "es"},
}


def guess(text: str, fallback: str = "en") -> str:
    """The language of typed text (Whisper says which for speech): Arabic script, else the language
    whose common words appear most; the fallback when nothing tells."""
    if ARABIC.search(text):
        return "ar"
    found = words(text)
    scores = {code: sum(w in common for w in found) for code, common in STOPWORDS.items()}
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return fallback if not fallback.startswith("ar") else "en"
    return best


def script_language(text: str, fallback: str) -> str:
    """The language a reply is written in, as far as its script tells: Arabic script means Arabic
    (in the user's dialect when the user spoke Arabic); otherwise the user's language."""
    arabic = len(ARABIC.findall(text))
    letters = sum(ch.isalpha() for ch in text) or 1
    if arabic / letters > 0.5:
        return fallback if fallback.startswith("ar") else "ar"
    return fallback if not fallback.startswith("ar") else "en"


YES = {"yes", "yeah", "yep", "sure", "ok", "okay", "confirm", "oui", "ouais", "d'accord", "vas-y", "si", "sí",
       "claro", "vale", "dale", "ja", "jawohl", "genau", "klar", "نعم", "اجل", "أجل", "اكيد", "أكيد", "تمام",
       "ايوه", "أيوه", "ايوة", "أيوة", "اه", "آه", "ماشي", "موافق", "طبعا", "طبعًا"}
# Only words that mean "no" on their own; a bare negation ("pas", "nicht", "مش") is often part of a yes
# ("pas de problème", "أيوه مش مشكلة").
NO = {"no", "nope", "cancel", "stop", "don't", "non", "annule", "annuler", "nein", "abbrechen", "لا", "لأ",
      "لاء", "الغي", "ألغي", "الغاء", "إلغاء", "بلاش", "استنى", "متعملش"}


def yes_or_no(text: str) -> bool | None:
    """True for a clear yes, False for a clear no, None when it is neither or both. A no always wins
    over a yes in the same answer ("yes, no wait" is a no): an action is only taken on a clear yes."""
    found = set(words(text))
    if found & _NO:
        return False
    if found & _YES:
        return True
    return None


_EGYPTIAN, _WEAK_N, _YES, _NO = _norm(EGYPTIAN), _norm(_WEAK), _norm(YES), _norm(NO)

PHRASES = {
    "listening": {"en": "Yes?", "fr": "Oui ?", "es": "¿Sí?", "de": "Ja?", "ar": "نعم؟", "ar-EG": "أيوه؟"},
    "not_heard": {"en": "Sorry, I didn't catch that.", "fr": "Désolé, je n'ai pas compris.",
                  "es": "Perdona, no te he entendido.", "de": "Entschuldigung, das habe ich nicht verstanden.",
                  "ar": "عذرًا، لم أفهم ذلك.", "ar-EG": "معلش، مسمعتش كويس."},
    "confirm": {"en": "Should I {action}? Say yes or no.", "fr": "Je dois {action} ? Dis oui ou non.",
                "es": "¿Quieres que {action}? Di sí o no.", "de": "Soll ich {action}? Sag ja oder nein.",
                "ar": "هل أقوم بـ{action}؟ قل نعم أو لا.", "ar-EG": "أعمل {action}؟ قول آه أو لأ."},
    "cancelled": {"en": "Okay, I won't.", "fr": "D'accord, je ne le fais pas.", "es": "Vale, no lo hago.",
                  "de": "Okay, ich lasse es.", "ar": "حسنًا، لن أفعل.", "ar-EG": "ماشي، مش هعمل كده."},
    "timer_done": {"en": "Your timer is done: {label}", "fr": "Ton minuteur est terminé : {label}",
                   "es": "Tu temporizador ha terminado: {label}", "de": "Dein Timer ist abgelaufen: {label}",
                   "ar": "انتهى المؤقت: {label}", "ar-EG": "التايمر خلص: {label}"},
    "reminder": {"en": "Reminder: {label}", "fr": "Rappel : {label}", "es": "Recordatorio: {label}",
                 "de": "Erinnerung: {label}", "ar": "تذكير: {label}", "ar-EG": "فكّرتك: {label}"},
    "scene_done": {"en": "Done.", "fr": "C'est fait.", "es": "Hecho.", "de": "Erledigt.", "ar": "تم.",
                   "ar-EG": "تمام، خلصت."},
    "brain_down": {"en": "I can't reach my brain right now.", "fr": "Je n'arrive pas à réfléchir pour le moment.",
                   "es": "Ahora mismo no puedo pensar.", "de": "Ich kann gerade nicht nachdenken.",
                   "ar": "لا أستطيع التفكير الآن.", "ar-EG": "مش قادر أفكر دلوقتي."},
}


def phrase(key: str, tag: str, **values: str) -> str:
    table = PHRASES[key]
    return table.get(tag, table["en"]).format(**values)
