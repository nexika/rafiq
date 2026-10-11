"""`rafiq update` and `rafiq backup`.

Update: `git pull --ff-only` in the checkout Rafiq runs from, then pip installs it again with the extras that are
already installed, so new dependencies arrive too.

Backup: a .tar.gz of the settings folder (settings, keys, recordings, alarms and everything else Rafiq saved),
written only for its owner (0600, in a 0700 folder). It stays on this computer: nothing is pushed anywhere, because
the archive holds secrets. Models that are downloaded again on their own are left out.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterable
from datetime import datetime
from importlib import metadata
from pathlib import Path

from . import config

# Relative to the settings folder: earlier backups, downloaded models and the panel token (made again on start).
SKIP = ("backups", "data/oww", "data/piper", "panel.token")
EXTRA = re.compile(r"""^\s*([A-Za-z0-9._-]+).*;\s*extra\s*==\s*["']([^"']+)["']""")


class MaintenanceError(Exception):
    pass


def _skipped(rel: Path, extra: Path | None) -> bool:
    return any(rel == Path(s) or Path(s) in rel.parents for s in SKIP) or (
        extra is not None and (rel == extra or extra in rel.parents))


def backup(dest: Path | None = None, now: datetime | None = None) -> Path:
    """Archive the settings folder into `dest` (default: <settings>/backups) and return the archive's path."""
    home = config.home()
    if not home.is_dir() or not any(home.iterdir()):
        raise MaintenanceError(f"nothing to back up in {home}")
    dest = Path(dest) if dest else home / "backups"
    if not dest.exists() or dest == home / "backups":
        # A folder we make (or our own) is closed to others; one the user chose already keeps its permissions.
        dest.mkdir(parents=True, exist_ok=True)
        dest.chmod(0o700)
    try:
        inside = dest.resolve().relative_to(home.resolve())
    except ValueError:
        inside = None
    now = now or datetime.now().astimezone()
    path = dest / f"rafiq-backup-{now:%Y%m%d-%H%M%S}.tar.gz"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out, tarfile.open(fileobj=out, mode="w:gz") as tar:
            for root, dirs, files in os.walk(home):
                here = Path(root)
                dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink()
                                 and not _skipped((here / d).relative_to(home), inside))
                for name in sorted(files):
                    file = here / name
                    rel = file.relative_to(home)
                    if file.is_symlink() or not file.is_file() or _skipped(rel, inside):
                        continue
                    tar.add(file, arcname=str(Path("rafiq") / rel), recursive=False)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def installed_extras(requires: Iterable[str] | None = None,
                     is_installed: Callable[[str], bool] | None = None) -> list[str]:
    """The optional extras (google, offline-voice, dev) whose packages are all installed, so an update keeps them."""
    if requires is None:
        try:
            requires = metadata.distribution("rafiq").requires or []
        except metadata.PackageNotFoundError:
            requires = []
    if is_installed is None:
        def is_installed(name: str) -> bool:
            try:
                metadata.distribution(name)
            except metadata.PackageNotFoundError:
                return False
            return True
    needs: dict[str, list[str]] = {}
    for line in requires:
        match = EXTRA.match(line)
        if match:
            needs.setdefault(match.group(2), []).append(match.group(1).lower().replace("_", "-"))
    return sorted(extra for extra, names in needs.items() if all(is_installed(n) for n in names))


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MaintenanceError(f"{args[0]} could not run: {exc}") from exc


def update(repo: Path | None = None, run=_run, extras: list[str] | None = None, say=print) -> None:
    """Pull the latest Rafiq and install its dependencies. Raises MaintenanceError at the first step that fails."""
    repo = Path(repo) if repo else Path(__file__).resolve().parent.parent
    if not (repo / ".git").exists():
        raise MaintenanceError(f"{repo} is not a git checkout: update Rafiq the way you installed it")
    extras = installed_extras() if extras is None else extras
    target = f"{repo}[{','.join(extras)}]" if extras else str(repo)
    steps = [("git pull", ["git", "pull", "--ff-only"]),
             ("pip install", [sys.executable, "-m", "pip", "install", "--upgrade", "-e", target])]
    for label, args in steps:
        say(f"{label}...")
        result = run(args, repo)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise MaintenanceError(f"{label} failed: {detail[-1] if detail else f'exit {result.returncode}'}")
        lines = (result.stdout or "").strip().splitlines()
        if lines:
            say(f"  {lines[-1]}")
