import pytest
from test_brain_panel import make_brain, ollama, panel, request  # noqa: F401  (fixtures)

from rafiq import config, persona
from rafiq.brain import with_note
from rafiq.tools import REGISTRY, load_all


def test_neutral_is_the_default_and_adds_nothing_to_the_note():
    cfg = config.load()
    assert persona.active(cfg) == "neutral"
    assert persona.instruction("neutral") == ""


def test_the_config_file_sets_the_starting_style():
    cfg = config.load()
    cfg["brain"]["persona"] = "butler"
    assert persona.active(cfg) == "butler"


def test_an_unknown_style_in_the_config_falls_back_to_neutral():
    cfg = config.load()
    cfg["brain"]["persona"] = "pirate"
    assert persona.active(cfg) == "neutral"


def test_a_chosen_style_is_remembered_over_the_config():
    cfg = config.load()
    persona.choose("concise")
    assert persona.active(cfg) == "concise"


def test_choosing_an_unknown_style_is_refused():
    with pytest.raises(ValueError):
        persona.choose("pirate")
    assert persona.active(config.load()) == "neutral"


@pytest.mark.parametrize("name", ["concise", "butler"])
def test_every_style_but_neutral_has_an_instruction(name):
    assert persona.instruction(name)


def test_the_style_goes_in_the_note_and_the_language_still_comes_last():
    note = with_note("hello", "ar-EG", style=persona.instruction("butler"))
    assert "butler" in note
    assert note.endswith("[Answer in Egyptian Arabic.]")


def test_the_style_changes_the_request_not_the_cached_prefix(ollama):  # noqa: F811
    script, requests = ollama
    script += [{"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"}]
    brain = make_brain()
    brain.think("hello", "en")
    persona.choose("butler")
    brain.think("hello", "en")
    assert requests[0]["messages"][0] == requests[1]["messages"][0]
    assert requests[0]["tools"] == requests[1]["tools"]
    assert "butler" not in requests[0]["messages"][-1]["content"]
    assert "butler" in requests[1]["messages"][-1]["content"]


def test_the_tool_switches_the_style_without_asking():
    load_all({})
    tool = REGISTRY["set_personality"]
    assert tool.action({"style": "butler"}) is None
    assert tool.schema()["properties"]["style"]["enum"] == list(persona.PERSONAS)
    assert "butler" in tool.run({"style": "butler"}, None)
    assert persona.active(config.load()) == "butler"
    assert tool.run({"style": "pirate"}, None).startswith("error")


def test_the_panel_sets_the_style(panel):  # noqa: F811
    assistant, port = panel
    assert request(port, "/api/persona", {"name": "concise"})[0] == 200
    assert persona.active(config.load()) == "concise"
    assert assistant.events.history[-1]["kind"] == "persona"
    assert assistant.events.history[-1]["persona"] == "concise"


def test_the_panel_refuses_an_unknown_style(panel):  # noqa: F811
    _, port = panel
    assert request(port, "/api/persona", {"name": "pirate"})[0] == 400
    assert persona.active(config.load()) == "neutral"


def test_the_page_offers_every_style(panel):  # noqa: F811
    _, port = panel
    _, page = request(port, "/")
    for name in persona.PERSONAS:
        assert f'value="{name}"' in page
