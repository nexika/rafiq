"""Background tasks: long research handed to an agent that works while Rafiq keeps listening."""

from __future__ import annotations

from . import tool

OFF = "error: background tasks need Claude (an Anthropic API key) and the running assistant"


@tool("start_background_task",
      "Hand a long task (research, comparing products, reading several pages) to a background agent. It "
      "returns at once; tell the user briefly that you are on it. The answer is said aloud when it is "
      "ready. Each task is a paid model call, so use it only for work that needs it.",
      {"goal": {"type": "string", "description": "the task, complete and self-contained, in the user's words"}},
      required=("goal",), level="act")
def start_background_task(goal: str, ctx=None) -> str:
    from ..tasks import TooManyTasks

    tasks = getattr(ctx, "tasks", None)
    if tasks is None:
        return OFF
    try:
        task = tasks.start(goal, ctx.tag)
    except TooManyTasks as exc:
        return f"error: {exc}; ask the user to wait for one to finish"
    return f"started task {task.id}: {goal}"


@tool("cancel_task",
      "Cancel a background task the user no longer wants. It ends after the step it is running and keeps "
      "what it cost so far. Find its id with background_tasks first.",
      {"id": {"type": "string", "description": "the task's id, from background_tasks"}},
      required=("id",), level="act")
def cancel_task(id: str, ctx=None) -> str:
    tasks = getattr(ctx, "tasks", None)
    if tasks is None:
        return OFF
    if not tasks.cancel(id):
        return f"error: no running task {id}; it may have finished already"
    return f"cancelling task {id}: it ends after the step it is running, and Rafiq says so"


def _cost(cost: float | None) -> str:
    return f"${cost:.4f}" if cost is not None else "cost unknown"


@tool("background_tasks", "The background tasks: what each is about, running or done, its cost and its answer.",
      level="private")
def background_tasks(ctx=None) -> str:
    tasks = getattr(ctx, "tasks", None)
    if tasks is None:
        return OFF
    lines = []
    for t in tasks.all():
        line = f"task {t.id} '{t.goal}': {t.state}"
        if t.state != "running":
            line += f", {_cost(t.cost)}: {t.result or t.error}"
        lines.append(line)
    return "\n".join(lines) or "no background tasks"
