"""Explaining a video that plays on this computer (rafiq/video.py): start listening, and stop early."""

from __future__ import annotations

from . import tool

OFF = "error: listening to a video needs the running Rafiq (rafiq run)"


@tool("explain_video",
      "Listen to the video (or talk, lecture, podcast) playing on this computer and explain it when it ends or "
      "when the user says stop. Returns at once: tell the user briefly that you're listening. The sound stays on "
      "this computer; only its transcript is read.",
      {"question": {"type": "string", "description": "what the user wants to know about it; empty for an "
                                                     "explanation of the whole"}},
      level="private")
def explain_video(question: str = "", ctx=None) -> str:
    from ..video import AlreadyWatching

    if getattr(ctx, "room", ""):
        return "error: a satellite can't listen to this computer's sound; ask on the computer itself"
    video = getattr(ctx, "video", None)
    if video is None:
        return OFF
    try:
        video.start(question, ctx.tag)
    except AlreadyWatching as busy:
        if str(busy) == "explaining":
            return "error: the last video is still being explained; try again in a moment"
        return "error: a video is already being listened to; the user can say stop first"
    return ("listening to the video on this computer: the explanation is shown and said when it ends or when "
            "the user says stop")


@tool("stop_video", "Stop listening to the video now and explain what was heard so far (\"stop, explain it now\").",
      level="safe")
def stop_video(ctx=None) -> str:
    video = getattr(ctx, "video", None)
    if video is None or not video.stop():
        return "error: no video is being listened to (one being explained finishes on its own)"
    return "stopped listening: Rafiq now shows and says what it heard, explained; tell the user briefly"
