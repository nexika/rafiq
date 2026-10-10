"""Scenes (moods): a name and a list of tool calls from the config, such as "film mode" or "turn
everything off". Saying one of a scene's names, in any language it lists, runs it straight away; the
brain can also start one with the run_scene tool.

    [scenes.film]
    names = ["film mode", "mode film", "وضع الفيلم"]
    steps = [{ tool = "volume", args = { level = 40 } },
             { tool = "lights", args = { brightness = 10 }, hours = [18, 6] }]   # 18:00 to 06:00 only

A step that changes something is asked about first, as when the brain calls it. A step whose tool is
missing (not installed, not set up) is skipped and the rest still run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .. import lang
from . import tool


@dataclass
class Step:
    tool: str
    args: dict = field(default_factory=dict)
    hours: tuple[int, int] | None = None    # run only from the first hour to the second (wraps at midnight)
    speak: bool = False                     # read the tool's answer aloud (a daily brief)


@dataclass
class Scene:
    id: str
    names: list[str]
    steps: list[Step]


@dataclass
class Outcome:
    tool: str
    result: str
    speak: bool = False


def scenes_from(cfg: dict) -> dict[str, Scene]:
    """The scenes in the config. A step with no tool is a mistake worth hearing about at once."""
    found = {}
    for scene_id, entry in (cfg.get("scenes") or {}).items():
        steps = []
        for raw in entry.get("steps") or []:
            if not raw.get("tool"):
                raise ValueError(f"scene {scene_id!r}: every step needs a tool")
            hours = raw.get("hours")
            steps.append(Step(raw["tool"], dict(raw.get("args") or {}),
                              (int(hours[0]), int(hours[1])) if hours else None, bool(raw.get("speak"))))
        names = list(entry.get("names") or []) or [scene_id.replace("_", " ")]
        found[scene_id] = Scene(scene_id, names, steps)
    return found


def _key(text: str) -> str:
    return " ".join(lang.words(text.replace("-", " ")))


def match(text: str, scenes: dict[str, Scene]) -> Scene | None:
    """The scene whose name is all that was said; anything longer is a question for the brain."""
    said = _key(text)
    if not said:
        return None
    for scene in scenes.values():
        if said in {_key(n) for n in scene.names} | {_key(scene.id.replace("_", " "))}:
            return scene
    return None


def in_hours(hours: tuple[int, int], hour: int) -> bool:
    start, end = hours
    return start <= hour < end if start <= end else hour >= start or hour < end


def play(scene: Scene, ctx, hour: int | None = None) -> list[Outcome]:
    """Run each step in order. Nothing that changes something runs without a yes: with no one to ask,
    such a step is skipped."""
    hour = datetime.now().astimezone().hour if hour is None else hour
    tools = ctx.tools or {}
    outcomes = []
    for step in scene.steps:
        found = tools.get(step.tool)
        if step.hours and not in_hours(step.hours, hour):
            result = f"skipped: only between {step.hours[0]}:00 and {step.hours[1]}:00"
        elif found is None or step.tool == "run_scene":
            result = f"skipped: no tool named {step.tool}"
        else:
            action = found.action(step.args)
            if action and not (ctx.confirm and ctx.confirm(action, ctx.tag)):
                result = "declined"
            else:
                try:
                    result = found.run(step.args, ctx)
                except Exception as exc:   # one broken step should not stop the lights going off
                    result = f"error: {type(exc).__name__}: {exc}"
        outcomes.append(Outcome(step.tool, result, step.speak))
    return outcomes


def report(outcomes: list[Outcome]) -> str:
    return "\n".join(f"{o.tool}: {o.result}" for o in outcomes) or "the scene has no steps"


def first_start_today(path: Path, today: date) -> bool:
    """True the first time this is asked on a given day; it remembers the day in the file."""
    try:
        last = json.loads(path.read_text(encoding="utf-8")).get("date")
    except (OSError, ValueError, AttributeError):
        last = None
    if last == today.isoformat():
        return False
    path.write_text(json.dumps({"date": today.isoformat()}), encoding="utf-8")
    return True


def describe(scenes: dict[str, Scene]) -> str:
    return "; ".join(f"{s.id} ({', '.join(s.names)})" for s in scenes.values()) or "none"


@tool("run_scene", "Run one of the user's scenes (moods), a set of actions saved under a name, such as "
                   "'film mode' or 'turn everything off'.",
      {"name": {"type": "string", "description": "the scene's name or id"}}, required=("name",))
def run_scene(name: str, ctx=None) -> str:
    scenes = scenes_from(ctx.cfg)
    scene = scenes.get(name) or match(name, scenes)
    if scene is None:
        return f"error: no scene called {name!r}; the scenes are: {describe(scenes)}"
    return report(play(scene, ctx))
