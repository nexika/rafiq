"""Media: "put X on Netflix", "play X on Spotify", in the six languages, straight to the service's page.

A request in one of these fixed phrasings is handled before the model is asked: it is read with a few
rules, and the search page of the named service opens in the browser (Netflix, YouTube, Spotify's web
player). Anything else ("what's on Netflix tonight?") goes to the model as usual, which can use the
play_media tool for a phrasing the rules miss.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, quote_plus

from .. import lang
from . import tool
from .web import open_in_browser

NAMES = {"netflix": "Netflix", "youtube": "YouTube", "spotify": "Spotify"}
ARABIC_NAMES = {"netflix": "نتفليكس", "youtube": "يوتيوب", "spotify": "سبوتيفاي"}

# How speech-to-text writes each service; the Arabic after diacritics are taken out.
SPELLINGS = {
    "netflix": ("netflix", "net flix", "نتفليكس", "نتفلكس", "نيتفليكس", "نتفلیکس"),
    "youtube": ("youtube", "you tube", "يوتيوب", "يوتوب", "اليوتيوب"),
    "spotify": ("spotify", "سبوتيفاي", "سبوتيفاى", "سبوتفاي", "سبوتيفي"),
}
# The verb that starts the request, longest first so "put on" wins over "put".
VERBS = (
    "listen to", "put on", "play", "put", "watch", "start", "stream", "show me",                    # en
    "mets-moi", "mets", "joue", "lance", "regarde", "écoute", "ecoute", "passe",                    # fr
    "quiero escuchar", "quiero ver", "ponme", "pon", "reproduce", "toca", "mira", "escucha",        # es
    "spiel mir", "spiele", "spiel", "starte", "zeig mir", "zeige", "zeig", "mach",                  # de
    "شغللي", "شغلي", "شغلى", "شغل", "حطلي", "حط", "اعرضلي", "اعرض", "[اأ]سمعني", "اسمع", "افتحلي", "افتح",  # ar
)
PREPOSITIONS = ("on", "from", "sur", "en", "auf", "bei", "على", "علي", "في")
POLITE = ("please", "s'il te plaît", "s'il te plait", "s'il vous plaît", "s'il vous plait", "por favor",
          "bitte", "لو سمحت", "من فضلك")

_SERVICE = {s: name for name, spellings in SPELLINGS.items() for s in spellings}
_PATTERN = re.compile(
    r"^(?:{verbs})\s+(?P<query>.+?)\s+(?:{preps})\s+(?P<service>{services})(?:\s+(?:ab|an))?$".format(
        verbs="|".join(v if v.startswith("[") else re.escape(v) for v in VERBS),
        preps="|".join(map(re.escape, PREPOSITIONS)),
        services="|".join(re.escape(s) for s in sorted(_SERVICE, key=len, reverse=True))),
    re.IGNORECASE)
_POLITE = "|".join(map(re.escape, POLITE))
_EDGES = re.compile(rf"^(?:(?:{_POLITE})[\s,،]+)+|(?:[\s,،]+(?:{_POLITE}))+$", re.IGNORECASE)
_DIACRITICS = re.compile(r"[ً-ْـ]")


@dataclass(frozen=True)
class MediaRequest:
    service: str      # "netflix", "youtube" or "spotify"
    query: str        # what to play, as said


def parse(text: str) -> MediaRequest | None:
    """The service and what to play, when the request is in one of the fixed phrasings."""
    text = _DIACRITICS.sub("", unicodedata.normalize("NFKC", text))
    text = re.sub(r"\s+", " ", text).strip(" .!?؟,،")
    text = _EDGES.sub("", text).strip(" .!?؟,،")
    match = _PATTERN.match(text)
    if not match:
        return None
    query = match["query"].strip(" ,،\"'«»")
    if not query:
        return None
    return MediaRequest(_SERVICE[match["service"].lower()], query)


def search_url(service: str, query: str) -> str:
    if service == "netflix":
        return f"https://www.netflix.com/search?q={quote(query)}"
    if service == "youtube":
        return f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    return f"https://open.spotify.com/search/{quote(query)}"


def open_url(url: str) -> str:
    return open_in_browser(url)


def direct(text: str, tag: str, opener: Callable[[str], object] | None = None) -> str | None:
    """Open the page for a fixed-phrasing request and return what to say; None for anything else."""
    request = parse(text)
    if request is None:
        return None
    (opener or open_url)(search_url(request.service, request.query))
    names = ARABIC_NAMES if tag.startswith("ar") else NAMES
    return lang.phrase("media_opened", tag, query=request.query, service=names[request.service])


@tool("play_media", "Open a series, film, song or artist on Netflix, YouTube or Spotify (its search page).",
      {"service": {"type": "string", "enum": list(NAMES)}, "query": {"type": "string"}},
      required=("service", "query"))
def play_media(service: str, query: str) -> str:
    service = service.lower().strip()
    if service not in NAMES:
        return "error: only Netflix, YouTube and Spotify"
    url = search_url(service, query)
    open_url(url)
    return f"opened {url}"
