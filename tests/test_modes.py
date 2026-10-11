from types import SimpleNamespace

import pytest

from rafiq import config, modes
from rafiq.assistant import Context
from rafiq.brain import Brain
from rafiq.tools import REGISTRY, load_all
from rafiq.tts import Speaker


class FakeClaude:
    """Records the model each request asks for and answers in words."""

    def __init__(self):
        self.models = []
        self.messages = self

    def create(self, model, **_kwargs):
        self.models.append(model)
        block = SimpleNamespace(type="text", text="Hi.", model_dump=lambda: {"type": "text", "text": "Hi."})
        return SimpleNamespace(content=[block])

    def stream(self, model, **_kwargs):
        """Streamed like a real reply: the text, then the whole message."""
        message = self.create(model)

        class Stream:
            text_stream = ["Hi."]

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def get_final_message(self):
                return message
        return Stream()


def claude_brain(mode: str):
    cfg = config.load()
    cfg["anthropic_api_key"] = "sk-test"
    cfg["brain"]["mode"] = "claude"
    modes.switch(cfg, mode)
    brain = Brain(cfg, {}, lambda action, tag: True)
    brain._claude = FakeClaude()
    return brain


def test_everyday_is_the_default_mode():
    assert modes.current(config.load()) == "everyday"


def test_everyday_mode_uses_the_fast_model():
    brain = claude_brain("everyday")
    brain.think("hello", "en")
    assert brain._claude.models == [brain.cfg["brain"]["claude_model"]]


def test_quality_mode_uses_the_strongest_model():
    brain = claude_brain("quality")
    brain.think("hello", "en")
    assert brain._claude.models == [brain.cfg["brain"]["quality_model"]]
    assert brain.cfg["brain"]["quality_model"] != brain.cfg["brain"]["claude_model"]


@pytest.mark.parametrize("backend", ["auto", "claude"])
def test_offline_mode_only_uses_the_local_model(backend):
    cfg = config.load()
    cfg["anthropic_api_key"] = "sk-test"
    cfg["brain"]["mode"] = backend
    modes.switch(cfg, "offline")
    assert Brain(cfg, {}, lambda action, tag: True).backends() == ["ollama"]


@pytest.mark.parametrize("engine", ["auto", "azure", "edge"])
def test_offline_mode_only_uses_local_voices(monkeypatch, engine):
    monkeypatch.setenv("AZURE_SPEECH_KEY", "key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    cfg = config.load()
    cfg["voice"]["engine"] = engine
    speaker = Speaker(cfg)
    assert "piper" in speaker.engines() and len(speaker.engines()) > 1
    modes.switch(cfg, "offline")
    assert speaker.engines() == ["piper"]


def test_switching_back_from_offline_brings_the_cloud_back():
    cfg = config.load()
    cfg["anthropic_api_key"] = "sk-test"
    modes.switch(cfg, "offline")
    modes.switch(cfg, "everyday")
    assert Brain(cfg, {}, lambda action, tag: True).backends() == ["claude", "ollama"]


def test_an_unknown_mode_is_refused_and_nothing_changes():
    cfg = config.load()
    with pytest.raises(ValueError):
        modes.switch(cfg, "turbo")
    assert modes.current(cfg) == "everyday"


@pytest.mark.parametrize("said", ["Quality", " offline ", "EVERYDAY"])
def test_the_mode_name_is_read_loosely(said):
    cfg = config.load()
    assert modes.switch(cfg, said) == said.strip().lower()


# ---------------------------------------------------------------------- the voice tool

def mode_tool():
    load_all(config.load())
    return REGISTRY["set_mode"]


def test_the_voice_tool_switches_mode_and_tells_the_assistant():
    cfg, told = config.load(), []
    ctx = Context(cfg, scheduler=None, on_mode=told.append)
    result = mode_tool().run({"mode": "quality"}, ctx)
    assert modes.current(cfg) == "quality" and told == ["quality"]
    assert "quality" in result


def test_the_voice_tool_without_a_mode_says_the_current_one():
    cfg = config.load()
    modes.switch(cfg, "offline")
    assert "offline" in mode_tool().run({}, Context(cfg, scheduler=None))
    assert modes.current(cfg) == "offline"


def test_the_voice_tool_reports_an_unknown_mode_to_the_model():
    cfg = config.load()
    assert mode_tool().run({"mode": "turbo"}, Context(cfg, scheduler=None)).startswith("error")
    assert modes.current(cfg) == "everyday"


def test_switching_mode_needs_no_confirmation():
    assert mode_tool().action({"mode": "offline"}) is None
