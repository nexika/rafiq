from pathlib import Path

import pytest

from rafiq import assistant as assistant_mod
from rafiq import config
from rafiq.assistant import Context, Events
from rafiq.practice import Practice
from rafiq.tools import media


@pytest.mark.parametrize("text, service, query", [
    ("Put Stranger Things on Netflix", "netflix", "Stranger Things"),
    ("play Bohemian Rhapsody on Spotify.", "spotify", "Bohemian Rhapsody"),
    ("Please watch cat videos on YouTube", "youtube", "cat videos"),
    ("listen to Fairuz on spotify please", "spotify", "Fairuz"),
    ("put on The Crown on netflix", "netflix", "The Crown"),
    ("Mets Lupin sur Netflix s'il te plaît", "netflix", "Lupin"),
    ("joue Stromae sur Spotify", "spotify", "Stromae"),
    ("Pon La casa de papel en Netflix", "netflix", "La casa de papel"),
    ("reproduce Despacito en YouTube", "youtube", "Despacito"),
    ("Spiel Rammstein auf Spotify ab", "spotify", "Rammstein"),
    ("Mach Dark auf Netflix an", "netflix", "Dark"),
    ("شغّل أم كلثوم على سبوتيفاي", "spotify", "أم كلثوم"),
    ("شغل مسلسل الحشاشين على نتفليكس", "netflix", "مسلسل الحشاشين"),
    ("اعرض أغنية فيروز على يوتيوب", "youtube", "أغنية فيروز"),
    ("شغللي عمرو دياب على يوتيوب لو سمحت", "youtube", "عمرو دياب"),
])
def test_fixed_phrasings_name_the_service_and_what_to_play(text, service, query):
    assert media.parse(text) == media.MediaRequest(service, query)


@pytest.mark.parametrize("text", [
    "what's on Netflix tonight", "play some music", "open YouTube", "put the kettle on",
    "play on Netflix", "شغل الموسيقى", "",
])
def test_other_requests_are_left_to_the_model(text):
    assert media.parse(text) is None


@pytest.mark.parametrize("service, query, url", [
    ("netflix", "La casa de papel", "https://www.netflix.com/search?q=La%20casa%20de%20papel"),
    ("youtube", "cat videos & dogs", "https://www.youtube.com/results?search_query=cat+videos+%26+dogs"),
    ("spotify", "أم كلثوم", "https://open.spotify.com/search/%D8%A3%D9%85%20%D9%83%D9%84%D8%AB%D9%88%D9%85"),
])
def test_search_addresses(service, query, url):
    assert media.search_url(service, query) == url


def test_a_direct_request_opens_the_page_and_says_so():
    opened = []
    answer = media.direct("Mets Lupin sur Netflix", "fr", opener=opened.append)
    assert opened == ["https://www.netflix.com/search?q=Lupin"]
    assert "Lupin" in answer and "Netflix" in answer


def test_anything_else_is_not_handled_directly():
    opened = []
    assert media.direct("what time is it", "en", opener=opened.append) is None
    assert opened == []


def test_the_tool_rejects_an_unknown_service(monkeypatch):
    opened = []
    monkeypatch.setattr(media, "open_url", opened.append)
    assert media.play_media("disney", "Frozen").startswith("error")
    assert media.play_media("YouTube", "Frozen") == "opened https://www.youtube.com/results?search_query=Frozen"
    assert opened == ["https://www.youtube.com/results?search_query=Frozen"]


class FakeBrain:
    def __init__(self):
        self.asked = []

    def think(self, text, tag, ctx=None, on_speech=None):
        self.asked.append(text)
        return "from the model"


class FakeStream:
    def put(self, text):
        pass

    def close(self):
        pass


class FakeSpeaker:
    def stream(self):
        return FakeStream()


def bare_assistant(said):
    """An Assistant without a microphone, a speaker or a model: just what respond() uses."""
    a = object.__new__(assistant_mod.Assistant)
    a.practice = Practice(Path("practice.json"))   # no session running
    a.cfg = config.load()
    a.events = Events()
    a.ctx = Context(a.cfg, scheduler=None)
    a.brain = FakeBrain()
    a.say = lambda text, tag: said.append((text, tag))
    a.speaker = FakeSpeaker()
    a.speech, a.silent = None, False
    a.set_state = lambda state: None
    a.publish_spending = lambda tag: None
    return a


def test_respond_plays_directly_without_the_model(monkeypatch):
    opened, said = [], []
    monkeypatch.setattr(media, "open_url", opened.append)
    a = bare_assistant(said)
    a.respond("شغل أم كلثوم على سبوتيفاي", "ar-EG")
    assert a.brain.asked == [] and len(opened) == 1 and "أم كلثوم" in said[0][0]
    a.respond("what time is it", "en")
    assert a.brain.asked == ["what time is it"]
    last = a.events.history[-1]
    assert (last["kind"], last["text"], last["tag"]) == ("said", "from the model", "en")
