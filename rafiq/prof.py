"""Nexika's prof plugin, for a learner (#205): Rafiq explains at the level in prof's profile, and, after a yes,
records what was learned so prof can review and quiz it later.

Rafiq only reads prof's profile and asks prof's own helper (prof_store.py: `topic SLUG`, `merge-report FILE`),
as an argument list with a timeout. It writes nothing in prof's folder: its report is a file in Rafiq's data
folder, which prof's merge-report applies. Without prof installed, nothing changes. What the learner knows is
private: the tools that carry it keep it out of the logs and the panel.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config

PLUGINS = Path.home() / ".claude" / "plugins" / "cache" / "nexika" / "prof"
LEVELS = ("beginner", "junior", "intermediate")         # what prof's profile says (its progress skill)
HELPER_SECONDS = 20
TOPIC_CHARS = 3000
STATUSES = ("missed", "shaky", "not-checked", "understood")
MAX_CONCEPTS = 12
FIELD_CHARS = 120

run = subprocess.run                     # replaced in tests


def home() -> Path:
    """prof's data folder, as prof finds it: PROF_HOME, or ~/.claude/nexika/prof."""
    return Path(os.environ.get("PROF_HOME") or Path.home() / ".claude" / "nexika" / "prof")


def _version(path: Path) -> tuple[int, ...]:
    try:
        return tuple(int(n) for n in path.parts[-3].split("."))
    except ValueError:
        return ()


def helper(cfg: dict) -> Path | None:
    """prof's helper: [prof] helper when set, else the newest prof installed for Claude Code; None without prof."""
    named = cfg.get("prof", {}).get("helper", "")
    if named:
        path = Path(named).expanduser()
        return path if path.is_file() else None
    found = sorted(PLUGINS.glob("*/scripts/prof_store.py"), key=_version)
    return found[-1] if found else None


def level() -> str | None:
    """The learner's level from prof's profile ("- Level: junior"), or None."""
    try:
        text = (home() / "profile.md").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"^\s*-\s*Level:\s*([A-Za-z-]+)", text, re.MULTILINE | re.IGNORECASE)
    found = m.group(1).lower() if m else ""
    return found if found in LEVELS else None


def note() -> str:
    """One sentence for a prompt that explains, when prof knows the learner's level; empty otherwise."""
    found = level()
    if not found:
        return ""
    return (f" The user is learning, at a {found} level (from their Nexika prof profile): explain at that level, "
            f"with the terms it knows and an example.")


def slug(text: str) -> str:
    """As prof makes them: lowercase-with-dashes, for a topic named in English."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "general"


def _call(path: Path, args: list[str]) -> str:
    result = run([sys.executable, "-I", str(path), *args], capture_output=True, text=True,
                 timeout=HELPER_SECONDS, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"prof's helper failed (code {result.returncode})")
    return result.stdout or ""


def topic(path: Path, name: str) -> str:
    """Everything prof knows of a topic, cut to size."""
    return _call(path, ["topic", slug(name)])[:TOPIC_CHARS]


def _clean(text, limit: int = FIELD_CHARS) -> str:
    """One field of a checklist line: no line breaks, no "::" (prof's separator), no "[" that starts a status."""
    text = " ".join(str(text or "").replace("::", " ").replace("[", " ").replace("]", " ").split())
    return text[:limit]


def report(title: str, topic_slug: str, concepts: list) -> str:
    """A report in prof's format, with only what Rafiq knows: what was explained, and the concept checklist."""
    rows = []
    for c in (concepts if isinstance(concepts, list) else [])[:MAX_CONCEPTS]:
        if not isinstance(c, dict) or not _clean(c.get("concept")):
            continue
        status = c.get("status") if c.get("status") in STATUSES else "not-checked"
        rows.append((status, _clean(c.get("concept")), _clean(c.get("evidence")) or "-"))
    name, key = _clean(title) or "General", slug(topic_slug or title)
    learned = "\n".join(f"- {concept}" for _, concept, _ in rows) or "- (nothing)"
    checklist = "\n".join(f"- [{status}] {key} :: {name} :: {concept} :: {evidence}"
                          for status, concept, evidence in rows)
    return (f"# Tutor session report - {time.strftime('%Y-%m-%d')}\nSession: rafiq · Source: Rafiq\n\n"
            f"## What you learned today\n{learned}\n\n## Concept checklist\n<!-- bayan: off -->\n{checklist}\n")


def record(path: Path, title: str, topic_slug: str, concepts: list) -> str:
    """Write the report in Rafiq's data folder and have prof merge it; prof's answer."""
    text = report(title, topic_slug, concepts)
    if "- [" not in text:
        raise ValueError("there is nothing to record")
    folder = config.data_dir() / "prof"
    folder.mkdir(parents=True, exist_ok=True)
    # prof reads the date from the name: YYYY-MM-DD_HHMM_...
    file = folder / f"{time.strftime('%Y-%m-%d_%H%M')}_rafiq-{slug(topic_slug or title)[:30]}.md"
    file.write_text(text, encoding="utf-8")
    return _call(path, ["merge-report", str(file)]).strip() or "merged"
