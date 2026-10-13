import socket
import threading

import numpy as np
import pytest

from rafiq import config, satellite
from rafiq.brain import with_note
from rafiq.stt import Heard
from rafiq.tools import Tool

TOKEN = "k" * 32


def make_cfg(**device):
    cfg = config.load()
    cfg["satellites"]["devices"] = {"kitchen": {"token": TOKEN, "room": "kitchen", **device}}
    return cfg


class FakeBrain:
    """Answers from a script; asks to confirm when the request mentions shutting down."""

    def __init__(self, tools, confirm, log):
        self.tools, self.confirm, self.log = tools, confirm, log

    def think(self, text, tag, ctx):
        self.log.append((text, tag, ctx.room, sorted(self.tools)))
        if "shut down" in text:
            return "Shutting down." if self.confirm("shut down the computer", tag) else "I didn't."
        return f"It is noon in the {ctx.room}."


def tools():
    return {"get_time": Tool("get_time", "time", lambda: "noon"),
            "power": Tool("power", "power off", lambda: "done", confirm="shut down the computer")}


@pytest.fixture
def hub():
    """A hub on a free port of this computer, with a fake transcriber that 'hears' a queued text."""
    heard, log, servers = [], [], []

    def start(cfg=None):
        cfg = cfg or make_cfg()
        h = satellite.Hub(cfg, transcribe=lambda audio: Heard(heard.pop(0), "en", 1.0),
                          make_brain=lambda tools, confirm: FakeBrain(tools, confirm, log),
                          tools=tools(), scheduler=None)
        server = h.serve("127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start, heard, log
    for server in servers:
        server.shutdown()
        server.server_close()


def audio(seconds=0.5):
    return np.zeros(int(seconds * 16000), dtype="float32")


def test_pcm_round_trip_keeps_the_samples():
    samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype="float32")
    back = satellite.from_pcm(satellite.to_pcm(samples))
    assert np.allclose(back, samples, atol=1 / 32767)


def test_frames_round_trip_over_a_socket():
    a, b = socket.socketpair()
    with a, b:
        satellite.send_json(a, {"type": "hello", "name": "kitchen"})
        satellite.send_audio(a, np.array([0.25, -0.25], dtype="float32"))
        assert satellite.recv(b) == ("json", {"type": "hello", "name": "kitchen"})
        kind, samples = satellite.recv(b)
        assert kind == "audio" and np.allclose(samples, [0.25, -0.25], atol=1e-4)
        a.close()
        assert satellite.recv(b) is None


def test_an_oversized_frame_is_refused():
    a, b = socket.socketpair()
    with a, b:
        a.sendall(b"J" + (satellite.MAX_FRAME + 1).to_bytes(4, "big"))
        with pytest.raises(satellite.ProtocolError):
            satellite.recv(b)


def test_devices_need_a_long_token():
    cfg = config.load()
    cfg["satellites"]["devices"] = {"kitchen": {"token": TOKEN, "room": "kitchen"}, "weak": {"token": "1234"}}
    assert list(satellite.devices(cfg)) == ["kitchen"]


def test_authenticate_checks_name_and_token():
    devices = satellite.devices(make_cfg())
    assert satellite.authenticate(devices, "kitchen", TOKEN).room == "kitchen"
    assert satellite.authenticate(devices, "kitchen", "x" * 32) is None
    assert satellite.authenticate(devices, "garage", TOKEN) is None
    assert satellite.authenticate(devices, "kitchen", None) is None


def test_room_tools_hide_blocked_and_risky_tools():
    device = satellite.devices(make_cfg(blocked_tools=["get_time"]))["kitchen"]
    assert list(satellite.room_tools(tools(), device)) == ["power"]
    device = satellite.devices(make_cfg(risky="block"))["kitchen"]
    assert list(satellite.room_tools(tools(), device)) == ["get_time"]


def test_the_note_names_the_room():
    assert "in the kitchen" in with_note("turn on the light", "en", room="kitchen")
    assert "in the" not in with_note("turn on the light", "en")


def test_a_wrong_token_is_turned_away(hub):
    start, _, _ = hub
    port = start()
    with pytest.raises(satellite.ProtocolError, match="token"):
        satellite.Client("127.0.0.1", port, "kitchen", "x" * 32).connect()


def test_a_request_is_answered_with_the_room_known(hub):
    start, heard, log = hub
    port = start()
    heard.append("Rafiq, what time is it?")
    spoken = []
    with satellite.Client("127.0.0.1", port, "kitchen", TOKEN) as client:
        assert client.room == "kitchen"
        said = client.request(audio(), speak=lambda text, tag: spoken.append(text))
    assert said == ["It is noon in the kitchen."] == spoken
    assert log[0][:3] == ("what time is it?", "en", "kitchen")


def test_a_risky_action_is_confirmed_on_the_satellite(hub):
    start, heard, _ = hub
    port = start()
    heard += ["shut down the computer", "yes"]
    spoken = []
    with satellite.Client("127.0.0.1", port, "kitchen", TOKEN) as client:
        said = client.request(audio(), speak=lambda text, tag: spoken.append(text), listen=lambda: audio())
    assert said[-1] == "Shutting down."
    assert any("shut down the computer" in s for s in spoken[:-1])


def test_a_risky_action_answered_no_is_not_done(hub):
    start, heard, _ = hub
    port = start()
    heard.append("shut down the computer")
    with satellite.Client("127.0.0.1", port, "kitchen", TOKEN) as client:
        said = client.request(audio(), speak=lambda text, tag: None, listen=lambda: False)
    assert said[-1] == "I didn't."


def test_blocked_tools_never_reach_the_brain_in_that_room(hub):
    start, heard, log = hub
    port = start(make_cfg(risky="block"))
    heard.append("what time is it")
    with satellite.Client("127.0.0.1", port, "kitchen", TOKEN) as client:
        client.request(audio(), speak=lambda text, tag: None)
    assert log[0][3] == ["get_time"]


def test_simulate_reports_the_answer_and_latency(hub):
    start, heard, _ = hub
    port = start()
    heard.append("what time is it")
    result = satellite.simulate("127.0.0.1", port, "kitchen", TOKEN, audio())
    assert result.said == ["It is noon in the kitchen."]
    assert result.room == "kitchen"
    assert 0 <= result.seconds < 5


@pytest.mark.parametrize("address, expected", [
    ("192.168.1.10:9000", ("192.168.1.10", 9000)),
    ("rafiq.lan", ("rafiq.lan", 8766)),
    ("[fd00::1]:8766", ("fd00::1", 8766)),
])
def test_parse_hub(address, expected):
    assert satellite.parse_hub(address) == expected


def test_satellite_simulate_command_prints_the_answer(hub, tmp_path, capsys):
    import wave

    from rafiq.__main__ import main

    start, heard, _ = hub
    port = start()
    heard.append("what time is it")
    path = tmp_path / "request.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(satellite.to_pcm(audio()))
    assert main(["satellite", "simulate", str(path), "--hub", f"127.0.0.1:{port}", "--name", "kitchen",
                 "--token", TOKEN]) == 0
    out = capsys.readouterr().out
    assert "said: It is noon in the kitchen." in out and "answered in" in out


def test_satellite_token_command_prints_a_config_snippet(capsys):
    from rafiq.__main__ import main

    assert main(["satellite", "token", "--name", "garage", "--room", "garage"]) == 0
    out = capsys.readouterr().out
    assert "[satellites.devices.garage]" in out and 'room = "garage"' in out


# ---------------------------------------------------------------------- on a Raspberry Pi (#64)

def test_a_refused_satellite_raises_refused(hub):
    port = hub[0]()
    with pytest.raises(satellite.Refused):
        satellite.Client("127.0.0.1", port, "kitchen", "x" * 32).connect()


def test_a_refused_satellite_stops_with_code_2_before_opening_any_device(hub, monkeypatch):
    import rafiq.audio

    monkeypatch.setattr(rafiq.audio.Microphone, "start", lambda self: pytest.fail("opened the microphone"))
    cfg = make_cfg()
    cfg["satellite"].update(hub=f"127.0.0.1:{hub[0]()}", name="kitchen", token="x" * 32)
    assert satellite.run_device(cfg) == 2


def test_the_token_can_come_from_the_environment(monkeypatch):
    cfg = config.load()
    cfg["satellite"].update(hub="192.168.1.10", name="kitchen", token="")
    monkeypatch.setenv("RAFIQ_SATELLITE_TOKEN", TOKEN)
    assert satellite.settings(cfg) == ("192.168.1.10", 8766, "kitchen", TOKEN)
    monkeypatch.delenv("RAFIQ_SATELLITE_TOKEN")
    assert satellite.settings(cfg) is None


def test_the_unit_restarts_except_when_refused():
    unit = satellite.unit("/opt/rafiq/.venv/bin/python")
    assert "ExecStart=/opt/rafiq/.venv/bin/python -P -m rafiq satellite run" in unit
    assert "Restart=always" in unit and "RestartPreventExitStatus=2" in unit
    assert "WantedBy=default.target" in unit


def test_a_score_line_says_whether_it_would_wake():
    assert satellite.score_line(0.31, 0.4) == "0.310  <= 0.400  wakes"
    assert satellite.score_line(0.52, 0.4) == "0.520  >  0.400  ignored"


def test_scores_need_recordings_of_the_name(monkeypatch, capsys):
    from rafiq import kws

    monkeypatch.setattr(kws.Spotter, "load", classmethod(lambda cls: None))
    assert satellite.print_scores(config.load()) == 1
    assert "rafiq enroll" in capsys.readouterr().out
