import pytest


@pytest.fixture(autouse=True)
def rafiq_home(tmp_path, monkeypatch):
    """Every test gets its own settings folder and no real keys."""
    monkeypatch.setenv("RAFIQ_HOME", str(tmp_path / "rafiq"))
    for key in ("ANTHROPIC_API_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION", "HASS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path / "rafiq"
