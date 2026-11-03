"""macOS (#192): each area's macOS commands, with the programs faked; the Linux ones stay as they were."""

import plistlib
import subprocess

import pytest

from rafiq import desktop

SYSTEM = desktop.system          # as defined: conftest.py replaces it with "linux" for every test


class Runs:
    """subprocess.run as the test wants it: answers by program (or by a word in the AppleScript), records every call."""

    def __init__(self, answers=None, code=0):
        self.calls, self.answers, self.code = [], answers or {}, code

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        text = " ".join(args)
        out = next((v for k, v in self.answers.items() if k in text), "")
        return subprocess.CompletedProcess(args, self.code, out, "")

    def args(self):
        return [a for a, _ in self.calls]


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(desktop, "system", lambda: "mac")


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(desktop, "system", lambda: "linux")


def script(args):
    """The AppleScript lines of an osascript call, and its arguments."""
    lines = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
    return lines, args[args.index("--") + 1:]


# ---------------------------------------------------------------------- the platform and AppleScript

def test_the_platform_is_read_from_sys_platform(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    assert SYSTEM() == "mac"
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    assert SYSTEM() == "linux"


def test_words_reach_applescript_only_as_arguments():
    runs = Runs()
    desktop.osascript(['display notification (item 1 of argv) with title "Rafiq"'], '" & do shell script "rm -rf ~',
                      run=runs)
    args, kwargs = runs.calls[0]
    lines, given = script(args)
    assert args[0] == "osascript" and lines[0] == "on run argv" and lines[-1] == "end run"
    assert given == ['" & do shell script "rm -rf ~']
    assert all("rm -rf" not in line for line in lines)
    assert kwargs["timeout"] and not kwargs.get("shell")


def test_a_notification_on_macos(mac):
    runs = Runs()
    desktop.notify("Tea is ready", run=runs, which=lambda p: "/usr/bin/" + p)
    lines, given = script(runs.args()[0])
    assert "display notification" in lines[1] and given == ["Tea is ready"]


def test_a_notification_on_linux(linux):
    runs = Runs()
    desktop.notify("Tea is ready", run=runs, which=lambda p: "/usr/bin/" + p)
    assert runs.args()[0] == ["notify-send", "Rafiq", "Tea is ready"]


@pytest.mark.parametrize("system, command", [("mac", "open"), ("linux", "xdg-open")])
def test_pages_open_with_the_platform_s_opener(monkeypatch, system, command):
    monkeypatch.setattr(desktop, "system", lambda: system)
    assert desktop.open_command("https://example.com") == [command, "https://example.com"]


# ---------------------------------------------------------------------- volume

def test_the_volume_is_read_on_macos(mac):
    assert desktop.mac_volume(run=Runs({"osascript": "45,true"})) == (45, True)


def test_the_volume_is_set_with_the_level_as_an_argument(mac):
    runs = Runs()
    desktop.set_mac_volume(level=70, run=runs)
    lines, given = script(runs.args()[0])
    assert "set volume output volume" in " ".join(lines) and given == ["70"]


def test_muting_on_macos(mac):
    runs = Runs()
    desktop.set_mac_volume(mute=True, run=runs)
    lines, given = script(runs.args()[0])
    assert "output muted" in " ".join(lines) and given == ["true"]


def test_the_volume_tool_on_macos(mac, monkeypatch):
    from rafiq.tools import pc

    runs = Runs({"get volume settings": "40,false"})
    monkeypatch.setattr(pc, "_run", lambda args, timeout=10: runs(args, timeout=timeout))
    monkeypatch.setattr(desktop, "run", runs)
    assert pc.volume(change=10) == "volume 50%"
    assert not any(a[0] == "wpctl" for a in runs.args())


# ---------------------------------------------------------------------- media

def test_media_goes_to_the_player_that_is_playing(mac):
    runs = Runs({"pgrep -x": "123", '"Spotify" to return player state': "paused",
                 '"Music" to return player state': "playing"})
    assert desktop.mac_media("pause", run=runs) == "paused Music"
    lines, _ = script(runs.args()[-1])
    assert 'tell application "Music" to pause' in " ".join(lines)


def test_media_status_says_what_is_playing(mac):
    runs = Runs({"pgrep -x Spotify": "123", '"Spotify" to return player state': "playing",
                 "current track": "Fairuz - Kifak Inta"})
    assert desktop.mac_media("status", run=runs) == "Spotify is playing: Fairuz - Kifak Inta"


def test_media_with_no_player_running(mac):
    assert desktop.mac_media("next", run=Runs()) == "error: nothing is playing music or video"


def test_the_media_tool_on_macos(mac, monkeypatch):
    from rafiq.tools import pc

    runs = Runs({"pgrep -x Spotify": "123", '"Spotify" to return player state': "playing"})
    monkeypatch.setattr(desktop, "run", runs)
    assert pc.media("next") == "skipped to the next on Spotify"
    assert not any(a[0] == "playerctl" for a in runs.args())


# ---------------------------------------------------------------------- outputs

def test_outputs_on_macos_come_from_switchaudiosource(mac):
    runs = Runs({"-a -t output": "MacBook Pro Speakers\nAirPods Pro\nLG TV\n", "-c -t output": "AirPods Pro\n"})
    outputs = desktop.mac_outputs(run=runs)
    assert [(o.name, o.default) for o in outputs] == [("MacBook Pro Speakers", False), ("AirPods Pro", True),
                                                      ("LG TV", False)]


def test_the_output_tool_on_macos_switches_by_name(mac, monkeypatch):
    from rafiq.tools import pc

    runs = Runs({"-a -t output": "MacBook Pro Speakers\nAirPods Pro\n", "-c -t output": "MacBook Pro Speakers\n"})
    monkeypatch.setattr(desktop, "run", runs)
    assert pc.audio_output("my airpods") == "now playing on AirPods Pro"
    assert runs.args()[-1] == ["SwitchAudioSource", "-t", "output", "-s", "AirPods Pro"]


def test_without_switchaudiosource_the_output_tool_does_not_load_on_macos(mac, monkeypatch):
    from rafiq.tools import load_all

    monkeypatch.setattr(desktop.shutil, "which", lambda p: None)
    assert "audio_output" not in load_all({})


def test_the_output_tool_loads_on_macos_with_switchaudiosource(mac, monkeypatch):
    from rafiq.tools import load_all

    monkeypatch.setattr(desktop.shutil, "which", lambda p: "/opt/homebrew/bin/" + p)
    assert "audio_output" in load_all({})


# ---------------------------------------------------------------------- lock and power

def test_lock_and_power_on_macos(mac):
    assert desktop.lock_command() == ["pmset", "displaysleepnow"]
    for action, word in (("shutdown", "shut down"), ("restart", "restart"), ("suspend", "sleep")):
        lines, given = script(desktop.power_command(action))
        assert f'tell application "System Events" to {word}' in " ".join(lines) and given == []


def test_lock_and_power_on_linux(linux):
    assert desktop.power_command("shutdown") == ["systemctl", "poweroff"]
    assert desktop.lock_command()[0] == "loginctl"


def test_the_power_tool_runs_the_macos_command(mac, monkeypatch):
    from rafiq.tools import pc

    ran = []
    monkeypatch.setattr(pc, "_run", lambda args, timeout=10: ran.append(args) or subprocess.CompletedProcess(args, 0))
    pc.power("restart")
    assert ran and ran[0][0] == "osascript"


# ---------------------------------------------------------------------- clipboard and reading

def test_the_clipboard_on_macos(mac):
    assert desktop.clipboard_commands() == (["pbpaste"], ["pbcopy"])


def test_the_clipboard_tool_uses_pbpaste_on_macos(mac, monkeypatch):
    from rafiq.tools import clipboard

    assert clipboard.commands() == (["pbpaste"], ["pbcopy"])


def test_reading_the_selection_on_macos_never_reads_the_clipboard_instead(mac):
    """A copied password would be read aloud: the user copies the text and asks for the clipboard."""
    from rafiq import reader
    from rafiq.tools import reading

    assert reader.selection_commands() == []
    assert reading.read_aloud("selection").startswith("error:") and "copy" in reading.read_aloud("selection")


# ---------------------------------------------------------------------- keys and typing

def test_gesture_keys_on_macos_are_key_codes_with_fixed_modifiers(mac):
    lines, given = script(desktop.key_command("previous_tab"))
    assert 'key code 48 using {control down, shift down}' in " ".join(lines) and given == []
    lines, _ = script(desktop.key_command("zoom_in"))
    assert "key code 24 using {command down}" in " ".join(lines)


def test_gestures_on_macos_press_keys_and_control_media_there(mac, monkeypatch):
    from rafiq.gestures import SystemDesktop

    runs = Runs({"pgrep -x Music": "123", '"Music" to return player state': "playing"})
    monkeypatch.setattr(desktop, "run", runs)
    gestures = SystemDesktop(run=runs)
    gestures.keys("next_tab")
    gestures.media("pause")
    assert runs.args()[0][0] == "osascript" and not any(a[0] in ("playerctl", "xdotool") for a in runs.args())
    assert any("pause" in " ".join(a) for a in runs.args()[1:])


def test_typing_on_macos_passes_the_text_as_an_argument(mac):
    from rafiq.dictation import Keyboard

    runs = Runs()
    keyboard = Keyboard(env={}, which=lambda p: "/usr/bin/" + p, run=runs)
    assert keyboard.type('hello "world"') == "keystroke"
    lines, given = script(runs.args()[0])
    assert "keystroke (item 1 of argv)" in " ".join(lines) and given == ['hello "world"']


def test_typing_on_macos_pastes_when_keystrokes_fail(mac):
    from rafiq.dictation import Keyboard

    class Fails(Runs):
        def __call__(self, args, **kwargs):
            done = super().__call__(args, **kwargs)
            if "keystroke (item 1 of argv)" in " ".join(args):
                return subprocess.CompletedProcess(args, 1, "", "not allowed")
            return done

    runs = Fails()
    assert Keyboard(env={}, which=lambda p: "/usr/bin/" + p, run=runs).type("hello") == "paste"
    assert runs.args()[1][0] == "pbcopy" and runs.calls[1][1]["input"] == "hello"
    assert "key code 9 using {command down}" in " ".join(runs.args()[2])


def test_arabic_and_accents_on_macos_are_pasted_never_keystroked(mac):
    """keystroke goes through the keyboard layout: Arabic on a US layout would come out as other letters."""
    from rafiq.dictation import Keyboard

    for text in ("مرحبا", "café"):
        runs = Runs()
        assert Keyboard(env={}, which=lambda p: "/usr/bin/" + p, run=runs).type(text) == "paste"
        assert runs.args()[0][0] == "pbcopy" and not any("keystroke (item 1" in " ".join(a) for a in runs.args())


# ---------------------------------------------------------------------- apps

def test_apps_on_macos_are_the_app_bundles(mac, tmp_path):
    (tmp_path / "Safari.app").mkdir()
    (tmp_path / "Visual Studio Code.app").mkdir()
    (tmp_path / "notes.txt").write_text("")
    entries = desktop.mac_apps([str(tmp_path)])
    assert [e["name"] for e in entries] == ["Safari", "Visual Studio Code"]
    assert entries[0]["launch"] == ["open", "-a", str(tmp_path / "Safari.app")]


def test_open_app_on_macos_opens_the_bundle(mac, tmp_path, monkeypatch):
    from rafiq.tools import pc

    (tmp_path / "Calculator.app").mkdir()
    monkeypatch.setattr(desktop, "MAC_APP_DIRS", [str(tmp_path)])
    spawned = []
    monkeypatch.setattr(pc, "_spawn", spawned.append)
    assert pc.open_app("calculator") == "opened Calculator"
    assert spawned == [["open", "-a", str(tmp_path / "Calculator.app")]]


def test_a_steam_game_on_macos_opens_through_open(mac):
    from rafiq.tools import pc

    assert pc.launch_args({"steam": 49520}) == ["open", "steam://rungameid/49520"]


def test_a_flatpak_on_macos_is_refused(mac):
    from rafiq.tools import pc

    with pytest.raises(ValueError, match="Linux"):
        pc.launch_args({"flatpak": "com.valvesoftware.Steam"})


def test_open_in_browser_on_macos_uses_open(mac, monkeypatch):
    from rafiq.tools import web

    spawned = []
    monkeypatch.setattr(web.subprocess, "Popen", lambda args, **kwargs: spawned.append(args))
    web.open_in_browser("https://example.com")
    assert spawned == [["open", "https://example.com"]]


# ---------------------------------------------------------------------- the computer's own sound

class Device(dict):
    pass


def test_the_computer_s_sound_on_macos_comes_from_blackhole(mac, monkeypatch):
    import numpy as np

    from rafiq import audio

    recorded = []

    class FakeSD:
        @staticmethod
        def query_devices():
            return [{"name": "MacBook Pro Microphone", "max_input_channels": 1},
                    {"name": "BlackHole 2ch", "max_input_channels": 2, "default_samplerate": 48000.0}]

        @staticmethod
        def rec(frames, samplerate, channels, dtype, device):
            recorded.append((frames, samplerate, channels, device))
            return np.zeros((frames, channels), dtype=np.float32)

        @staticmethod
        def wait():
            pass

        @staticmethod
        def stop():
            pass

    monkeypatch.setattr(audio, "_sounddevice", lambda: FakeSD)
    out = audio.system_audio(2.0)
    # At BlackHole's own 48 kHz, then 16 kHz like the microphone.
    assert recorded == [(2 * 48000, 48000, 2, 1)] and out.shape == (2 * audio.RATE,) and out.dtype == np.float32


def test_without_blackhole_the_computer_s_sound_says_how_to_set_it_up(mac, monkeypatch):
    from rafiq import audio

    class FakeSD:
        @staticmethod
        def query_devices():
            return [{"name": "MacBook Pro Microphone", "max_input_channels": 1}]

    monkeypatch.setattr(audio, "_sounddevice", lambda: FakeSD)
    with pytest.raises(FileNotFoundError, match="BlackHole"):
        audio.system_audio(1.0)


# ---------------------------------------------------------------------- presence

def test_presence_on_macos_uses_ping_in_milliseconds_and_arp(mac):
    from rafiq import presence

    runs = Runs({"arp": "? (192.168.1.20) at aa:bb:cc:dd:ee:ff on en0 ifscope [ethernet]"}, code=1)
    assert presence.ping_command("192.168.1.20") == ["ping", "-c", "1", "-W", "1000", "192.168.1.20"]
    assert presence.neighbour_command("192.168.1.20") == ["arp", "-n", "192.168.1.20"]
    assert presence.neighbour_present(runs(["arp", "-n", "192.168.1.20"]).stdout, "AA:BB:CC:DD:EE:FF")


def test_presence_on_linux_is_unchanged(linux):
    from rafiq import presence

    assert presence.ping_command("10.0.0.2") == ["ping", "-c", "1", "-W", "1", "10.0.0.2"]
    assert presence.neighbour_command("10.0.0.2") == ["ip", "neigh", "show", "10.0.0.2"]


# ---------------------------------------------------------------------- screenshots

def test_a_screenshot_on_macos_comes_from_screencapture(mac):
    from rafiq.tools import screen

    assert ["screencapture", "-x", "-t", "png"] in screen.commands()


# ---------------------------------------------------------------------- start at login

def test_the_launch_agent_runs_rafiq_and_keeps_it_running(mac, tmp_path, monkeypatch):
    from rafiq import service

    monkeypatch.setenv("HOME", str(tmp_path))
    agent = plistlib.loads(service.plist_text("/opt/homebrew/bin/python3").encode())
    assert agent["Label"] == service.LABEL
    assert agent["ProgramArguments"] == ["/opt/homebrew/bin/python3", "-P", "-m", "rafiq", "run"]
    assert agent["RunAtLoad"] is True and agent["KeepAlive"] == {"SuccessfulExit": False}
    assert agent["EnvironmentVariables"]["RAFIQ_ENV_FILE"] == str(service.env_file())
    assert "KEY" not in str(agent)                      # keys stay in rafiq.env, never in the agent


def test_installing_on_macos_bootstraps_the_agent_once(mac, tmp_path, monkeypatch):
    from rafiq import service

    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    path = service.install(run=lambda args: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    assert path == tmp_path / "Library" / "LaunchAgents" / f"{service.LABEL}.plist" and path.is_file()
    domain = f"gui/{service.os.getuid()}"
    assert calls == [["bootout", f"{domain}/{service.LABEL}"], ["bootstrap", domain, str(path)]]


def test_uninstalling_on_macos_boots_it_out_and_removes_the_agent(mac, tmp_path, monkeypatch):
    from rafiq import service

    monkeypatch.setenv("HOME", str(tmp_path))
    done = lambda args: subprocess.CompletedProcess(args, 0, "", "")  # noqa: E731
    path = service.install(run=done)
    calls = []
    assert service.uninstall(run=lambda args: calls.append(args) or done(args)) is True
    assert not path.exists() and calls[0][0] == "bootout"


def test_keys_from_rafiq_env_are_loaded_at_start_without_overriding_the_environment(tmp_path):
    from rafiq import service

    env_file = tmp_path / "rafiq.env"
    env_file.write_text("# keys\nANTHROPIC_API_KEY=sk-from-file\nexport ELEVENLABS_API_KEY=\"el-key\"\nbad line\n")
    environ = {"RAFIQ_ENV_FILE": str(env_file), "ELEVENLABS_API_KEY": "already-set"}
    service.load_env(environ)
    assert environ["ANTHROPIC_API_KEY"] == "sk-from-file" and environ["ELEVENLABS_API_KEY"] == "already-set"


def test_the_service_state_on_macos_comes_from_launchctl(mac):
    from rafiq import hosting

    assert hosting.service_command() == ["launchctl", "print", f"gui/{hosting.os.getuid()}/com.nexika.rafiq"]


# ---------------------------------------------------------------------- tools with no macOS version

def test_the_gpu_and_click_tools_do_not_load_on_macos(mac):
    from rafiq.tools import load_all

    tools = load_all({})
    assert "gpu_status" not in tools and "click_on_screen" not in tools
    assert "Click on screen" not in doctor_checks(mac_probe())


def test_on_linux_the_same_tools_load_as_before(linux, monkeypatch):
    from rafiq.tools import load_all

    tools = load_all({})
    assert "gpu_status" in tools and "audio_output" in tools


# ---------------------------------------------------------------------- rafiq doctor

def mac_probe(programs=(), accessibility="true", inputs=()):
    from test_doctor import Probe

    class MacProbe(Probe):
        def run(self, args, timeout=5):
            if args[0] == "osascript":
                return subprocess.CompletedProcess(args, 0, accessibility + "\n", "")
            return super().run(args, timeout)

        def input_devices(self):
            return list(inputs)

    return MacProbe(programs=programs)


def doctor_checks(probe):
    from test_doctor import by_label

    from rafiq import config, doctor

    return by_label(doctor.integrations(config.load(), probe))


def test_doctor_on_macos_checks_the_macos_programs(mac):
    checks = doctor_checks(mac_probe(programs=("osascript", "open", "pmset")))
    assert checks["Volume"].ok and checks["Volume"].detail == "osascript"
    assert checks["Open apps and pages"].ok and checks["Lock screen"].ok and checks["Desktop notifications"].ok


def test_doctor_on_macos_says_how_to_switch_outputs(mac):
    check = doctor_checks(mac_probe())["Switch outputs"]
    assert not check.ok and check.optional and "brew install switchaudio-osx" in check.fix


def test_doctor_on_macos_checks_the_accessibility_permission(mac):
    assert doctor_checks(mac_probe(programs=("osascript",)))["Typing and keys (Accessibility)"].ok
    check = doctor_checks(mac_probe(programs=("osascript",), accessibility="false"))["Typing and keys (Accessibility)"]
    assert not check.ok and "Accessibility" in check.fix


def test_doctor_on_macos_finds_blackhole_for_the_mac_s_own_sound(mac):
    assert doctor_checks(mac_probe(inputs=["MacBook Pro Microphone", "BlackHole 2ch"]))["The Mac's own sound"].ok
    check = doctor_checks(mac_probe(inputs=["MacBook Pro Microphone"]))["The Mac's own sound"]
    assert not check.ok and check.optional and "blackhole-2ch" in check.fix


def test_doctor_on_linux_is_unchanged(linux):
    checks = doctor_checks(mac_probe(programs=("wpctl",)))
    assert checks["Volume"].detail == "wpctl" and "Switch outputs" not in checks


# ---------------------------------------------------------------------- after the security review

def test_installing_on_macos_makes_the_key_file_and_the_log_private(mac, tmp_path, monkeypatch):
    import stat

    from rafiq import config, service

    monkeypatch.setenv("HOME", str(tmp_path))
    service.install(run=lambda args: subprocess.CompletedProcess(args, 0, "", ""))
    assert stat.S_IMODE(config.home().stat().st_mode) == 0o700
    for path in (service.env_file(), config.home() / "launchd.log"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_installing_again_keeps_the_keys_already_in_rafiq_env(mac, tmp_path, monkeypatch):
    from rafiq import service

    monkeypatch.setenv("HOME", str(tmp_path))
    done = lambda args: subprocess.CompletedProcess(args, 0, "", "")  # noqa: E731
    service.install(run=done)
    service.env_file().write_text("ANTHROPIC_API_KEY=sk-keep\n")
    service.install(run=done)
    assert service.env_file().read_text() == "ANTHROPIC_API_KEY=sk-keep\n"


def test_the_launch_agent_keeps_its_files_private_and_finds_homebrew(mac):
    from rafiq import service

    agent = plistlib.loads(service.plist_text("/usr/bin/python3").encode())
    assert agent["Umask"] == 0o077 and agent["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:")


def test_a_key_file_others_can_read_is_made_private_before_it_is_read(tmp_path):
    import stat

    from rafiq import service

    env_file = tmp_path / "rafiq.env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-x\n")
    env_file.chmod(0o644)
    environ = {"RAFIQ_ENV_FILE": str(env_file)}
    service.load_env(environ)
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600 and environ["ANTHROPIC_API_KEY"] == "sk-x"


def test_a_key_file_of_another_user_is_not_read(tmp_path, monkeypatch):
    from rafiq import service

    env_file = tmp_path / "rafiq.env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-planted\n")
    monkeypatch.setattr(service.os, "getuid", lambda: 12345)
    environ = {"RAFIQ_ENV_FILE": str(env_file)}
    service.load_env(environ)
    assert "ANTHROPIC_API_KEY" not in environ


def test_a_long_dictation_gets_time_to_be_typed(mac):
    from rafiq.dictation import Keyboard

    runs = Runs()
    Keyboard(env={}, which=lambda p: "/usr/bin/" + p, run=runs).type("x" * 600)
    assert runs.calls[0][1]["timeout"] >= 30



# ---------------------------------------------------------------------- after the code review

def test_the_default_gesture_runner_controls_media_on_macos(mac, monkeypatch):
    """The gestures' own runner takes no timeout and returns nothing: media goes through the platform's runner."""
    from rafiq.gestures import SystemDesktop

    runs = Runs({"pgrep -x Music": "123", '"Music" to return player state': "playing"})
    monkeypatch.setattr(desktop, "run", runs)
    SystemDesktop().media("play-pause")
    assert 'tell application "Music" to playpause' in " ".join(runs.args()[-1])


def test_a_player_that_is_not_running_is_never_named_in_a_script(mac):
    runs = Runs({"pgrep -x Music": "123", '"Music" to return player state': "playing"})
    desktop.mac_media("pause", run=runs)
    assert not any("Spotify" in " ".join(a) for a in runs.args() if a[0] == "osascript")


def test_reinstalling_waits_for_launchd_to_let_the_old_agent_go(mac, tmp_path, monkeypatch):
    from rafiq import service

    monkeypatch.setenv("HOME", str(tmp_path))
    codes, slept = [5, 37, 0], []

    def run(args):
        code = codes.pop(0) if args[0] == "bootstrap" else 0
        return subprocess.CompletedProcess(args, code, "", "Bootstrap failed: 5: Input/output error" if code else "")

    service._install_mac(run, None, sleep=slept.append)
    assert codes == [] and len(slept) == 2


def test_reinstalling_gives_up_with_launchd_s_error(mac, tmp_path, monkeypatch):
    from rafiq import service

    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(service.ServiceError, match="Input/output"):
        service._install_mac(lambda args: subprocess.CompletedProcess(
            args, 5 if args[0] == "bootstrap" else 0, "", "Bootstrap failed: 5: Input/output error"), None,
            sleep=lambda s: None)


def test_an_output_without_its_own_volume_says_so_on_macos(mac, monkeypatch):
    from rafiq.tools import pc

    monkeypatch.setattr(desktop, "run", Runs({"get volume settings": "missing value,false"}))
    assert pc.volume(change=10).startswith("error:") and "volume control" in pc.volume()


def test_a_notification_that_hangs_on_linux_is_not_an_error(linux):
    def hangs(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 5)

    desktop.notify("hi", run=hangs, which=lambda p: "/usr/bin/" + p)


def test_without_blackhole_song_recognition_names_the_mac_fix(mac):
    from rafiq.tools import songs

    assert "blackhole" in songs._no_system_sound().lower() and "parec" not in songs._no_system_sound()


def test_on_macos_the_screen_prompt_knows_a_bare_desktop_means_no_permission(mac):
    from rafiq.tools import screen

    assert "Screen Recording" in screen._mac_note()


def test_the_install_service_message_points_to_the_launchd_log(mac, tmp_path, monkeypatch, capsys):
    from rafiq import service
    from rafiq.__main__ import main

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(service, "launchctl", lambda args: subprocess.CompletedProcess(args, 0, "", ""))
    main(["install-service"])
    assert "launchd.log" in capsys.readouterr().out
