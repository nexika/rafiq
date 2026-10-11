"""Running Rafiq around the clock: `rafiq status` and `rafiq update`, for a server you reach over SSH.

Commands go through `run` (an argument list in, exit code and output back), so tests never start a
process. The service is a systemd user unit named "rafiq" (rafiq/rafiq.service, see docs/hosting.md).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import __version__, config

Run = Callable[..., tuple[int, str]]
SERVICE = "rafiq"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
ROOT = Path(__file__).resolve().parent.parent   # the git checkout, when installed with pip install -e


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""


def run_command(cmd: list[str], timeout: float = 60) -> tuple[int, str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{cmd[0]}: {type(exc).__name__}"
    return done.returncode, (done.stdout + done.stderr).strip()


def panel_up(host: str, port: int) -> bool:
    """True when a Rafiq panel answers on host:port."""
    name = f"[{host}]" if ":" in host else host
    try:
        resp = httpx.get(f"http://{name}:{port}/", timeout=3, headers={"Host": f"127.0.0.1:{port}"})
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


def panel_private(cfg: dict) -> bool:
    return cfg["panel"]["host"] in LOOPBACK


def is_checkout(root: Path) -> bool:
    return (root / ".git").exists()   # a file, not a folder, in a git worktree


def _service_state(run: Run) -> str:
    _code, out = run(["systemctl", "--user", "is-active", SERVICE])
    return out.splitlines()[-1] if out else "unknown"


def status(cfg: dict, root: Path = ROOT, run: Run = run_command, probe: Callable[[str, int], bool] = panel_up,
           has_systemctl: bool | None = None) -> list[Check]:
    if has_systemctl is None:
        has_systemctl = shutil.which("systemctl") is not None
    host, port = cfg["panel"]["host"], int(cfg["panel"]["port"])
    up = probe(host, port)
    where = f"{host}:{port}"
    checks = [Check("Rafiq running", up, f"panel answers on {where}" if up else f"nothing answers on {where}")]
    if has_systemctl:
        state = _service_state(run)
        # Started by hand (rafiq run in a terminal) is fine too: only a Rafiq that is down needs attention.
        checks.append(Check("service", state == "active" or up,
                            state if state == "active" or not up else f"{state} (running outside the service)"))
    version = __version__
    if is_checkout(root):
        _c, commit = run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"])
        _c, branch = run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"])
        version += f" ({branch} {commit})"
    checks.append(Check("version", True, version))
    checks.append(Check("panel private", panel_private(cfg),
                        f"listens on {host} only" if panel_private(cfg) else
                        f"{host} is reachable from the network: set [panel] host = \"127.0.0.1\""))
    free = shutil.disk_usage(config.data_dir()).free / 1e9
    checks.append(Check("disk free", free >= 1, f"{free:.1f} GB"))
    return checks


def update(root: Path = ROOT, run: Run = run_command, python: str = sys.executable,
           has_systemctl: bool | None = None, check: bool = False, restart: bool = True) -> tuple[int, list[str]]:
    """Pull the new commits, reinstall, and restart the service if it was running."""
    if has_systemctl is None:
        has_systemctl = shutil.which("systemctl") is not None
    if not is_checkout(root):
        return 1, [f"{root} is not a git checkout: reinstall Rafiq with pip to update it."]

    def git(*args: str, timeout: float = 60) -> tuple[int, str]:
        return run(["git", "-C", str(root), *args], timeout=timeout)

    code, dirty = git("status", "--porcelain")
    if code != 0:
        return 1, [f"git status failed: {dirty}"]
    if dirty:
        return 1, ["There are local changes in the checkout; commit or stash them first:", dirty]
    code, out = git("fetch", "--quiet", timeout=120)
    if code != 0:
        return 1, [f"git fetch failed: {out}"]
    code, out = git("rev-list", "--count", "HEAD..@{u}")
    if code != 0 or not out.strip().isdigit():
        return 1, [f"Can't compare with the upstream branch: {out}"]
    behind = int(out)
    if behind == 0:
        return 0, ["Rafiq is up to date."]
    lines = [f"{behind} new commit{'s' if behind != 1 else ''} upstream."]
    if check:
        return 0, lines + ["Run `rafiq update` to install them."]
    code, out = git("pull", "--ff-only", "--quiet", timeout=120)
    if code != 0:
        return 1, lines + [f"git pull failed: {out}"]
    code, out = run([python, "-m", "pip", "install", "--quiet", "-e", str(root)], timeout=900)
    if code != 0:
        return 1, lines + [f"pip install failed: {out[-500:]}"]
    lines.append("Installed.")
    if restart and has_systemctl and _service_state(run) == "active":
        code, out = run(["systemctl", "--user", "restart", SERVICE])
        lines.append("Service restarted." if code == 0 else f"Restart failed: {out}")
        return (0 if code == 0 else 1), lines
    lines.append("Restart Rafiq to use the new version.")
    return 0, lines
