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


SLEEP = {"go to sleep", "sleep", "mets toi en veille", "en veille", "va dormir", "dors", "vete a dormir",
         "a dormir", "duérmete", "duermete", "geh schlafen", "schlaf", "نام", "نم", "روح نام", "اذهب للنوم",
         "اذهب إلى النوم"}
# Said around the phrase without changing it ("go to sleep please", "نام يا رفيق").
_SLEEP_FILLER = {"please", "now", "thanks", "rafiq", "rafik", "s'il", "te", "plait", "plaît", "merci",
                 "maintenant", "por", "favor", "ya", "bitte", "jetzt", "يا", "رفيق", "خلاص", "دلوقتي", "شكرا"}


def is_sleep(text: str) -> bool:
    """True when the whole request is "go to sleep" (or "نام", "mets-toi en veille"...); a request that
    only mentions sleep ("set a sleep timer") is not."""
    return " ".join(w for w in words(text) if w not in _SLEEP_FILLER_N) in _SLEEP_N


# What stops Rafiq while it speaks. Said over its voice, so only a short utterance counts: a long one is
# more likely Rafiq's own sentence leaking past the echo check than you.
STOP = {"stop", "wait", "enough", "attends", "arrête", "arrete", "espera", "para", "basta", "warte",
        "stopp", "halt", "genug", "توقف", "قف", "استنى", "كفاية", "خلاص", "اسكت"}
STOP_MAX_WORDS = 4


def is_stop(text: str) -> bool:
    """True for a short "stop" in any of Rafiq's languages ("Rafiq, stop", "attends", "استنى")."""
    found = words(text)
    return 0 < len(found) <= STOP_MAX_WORDS and any(w in _STOP for w in found)


_EGYPTIAN, _WEAK_N, _YES, _NO = _norm(EGYPTIAN), _norm(_WEAK), _norm(YES), _norm(NO)
_SLEEP_N = frozenset(" ".join(words(p)) for p in SLEEP)
_SLEEP_FILLER_N = _norm(_SLEEP_FILLER)
_STOP = _norm(STOP)

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
    "task_done": {"en": "Your task is done. {result}", "fr": "Ta tâche est terminée. {result}",
                  "es": "Tu tarea está lista. {result}", "de": "Deine Aufgabe ist fertig. {result}",
                  "ar": "انتهت مهمتك. {result}", "ar-EG": "المهمة خلصت. {result}"},
    "task_failed": {"en": "I couldn't finish the task: {goal}", "fr": "Je n'ai pas pu finir la tâche : {goal}",
                    "es": "No he podido terminar la tarea: {goal}",
                    "de": "Ich konnte die Aufgabe nicht abschließen: {goal}",
                    "ar": "لم أستطع إنهاء المهمة: {goal}", "ar-EG": "مقدرتش أخلّص المهمة: {goal}"},
    "shutdown_soon": {"en": "Shutting down in {seconds} seconds. Say cancel the shutdown to stop it.",
                      "fr": "Extinction dans {seconds} secondes. Dis « annule l'extinction » pour l'arrêter.",
                      "es": "Me apago en {seconds} segundos. Di «cancela el apagado» para pararlo.",
                      "de": "Ich fahre in {seconds} Sekunden herunter. "
                            "Sag „Herunterfahren abbrechen“, um es zu stoppen.",
                      "ar": "سأطفئ الجهاز بعد {seconds} ثوانٍ. قل «ألغِ إيقاف التشغيل» لإيقافه.",
                      "ar-EG": "هقفل الجهاز كمان {seconds} ثواني. قول «إلغي القفل» لو عايز توقفه."},
    "shutdown_failed": {"en": "I couldn't shut the computer down.",
                        "fr": "Je n'ai pas pu éteindre l'ordinateur.", "es": "No he podido apagar el ordenador.",
                        "de": "Ich konnte den Computer nicht herunterfahren.",
                        "ar": "لم أستطع إطفاء الجهاز.", "ar-EG": "معرفتش أقفل الجهاز."},
    "brain_down": {"en": "I can't reach my brain right now.", "fr": "Je n'arrive pas à réfléchir pour le moment.",
                   "es": "Ahora mismo no puedo pensar.", "de": "Ich kann gerade nicht nachdenken.",
                   "ar": "لا أستطيع التفكير الآن.", "ar-EG": "مش قادر أفكر دلوقتي."},
    # Said before a slow tool that has no waiting phrase of its own (see Tool.waiting_phrase).
    "wait": {"en": "One moment.", "fr": "Un instant.", "es": "Un momento.", "de": "Einen Moment.",
             "ar": "لحظة من فضلك.", "ar-EG": "ثانية واحدة."},
    "limit": {"en": "That was taking too long, so I stopped. Try asking in a simpler way.",
              "fr": "Ça prenait trop de temps, alors j'ai arrêté. Essaie de demander plus simplement.",
              "es": "Estaba tardando demasiado, así que paré. Prueba a pedirlo de forma más sencilla.",
              "de": "Das hat zu lange gedauert, also habe ich aufgehört. Frag es bitte einfacher.",
              "ar": "استغرق ذلك وقتًا طويلًا فتوقفت. جرّب أن تسأل بطريقة أبسط.",
              "ar-EG": "ده كان واخد وقت كتير فوقفت. جرّب تسأل بطريقة أبسط."},
}


def phrase(key: str, tag: str, **values: str) -> str:
    table = PHRASES[key]
    return table.get(tag, table["en"]).format(**values)
