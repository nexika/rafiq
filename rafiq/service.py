"""Start Rafiq at login: a systemd user service (rafiq install-service, rafiq uninstall-service); on macOS a
launchd agent, ~/Library/LaunchAgents/com.nexika.rafiq.plist, which launchd starts at login and again if it fails.
launchd reads no environment file, so the agent names rafiq.env in RAFIQ_ENV_FILE and Rafiq loads it as it
starts (load_env); the keys stay in that file, never in the agent.

systemd keeps one Rafiq running, restarts it when it fails and sends its output to the journal
(journalctl --user -u rafiq). A user service does not see the shell's exported keys, so they go in
an optional file, ~/.config/rafiq/rafiq.env (KEY=value lines), which the unit reads but never copies.

On Windows (#193), a Task Scheduler task named "rafiq" that starts at this user's logon, without a window
(pythonw.exe), and writes Rafiq's output to task.log in the settings folder. It can't set variables, so Rafiq reads
rafiq.env from the settings folder itself.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import config

NAME = "rafiq.service"
LABEL = "com.nexika.rafiq"

Systemctl = Callable[[list[str]], subprocess.CompletedProcess]


class ServiceError(RuntimeError):
    pass


def systemctl(args: list[str]) -> subprocess.CompletedProcess:
    exe = shutil.which("systemctl")
    if not exe:
        raise ServiceError("systemctl not found: Rafiq can start at login only on a systemd system")
    return subprocess.run([exe, "--user", *args], capture_output=True, text=True, timeout=30, check=False)


def unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "systemd" / "user" / NAME


def env_file() -> Path:
    return config.home() / "rafiq.env"


def _quote(value: str) -> str:
    """One systemd argument: % is a specifier, spaces and quotes need double quotes."""
    value = value.replace("%", "%%")
    if any(c in value for c in ' \t"\\\''):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def unit_text(python: str | None = None) -> str:
    python = python or sys.executable
    lines = [
        "[Unit]",
        "Description=Rafiq voice assistant",
        # The microphone and speakers come from the user's PipeWire; harmless where it is absent.
        "After=pipewire.service pipewire-pulse.service",
        "",
        "[Service]",
        # -P: systemd starts in ~, and a checkout at ~/rafiq would otherwise be imported as the package.
        f"ExecStart={_quote(python)} -P -m rafiq run",
        "Restart=on-failure",
        "RestartSec=5",
        # 78 (EX_CONFIG): a setting to fix, such as the phone page's; starting again would only fail again.
        "RestartPreventExitStatus=78",
        # Rafiq runs in about 150 MB. A runaway one once reached 10.5 GB of a 14 GB laptop and the kernel
        # killed it; with these caps systemd squeezes, then restarts, Rafiq alone.
        "MemoryHigh=1500M",
        "MemoryMax=2G",
        "Environment=PYTHONUNBUFFERED=1",      # print() reaches the journal at once
        f"EnvironmentFile=-{env_file()}",
    ]
    if os.environ.get("RAFIQ_HOME"):
        lines.append("Environment=" + _quote(f"RAFIQ_HOME={config.home()}"))
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def _call(run: Systemctl, *args: str) -> None:
    result = run(list(args))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ServiceError(f"systemctl --user {' '.join(args)} failed: {detail}")


def refresh(run: Systemctl | None = None) -> bool:
    """Give a unit installed before #238 its `RestartPreventExitStatus=78`, so a setting to fix stops Rafiq instead
    of restarting it every 5 s. Only that line is added; the rest, its python included, stays as installed. True when
    the unit changed and systemd reloaded it."""
    path = unit_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if "RestartPreventExitStatus=" in text or "\n[Service]" not in "\n" + text:
        return False
    lines = text.splitlines(keepends=True)
    after = next((i for i, line in enumerate(lines) if line.startswith("RestartSec=")),
                 next(i for i, line in enumerate(lines) if line.strip() == "[Service]"))
    lines.insert(after + 1, "RestartPreventExitStatus=78\n")
    try:
        path.write_text("".join(lines), encoding="utf-8")
        _call(run or systemctl, "daemon-reload")
    except (OSError, subprocess.SubprocessError, ServiceError):
        return False
    return True


# ---------------------------------------------------------------------- macOS: launchd

def launchctl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30, check=False)


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def plist_text(python: str | None = None) -> str:
    # launchd's PATH is /usr/bin:/bin:/usr/sbin:/sbin: Homebrew's programs (ffplay, SwitchAudioSource) are added.
    env = {"PYTHONUNBUFFERED": "1", "RAFIQ_ENV_FILE": str(env_file()),
           "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    if os.environ.get("RAFIQ_HOME"):
        env["RAFIQ_HOME"] = str(config.home())
    logs = config.home() / "launchd.log"
    agent = {
        "Label": LABEL,
        # -P: launchd starts in /, but a checkout as the working folder would otherwise be imported as the package.
        "ProgramArguments": [python or sys.executable, "-P", "-m", "rafiq", "run"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},      # again after a failure, not after `rafiq` quits on purpose
        "ThrottleInterval": 5,
        # Files Rafiq and launchd make (the log, anything without its own mode) are the user's only: a Mac's home
        # folder can be read by every account on it.
        "Umask": 0o077,
        "EnvironmentVariables": env,
        "StandardOutPath": str(logs),
        "StandardErrorPath": str(logs),
    }
    return plistlib.dumps(agent).decode()


def _private_file(path: Path) -> None:
    """The file, created empty if missing, readable only by the user."""
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    path.chmod(0o600)


BOOTSTRAP_TRIES = 10          # bootout returns before launchd has let the old agent go: bootstrap then fails a moment
BOOTSTRAP_WAIT = 0.5          # with 5 (Input/output error) or 37 (Operation already in progress)


def _install_mac(run, python: str | None, sleep=time.sleep) -> Path:
    home = config.home()
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)                                  # it holds config.toml and rafiq.env, with the keys
    _private_file(env_file())
    _private_file(home / "launchd.log")
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist_text(python), encoding="utf-8")
    run(["bootout", f"{_domain()}/{LABEL}"])          # the one running, if any: never two
    for attempt in range(BOOTSTRAP_TRIES):
        done = run(["bootstrap", _domain(), str(path)])
        if done.returncode not in (5, 37) or attempt == BOOTSTRAP_TRIES - 1:
            break
        sleep(BOOTSTRAP_WAIT)
    if done.returncode != 0:
        raise ServiceError(f"launchctl bootstrap failed: {(done.stderr or done.stdout or '').strip()}")
    return path


def _uninstall_mac(run) -> bool:
    path = plist_path()
    if not path.is_file():
        return False
    run(["bootout", f"{_domain()}/{LABEL}"])
    path.unlink()
    return True


def _env_text(data: bytes) -> str:
    """rafiq.env as text: Windows PowerShell 5.1's `>` writes UTF-16 and its Set-Content -Encoding UTF8 a BOM, which
    would otherwise stop Rafiq or hide the first key."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def load_env(environ=None) -> None:
    """KEY=value lines from the file in RAFIQ_ENV_FILE (rafiq.env), for keys a launchd agent can't otherwise
    get; what the environment already has wins. Nothing is printed or logged."""
    from . import desktop

    environ = os.environ if environ is None else environ
    windows = desktop.system() == "windows"
    # Task Scheduler can't set a variable for its task, so on Windows the file in the settings folder is read
    # without being named. The folder's ACL keeps it the user's (config.make_home_private), not an owner check: where
    # that ACL couldn't be set, another account could have written it (a proxy, another API address), and it is
    # left unread.
    name = environ.get("RAFIQ_ENV_FILE") or (str(env_file()) if windows else "")
    if not name or (windows and not config.home_private()):
        return
    path = Path(name)
    try:
        if not windows:
            info = path.stat()
            if info.st_uid != os.getuid():       # someone else's file: its keys could be anyone's
                return
            if info.st_mode & 0o077:             # written as other files are (0644): the keys are the user's only
                path.chmod(0o600)
        lines = _env_text(path.read_bytes()).splitlines()
    except (OSError, UnicodeError):
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        environ.setdefault(key, value)


def install(run: Systemctl | None = None, python: str | None = None) -> Path:
    """Write the unit, enable it at login and (re)start it now. Installing again replaces the unit
    and restarts the one Rafiq, so there are never two. On macOS, the launchd agent."""
    from . import desktop

    if desktop.system() == "windows":
        return _install_windows(run or globals()["windows_run"], python)
    if desktop.system() == "mac":
        return _install_mac(run or launchctl, python)
    run = run or systemctl
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(python), encoding="utf-8")
    _call(run, "daemon-reload")
    _call(run, "enable", NAME)
    _call(run, "restart", NAME)
    return path


def uninstall(run: Systemctl | None = None) -> bool:
    """Stop Rafiq, stop starting it at login and remove the unit. False when it was not installed."""
    from . import desktop

    if desktop.system() == "windows":
        return _uninstall_windows(run or globals()["windows_run"])
    if desktop.system() == "mac":
        return _uninstall_mac(run or launchctl)
    run = run or systemctl
    path = unit_path()
    if not path.is_file():
        return False
    _call(run, "disable", "--now", NAME)
    path.unlink()
    _call(run, "daemon-reload")
    return True


# ---------------------------------------------------------------------- Windows: Task Scheduler

TASK = "Rafiq"


def task_name(sid: str | None = None) -> str:
    """One task per account: Task Scheduler's names are the whole computer's, and another account's "Rafiq" task
    couldn't be replaced. The SID is digits and dashes only, so the name is safe inside a script."""
    import re

    from . import desktop

    sid = sid or desktop.user_sid() or ""
    return f"{TASK}-{sid}" if re.fullmatch(r"S-1(-\d+)+", sid) else TASK
TASK_LOG_BYTES = 5_000_000          # task.log starts again past this when Rafiq starts, at each logon


def windows_run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    from . import desktop

    kwargs.setdefault("timeout", 30)
    return desktop.run(args, **kwargs)


def task_path() -> Path:
    return config.home() / "rafiq-task.xml"


def _pythonw(python: str) -> str:
    """The windowless interpreter beside python.exe: a task that ran python.exe would open a console at logon."""
    from pathlib import PureWindowsPath

    path = PureWindowsPath(python)
    return python if path.name.lower() == "pythonw.exe" else str(path.with_name("pythonw.exe"))


def task_xml(python: str | None = None, sid: str = "") -> str:
    """The task: at this user's logon, as this user without raised rights, for as long as it runs, one at a time.
    Every value goes in through ElementTree, so a path with & or < in it stays a path."""
    import xml.etree.ElementTree as ET

    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    task = ET.Element("Task", {"version": "1.2", "xmlns": ns})

    def add(parent, tag: str, text: str | None = None, **attrs):
        child = ET.SubElement(parent, tag, attrs)
        if text is not None:
            child.text = text
        return child

    add(add(task, "RegistrationInfo"), "Description", "Rafiq voice assistant")
    trigger = add(add(task, "Triggers"), "LogonTrigger")
    add(trigger, "Enabled", "true")
    add(trigger, "UserId", sid)
    principal = add(add(task, "Principals"), "Principal", id="Author")
    add(principal, "UserId", sid)
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(task, "Settings")
    # Installing again runs the task again: the new Rafiq replaces the old one, as `systemctl restart` does.
    add(settings, "MultipleInstancesPolicy", "StopExisting")
    add(settings, "DisallowStartIfOnBatteries", "false")
    add(settings, "StopIfGoingOnBatteries", "false")
    add(settings, "ExecutionTimeLimit", "PT0S")            # no limit: Task Scheduler would stop it after 3 days
    add(settings, "Priority", "5")                         # normal; a task's default, 7, is below normal and would
    #                                                        starve the voice loop while a game or a build runs
    restart = add(settings, "RestartOnFailure")
    add(restart, "Interval", "PT1M")
    add(restart, "Count", "3")
    add(settings, "Enabled", "true")
    action = add(add(task, "Actions", Context="Author"), "Exec")
    add(action, "Command", _pythonw(python or sys.executable))
    # -X utf8: Rafiq's files and pipes in UTF-8 whatever the code page; -P: the working folder isn't imported.
    add(action, "Arguments", "-X utf8 -P -m rafiq run")
    add(action, "WorkingDirectory", str(config.home()))
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(task, encoding="unicode")


def _schtasks(run, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    from . import desktop

    done = run([desktop.win_exe("schtasks.exe"), *args], timeout=30)
    if check and done.returncode != 0:
        raise ServiceError(f"schtasks {' '.join(args[:1])} failed: {(done.stderr or done.stdout or '').strip()}")
    return done


def _install_windows(run, python: str | None) -> Path:
    from . import desktop

    sid = desktop.user_sid(run)
    if not sid:
        raise ServiceError("couldn't read this account's SID from `whoami /user`")
    home = config.home()
    home.mkdir(parents=True, exist_ok=True)
    path = task_path()
    path.write_text(task_xml(python, sid), encoding="utf-16")      # schtasks reads the XML as UTF-16
    name = task_name(sid)
    _schtasks(run, "/Create", "/XML", str(path), "/TN", name, "/F")
    _schtasks(run, "/Run", "/TN", name)                           # StopExisting: the one running, if any, stops
    return path


def _uninstall_windows(run) -> bool:
    from . import desktop

    name = task_name(desktop.user_sid(run))
    if _schtasks(run, "/Query", "/TN", name, check=False).returncode != 0:
        return False
    _schtasks(run, "/End", "/TN", name, check=False)
    _schtasks(run, "/Delete", "/TN", name, "/F")
    task_path().unlink(missing_ok=True)
    return True


def detached_output() -> None:
    """Started by the logon task, under pythonw, Rafiq has no stdout: what it prints goes to task.log instead. The
    programs it starts open no window of their own (desktop.no_window), since there is no console to share."""
    from . import desktop

    if desktop.system() != "windows" or sys.stdout is not None:
        return
    log = config.home() / "task.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log.stat().st_size > TASK_LOG_BYTES:
            log.unlink()
    except OSError:
        pass
    out = open(log, "a", encoding="utf-8", buffering=1)           # noqa: SIM115 (open for the life of the process)
    sys.stdout = sys.stderr = out
