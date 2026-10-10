"""Read aloud: the selected text, the clipboard, a web page or a PDF. The tool only loads the text;
Rafiq starts reading once it has given its short reply, so the text never goes to the model."""

from __future__ import annotations

import subprocess

from .. import reader
from . import tool

SOURCES = ("selection", "clipboard", "page", "pdf")


@tool("read_aloud", "Read a text aloud to the user, sentence by sentence in the right voice for each language: "
      "the text they selected (highlighted), the clipboard, a web page or a PDF file. Reading starts after your "
      "reply, so reply in a few words only. 'Read this to me' means the selection.",
      {"source": {"type": "string", "enum": list(SOURCES)},
       "url": {"type": "string", "description": "the page's address, for source 'page'"},
       "path": {"type": "string", "description": "the PDF's path, for source 'pdf'"}},
      required=("source",))
def read_aloud(source: str, url: str = "", path: str = "", ctx=None) -> str:
    if source == "page" and not url:
        return "error: reading a page needs its address"
    if source == "pdf" and not path:
        return "error: reading a PDF needs its path"
    try:
        if source == "selection":
            text = reader.selection()
        elif source == "clipboard":
            text = reader.clipboard()
        elif source == "page":
            text = reader.page_text(url)
        elif source == "pdf":
            text = reader.pdf_text(path)
        else:
            return f"error: source is one of {', '.join(SOURCES)}"
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return f"error: {exc}"
    parts = reader.segments(text, ctx.tag)
    if not parts:
        return f"error: there is no text in the {source}"
    ctx.reader.load(parts)
    languages = ", ".join(sorted({p.tag for p in parts}))
    return f"reading {len(parts)} sentences ({languages}) after your reply"


@tool("reading", "Control the reading aloud: pause, resume, skip the sentence, or stop.",
      {"action": {"type": "string", "enum": ["pause", "resume", "skip", "stop"]}}, required=("action",))
def reading(action: str, ctx=None) -> str:
    r = ctx.reader
    if action == "stop":
        r.stop()
        return "stopped"
    done = {"pause": r.pause, "resume": r.resume, "skip": r.skip}.get(action)
    if done is None:
        return "error: action is pause, resume, skip or stop"
    if not done():
        return f"error: nothing to {action}"
    return {"pause": "paused", "resume": "resumed", "skip": "skipped"}[action]
