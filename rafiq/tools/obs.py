"""OBS Studio: stream, record, switch scenes, save a clip from the replay buffer. OBS 28 or newer, with
the WebSocket server on (Tools > WebSocket Server Settings), through obsws-python.

Settings under [obs] in the config; the password comes from OBS_WEBSOCKET_PASSWORD first, then the file,
and is never logged. Starting or stopping a stream or a recording is confirmed out loud first.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager

from . import tool


def settings(cfg: dict) -> dict:
    obs = cfg.get("obs", {})
    return {"host": obs.get("host") or "localhost", "port": int(obs.get("port") or 4455),
            "password": os.environ.get("OBS_WEBSOCKET_PASSWORD") or obs.get("password", ""),
            "timeout": obs.get("timeout", 5)}


def connect(settings: dict):
    """A request client for OBS's WebSocket v5; tests replace this."""
    import obsws_python

    return obsws_python.ReqClient(**settings)


@contextmanager
def _obs(ctx):
    where = settings(ctx.cfg)
    try:
        client = connect(where)
    except (OSError, TimeoutError) as exc:
        raise RuntimeError(f"OBS is not reachable at {where['host']}:{where['port']}: is OBS running with "
                           "the WebSocket server on?") from exc
    try:
        yield client
    finally:
        if hasattr(client, "disconnect"):
            client.disconnect()


def _safely(fn):
    """Run fn and turn an OBS that is closed or refuses into an error the model can say."""
    try:
        return fn()
    except RuntimeError as exc:
        return f"error: {exc}"


# ---------------------------------------------------------------------- status

def _scene_names(client) -> tuple[list[str], str]:
    found = client.get_scene_list()
    # OBS lists scenes bottom first; the user sees them top first.
    names = [s["sceneName"] for s in sorted(found.scenes, key=lambda s: -s.get("sceneIndex", 0))]
    return names, found.current_program_scene_name


@tool("obs_status", "What OBS Studio is doing: streaming, recording, the current scene and the scene names.")
def obs_status(ctx) -> str:
    def status() -> str:
        with _obs(ctx) as client:
            names, current = _scene_names(client)
            streaming = client.get_stream_status().output_active
            recording = client.get_record_status().output_active
        return (f"{'streaming' if streaming else 'not streaming'}, {'recording' if recording else 'not recording'}, "
                f"scene {current}; scenes: {', '.join(names)}")
    return _safely(status)


# ---------------------------------------------------------------------- stream and record

STREAM = {"start": "start streaming in OBS", "stop": "stop the OBS stream"}
RECORD = {"start": "start recording in OBS", "stop": "stop the OBS recording"}


@tool("obs_stream", "Start or stop streaming in OBS Studio. Rafiq asks the user first.",
      {"action": {"type": "string", "enum": list(STREAM)}}, required=("action",),
      confirm=lambda a: STREAM.get(a.get("action", ""), "change the OBS stream"))
def obs_stream(action: str, ctx) -> str:
    if action not in STREAM:
        return f"error: action is one of {', '.join(STREAM)}"

    def run() -> str:
        with _obs(ctx) as client:
            active = client.get_stream_status().output_active
            if action == "start":
                if active:
                    return "already streaming"
                client.start_stream()
                return "stream started"
            if not active:
                return "not streaming"
            client.stop_stream()
            return "stream stopped"
    return _safely(run)


@tool("obs_record", "Start or stop recording in OBS Studio. Rafiq asks the user first.",
      {"action": {"type": "string", "enum": list(RECORD)}}, required=("action",),
      confirm=lambda a: RECORD.get(a.get("action", ""), "change the OBS recording"))
def obs_record(action: str, ctx) -> str:
    if action not in RECORD:
        return f"error: action is one of {', '.join(RECORD)}"

    def run() -> str:
        with _obs(ctx) as client:
            active = client.get_record_status().output_active
            if action == "start":
                if active:
                    return "already recording"
                client.start_record()
                return "recording started"
            if not active:
                return "not recording"
            saved = getattr(client.stop_record(), "output_path", "")
            return "recording stopped" + (f", saved as {saved}" if saved else "")
    return _safely(run)


# ---------------------------------------------------------------------- scenes

def find_scene(query: str, names: list[str]) -> str | None:
    """The scene a spoken name means: exact, then a name starting with it, then a name containing it.
    "the camera scene" means "camera"."""
    q = re.sub(r"^(the|my)\s+|\s+scene$", "", query.lower().strip()).strip()
    if not q:
        return None
    for test in (lambda n: n == q, lambda n: n.startswith(q), lambda n: q in n):
        matches = [name for name in names if test(name.lower())]
        if matches:
            return min(matches, key=len)
    return None


@tool("obs_scene", "Switch OBS Studio to a scene, e.g. 'Camera' or 'Be right back'.",
      {"name": {"type": "string", "description": "the scene's name as the user said it"}}, required=("name",))
def obs_scene(name: str, ctx) -> str:
    def run() -> str:
        with _obs(ctx) as client:
            names, _ = _scene_names(client)
            scene = find_scene(name, names)
            if scene is None:
                return f"error: no scene matches {name!r}; the scenes are {', '.join(names)}"
            client.set_current_program_scene(scene)
        return f"switched to {scene}"
    return _safely(run)


# ---------------------------------------------------------------------- clips

@tool("obs_clip", "Save the last moments as a clip from OBS Studio's replay buffer (\"clip that\").")
def obs_clip(ctx) -> str:
    def run() -> str:
        with _obs(ctx) as client:
            if not client.get_replay_buffer_status().output_active:
                return "error: the OBS replay buffer is off; start it in OBS (Start Replay Buffer) first"
            client.save_replay_buffer()
        return "clip saved"
    return _safely(run)
