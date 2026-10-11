"""Voice notes: one Markdown file per kind of note, one dated bullet per note, so the folder can be
an Obsidian vault and notes written there by hand are read back too."""

from __future__ import annotations

import random
import re
from datetime import date
from pathlib import Path

from .. import config
from . import tool
from .everyday import now

# kind -> (file, heading written at the top of a new file)
KINDS = {"note": ("notes.md", "Notes"), "idea": ("ideas.md", "Ideas"), "todo": ("todo.md", "To do"),
         "shopping": ("shopping.md", "Shopping")}
_DATED = re.compile(r"^- (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) (.+)$")


def folder(cfg: dict) -> Path:
    chosen = cfg.get("notes", {}).get("folder")
    return Path(chosen).expanduser() if chosen else config.data_dir() / "notes"


def _bullets(path: Path) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip().startswith("- ") and line.strip()[2:].strip()]


_KIND_PARAM = {"type": "string", "enum": list(KINDS),
               "description": "note (default), idea, todo or shopping"}


@tool("note_add", "Write down a note or an idea the user dictates, in their own words and language.",
      {"text": {"type": "string", "description": "the note, as the user said it"}, "kind": _KIND_PARAM},
      required=("text",), level="act")
def note_add(text: str, kind: str = "note", ctx=None) -> str:
    text = " ".join(text.split())
    kind = kind or "note"
    if not text:
        return "error: the note is empty"
    if kind not in KINDS:
        return f"error: unknown kind {kind!r}; use one of {', '.join(KINDS)}"
    name, heading = KINDS[kind]
    path = folder(ctx.cfg) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.is_file() or not path.read_text(encoding="utf-8").strip()
    with path.open("a", encoding="utf-8") as out:
        if new:
            out.write(f"# {heading}\n\n")
        out.write(f"- {now():%Y-%m-%d %H:%M} {text}\n")
    return f"noted ({kind}): {text}"


@tool("note_random", "A random note from the user's notes, by default one of their ideas.",
      {"kind": _KIND_PARAM | {"description": "idea (default), note, todo or shopping"}},
      level="private")
def note_random(kind: str = "idea", ctx=None, rng: random.Random | None = None) -> str:
    kind = kind or "idea"
    if kind not in KINDS:
        return f"error: unknown kind {kind!r}; use one of {', '.join(KINDS)}"
    bullets = _bullets(folder(ctx.cfg) / KINDS[kind][0])
    if not bullets:
        return f"no {kind} notes yet"
    line = (rng or random).choice(bullets)
    dated = _DATED.match(line)
    return f"{dated.group(3)} ({dated.group(1)})" if dated else line[2:].strip()


@tool("notes_on", "The notes of every kind the user wrote on one day, by default today.",
      {"day": {"type": "string", "description": "date as YYYY-MM-DD; empty for today"}},
      level="private")
def notes_on(day: str = "", ctx=None) -> str:
    try:
        wanted = date.fromisoformat(day).isoformat() if day else f"{now():%Y-%m-%d}"
    except ValueError:
        return "error: give the day as YYYY-MM-DD"
    found = []
    for kind, (name, _) in KINDS.items():
        for line in _bullets(folder(ctx.cfg) / name):
            dated = _DATED.match(line)
            if dated and dated.group(1) == wanted:
                found.append(f"{dated.group(2)} {kind}: {dated.group(3)}")
    return "\n".join(sorted(found)) if found else f"no notes on {wanted}"
