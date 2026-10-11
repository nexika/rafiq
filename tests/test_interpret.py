import pytest

from rafiq import config, lang
from rafiq.brain import Brain
from rafiq.interpret import Pair, Session, is_stop, parse_request


@pytest.mark.parametrize("text, pair", [
    ("Interpret between me and her, Egyptian and German", Pair("ar-EG", "de")),
    ("Rafiq, be our interpreter: English and French", Pair("en", "fr")),
    ("translate between Spanish and Egyptian Arabic", Pair("es", "ar-EG")),
    ("Dolmetsche zwischen Deutsch und Englisch", Pair("de", "en")),
    ("Traduis entre français et arabe", Pair("fr", "ar")),
    ("ترجم بيني وبينها بالمصري والألماني", Pair("ar-EG", "de")),
])
def test_a_request_for_an_interpreter_names_the_two_languages(text, pair):
    assert parse_request(text) == pair


@pytest.mark.parametrize("text", [
    "translate hello into German",                 # one sentence, not a conversation
    "translate good morning from English to French",
    "what's the weather in Berlin",
    "interpret between me and her",                # no languages
    "interpret between Egyptian and Arabic",       # the same language twice
])
def test_other_requests_do_not_start_interpreting(text):
    assert parse_request(text) is None


@pytest.mark.parametrize("text", ["stop", "Rafiq, stop", "Stopp!", "arrête", "basta", "كفاية", "خلاص"])
def test_stop_words_in_every_language(text):
    assert is_stop(text)


def test_a_sentence_with_a_stop_word_is_still_translated():
    assert not is_stop("please stop at the pharmacy on the way home")
    assert not is_stop("")


def test_the_start_and_end_phrases_exist_in_every_language():
    for tag in lang.TAGS:
        assert lang.phrase("interpret_start", tag) and lang.phrase("interpret_end", tag)


class Script:
    """A conversation: what each utterance is heard as, and what Rafiq says back."""

    def __init__(self, heard):
        self.heard = list(heard)
        self.said, self.events, self.asked = [], [], []

    def listen(self, timeout):
        return len(self.heard) if self.heard else None

    def hear(self, audio, languages):
        self.asked.append(languages)
        return self.heard.pop(0)

    def translate(self, text, source, target):
        return f"<{text} {source}->{target}>"

    def say(self, text, tag):
        self.said.append((text, tag))

    def session(self, pair):
        return Session(pair, self.listen, self.hear, self.translate, self.say,
                       lambda kind, data: self.events.append((kind, data)))


def test_each_utterance_is_said_in_the_other_language_until_stop():
    script = Script([("إزيك يا حاج", "ar"), ("Guten Tag", "de"), ("كفاية", "ar"), ("never heard", "en")])
    script.session(Pair("ar-EG", "de")).run()
    turns = script.said[2:-1]          # after the two greetings, before the goodbye
    assert turns == [("<إزيك يا حاج ar-EG->de>", "de"), ("<Guten Tag de->ar-EG>", "ar-EG")]
    assert script.heard == [("never heard", "en")]


def test_whisper_only_chooses_between_the_two_languages():
    script = Script([("hola", "es"), ("stop", "en")])
    script.session(Pair("es", "ar-EG")).run()
    assert script.asked == [["es", "ar"], ["es", "ar"]]


def test_both_people_are_told_how_it_works_in_their_own_language():
    script = Script([])
    script.session(Pair("ar-EG", "de")).run()
    assert script.said[0] == (lang.phrase("interpret_start", "ar-EG"), "ar-EG")
    assert script.said[1] == (lang.phrase("interpret_start", "de"), "de")


def test_a_long_silence_ends_it_and_the_panel_sees_both_sides():
    script = Script([("Wie geht's?", "de")])
    script.session(Pair("ar-EG", "de")).run()
    kinds = [kind for kind, _ in script.events]
    assert kinds == ["interpret", "interpreted", "interpret"]
    assert script.events[1][1] == {"text": "Wie geht's?", "source": "de",
                                   "translation": "<Wie geht's? de->ar-EG>", "target": "ar-EG"}
    assert script.said[-1] == (lang.phrase("interpret_end", "de"), "de")


def test_a_misheard_language_is_taken_as_the_other_persons_turn():
    script = Script([("Good morning", "en"), ("Bonjour", "es"), ("stop", "en")])
    script.session(Pair("en", "fr")).run()
    assert script.said[3] == ("<Bonjour fr->en>", "en")


# ---------------------------------------------------------------------- the translation itself

class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_translation_uses_no_tools_and_no_memory(monkeypatch):
    requests = []

    def post(url, json=None, timeout=None):
        requests.append(json)
        return FakeResponse({"message": {"role": "assistant", "content": " Guten Tag. "}})

    import httpx

    monkeypatch.setattr(httpx, "post", post)
    cfg = config.load()
    cfg["brain"]["mode"] = "ollama"
    brain = Brain(cfg, {}, lambda action, tag: True)
    assert brain.translate("صباح الخير", "ar-EG", "de") == "Guten Tag."
    sent = requests[0]
    assert "tools" not in sent
    assert "Egyptian Arabic" in sent["messages"][0]["content"] and "German" in sent["messages"][0]["content"]
    assert sent["messages"][1] == {"role": "user", "content": "صباح الخير"}
    assert brain.memory.turns == []
