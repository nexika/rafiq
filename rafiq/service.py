"""Start Rafiq at login: a systemd user service (rafiq install-service, rafiq uninstall-service); on macOS a
launchd agent, ~/Library/LaunchAgents/com.nexika.rafiq.plist, which launchd starts at login and again if it fails.
launchd reads no environment file, so the agent names rafiq.env in RAFIQ_ENV_FILE and Rafiq loads it as it
starts (load_env); the keys stay in that file, never in the agent.

systemd keeps one Rafiq running, restarts it when it fails and sends its output to the journal
(journalctl --user -u rafiq). A user service does not see the shell's exported keys, so they go in
an optional file, ~/.config/rafiq/rafiq.env (KEY=value lines), which the unit reads but never copies.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
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


# ---------------------------------------------------------------------- macOS: launchd

def launchctl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30, check=False)


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def plist_text(python: str | None = None) -> str:
    env = {"PYTHONUNBUFFERED": "1", "RAFIQ_ENV_FILE": str(env_file())}
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
        "EnvironmentVariables": env,
        "StandardOutPath": str(logs),
        "StandardErrorPath": str(logs),
    }
    return plistlib.dumps(agent).decode()


def _install_mac(run, python: str | None) -> Path:
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist_text(python), encoding="utf-8")
    run(["bootout", f"{_domain()}/{LABEL}"])          # the one running, if any: never two
    done = run(["bootstrap", _domain(), str(path)])
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


def load_env(environ=None) -> None:
    """KEY=value lines from the file in RAFIQ_ENV_FILE (rafiq.env), for keys a launchd agent can't otherwise
    get; what the environment already has wins. Nothing is printed or logged."""
    environ = os.environ if environ is None else environ
    name = environ.get("RAFIQ_ENV_FILE")
    if not name:
        return
    try:
        lines = Path(name).read_text(encoding="utf-8").splitlines()
    except OSError:
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
