"""The video pipeline for content creators: each video moves idea → script → shoot → edit → published,
with an optional date it should be out by. Saved to data/content.json; nothing leaves the computer."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from .. import config
from . import tool

STAGES = ("idea", "script", "shoot", "edit", "published")


@dataclass
class Video:
    id: str
    title: str
    stage: str
    due: str            # YYYY-MM-DD it should be published by, or ""
    updated: str        # YYYY-MM-DD of the last change


def _check_due(due: str) -> str:
    due = (due or "").strip()
    if due:
        try:
            date.fromisoformat(due)
        except ValueError:
            raise ValueError("give the date as YYYY-MM-DD") from None
    return due


def _check_stage(stage: str) -> str:
    stage = stage.lower().strip()
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; the stages are {', '.join(STAGES)}")
    return stage


class Pipeline:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.videos: dict[str, Video] = {}
        try:
            for item in json.loads(path.read_text(encoding="utf-8")):
                video = Video(**item)
                self.videos[video.id] = video
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(v) for v in self.videos.values()], ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def items(self) -> list[Video]:
        """In pipeline order, then by date (undated last)."""
        with self.lock:
            return sorted(self.videos.values(), key=lambda v: (STAGES.index(v.stage), v.due or "9999", v.title))

    def find(self, name: str) -> Video:
        """By id, by exact title, or by words in the title when only one video has them."""
        key = name.lower().strip()
        with self.lock:
            if key in self.videos:
                return self.videos[key]
            exact = [v for v in self.videos.values() if v.title.lower() == key]
            found = exact or [v for v in self.videos.values() if key and key in v.title.lower()]
        if not found:
            raise LookupError(f"no video matches {name!r}")
        if len(found) > 1:
            raise LookupError(f"{name!r} matches several videos: " + ", ".join(v.title for v in found))
        return found[0]

    def add(self, title: str, due: str = "", stage: str = "idea", today: date | None = None) -> Video:
        title = title.strip()
        if not title:
            raise ValueError("a video needs a title")
        video = Video(uuid.uuid4().hex[:6], title, _check_stage(stage), _check_due(due),
                      (today or date.today()).isoformat())
        with self.lock:
            self.videos[video.id] = video
            self._save()
        return video

    def advance(self, name: str, stage: str = "", today: date | None = None) -> Video:
        """To the named stage, or else to the next one."""
        video = self.find(name)
        if stage:
            target = _check_stage(stage)
        elif video.stage == STAGES[-1]:
            raise ValueError(f"{video.title!r} is already published")
        else:
            target = STAGES[STAGES.index(video.stage) + 1]
        with self.lock:
            video.stage, video.updated = target, (today or date.today()).isoformat()
            self._save()
        return video

    def remove(self, name: str) -> Video:
        video = self.find(name)
        with self.lock:
            del self.videos[video.id]
            self._save()
        return video

    def late(self, today: date) -> list[Video]:
        """Past their date and not published yet."""
        return [v for v in self.items() if v.due and v.stage != "published" and v.due < today.isoformat()]


def today() -> date:
    return datetime.now().astimezone().date()


def pipeline() -> Pipeline:
    return Pipeline(config.data_dir() / "content.json")


def _describe(video: Video) -> str:
    return f"{video.id} '{video.title}' at {video.stage}" + (f", due {video.due}" if video.due else "")


@tool("content_add", "Add a video to the content pipeline (idea → script → shoot → edit → published).",
      {"title": {"type": "string", "description": "the video's working title, in the user's language"},
       "due": {"type": "string", "description": "date it should be published by, YYYY-MM-DD; empty for none"},
       "stage": {"type": "string", "enum": list(STAGES), "description": "where it is now; default idea"}},
      required=("title",))
def content_add(title: str, due: str = "", stage: str = "idea") -> str:
    try:
        return "added: " + _describe(pipeline().add(title, due, stage or "idea", today()))
    except ValueError as exc:
        return f"error: {exc}"


@tool("content_advance", "Move a video in the content pipeline to its next stage, or to a named stage.",
      {"video": {"type": "string", "description": "its id or words from its title"},
       "stage": {"type": "string", "enum": list(STAGES), "description": "empty for the next stage"}},
      required=("video",))
def content_advance(video: str, stage: str = "") -> str:
    try:
        moved = pipeline().advance(video, stage, today())
    except (LookupError, ValueError) as exc:
        return f"error: {exc}"
    return f"{moved.title!r} is now at {moved.stage}"


@tool("content_list", "The videos in the content pipeline, late ones first.",
      {"stage": {"type": "string", "enum": list(STAGES), "description": "only this stage; empty for all"}})
def content_list(stage: str = "") -> str:
    try:
        stage = _check_stage(stage) if stage else ""
    except ValueError as exc:
        return f"error: {exc}"
    current = pipeline()
    videos = [v for v in current.items() if not stage or v.stage == stage]
    if not videos:
        return f"no videos at {stage}" if stage else "no videos in the pipeline"
    late = [v for v in current.late(today()) if v in videos]
    lines = ["late: " + "; ".join(_describe(v) for v in late)] if late else []
    return "\n".join(lines + [_describe(v) for v in videos])


@tool("content_remove", "Take a video out of the content pipeline.",
      {"video": {"type": "string", "description": "its id or words from its title"}}, required=("video",))
def content_remove(video: str) -> str:
    try:
        return "removed: " + _describe(pipeline().remove(video))
    except LookupError as exc:
        return f"error: {exc}"
