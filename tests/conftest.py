import pytest


@pytest.fixture(autouse=True)
def never_the_real_screen(monkeypatch):
    """No test may take a real screenshot or click: on GNOME the screenshot portal answers without asking, so a
    test that reached it captured the user's screen without a sign. A test that needs a screenshot gives its own."""
    from rafiq import pointer
    from rafiq.tools import screen

    def no_screen(*args, **kwargs):
        raise screen.ScreenError("no real screen in tests")

    def no_pointer(*args, **kwargs):
        raise pointer.PointerError("no real pointer in tests")

    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(screen, "portal_screenshot", no_screen)
    monkeypatch.setattr(screen, "run_command", lambda args, **kwargs: (_ for _ in ()).throw(FileNotFoundError(args[0])))
    monkeypatch.setattr(pointer, "portal_click", no_pointer)
    monkeypatch.setattr(pointer, "JeepneyBus", no_pointer)


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
