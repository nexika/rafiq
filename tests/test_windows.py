"""Windows (#193): each area's Windows calls, with every program faked; Linux and macOS stay as they were. Nothing
here runs icacls, schtasks, whoami or PowerShell for real, and the CI job on Windows runs these same fakes."""

import os
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest

from rafiq import __main__ as cli
from rafiq import config, desktop, maintain, panel, service, sock

SYSTEM = desktop.system          # as defined: conftest.py replaces it with "linux" for every test
RERUN = cli._rerun_in_utf8       # as defined: conftest.py keeps every test from starting a second Rafiq
SID = "S-1-5-21-1-2-3-1001"
TASK = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


class Runs:
    """subprocess.run as the test wants it: answers by program (or by a word in the command), records every call."""

    def __init__(self, answers=None, code=0, codes=None):
        self.calls, self.answers, self.code, self.codes = [], answers or {}, code, codes or {}

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        text = " ".join(str(a) for a in args)
        out = next((v for k, v in self.answers.items() if k in text), "")
        code = next((v for k, v in self.codes.items() if k in text), self.code)
        return subprocess.CompletedProcess(args, code, out, "")

    def args(self):
        return [a for a, _ in self.calls]


WHOAMI = {"whoami": f'"desktop\\loai","{SID}"\r\n'}


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(desktop, "system", lambda: "windows")
    monkeypatch.setattr(panel, "tcp_programs", lambda: False)


# ---------------------------------------------------------------------- the platform, and imports without POSIX

def test_the_platform_is_windows_when_sys_platform_says_win32(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    assert SYSTEM() == "windows"
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    assert SYSTEM() == "mac"
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    assert SYSTEM() == "linux"


POSIX_ONLY = """
import importlib, os, pkgutil, socket, socketserver, sys
sys.path.insert(0, sys.argv[1])
for name in ("fcntl", "pwd", "grp", "termios", "resource"):
    sys.modules[name] = None
for owner, name in ((socket, "AF_UNIX"), (socketserver, "UnixStreamServer"), (os, "getuid")):
    if hasattr(owner, name):            # on Windows itself none of them is there
        delattr(owner, name)
import rafiq
bad = []
for info in pkgutil.walk_packages(rafiq.__path__, "rafiq."):
    try:
        importlib.import_module(info.name)
    except ModuleNotFoundError as exc:      # an optional extra that isn't installed is fine; POSIX isn't
        if exc.name in ("fcntl", "pwd", "grp", "termios", "resource") or "halted" in str(exc):
            bad.append(f"{info.name}: {exc!r}")
    except Exception as exc:
        bad.append(f"{info.name}: {exc!r}")
print("\\n".join(bad))
"""


def test_every_module_imports_where_posix_only_parts_are_missing(tmp_path):
    root = str(Path(__file__).resolve().parent.parent)
    env = {k: v for k, v in os.environ.items() if k != "DBUS_SESSION_BUS_ADDRESS"}
    env["RAFIQ_HOME"] = str(tmp_path / "home")
    done = subprocess.run([sys.executable, "-I", "-c", POSIX_ONLY, root], capture_output=True, text=True,
                          timeout=120, env=env)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == ""


def test_windows_programs_get_utf8_text(windows, monkeypatch):
    seen = {}
    monkeypatch.setattr(desktop.subprocess, "run", lambda args, **kw: seen.update(kw) or
                        subprocess.CompletedProcess(args, 0, "", ""))
    desktop.run(["whoami"])
    assert seen["encoding"] == "utf-8" and "shell" not in seen and seen["timeout"]


# ---------------------------------------------------------------------- the settings folder

def test_settings_live_in_appdata_on_windows(windows, monkeypatch, tmp_path):
    monkeypatch.delenv("RAFIQ_HOME")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert config.home() == tmp_path / "Roaming" / "rafiq"


def test_rafiq_home_still_moves_the_settings_on_windows(windows, monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("RAFIQ_HOME", str(tmp_path / "elsewhere"))
    assert config.home() == tmp_path / "elsewhere"


def test_the_user_s_sid_is_read_from_whoami_even_with_a_comma_in_the_name():
    runs = Runs({"whoami": f'"desktop\\Loai, Jr","{SID}"\r\n'})
    assert desktop.user_sid(run=runs) == SID
    assert runs.args() == [[desktop.win_exe("whoami.exe"), "/user", "/fo", "csv", "/nh"]]


def test_the_settings_folder_is_closed_to_everyone_but_the_user(tmp_path):
    runs = Runs(WHOAMI)
    assert desktop.make_private(tmp_path, run=runs) is True
    icacls = desktop.win_exe("icacls.exe")
    assert runs.args()[1:] == [
        [icacls, str(tmp_path), "/setowner", f"*{SID}", "/T", "/C", "/Q"],
        [icacls, str(tmp_path), "/inheritance:r", "/grant:r", f"*{SID}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
         "/remove:g", "*S-1-1-0", "*S-1-5-11", "*S-1-5-32-545"],
        [icacls, str(tmp_path / "*"), "/reset", "/T", "/C", "/Q"]]
    assert all(kw.get("timeout") and not kw.get("shell") for _, kw in runs.calls)


def test_no_acl_is_set_when_the_sid_cannot_be_read(tmp_path):
    runs = Runs(code=1)
    assert desktop.make_private(tmp_path, run=runs) is False
    assert [a[0] for a in runs.args()] == [desktop.win_exe("whoami.exe")]


def test_a_folder_that_takes_no_acl_says_so(tmp_path):
    runs = Runs(WHOAMI, codes={"icacls": 5})          # FAT, exFAT or a network share
    assert desktop.make_private(tmp_path, run=runs) is False


@pytest.fixture
def first_time(monkeypatch, tmp_path):
    """A process that hasn't closed the settings folder yet, with its own %LOCALAPPDATA%."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(config, "_private_done", False)
    monkeypatch.setattr(config, "_private_ok", False)


def test_rafiq_makes_its_settings_private_before_a_command_on_windows(windows, first_time, monkeypatch):
    from rafiq import __main__ as cli

    made = []
    monkeypatch.setattr(desktop, "make_private", lambda folder, run=None: made.append(folder) or True)
    assert cli.main(["hooks", "shell"]) == 0
    assert made == [config.home()] and config.home_private()


def test_the_settings_are_closed_once_not_at_every_command(windows, first_time, monkeypatch):
    made = []
    monkeypatch.setattr(desktop, "make_private", lambda folder, run=None: made.append(folder) or True)
    config.make_home_private()
    monkeypatch.setattr(config, "_private_done", False)       # the next rafiq command
    monkeypatch.setattr(config, "_private_ok", False)
    config.make_home_private()
    assert made == [config.home()] and config.home_private()


def test_a_mark_inside_the_settings_folder_doesn_t_count_as_closed(windows, first_time, monkeypatch):
    """Whoever made the folder before Rafiq closed it could have put a mark there."""
    config.home().mkdir(parents=True, exist_ok=True)
    (config.home() / "private-folders.txt").write_text(str(config.home().resolve()).lower(), encoding="utf-8")
    made = []
    monkeypatch.setattr(desktop, "make_private", lambda folder, run=None: made.append(folder) or True)
    config.make_home_private()
    assert made == [config.home()]


def test_a_folder_that_takes_no_acl_is_a_warning_not_a_stop(windows, first_time, monkeypatch, capsys):
    monkeypatch.setattr(desktop, "make_private", lambda folder, run=None: False)
    config.make_home_private()
    assert "only you" in capsys.readouterr().err and not config.home_private()


def test_keys_are_not_read_from_a_settings_folder_other_accounts_can_write(windows, monkeypatch):
    config.home().mkdir(parents=True, exist_ok=True)
    service.env_file().write_text("ANTHROPIC_BASE_URL=https://elsewhere.example\n", encoding="utf-8")
    monkeypatch.setattr(config, "_private_ok", False)
    environ = {}
    service.load_env(environ)
    assert environ == {}


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8-sig"])
def test_keys_written_by_windows_powershell_are_read(windows, encoding):
    """PowerShell 5.1's `>` writes UTF-16, and Set-Content -Encoding UTF8 starts with a BOM."""
    config.home().mkdir(parents=True, exist_ok=True)
    service.env_file().write_bytes("ANTHROPIC_API_KEY=sk-test\r\n".encode(encoding))
    environ = {}
    service.load_env(environ)
    assert environ == {"ANTHROPIC_API_KEY": "sk-test"}


def test_linux_never_sets_an_acl(monkeypatch):
    monkeypatch.setattr(desktop, "make_private", lambda folder, run=None: pytest.fail("an ACL on Linux"))
    monkeypatch.setattr(config, "_private_done", False, raising=False)
    config.make_home_private()


@pytest.mark.parametrize("out, private", [
    (f"protected=True\r\nowner={SID}\r\n{SID}\r\nS-1-5-18\r\n", True),
    (f"protected=True\r\nowner={SID}\r\n{SID}\r\nS-1-5-11\r\n", False),       # Authenticated Users
    (f"protected=False\r\nowner={SID}\r\n{SID}\r\n", False),                  # still inherits from above
    (f"protected=True\r\nowner=S-1-5-21-9-9-9-1002\r\n{SID}\r\n", False),     # another account owns it
])
def test_whether_the_settings_are_private_is_read_from_their_acl(tmp_path, out, private):
    runs = Runs({"powershell": out, **WHOAMI})
    assert desktop.is_private(tmp_path, run=runs) is private
    script = next((a, kw) for a, kw in runs.calls if a[0] == desktop.win_exe("powershell.exe"))
    assert str(tmp_path) not in " ".join(script[0]) and script[1]["input"] == str(tmp_path)


# ---------------------------------------------------------------------- file locks

class FakeMsvcrt:
    LK_LOCK = 1

    def __init__(self):
        self.locked = []

    def locking(self, fd, mode, size):
        self.locked.append((fd, mode, size))


def test_a_file_lock_on_windows_waits_past_msvcrt_s_ten_seconds(windows, monkeypatch, tmp_path):
    class Busy(FakeMsvcrt):
        def locking(self, fd, mode, size):
            super().locking(fd, mode, size)
            if len(self.locked) < 3:
                raise OSError(36, "Resource deadlock avoided")      # what msvcrt says after 10 s of trying

    fake = Busy()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    with open(tmp_path / "lock", "a+") as f:
        desktop.lock_exclusive(f)
    assert len(fake.locked) == 3


def test_a_file_lock_works_without_fcntl_on_windows(windows, monkeypatch, tmp_path):
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setitem(sys.modules, "fcntl", None)
    with open(tmp_path / "lock", "a+") as f:
        f.write("x")
        desktop.lock_exclusive(f)
        assert f.tell() == 0 and fake.locked == [(f.fileno(), 1, 1)]


def test_the_morning_paper_s_lock_works_on_windows(windows, monkeypatch, tmp_path):
    from datetime import date

    from rafiq import paper_print

    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt())
    monkeypatch.setitem(sys.modules, "fcntl", None)
    lock = paper_print.DailyLock(tmp_path / "printed")
    assert lock._locked(lambda old: date(2026, 11, 4).isoformat()) and lock.printed_on(date(2026, 11, 4))


def test_keys_from_rafiq_env_are_loaded_on_windows_without_an_owner_check(windows, monkeypatch):
    config.home().mkdir(parents=True, exist_ok=True)
    service.env_file().write_text("ANTHROPIC_API_KEY=sk-test\n", encoding="utf-8")
    monkeypatch.delattr(os, "getuid", raising=False)
    environ = {}
    service.load_env(environ)
    assert environ == {"ANTHROPIC_API_KEY": "sk-test"}


# ---------------------------------------------------------------------- start at login: Task Scheduler

PYTHON = "C:\\Users\\Loai & Co\\rafiq\\.venv\\Scripts\\python.exe"


def task(xml: str):
    """The task's XML, and a find() that takes paths without the namespace."""
    root = ET.fromstring(xml)
    return root, lambda path: root.find("/".join(TASK + part for part in path.split("/")))


def test_the_logon_task_runs_rafiq_without_a_window_for_this_user(windows):
    root, find = task(service.task_xml(PYTHON, SID))
    assert find("Triggers/LogonTrigger/UserId").text == SID
    assert find("Principals/Principal/UserId").text == SID
    assert find("Principals/Principal/LogonType").text == "InteractiveToken"
    assert find("Principals/Principal/RunLevel").text == "LeastPrivilege"
    assert find("Actions/Exec/Command").text == "C:\\Users\\Loai & Co\\rafiq\\.venv\\Scripts\\pythonw.exe"
    assert find("Actions/Exec/Arguments").text == "-X utf8 -P -m rafiq run"
    assert find("Settings/ExecutionTimeLimit").text == "PT0S"
    assert find("Settings/MultipleInstancesPolicy").text == "StopExisting"
    assert find("Settings/Priority").text == "5"                     # normal, not the tasks' below-normal 7
    assert find("Settings/RestartOnFailure/Count").text


def test_the_logon_task_holds_no_key(windows, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    assert "sk-secret" not in service.task_xml(PYTHON, SID)


def test_installing_on_windows_registers_the_task_and_starts_it_once(windows):
    runs = Runs(WHOAMI)
    path = service.install(run=runs, python=PYTHON)
    schtasks = [a[1:] for a in runs.args() if a[0] == desktop.win_exe("schtasks.exe")]
    assert schtasks == [["/Create", "/XML", str(path), "/TN", f"Rafiq-{SID}", "/F"], ["/Run", "/TN", f"Rafiq-{SID}"]]
    assert path.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")        # UTF-16, as schtasks wants
    assert all(kw.get("timeout") and not kw.get("shell") for _, kw in runs.calls)


def test_installing_on_windows_reports_schtasks_s_error(windows):
    with pytest.raises(service.ServiceError, match="schtasks"):
        service.install(run=Runs(WHOAMI, codes={"/Create": 1}), python=PYTHON)


def test_uninstalling_on_windows_ends_and_deletes_the_task(windows):
    runs = Runs(WHOAMI)
    assert service.uninstall(run=runs) is True
    name = f"Rafiq-{SID}"
    assert [a[1:] for a in runs.args()[1:]] == [["/Query", "/TN", name], ["/End", "/TN", name],
                                                ["/Delete", "/TN", name, "/F"]]


def test_uninstalling_when_no_task_exists_says_so(windows):
    runs = Runs(WHOAMI, codes={"/Query": 1})
    assert service.uninstall(run=runs) is False and len(runs.calls) == 2


def test_each_account_has_its_own_task(windows):
    """Task names are the whole computer's: a second account couldn't replace the first one's "Rafiq"."""
    assert service.task_name(SID) == f"Rafiq-{SID}"
    assert service.task_name("S-1-5'; Remove-Item ~") == "Rafiq"


def test_the_service_state_on_windows_comes_from_the_scheduled_task(windows, monkeypatch):
    from rafiq import hosting

    monkeypatch.setattr(desktop, "user_sid", lambda run=None: SID)
    assert hosting._service_manager() == "schtasks"
    assert hosting._service_state(lambda cmd: (0, "Running")) == "active"
    assert hosting._service_state(lambda cmd: (0, "Ready")) == "Ready"
    assert hosting._service_state(lambda cmd: (1, "+ FullyQualifiedErrorId : NotFound")) == "not installed"
    assert f"Get-ScheduledTask -TaskName 'Rafiq-{SID}'" in " ".join(hosting.service_command())


def test_update_on_windows_restarts_the_task(windows, monkeypatch):
    from rafiq import hosting

    monkeypatch.setattr(desktop, "user_sid", lambda run=None: SID)
    command = " ".join(hosting.restart_command())
    assert f"Stop-ScheduledTask -TaskName 'Rafiq-{SID}'" in command and "Start-ScheduledTask" in command


def test_a_rafiq_without_a_console_writes_its_output_to_the_task_log(windows, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    service.detached_output()
    try:
        print("نعم", flush=True)
        assert (config.home() / "task.log").read_text(encoding="utf-8").strip() == "نعم"
    finally:
        sys.stdout.close()


def test_a_rafiq_in_a_terminal_keeps_its_output(windows):
    out = sys.stdout
    service.detached_output()
    assert sys.stdout is out


def test_the_task_log_is_ready_before_the_settings_are_closed(windows, first_time, monkeypatch):
    """A warning that the folder couldn't be closed must reach task.log, not pythonw's missing stderr."""
    from rafiq import __main__ as cli

    order = []
    monkeypatch.setattr(service, "detached_output", lambda: order.append("log"))
    monkeypatch.setattr(desktop, "make_private", lambda folder, run=None: order.append("acl") or True)
    cli.main(["hooks", "shell"])
    assert order == ["log", "acl"]


# ---------------------------------------------------------------------- desktop

def test_a_notification_on_windows_passes_the_text_on_stdin_only(windows):
    runs = Runs()
    desktop.notify('"; Remove-Item -Recurse ~ #', run=runs, which=lambda p: p)
    args, kwargs = runs.calls[0]
    assert args[0] == desktop.win_exe("powershell.exe") and "Remove-Item" not in " ".join(args)
    assert kwargs["input"] == '"; Remove-Item -Recurse ~ #'


def test_pages_and_links_open_whole_with_the_windows_opener(windows):
    """explorer.exe would split a maps address at its commas; ShellExecute through url.dll takes it whole."""
    url = "https://www.google.com/maps/@30.04,31.23,15z"
    assert desktop.open_command(url) == [desktop.win_exe("rundll32.exe"), "url.dll,FileProtocolHandler", url]


def test_windows_programs_are_named_by_their_full_path(windows, monkeypatch):
    """A bare name would be looked for in the current folder first: a whoami.exe in a download would run."""
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    assert desktop.win_exe("whoami.exe") == os.path.join("C:\\Windows", "System32", "whoami.exe")
    assert desktop.win_exe("powershell.exe").endswith(os.path.join("WindowsPowerShell", "v1.0", "powershell.exe"))
    for command in (desktop.lock_command(), desktop.power_command("shutdown"), desktop.open_command("x"),
                    desktop.clipboard_commands()[0]):
        assert command[0].startswith("C:\\Windows")


def test_programs_rafiq_starts_on_windows_open_no_window(windows, monkeypatch):
    seen = {}
    monkeypatch.setattr(desktop.subprocess, "run", lambda args, **kw: seen.update(kw) or
                        subprocess.CompletedProcess(args, 0, "", ""))
    desktop.run(["x"])
    assert seen["creationflags"] == 0x08000000              # CREATE_NO_WINDOW


def test_windows_powershell_starts_without_powershell_7_s_module_path(windows, monkeypatch):
    """Started from a pwsh 7 terminal, powershell.exe 5.1 inherits PS7's PSModulePath and can't load Get-Acl or
    Get-ScheduledTask: found on the Windows runner, where the ACL check said its permissions couldn't be read."""
    monkeypatch.setenv("PSModulePath", "C:\\Program Files\\PowerShell\\7\\Modules")
    seen = {}
    monkeypatch.setattr(desktop.subprocess, "run", lambda args, **kw: seen.update(kw) or
                        subprocess.CompletedProcess(args, 0, "", ""))
    desktop.run(desktop.powershell("Get-Acl x"))
    assert "PSModulePath" not in seen["env"] and seen["env"].get("PATH") == os.environ.get("PATH")
    seen.clear()
    desktop.run([desktop.win_exe("whoami.exe")])
    assert "env" not in seen                                  # other programs keep the environment as it is


def test_linux_programs_get_no_windows_flags():
    assert desktop.no_window() == {}


def test_lock_and_power_on_windows(windows):
    assert desktop.lock_command() == [desktop.win_exe("rundll32.exe"), "user32.dll,LockWorkStation"]
    assert desktop.power_command("shutdown") == [desktop.win_exe("shutdown.exe"), "/s", "/t", "0"]
    assert desktop.power_command("restart") == [desktop.win_exe("shutdown.exe"), "/r", "/t", "0"]
    assert desktop.power_command("suspend")[0] == desktop.win_exe("rundll32.exe")


def test_the_clipboard_on_windows_reads_and_writes_utf8_through_powershell(windows):
    read, write = desktop.clipboard_commands()
    assert read[0] == write[0] == desktop.win_exe("powershell.exe")
    assert "UTF8" in read[-1] and "UTF8" in write[-1] and "In.ReadToEnd" in write[-1]


def test_copying_arabic_on_windows_goes_in_as_utf8(windows, monkeypatch):
    from rafiq.tools import clipboard

    seen = []
    monkeypatch.setattr(clipboard.subprocess, "run", lambda args, **kw: seen.append(kw) or
                        subprocess.CompletedProcess(args, 0, "", ""))
    clipboard._run(["powershell.exe"], "مرحبا")
    clipboard._run(["powershell.exe"])
    assert all(kw["encoding"] == "utf-8" for kw in seen) and seen[0]["input"] == "مرحبا"


def test_reading_the_selection_on_windows_never_reads_the_clipboard_instead(windows):
    from rafiq import reader
    from rafiq.tools import reading

    assert reader.selection_commands() == []
    assert "copy the text" in reading.read_aloud("selection")


def test_reading_the_clipboard_on_windows_goes_through_powershell(windows):
    from rafiq import reader

    asked = []
    reader.clipboard(run=lambda args: asked.append(args) or subprocess.CompletedProcess(args, 0, "نص", ""))
    assert asked and asked[0][0] == desktop.win_exe("powershell.exe")


class Endpoint:
    def __init__(self, level=0.42, muted=0):
        self.level, self.muted = level, muted

    def GetMasterVolumeLevelScalar(self):  # noqa: N802 (the Core Audio names)
        return self.level

    def SetMasterVolumeLevelScalar(self, level, context):  # noqa: N802
        self.level = level

    def GetMute(self):  # noqa: N802
        return self.muted

    def SetMute(self, muted, context):  # noqa: N802
        self.muted = muted


def test_the_volume_is_read_and_set_on_windows(windows, monkeypatch):
    from rafiq.tools import pc

    endpoint = Endpoint()
    monkeypatch.setattr(desktop, "_endpoint", lambda: endpoint)
    assert pc.volume() == "volume 42%"
    assert pc.volume(change=10) == "volume 52%" and round(endpoint.level, 2) == 0.52
    assert pc.volume(mute=True) == "volume 52%, muted" and endpoint.muted == 1


def test_without_pycaw_the_volume_tool_says_what_to_install(windows, monkeypatch):
    from rafiq.tools import pc

    monkeypatch.setitem(sys.modules, "pycaw", None)
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", None)
    assert ".[windows]" in pc.volume()


def test_apps_on_windows_are_the_start_menu_shortcuts(tmp_path):
    mine, everyone = tmp_path / "mine", tmp_path / "all"
    (mine / "Games").mkdir(parents=True)
    everyone.mkdir()
    (mine / "Games" / "Steam.lnk").write_bytes(b"")
    (everyone / "Firefox.lnk").write_bytes(b"")
    (everyone / "steam.lnk").write_bytes(b"")
    (everyone / "readme.txt").write_bytes(b"")
    apps = desktop.win_apps([str(mine), str(everyone)])
    assert [a["name"] for a in apps] == ["Steam", "Firefox"]
    assert apps[0]["launch"] == desktop.open_command(str(mine / "Games" / "Steam.lnk"))


def test_installed_apps_on_windows_come_from_the_start_menu(windows, monkeypatch):
    from rafiq.tools import pc

    monkeypatch.setattr(desktop, "win_apps", lambda dirs=None: [{"id": "Firefox"}])
    assert pc.installed_apps() == [{"id": "Firefox"}]


def test_a_flatpak_on_windows_is_refused(windows):
    from rafiq.tools import pc

    with pytest.raises(ValueError, match="Linux"):
        pc.launch_args({"flatpak": "com.valvesoftware.Steam"})


def test_the_tools_with_no_windows_version_do_not_load(windows):
    from rafiq.tools import not_here

    assert {"gpu_status", "click_on_screen", "operate_computer", "look_at_screen", "audio_output", "media",
            "hand_gestures", "presence"} <= not_here()


# ---------------------------------------------------------------------- not on Windows yet

@pytest.mark.parametrize("argv", [["listen"], ["dictate"], ["gestures", "on"], ["panel"]])
def test_commands_that_reach_the_running_rafiq_say_they_are_not_on_windows_yet(windows, monkeypatch, capsys, argv):
    from rafiq import __main__ as cli

    monkeypatch.setattr(sock, "path", lambda: pytest.fail("looked for the socket"))
    monkeypatch.setattr(desktop, "make_private", lambda folder, run=None: True)
    assert cli.main(argv) == 1
    assert "not on Windows yet" in capsys.readouterr().err


def test_rafiq_ask_on_windows_answers_here_without_the_socket(windows, monkeypatch):
    from rafiq import __main__ as cli

    monkeypatch.setattr(sock, "path", lambda: pytest.fail("looked for the socket"))
    assert cli._ask_through_running(config.load(), "hi", None, False) is None


def test_rafiq_run_on_windows_starts_no_socket(windows, monkeypatch):
    from rafiq import __main__ as cli

    monkeypatch.setattr(panel, "serve_socket", lambda tcp: pytest.fail("made a socket"))
    assert cli._start_socket(object()) is None


def test_rafiq_run_in_a_terminal_on_windows_opens_the_panel_once(windows, monkeypatch):
    from rafiq import __main__ as cli

    opened = []
    monkeypatch.setattr(cli, "_open_browser", opened.append)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    server = panel.serve(object(), "127.0.0.1", 0, "token")
    try:
        cli._open_panel_here(server)
        port = server.server_address[1]
        assert len(opened) == 1 and opened[0].startswith(f"http://127.0.0.1:{port}/#key=")
    finally:
        server.server_close()


def test_rafiq_run_from_the_logon_task_opens_no_browser(windows, monkeypatch):
    from rafiq import __main__ as cli

    monkeypatch.setattr(cli, "_open_browser", lambda url: pytest.fail("opened a browser at logon"))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    server = panel.serve(object(), "127.0.0.1", 0, "token")
    try:
        cli._open_panel_here(server)
    finally:
        server.server_close()


def test_the_key_rafiq_opens_the_panel_with_becomes_a_session(windows):
    server = panel.serve(object(), "127.0.0.1", 0, "token")
    import threading

    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        port = server.server_address[1]
        key = server.RequestHandlerClass.new_key()
        resp = httpx.post(f"http://127.0.0.1:{port}/api/panel-key/redeem", json={"key": key}, timeout=5,
                          headers={"X-Rafiq-Panel": "1"})
        assert resp.status_code == 200 and resp.json().get("session")
        token_only = httpx.post(f"http://127.0.0.1:{port}/api/panel-key", json={}, timeout=5,
                                headers={"X-Rafiq-Token": "token"})
        assert token_only.status_code == 403          # no program may come in over TCP here
    finally:
        server.shutdown()
        server.server_close()


def test_the_panel_answers_where_there_are_no_unix_sockets(windows, monkeypatch):
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    test_the_key_rafiq_opens_the_panel_with_becomes_a_session(windows)


def test_the_phone_page_on_windows_is_off_with_a_plain_reason(windows, monkeypatch, capsys):
    from rafiq import __main__ as cli

    monkeypatch.setattr(service, "refresh", lambda run=None: pytest.fail("touched the systemd unit"))
    cfg = config.load()
    cfg["mobile"]["enabled"] = True
    cli._check_mobile(cfg)
    assert cli._start_mobile(cfg, object()) is None
    assert "not on Windows yet" in capsys.readouterr().err


def test_rafiq_mobile_on_windows_says_not_yet(windows, monkeypatch, capsys):
    from rafiq import __main__ as cli

    monkeypatch.setattr(desktop, "make_private", lambda folder, run=None: True)
    assert cli.main(["mobile", "devices"]) == 1
    assert "not on Windows yet" in capsys.readouterr().err


def test_dictation_on_windows_says_it_is_not_there_yet(windows):
    from rafiq.dictation import Keyboard, TypingFailed

    with pytest.raises(TypingFailed, match="not on Windows yet"):
        Keyboard().type("hello")


def test_the_computer_s_sound_on_windows_says_it_is_not_there_yet(windows):
    from rafiq import audio
    from rafiq.tools import songs

    with pytest.raises(FileNotFoundError, match="not on Windows yet"):
        audio.system_audio(1, spawn=lambda *a, **k: pytest.fail("started a recorder"))
    assert "not on Windows yet" in songs._no_system_sound()


# ---------------------------------------------------------------------- rafiq doctor

def win_probe(programs=(), modules=()):
    from test_doctor import Probe

    return Probe(programs=programs, modules=modules)


def doctor_checks(probe):
    from test_doctor import by_label

    from rafiq import doctor

    return by_label(doctor.integrations(config.load(), probe))


def test_doctor_on_windows_checks_the_windows_tools(windows, monkeypatch):
    monkeypatch.setattr(desktop, "is_private", lambda folder, run=None: True)
    checks = doctor_checks(win_probe(programs=("explorer.exe", "rundll32.exe", "powershell.exe"),
                                     modules=("pycaw",)))
    assert checks["Volume"].ok and checks["Open apps and pages"].ok and checks["Lock screen"].ok
    assert checks["Desktop notifications"].ok and checks["Settings folder private"].ok


def test_doctor_on_windows_says_how_to_get_the_volume(windows, monkeypatch):
    monkeypatch.setattr(desktop, "is_private", lambda folder, run=None: True)
    check = doctor_checks(win_probe())["Volume"]
    assert not check.ok and ".[windows]" in check.fix


def test_doctor_on_windows_says_when_the_settings_are_open_to_others(windows, monkeypatch):
    monkeypatch.setattr(desktop, "is_private", lambda folder, run=None: False)
    check = doctor_checks(win_probe())["Settings folder private"]
    assert not check.ok and "icacls" not in check.detail


def test_doctor_on_windows_lists_what_is_not_there_yet(windows, monkeypatch):
    monkeypatch.setattr(desktop, "is_private", lambda folder, run=None: True)
    check = doctor_checks(win_probe())["Not on Windows yet"]
    assert check.optional and "dictation" in check.detail and "phone page" in check.detail


def test_doctor_on_windows_never_opens_the_phone_socket(windows, monkeypatch):
    from rafiq import mobile

    monkeypatch.setattr(desktop, "is_private", lambda folder, run=None: True)
    monkeypatch.setattr(mobile, "socket_path", lambda: pytest.fail("looked for the phone's socket"))
    cfg = config.load()
    cfg["mobile"]["enabled"] = True
    from test_doctor import by_label

    from rafiq import doctor

    check = by_label(doctor.integrations(cfg, win_probe()))["Phone page"]
    assert check.optional and "not on Windows yet" in check.detail


def test_doctor_on_windows_asks_only_for_ffmpeg_for_songs(windows, monkeypatch):
    monkeypatch.setattr(desktop, "is_private", lambda folder, run=None: True)
    cfg = config.load()
    cfg.setdefault("songs", {})["audd_token"] = "t"
    from test_doctor import by_label

    from rafiq import doctor

    check = by_label(doctor.integrations(cfg, win_probe()))["Song recognition"]
    assert "parec" not in check.detail and "winget install" in check.fix


# ---------------------------------------------------------------------- the installer

INSTALLER = Path(__file__).resolve().parent.parent / "install.ps1"


def test_the_windows_installer_names_only_real_extras():
    import re
    import tomllib

    pyproject = (INSTALLER.parent / "pyproject.toml").read_text(encoding="utf-8")
    extras = tomllib.loads(pyproject)["project"]["optional-dependencies"]
    named = {e for group in re.findall(r"\.\[([\w,-]+)\]", INSTALLER.read_text(encoding="utf-8"))
             for e in group.split(",")}
    assert named and named <= set(extras) and "windows" in named


def test_the_windows_installer_never_runs_a_string_as_code():
    text = INSTALLER.read_text(encoding="utf-8").lower()
    assert "invoke-expression" not in text and "iex " not in text and "iex(" not in text


# ---------------------------------------------------------------------- found in review

class Child:
    def __init__(self, code=0):
        self.code, self.args, self.env = code, None, None

    def __call__(self, args, env=None):
        self.args, self.env = args, env
        return self

    def wait(self):
        return self.code


def test_rafiq_from_a_terminal_on_windows_runs_again_in_utf8_mode(windows, monkeypatch):
    """Without it every file and pipe without an encoding is in the ANSI code page: an Arabic reminder can't be
    saved, and the clipboard's Arabic can't be read."""
    monkeypatch.setattr(sys, "flags", type("Flags", (), {"utf8_mode": 0})())
    child = Child(code=3)
    assert RERUN(["ask", "ذكرني"], popen=child) == 3
    assert child.args[1:] == ["-X", "utf8", "-P", "-m", "rafiq", "ask", "ذكرني"]
    assert child.env["PYTHONUTF8"] == "1"


def test_rafiq_already_in_utf8_mode_runs_once(windows, monkeypatch):
    monkeypatch.setattr(sys, "flags", type("Flags", (), {"utf8_mode": 1})())
    assert RERUN(["ask"], popen=lambda *a, **k: pytest.fail("ran twice")) is None


def test_rafiq_on_linux_never_runs_again():
    assert RERUN(["ask"], popen=lambda *a, **k: pytest.fail("ran twice")) is None


def test_rafiq_ask_on_windows_refuses_while_rafiq_runs(windows, monkeypatch, capsys):
    """Answering here next to the running Rafiq would save timers and facts that it then overwrites."""
    from rafiq import hosting

    monkeypatch.setattr(hosting, "panel_up", lambda host, port: True)
    assert cli._ask_through_running(config.load(), "remind me in 10 minutes", None, False) == 1
    assert "can't reach it on Windows yet" in capsys.readouterr().err


def test_rafiq_ask_on_windows_answers_here_when_nothing_runs(windows, monkeypatch):
    from rafiq import hosting

    monkeypatch.setattr(hosting, "panel_up", lambda host, port: False)
    assert cli._ask_through_running(config.load(), "hi", None, False) is None


def test_the_panel_s_port_is_rafiq_s_alone_on_windows(windows, monkeypatch):
    """SO_REUSEADDR on Windows lets another program bind a port in use, and take the key the browser opens with."""
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", -5, raising=False)
    assert panel.exclusive_port()


def test_the_panel_s_port_on_linux_binds_as_before():
    assert not panel.exclusive_port()


def test_a_backup_to_a_folder_of_the_user_s_goes_in_a_closed_folder_of_its_own(windows, monkeypatch, tmp_path):
    config.home().mkdir(parents=True, exist_ok=True)
    (config.home() / "config.toml").write_text("x = 1\n", encoding="utf-8")
    closed = []
    monkeypatch.setattr(desktop, "make_private", lambda folder, run=None: closed.append(folder) or True)
    chosen = tmp_path / "Backups"
    chosen.mkdir()
    path = maintain.backup(chosen)
    assert closed == [chosen / "rafiq-backups"] and path.parent == chosen / "rafiq-backups"


def test_a_backup_where_no_acl_can_be_set_is_refused(windows, monkeypatch, tmp_path):
    config.home().mkdir(parents=True, exist_ok=True)
    (config.home() / "config.toml").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(desktop, "make_private", lambda folder, run=None: False)
    with pytest.raises(maintain.MaintenanceError, match="only you"):
        maintain.backup(tmp_path / "usb")
