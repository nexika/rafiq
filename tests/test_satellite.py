import numpy as np
import pytest

from rafiq import __main__ as cli
from rafiq import satellite
from rafiq.satellite import LinkClosed, Refused, Satellite

SECOND = np.full(16000, 0.25, dtype="float32")


class FakeLink:
    """The server's side of the connection: a script of messages to receive, and what was sent."""

    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    def send(self, message):
        self.sent.append(message)

    def recv(self, timeout):
        if not self.incoming:
            raise LinkClosed("the server went away")
        item = self.incoming.pop(0)
        return None if item == "nothing" else item

    def close(self):
        self.closed = True

    def kinds(self):
        return [m["type"] if isinstance(m, dict) else "pcm" for m in self.sent]


class FakeEars:
    """Utterances heard, in order; None means nothing was said before the timeout."""

    def __init__(self, utterances=()):
        self.utterances = list(utterances)
        self.muted = []
        self.drained = 0

    def next_utterance(self, timeout):
        return self.utterances.pop(0) if self.utterances else None

    def mute(self, muted):
        self.muted.append(muted)

    def drain(self):
        self.drained += 1


class FakePlayer:
    def __init__(self):
        self.played = []

    def play(self, audio, rate):
        self.played.append((audio, rate))


def make(link, ears=None, score=0.1, threshold=0.3, **kw):
    return Satellite(server="ws://192.168.1.10:8766/satellite", name="kitchen", token="t" * 32,
                     connect=lambda url: link, ears=ears or FakeEars(), player=FakePlayer(),
                     wake_score=lambda audio: score, threshold=threshold, **kw)


WELCOME = {"type": "welcome", "room": "kitchen"}


def test_hello_names_the_satellite_and_carries_its_token():
    link = FakeLink([WELCOME])
    with pytest.raises(LinkClosed):
        make(link).session()
    hello = link.sent[0]
    assert hello["type"] == "hello" and hello["name"] == "kitchen" and hello["token"] == "t" * 32
    assert hello["protocol"] == satellite.PROTOCOL and hello["rate"] == 16000 and hello["format"] == "s16le"


def test_a_refused_satellite_stops_instead_of_retrying():
    link = FakeLink([{"type": "refused", "reason": "unknown token"}])
    with pytest.raises(Refused, match="unknown token"):
        make(link).session()
    assert make(FakeLink([{"type": "refused", "reason": "x"}])).run() == 2


def test_the_name_heard_sends_the_utterance_to_the_server():
    link = FakeLink([WELCOME, "nothing"])
    with pytest.raises(LinkClosed):
        make(link, FakeEars([SECOND]), score=0.1, threshold=0.3).session()
    assert link.kinds()[1] == "wake" and link.kinds()[-1] == "end"
    assert link.sent[1]["score"] == pytest.approx(0.1)
    pcm = b"".join(m for m in link.sent if isinstance(m, bytes))
    assert len(pcm) == 16000 * 2
    assert np.frombuffer(pcm, dtype="<i2")[0] == int(0.25 * 32767)


def test_speech_that_is_not_the_name_stays_on_the_satellite():
    link = FakeLink([WELCOME, "nothing"])
    with pytest.raises(LinkClosed):
        make(link, FakeEars([SECOND]), score=0.5, threshold=0.3).session()
    assert link.kinds() == ["hello"]


def test_when_the_server_listens_the_next_utterance_goes_without_the_name():
    link = FakeLink([WELCOME, {"type": "listen", "seconds": 6}, "nothing"])
    ears = FakeEars([SECOND])
    with pytest.raises(LinkClosed):
        make(link, ears, score=9.0, threshold=0.3).session()
    assert link.kinds()[1:] == ["pcm", "pcm", "pcm", "pcm", "end"]


def test_nothing_said_after_listen_is_reported():
    link = FakeLink([WELCOME, {"type": "listen", "seconds": 6}])
    with pytest.raises(LinkClosed):
        make(link, FakeEars([])).session()
    assert link.kinds()[1:] == ["silence"]


def test_the_answer_is_played_with_the_microphone_muted():
    tone = (np.arange(800) % 100).astype("<i2")
    link = FakeLink([WELCOME, {"type": "play", "rate": 22050}, tone[:400].tobytes(), tone[400:].tobytes(),
                     {"type": "play_end"}])
    ears = FakeEars()
    sat = make(link, ears)
    with pytest.raises(LinkClosed):
        sat.session()
    (audio, rate), = sat.player.played
    assert rate == 22050 and np.array_equal(audio, tone)
    assert ears.muted == [True, False] and ears.drained == 1


def test_it_reconnects_with_growing_waits_until_stopped():
    attempts, waits = [], []

    def connect(url):
        attempts.append(url)
        if len(attempts) == 4:
            sat.stop()
        raise OSError("connection refused")

    sat = Satellite(server="ws://10.0.0.2:8766/satellite", name="kitchen", token="t" * 32, connect=connect,
                    ears=FakeEars(), player=FakePlayer(), wake_score=lambda a: 1.0, threshold=0.3,
                    sleep=waits.append)
    assert sat.run() == 0
    assert waits == [1, 2, 4]


def test_a_welcomed_session_resets_the_wait():
    links = [FakeLink([WELCOME]), FakeLink([WELCOME])]
    waits = []

    def connect(url):
        if not links:
            sat.stop()
            raise OSError("gone")
        return links.pop(0)

    sat = Satellite(server="ws://10.0.0.2:8766/satellite", name="kitchen", token="t" * 32, connect=connect,
                    ears=FakeEars(), player=FakePlayer(), wake_score=lambda a: 1.0, threshold=0.3,
                    sleep=waits.append)
    sat.run()
    assert waits == [1, 1]


@pytest.mark.parametrize("attempt, wait", [(0, 1), (1, 2), (3, 8), (5, 30), (40, 30)])
def test_backoff_doubles_and_stops_at_thirty_seconds(attempt, wait):
    assert satellite.backoff(attempt) == wait


@pytest.mark.parametrize("url, ok", [
    ("ws://192.168.1.10:8766/satellite", True),
    ("wss://rafiq.example.org/satellite", True),
    ("ws://rafiq.example.org/satellite", False),     # a token in clear text over the internet
    ("http://192.168.1.10:8766", False),
    ("", False),
])
def test_server_address_must_be_websocket_and_private_unless_encrypted(url, ok):
    if ok:
        satellite.check_server(url)
    else:
        with pytest.raises(ValueError):
            satellite.check_server(url)


def test_service_unit_runs_the_satellite_and_restarts_it():
    unit = satellite.service_unit("/opt/rafiq/.venv/bin/python")
    assert "ExecStart=/opt/rafiq/.venv/bin/python -m rafiq satellite" in unit
    assert "Restart=always" in unit and "RestartPreventExitStatus=2" in unit
    assert "WantedBy=default.target" in unit


def test_the_command_explains_a_missing_server(capsys):
    assert cli.main(["satellite"]) == 1
    assert "server" in capsys.readouterr().err


def test_the_command_prints_the_service_unit(capsys):
    assert cli.main(["satellite", "--unit"]) == 0
    assert "[Service]" in capsys.readouterr().out


def test_the_token_comes_from_the_environment_first(monkeypatch, rafiq_home):
    rafiq_home.mkdir(parents=True)
    (rafiq_home / "config.toml").write_text('[satellite]\ntoken = "from-file"\n')
    from rafiq import config

    assert satellite.token(config.load()) == "from-file"
    monkeypatch.setenv("RAFIQ_SATELLITE_TOKEN", "from-env")
    assert satellite.token(config.load()) == "from-env"
