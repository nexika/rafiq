import subprocess

import pytest

from rafiq import config, presence
from rafiq.tools import REGISTRY, load_all


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Phone:
    """A probe that answers whatever the test says the phone is doing."""
    def __init__(self, seen=True):
        self.seen, self.calls = seen, 0

    def __call__(self):
        self.calls += 1
        return self.seen


def make(tmp_path, phone, clock, away_after=600):
    fired = []
    p = presence.Presence(tmp_path / "presence.json", phone, on_away=lambda: fired.append("away"),
                          on_home=lambda: fired.append("home"), away_after=away_after, clock=clock)
    return p, fired


def test_the_first_check_learns_where_the_phone_is_without_a_scene(tmp_path):
    p, fired = make(tmp_path, Phone(seen=True), Clock())
    assert p.check() is None and p.state == "home" and fired == []


def test_away_only_after_ten_minutes_off_the_wifi_then_welcome_home(tmp_path):
    phone, clock = Phone(seen=True), Clock()
    p, fired = make(tmp_path, phone, clock)
    p.check()
    phone.seen = False
    clock.t += 599
    assert p.check() is None and p.state == "home"      # a phone asleep for a few minutes is still home
    clock.t += 2
    assert p.check() == "away" and fired == ["away"]
    clock.t += 3600
    assert p.check() is None and fired == ["away"]      # once, not every check
    phone.seen = True
    assert p.check() == "home" and fired == ["away", "home"]
    assert p.check() is None and fired == ["away", "home"]


def test_a_short_gap_does_not_count(tmp_path):
    phone, clock = Phone(seen=True), Clock()
    p, fired = make(tmp_path, phone, clock)
    p.check()
    phone.seen = False
    clock.t += 500
    p.check()
    phone.seen = True
    p.check()
    phone.seen = False
    clock.t += 500
    assert p.check() is None and fired == []


def test_starting_while_away_is_silent(tmp_path):
    phone, clock = Phone(seen=False), Clock()
    p, fired = make(tmp_path, phone, clock)
    assert p.check() is None and p.state == "unknown"
    clock.t += 601
    assert p.check() is None and p.state == "away" and fired == []
    phone.seen = True
    assert p.check() == "home" and fired == ["home"]


def test_turned_off_it_neither_probes_nor_plays_and_stays_off_after_a_restart(tmp_path):
    phone, clock = Phone(seen=True), Clock()
    p, fired = make(tmp_path, phone, clock)
    p.check()
    p.set_enabled(False)
    phone.seen, calls = False, phone.calls
    clock.t += 3600
    assert p.check() is None and fired == [] and phone.calls == calls
    restarted, _ = make(tmp_path, phone, clock)
    assert not restarted.enabled
    restarted.set_enabled(True)
    assert make(tmp_path, phone, clock)[0].enabled


def test_turning_it_back_on_forgets_the_old_state(tmp_path):
    phone, clock = Phone(seen=True), Clock()
    p, fired = make(tmp_path, phone, clock)
    p.check()
    p.set_enabled(False)
    phone.seen = False
    clock.t += 3600
    p.set_enabled(True)
    assert p.check() is None and p.state == "unknown" and fired == []


def test_a_probe_that_fails_counts_as_not_seen(tmp_path):
    def broken():
        raise OSError("no network")

    p, fired = make(tmp_path, broken, Clock())
    assert p.check() is None and p.state == "unknown"


def completed(code=0, out=""):
    return subprocess.CompletedProcess([], code, out, "")


def test_phone_seen_by_ping():
    calls = []

    def run(args, timeout):
        calls.append(args)
        return completed(0)

    assert presence.phone_seen("192.168.1.20", run=run)
    assert calls == [["ping", "-c", "1", "-W", "1", "192.168.1.20"]]


@pytest.mark.parametrize("neigh, mac, seen", [
    ("192.168.1.20 dev wlp2s0 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n", "", True),
    ("192.168.1.20 dev wlp2s0 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n", "AA:BB:CC:DD:EE:FF", True),
    ("192.168.1.20 dev wlp2s0 lladdr 11:22:33:44:55:66 REACHABLE\n", "aa:bb:cc:dd:ee:ff", False),
    ("192.168.1.20 dev wlp2s0 lladdr aa:bb:cc:dd:ee:ff STALE\n", "", False),
    ("192.168.1.20 dev wlp2s0 FAILED\n", "", False),
    ("", "", False),
])
def test_a_sleeping_phone_that_ignores_ping_is_seen_by_arp(neigh, mac, seen):
    def run(args, timeout):
        return completed(1) if args[0] == "ping" else completed(0, neigh)

    assert presence.phone_seen("192.168.1.20", mac, run=run) is seen


def test_missing_commands_mean_not_seen():
    def run(args, timeout):
        raise FileNotFoundError(args[0])

    assert presence.phone_seen("192.168.1.20", run=run) is False


def test_a_scene_runs_its_command_as_an_argument_list():
    cfg = config.load()
    cfg["presence"]["away_command"] = ["hue", "scene", "away"]
    calls = []
    presence.run_scene(cfg, "away", run=lambda args, timeout: calls.append(args) or completed(0))
    presence.run_scene(cfg, "home", run=lambda args, timeout: calls.append(args) or completed(0))
    assert calls == [["hue", "scene", "away"]]


def test_the_presence_tool_turns_it_off_and_on_by_voice(tmp_path):
    cfg = config.load()
    cfg["presence"]["phone_ip"] = "192.168.1.20"
    load_all(cfg)
    p, _ = make(tmp_path, Phone(seen=True), Clock())
    ctx = type("Ctx", (), {"cfg": cfg, "presence": p})()
    tool = REGISTRY["presence"]
    assert tool.action({"action": "off"}) is None
    assert "off" in tool.run({"action": "off"}, ctx) and not p.enabled
    assert "on" in tool.run({"action": "on"}, ctx) and p.enabled
    p.check()
    assert "home" in tool.run({"action": "status"}, ctx)


def test_the_presence_tool_says_when_no_phone_is_set_up():
    load_all({"presence": {"phone_ip": "192.168.1.20"}})
    ctx = type("Ctx", (), {"cfg": config.load(), "presence": None})()
    assert REGISTRY["presence"].run({"action": "off"}, ctx).startswith("error")


def test_turning_it_off_from_another_process_is_seen_at_the_next_check(tmp_path):
    phone, clock = Phone(seen=True), Clock()
    running, fired = make(tmp_path, phone, clock)
    running.check()
    other, _ = make(tmp_path, Phone(), clock)
    other.set_enabled(False)
    phone.seen = False
    clock.t += 3600
    assert running.check() is None and not running.enabled and fired == []


@pytest.mark.parametrize("run, error", [
    (lambda args, timeout: completed(0), None),
    (lambda args, timeout: completed(2), "away scene: exit code 2"),
])
def test_a_scene_that_fails_says_why(run, error):
    cfg = config.load()
    cfg["presence"]["away_command"] = ["hue", "scene", "away"]
    assert presence.run_scene(cfg, "away", run=run) == error


def test_a_missing_scene_command_is_an_error_not_a_crash():
    cfg = config.load()
    cfg["presence"]["away_command"] = ["no-such-command-rafiq"]
    assert "FileNotFoundError" in presence.run_scene(cfg, "away")
