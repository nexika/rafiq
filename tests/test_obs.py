from types import SimpleNamespace

import pytest

from rafiq import config
from rafiq.tools import REGISTRY, load_all, obs


class FakeOBS:
    """Answers like obsws-python's ReqClient against OBS's WebSocket v5."""

    def __init__(self, scenes=("Desk", "Camera", "Be right back"), replay=True):
        self.scenes, self.scene = list(scenes), scenes[0]
        self.streaming = self.recording = False
        self.replay, self.calls = replay, []

    def get_scene_list(self):
        return SimpleNamespace(scenes=[{"sceneName": s, "sceneIndex": i} for i, s in enumerate(reversed(self.scenes))],
                               current_program_scene_name=self.scene)

    def set_current_program_scene(self, name):
        self.calls.append(("scene", name))
        self.scene = name

    def get_stream_status(self):
        return SimpleNamespace(output_active=self.streaming)

    def get_record_status(self):
        return SimpleNamespace(output_active=self.recording)

    def get_replay_buffer_status(self):
        return SimpleNamespace(output_active=self.replay)

    def start_stream(self):
        self.calls.append("start_stream")
        self.streaming = True

    def stop_stream(self):
        self.calls.append("stop_stream")
        self.streaming = False

    def start_record(self):
        self.calls.append("start_record")
        self.recording = True

    def stop_record(self):
        self.calls.append("stop_record")
        self.recording = False
        return SimpleNamespace(output_path="/home/me/Videos/2026-10-10 20-00-00.mkv")

    def save_replay_buffer(self):
        self.calls.append("save_replay_buffer")


@pytest.fixture
def fake(monkeypatch):
    client = FakeOBS()
    monkeypatch.setattr(obs, "connect", lambda settings: client)
    return client


def ctx(**obs_settings):
    cfg = config.load()
    cfg["obs"].update(obs_settings)
    return SimpleNamespace(cfg=cfg)


def test_obs_tools_load_only_when_enabled():
    load_all({"obs": {"enabled": True}})
    assert "obs_record" not in load_all({})
    tools = load_all({"obs": {"enabled": True}})
    for name in ("obs_status", "obs_stream", "obs_record", "obs_scene", "obs_clip"):
        assert name in tools and tools[name].description


def test_starting_and_stopping_a_stream_or_recording_is_confirmed_first():
    load_all({"obs": {"enabled": True}})
    assert REGISTRY["obs_stream"].action({"action": "start"}) == "start streaming in OBS"
    assert REGISTRY["obs_stream"].action({"action": "stop"}) == "stop the OBS stream"
    assert REGISTRY["obs_record"].action({"action": "start"}) == "start recording in OBS"
    assert REGISTRY["obs_record"].action({"action": "stop"}) == "stop the OBS recording"
    for name in ("obs_status", "obs_scene", "obs_clip"):
        assert REGISTRY[name].action({"name": "Camera"}) is None, name


def test_settings_come_from_the_config_and_the_password_from_the_environment_first(monkeypatch):
    assert obs.settings(config.load()) == {"host": "localhost", "port": 4455, "password": "", "timeout": 5}
    monkeypatch.setenv("OBS_WEBSOCKET_PASSWORD", "from-env")
    cfg = config.load()
    cfg["obs"].update(password="from-file", port="4460")
    assert obs.settings(cfg)["password"] == "from-env" and obs.settings(cfg)["port"] == 4460


def test_record_starts_and_stops(fake):
    assert obs.obs_record("start", ctx=ctx()) == "recording started"
    assert obs.obs_record("start", ctx=ctx()) == "already recording"
    assert "2026-10-10 20-00-00.mkv" in obs.obs_record("stop", ctx=ctx())
    assert obs.obs_record("stop", ctx=ctx()) == "not recording"
    assert fake.calls == ["start_record", "stop_record"]


def test_stream_starts_and_stops(fake):
    assert obs.obs_stream("start", ctx=ctx()) == "stream started"
    assert obs.obs_stream("stop", ctx=ctx()) == "stream stopped"
    assert obs.obs_stream("stop", ctx=ctx()) == "not streaming"
    assert obs.obs_stream("pause", ctx=ctx()).startswith("error")
    assert fake.calls == ["start_stream", "stop_stream"]


@pytest.mark.parametrize("spoken, scene", [
    ("Camera", "Camera"), ("camera", "Camera"), ("the camera scene", "Camera"), ("cam", "Camera"),
    ("BRB", None), ("be right back", "Be right back"), ("desk", "Desk"),
])
def test_find_scene(spoken, scene):
    assert obs.find_scene(spoken, ["Desk", "Camera", "Be right back"]) == scene


def test_switch_scene(fake):
    assert obs.obs_scene("the camera scene", ctx=ctx()) == "switched to Camera"
    assert fake.calls == [("scene", "Camera")]
    text = obs.obs_scene("garden", ctx=ctx())
    assert text.startswith("error") and "Desk, Camera, Be right back" in text


def test_clip_saves_the_replay_buffer(fake):
    assert obs.obs_clip(ctx=ctx()) == "clip saved"
    fake.replay = False
    assert "replay buffer" in obs.obs_clip(ctx=ctx())
    assert fake.calls == ["save_replay_buffer"]


def test_status(fake):
    fake.recording = True
    text = obs.obs_status(ctx=ctx())
    assert "recording" in text and "not streaming" in text and "scene Desk" in text and "Camera" in text


def test_obs_not_running_is_a_clear_error(monkeypatch):
    def refuse(settings):
        raise ConnectionRefusedError(111, "Connection refused")
    monkeypatch.setattr(obs, "connect", refuse)
    assert "OBS" in obs.obs_status(ctx=ctx()) and obs.obs_status(ctx=ctx()).startswith("error")
