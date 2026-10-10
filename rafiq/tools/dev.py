"""Developer questions about the current project, answered by Nexika's helpers when they are on the
PATH: barq (tests, build, lint, git status), tabib (why CI failed) and mizan (branch, PR and CI).

The project folder comes from the settings ([dev] project) or the panel. Every command is a fixed
argument list chosen here; the model only picks which question to ask, never any command text. None
of these tools change the repository.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from .. import config
from . import tool

CHECKS = ("test", "build", "lint")
MAX_OUTPUT = 4000
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _run(args: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def _saved() -> Path:
    return config.home() / "dev_project.txt"


def project_folder(cfg: dict) -> Path | None:
    """The folder named in the settings, else the one last set in the panel."""
    name = cfg.get("dev", {}).get("project", "")
    if not name and _saved().is_file():
        name = _saved().read_text(encoding="utf-8").strip()
    return Path(name).expanduser() if name else None


def set_project(path: str) -> str:
    """Remember the project folder chosen in the panel. Returns the folder, or an error."""
    folder = Path(path).expanduser()
    if not folder.is_dir():
        return f"error: {path!r} is not a folder"
    _saved().parent.mkdir(parents=True, exist_ok=True)
    _saved().write_text(str(folder.resolve()), encoding="utf-8")
    return str(folder.resolve())


def _short(text: str) -> str:
    text = ANSI.sub("", text).strip()
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + "\n… (cut; the rest is in the terminal)"


def helper(name: str, args: list[str], ctx, run: Callable = _run, which: Callable = shutil.which,
           timeout: float = 120) -> str:
    folder = project_folder(ctx.cfg)
    if folder is None:
        return "error: no project folder is set; set one in the panel or as [dev] project in the settings"
    if not folder.is_dir():
        return f"error: the project folder {folder} does not exist"
    program = which(name)
    if not program:
        return f"error: {name} is not installed; it comes with Nexika's {name} plugin and must be on the PATH"
    try:
        result = run([program, *args], cwd=folder, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"error: {name} took longer than {timeout:.0f} seconds"
    except OSError as exc:
        return f"error: {name} could not start: {exc}"
    output = _short((result.stdout or "") + ("\n" + result.stderr if result.stderr.strip() else ""))
    if result.returncode != 0 and not output:
        return f"error: {name} failed with code {result.returncode}"
    return output or "nothing to report"


SHORT = " Read the answer back in one or two short sentences; the panel shows the details."


@tool("dev_check", "Run the current project's tests, build or lint and get the verdict and failures." + SHORT,
      {"kind": {"type": "string", "enum": list(CHECKS), "description": "what to run; test by default"}})
def run_check(kind: str = "test", ctx=None, run: Callable = _run, which: Callable = shutil.which) -> str:
    if kind not in CHECKS:
        return f"error: kind is one of {', '.join(CHECKS)}"
    return helper("barq", [f"run:{kind}"], ctx, run, which, timeout=900)


@tool("dev_git_status", "The current project's git status: branch, ahead or behind, changed files." + SHORT)
def git_status(ctx=None, run: Callable = _run, which: Callable = shutil.which) -> str:
    return helper("barq", ["git-status"], ctx, run, which)


@tool("dev_ci_failure", "Why CI failed on the current project: what failed and what kind of failure." + SHORT)
def ci_failure(ctx=None, run: Callable = _run, which: Callable = shutil.which) -> str:
    folder = project_folder(ctx.cfg)
    return helper("tabib", ["triage", "--cwd", str(folder)], ctx, run, which)


@tool("dev_pr_status", "The current project's branch, its pull request, review and CI state." + SHORT)
def branch_and_pr(ctx=None, run: Callable = _run, which: Callable = shutil.which) -> str:
    folder = project_folder(ctx.cfg)
    return helper("mizan", ["report", "--cwd", str(folder)], ctx, run, which)
