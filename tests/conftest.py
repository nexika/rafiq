import pytest


@pytest.fixture(autouse=True)
def on_linux(monkeypatch):
    """Every test sees Linux, whatever runs it (CI runs the suite on macOS too, #192); a test of the macOS commands
    asks for them with test_macos.py's `mac` fixture."""
    from rafiq import desktop, panel

    monkeypatch.setattr(desktop, "system", lambda: "linux")
    # The panel's port takes the token only on Linux (#234); test_sock.py asks for the macOS port itself.
    monkeypatch.setattr(panel, "tcp_programs", lambda: True)


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
    monkeypatch.setattr(pointer, "wl_copy", lambda text, **kwargs: False)     # never the user's real clipboard


@pytest.fixture(autouse=True)
def never_the_real_service(tmp_path, monkeypatch):
    """No test may rewrite the user's real systemd unit or run systemctl: a phone page that can't start refreshes
    the unit and reloads systemd (#238). A test of the service gives its own folder and systemctl."""
    from rafiq import service

    def no_systemctl(args):
        raise service.ServiceError("no real systemctl in tests")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(service, "systemctl", no_systemctl)


@pytest.fixture(autouse=True)
def never_the_real_windows(monkeypatch):
    """No test may change a real Windows machine's permissions or tasks (#193): `rafiq` closes its settings folder
    with icacls before every command there, and install-service registers a logon task. A test of either gives its
    own runner, and resets config._private_done itself."""
    from rafiq import __main__ as cli
    from rafiq import config, desktop, service

    def no_windows(args, **kwargs):
        raise OSError(f"no real {args[0]} in tests")

    monkeypatch.setattr(config, "_private_done", True)
    monkeypatch.setattr(config, "_private_ok", True)
    monkeypatch.setattr(service, "windows_run", no_windows)
    # whoami only through the runner a test gives: the task's name would otherwise ask the real one.
    real_sid = desktop.user_sid
    monkeypatch.setattr(desktop, "user_sid", lambda run=None: None if run is None else real_sid(run))
    # A test that sees Windows must not start a second Rafiq in UTF-8 mode (test_windows.py checks that itself).
    monkeypatch.setattr(cli, "_rerun_in_utf8", lambda argv, popen=None: None)
    # Nor reach a real Rafiq's pipe, or make one (#242): no pipe is there, as with no Rafiq running. test_winpipe.py
    # gives its own pipes.
    from rafiq import winpipe

    monkeypatch.setattr(winpipe, "_api", NoPipes())


class NoPipes:
    """winpipe's Win32 calls, with no pipe anywhere."""

    def my_sid(self):
        return "S-1-5-21-0-0-0-1001"

    def open(self, name, timeout):
        raise FileNotFoundError(2, "no pipe in tests")

    def create(self, name, sid, first):
        raise OSError("no real pipe in tests")

    def server_sid(self, handle):
        return None

    def client_sid(self, handle):
        return None

    def close(self, handle):
        pass


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
