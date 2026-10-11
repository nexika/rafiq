"""Presence: whether Rafiq reacts when your phone leaves or comes back to the home Wi-Fi."""

from __future__ import annotations

from . import tool


@tool("presence", "Turn on or off playing the away and welcome-home scenes when the user's phone leaves "
                  "or comes back to the home Wi-Fi, or say whether it is on and whether the phone is home.",
      {"action": {"type": "string", "enum": ["on", "off", "status"]}}, required=("action",), level="act")
def presence(action: str, ctx=None) -> str:
    watcher = getattr(ctx, "presence", None)
    if watcher is None:
        return "error: no phone is set up (presence.phone_ip in the config)"
    if action in ("on", "off"):
        watcher.set_enabled(action == "on")
    elif action != "status":
        return "error: action is on, off or status"
    return watcher.status()
