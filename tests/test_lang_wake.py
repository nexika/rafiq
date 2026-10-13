import pytest

from rafiq import lang, wake


@pytest.mark.parametrize("text, expected", [
    ("عايز أعرف الجو النهارده", "ar-EG"),
    ("إزيك يا رفيق", "ar-EG"),
    ("احسبلي ١٥ في المية من ٢٤٠", "ar-EG"),
    ("الساعة كام دلوقتي", "ar-EG"),
    ("ما هو الطقس اليوم في القاهرة", "ar"),
    ("كيف حالك", "ar"),
])
def test_egyptian_or_standard_arabic(text, expected):
    assert lang.dialect("ar", text) == expected


def test_a_short_arabic_answer_keeps_the_conversations_dialect():
    assert lang.dialect("ar", "نعم", previous="ar-EG") == "ar-EG"
    assert lang.dialect("ar", "نعم", previous="ar") == "ar"


def test_other_languages_pass_through_and_unknown_ones_become_english():
    assert lang.dialect("fr", "bonjour") == "fr"
    assert lang.dialect("nl", "hallo") == "en"


def test_spelling_variants_are_the_same_word():
    assert lang.normalize_arabic("أيوة") == lang.normalize_arabic("ايوه")


@pytest.mark.parametrize("text, expected", [
    ("yes please", True), ("oui", True), ("sí, claro", True), ("ja, genau", True), ("نعم", True),
    ("أيوه", True), ("آه ماشي", True),
    # a bare negation inside a yes is still a yes
    ("pas de problème, oui", True), ("أيوه مش مشكلة", True),
    ("no", False), ("non merci", False), ("nein", False), ("لا", False), ("لأ استنى", False),
    # a no anywhere wins: actions only run on a clear yes
    ("yes, no wait", False),
    ("maybe later", None), ("", None),
])
def test_yes_or_no(text, expected):
    assert lang.yes_or_no(text) is expected


@pytest.mark.parametrize("text, expected", [
    ("what time is it", "en"), ("quel temps fait-il à Paris", "fr"), ("qué tiempo hace hoy", "es"),
    ("wie spät ist es", "de"), ("كم الساعة", "ar"), ("Rafiq", "en"),
])
def test_typed_text_language(text, expected):
    assert lang.guess(text, "en") == expected


def test_reply_voice_follows_the_script():
    assert lang.script_language("أهلًا بيك", "ar-EG") == "ar-EG"
    assert lang.script_language("مرحبا", "fr") == "ar"
    assert lang.script_language("Bonjour", "fr") == "fr"
    assert lang.script_language("Hello", "ar-EG") == "en"


def test_every_phrase_exists_in_every_language():
    for key, table in lang.PHRASES.items():
        assert set(table) == set(lang.TAGS), key
    assert lang.phrase("confirm", "ar-EG", action="أقفل الجهاز").startswith("أعمل أقفل الجهاز")


@pytest.mark.parametrize("text, rest", [
    ("Hey Rafiq, what time is it?", "what time is it?"),
    ("rafik open firefox", "open firefox"),
    ("Rafique, quel temps fait-il ?", "quel temps fait-il ?"),
    ("يا رفيق، الساعة كام؟", "الساعة كام؟"),
    ("يارفيق شغل الموسيقى", "شغل الموسيقى"),
    ("Rafiq", ""),
])
def test_the_name_is_found_with_the_command_after_it(text, rest):
    assert wake.find(text) == (True, rest)


@pytest.mark.parametrize("text", [
    "Rafael is coming tomorrow", "he is a terrific guy", "raft", "رفعت الصوت", "I ate rice", "",
])
def test_other_words_do_not_wake_rafiq(text):
    assert wake.find(text)[0] is False


def test_another_name_from_the_settings_wakes_with_the_command_after_it():
    assert wake.find("Jarvis, what time is it?", ["Jarvis"]) == (True, "what time is it?")
    assert wake.find("Jarvys open firefox", ["Jarvis"]) == (True, "open firefox")


def test_without_other_names_only_rafiq_wakes():
    assert wake.find("Jarvis, what time is it?")[0] is False


def test_rafiq_still_wakes_when_other_names_are_set():
    assert wake.find("Rafiq, what time is it?", ["Jarvis"]) == (True, "what time is it?")


def test_an_arabic_name_from_the_settings_is_found_after_ya():
    assert wake.find("يا صاحبي، شغل الموسيقى", ["صاحبي"]) == (True, "شغل الموسيقى")
    assert wake.find("ياصاحبي شغل الموسيقى", ["صاحبي"]) == (True, "شغل الموسيقى")


def test_a_short_name_must_be_heard_exactly():
    assert wake.find("Max, open firefox", ["Max"]) == (True, "open firefox")
    assert wake.find("I mix the paint", ["Max"])[0] is False


def test_a_name_of_two_words_is_found_whole():
    assert wake.find("Abu Ali, what time is it?", ["Abu Ali"]) == (True, "what time is it?")
    assert wake.find("Ali is coming", ["Abu Ali"])[0] is False


def test_other_names_come_from_the_wake_settings():
    assert wake.names({"wake": {"names": [" Jarvis ", "", 3]}}) == ("Jarvis",)
    assert wake.names({"wake": {"names": "Jarvis"}}) == ("Jarvis",)
    assert wake.names({}) == ()


def test_the_wake_listener_is_told_the_other_names():
    from rafiq import stt

    assert wake.prompt(()) == stt.WAKE_PROMPT
    assert "Jarvis" in wake.prompt(["Jarvis"])
