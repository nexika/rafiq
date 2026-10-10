"""Notes in three lists: ideas, shopping and tasks. Said at home or sent from the phone, they are kept in
data/notes.json, so "what's on my shopping list?" finds what was dictated in the street."""

from __future__ import annotations

import json
import threading
import time

from .. import config
from . import tool

LISTS = ("ideas", "shopping", "tasks")
MAX_TEXT = 1000
_lock = threading.Lock()      # the voice loop and the phone bridge both write


def _load() -> dict[str, list[dict]]:
    try:
        data = json.loads((config.data_dir() / "notes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {name: [n for n in data.get(name, []) if isinstance(n, dict)] for name in LISTS}


def _save(data: dict) -> None:
    path = config.data_dir() / "notes.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


_LIST_PARAM = {"type": "string", "enum": list(LISTS)}


@tool("add_note", "Add a note to the user's ideas, shopping or tasks list.",
      {"list": _LIST_PARAM, "text": {"type": "string", "description": "the note, in the user's words"}},
      required=("list", "text"), safe=True)
def add_note(list: str, text: str, source: str = "voice") -> str:  # noqa: A002 (the model's word)
    if list not in LISTS:
        return f"error: the lists are {', '.join(LISTS)}"
    text = text.strip()[:MAX_TEXT]
    if not text:
        return "error: the note is empty"
    with _lock:
        data = _load()
        data[list].append({"text": text, "time": time.time(), "source": source})
        _save(data)
    return f"added to {list}: {text}"


@tool("list_notes", "What is on the user's ideas, shopping or tasks list.",
      {"list": _LIST_PARAM}, required=("list",), safe=True)
def list_notes(list: str) -> str:  # noqa: A002
    if list not in LISTS:
        return f"error: the lists are {', '.join(LISTS)}"
    with _lock:
        items = _load()[list]
    return "\n".join(f"- {n.get('text', '')}" for n in items) if items else f"the {list} list is empty"


@tool("remove_note", "Remove notes from a list by words in them, e.g. when something has been bought.",
      {"list": _LIST_PARAM, "words": {"type": "string"}}, required=("list", "words"), safe=True)
def remove_note(list: str, words: str) -> str:  # noqa: A002
    if list not in LISTS or not words.strip():
        return f"error: give one of {', '.join(LISTS)} and words to match"
    with _lock:
        data = _load()
        gone = [n for n in data[list] if words.lower().strip() in str(n.get("text", "")).lower()]
        data[list] = [n for n in data[list] if n not in gone]
        _save(data)
    return f"removed {len(gone)}: " + ", ".join(n["text"] for n in gone) if gone else "nothing matched"
