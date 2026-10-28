"""Start Rafiq at login: a systemd user service (rafiq install-service, rafiq uninstall-service).

systemd keeps one Rafiq running, restarts it when it fails and sends its output to the journal
(journalctl --user -u rafiq). A user service does not see the shell's exported keys, so they go in
an optional file, ~/.config/rafiq/rafiq.env (KEY=value lines), which the unit reads but never copies.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import config

NAME = "rafiq.service"

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


def install(run: Systemctl | None = None, python: str | None = None) -> Path:
    """Write the unit, enable it at login and (re)start it now. Installing again replaces the unit
    and restarts the one Rafiq, so there are never two."""
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
    run = run or systemctl
    path = unit_path()
    if not path.is_file():
        return False
    _call(run, "disable", "--now", NAME)
    path.unlink()
    _call(run, "daemon-reload")
    return True
