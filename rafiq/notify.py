"""Spoken notifications from long jobs: `rafiq notify --done "the benchmark"` queues a message for the
running Rafiq through the panel API, which says it in the language of the conversation, unless Rafiq
is muted or it is quiet hours (then the panel shows it without a word).

`rafiq hooks claude-code|shell` prints ready-made hooks that call it.
"""

from __future__ import annotations

from datetime import datetime, time

from . import lang

EVENTS = {"": None, "done": "job_done", "failed": "job_failed"}

CLAUDE_CODE_HOOK = """\
Add this to ~/.claude/settings.json (or a project's .claude/settings.json) to hear when Claude Code
finishes a task:

{
  "hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "rafiq notify --done 'Claude Code' || true"}]}]
  }
}
"""

SHELL_HOOK = """\
# Add to ~/.bashrc or ~/.zshrc.
# announce make test   -> runs the command, then says "Finished: make test" or "Failed: make test"
announce() {
  "$@"; local status=$?
  if [ $status -eq 0 ]; then rafiq notify --done "$*"; else rafiq notify --failed "$*"; fi
  return $status
}

# watch-ci [run-id]    -> waits for a GitHub Actions run (the latest on this branch by default)
watch-ci() {
  local run=${1:-$(gh run list --branch "$(git branch --show-current)" --limit 1 \\
    --json databaseId -q '.[0].databaseId')}
  if gh run watch "$run" --exit-status >/dev/null; then rafiq notify --done CI; else rafiq notify --failed CI; fi
}
"""

HOOKS = {"claude-code": CLAUDE_CODE_HOOK, "shell": SHELL_HOOK}


def _clock(text: str) -> time:
    hour, minute = text.strip().split(":")
    return time(int(hour), int(minute))


def in_quiet_hours(hours: str, now: datetime) -> bool:
    """True when the local time is inside "HH:MM-HH:MM"; the range may wrap past midnight."""
    if not hours.strip():
        return False
    try:
        start, end = (_clock(part) for part in hours.split("-"))
    except ValueError as exc:
        raise ValueError(f'quiet hours look like "22:00-07:00", not {hours!r}') from exc
    current = now.time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def should_speak(cfg: dict, muted: bool, now: datetime | None = None) -> bool:
    return not muted and not in_quiet_hours(cfg["notify"]["quiet_hours"], now or datetime.now())


def message(text: str, event: str, tag: str) -> str:
    """The sentence to say: "Finished: the benchmark" in the user's language, or the text as given."""
    key = EVENTS[event]
    return lang.phrase(key, tag, label=text) if key else text
