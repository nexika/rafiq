import pytest


@pytest.fixture(autouse=True)
def rafiq_home(tmp_path, monkeypatch):
    """Every test gets its own settings folder, its own (empty) prof data, no prof installed unless it gives
    one, and no real keys."""
    from rafiq import prof

    monkeypatch.setenv("RAFIQ_HOME", str(tmp_path / "rafiq"))
    monkeypatch.setenv("PROF_HOME", str(tmp_path / "prof"))
    monkeypatch.setattr(prof, "PLUGINS", tmp_path / "no-prof-installed")
    for key in ("ANTHROPIC_API_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION", "ELEVENLABS_API_KEY",
                "OPENAI_API_KEY", "HOME_ASSISTANT_TOKEN", "RAFIQ_AGENT_TOKEN", "DISCORD_BOT_TOKEN",
                "AUDD_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path / "rafiq"
