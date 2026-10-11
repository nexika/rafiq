"""Picking the tools a request needs before the model sees it.

Keyword rules in the six languages name the domains a request touches (weather, time, computer...),
and the model gets only those domains' tools. A greeting or a thank-you gets no tools at all. Anything
the rules can't place keeps every tool, so a miss costs only what routing would have saved.
Fewer tools means a shorter prompt: cheaper with Claude, much faster with a small local model, which
also picks the right tool more often from a short list.
"""

from __future__ import annotations

from dataclasses import dataclass

from .lang import normalize_arabic, words
from .tools import Tool

# domain -> (its tools, words that point to it in any of the six languages)
DOMAINS: dict[str, tuple[tuple[str, ...], set[str]]] = {
    "weather": (("weather",), {
        "weather", "forecast", "rain", "raining", "sunny", "temperature", "umbrella", "snow", "snowing", "windy",
        "météo", "meteo", "temps", "pluie", "pleut", "pleuvoir", "température", "neige",
        "tiempo", "clima", "lluvia", "llueve", "llover", "temperatura", "pronóstico", "nieve",
        "wetter", "wettervorhersage", "regen", "regnet", "temperatur", "schnee", "schneit",
        "طقس", "جو", "مطر", "امطار", "حرارة", "الحرارة",
    }),
    "time": (("get_time", "set_timer", "set_reminder", "list_timers", "cancel_timer"), {
        "time", "clock", "timer", "timers", "alarm", "remind", "reminder", "reminders", "o'clock",
        "heure", "heures", "minuteur", "rappel", "rappelle", "réveil", "chrono",
        "hora", "horas", "temporizador", "recordatorio", "recuérdame", "alarma",
        "uhr", "uhrzeit", "spät", "wecker", "erinnerung", "erinnere",
        "ساعة", "وقت", "منبه", "مؤقت", "تايمر", "ذكرني", "فكرني", "تذكير",
    }),
    "calendar": (("calendar_events", "calendar_add"), {
        "calendar", "agenda", "schedule", "meeting", "meetings", "appointment", "appointments", "event", "events",
        "calendrier", "rendez", "réunion", "événement",
        "calendario", "cita", "citas", "reunión", "evento", "eventos",
        "kalender", "termin", "termine", "besprechung",
        "تقويم", "موعد", "مواعيد", "اجتماع", "اجندة",
    }),
    "email": (("email_list", "email_read", "email_send", "email_reply", "email_trash"), {
        "email", "emails", "mail", "mails", "inbox", "gmail",
        "courriel", "courriels",
        "correo", "correos",
        "posteingang",
        "بريد", "ايميل", "ايميلات", "الايميل",
    }),
    "computer": (("open_app", "list_apps", "add_app", "volume", "media", "system_status", "lock_screen", "power",
                  "wake_computer", "open_in_browser"), {
        "volume", "louder", "quieter", "mute", "unmute", "open", "launch", "app", "apps", "application",
        "shutdown", "shut", "restart", "reboot", "lock", "battery", "cpu", "computer", "pc", "suspend",
        "ouvre", "ouvrir", "lance", "ordinateur", "éteins", "éteindre", "redémarre", "verrouille", "batterie",
        "volumen", "abre", "abrir", "apaga", "apagar", "reinicia", "bloquea", "ordenador", "computadora",
        "batería", "aplicación",
        "lautstärke", "lauter", "leiser", "öffne", "öffnen", "starte", "rechner", "herunterfahren",
        "ausschalten", "sperre", "sperren", "akku",
        "صوت", "افتح", "شغل", "اقفل", "كمبيوتر", "جهاز", "بطارية", "اطفي", "اطفئ", "اغلق", "برنامج", "تطبيق",
    }),
    "math": (("calculate", "convert_units"), {
        "calculate", "minus", "divided", "multiplied", "percent", "squared", "sqrt", "convert",
        "calcule", "divisé", "multiplié", "pourcent", "convertis", "convertir",
        "calcula", "dividido", "multiplicado", "convierte",
        "rechne", "berechne", "geteilt", "umrechnen", "prozent",
        "احسب", "احسبلي", "ضرب", "مضروب", "قسمة", "زائد", "ناقص", "تحويل",
    }),
    "web": (("web_search", "read_page", "open_in_browser"), {
        "search", "google", "news", "wikipedia", "website",
        "cherche", "recherche", "actualités", "infos",
        "busca", "buscar", "noticias",
        "suche", "such", "nachrichten",
        "ابحث", "بحث", "دور", "دورلي", "اخبار",
    }),
}

# Words that mean arithmetic only next to a number: "plus tard", "how many times", "sag mal".
NUMBER_WORDS = {"times", "plus", "fois", "moins", "más", "menos", "por", "mal", "weniger"}

# Words a greeting, a thank-you or a goodbye is made of. A request made only of these is small talk.
# Yes and okay are left out on purpose: they may answer a question the model just asked.
SMALL_TALK = {
    "hello", "hi", "hey", "thanks", "thank", "you", "good", "morning", "afternoon", "evening", "night", "bye",
    "goodbye", "how", "are", "nice", "great", "cool", "very", "much", "so", "cheers", "doing", "what's", "up",
    "bonjour", "salut", "bonsoir", "coucou", "merci", "beaucoup", "ça", "va", "comment", "au", "revoir",
    "bonne", "tu", "vas",
    "hola", "gracias", "muchas", "buenos", "buenas", "días", "dias", "tardes", "noches", "adiós", "adios",
    "qué", "tal", "cómo", "estás",
    "hallo", "danke", "vielen", "schön", "guten", "morgen", "tag", "abend", "gute", "nacht", "tschüss", "wie",
    "geht's", "gehts", "es", "dir",
    "مرحبا", "اهلا", "السلام", "عليكم", "شكرا", "جزيلا", "ازيك", "ازيكم", "صباح", "مساء", "الخير", "النور",
    "عامل", "ايه", "كيف", "حالك", "تسلم", "يا", "مع",
    "rafiq", "رفيق",
}

# Letters stuck to the front of an Arabic word: the article and the conjunctions and prepositions.
_ARABIC_PREFIXES = ("وال", "بال", "فال", "لل", "ال", "و", "ب", "ف", "ل")


@dataclass
class Route:
    name: str               # "small talk", "all", or the domains joined ("weather, time")
    tools: dict[str, Tool]


def _norm(items: set[str]) -> frozenset[str]:
    return frozenset(normalize_arabic(w).lower() for w in items)


_KEYWORDS = {name: _norm(keys) for name, (_, keys) in DOMAINS.items()}
_SMALL_TALK = _norm(SMALL_TALK)
_NUMBER_WORDS = _norm(NUMBER_WORDS)


def _forms(word: str) -> set[str]:
    """The word, and the word without an Arabic prefix ("الطقس" is also "طقس")."""
    forms = {word}
    for prefix in _ARABIC_PREFIXES:
        if word.startswith(prefix) and len(word) - len(prefix) >= 2:
            forms.add(word[len(prefix):])
    return forms


def route(text: str, tools: dict[str, Tool]) -> Route:
    """The tools to offer the model for this request."""
    found = words(text)
    forms = set().union(*(_forms(w) for w in found)) if found else set()
    domains = [name for name, keys in _KEYWORDS.items() if forms & keys]
    if "math" not in domains and forms & _NUMBER_WORDS and any(w.isdigit() for w in found):
        domains.append("math")
    chosen = {name: tools[name] for d in domains for name in DOMAINS[d][0] if name in tools}
    if chosen:
        return Route(", ".join(domains), chosen)
    if found and all(w in _SMALL_TALK for w in found):
        return Route("small talk", {})
    return Route("all", tools)
