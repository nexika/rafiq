import json
import queue
import sys
import threading
import urllib.error
import urllib.request

import pytest

from rafiq import config, gestures, hands, tracker
from rafiq.gestures import GestureControl, SystemDesktop
from rafiq.panel import serve
from rafiq.tools import pc

# ---------------------------------------------------------------------- recognising a hand

MCP = {5: (0.44, 0.60), 9: (0.50, 0.58), 13: (0.56, 0.60), 17: (0.62, 0.63)}


def hand(thumb=False, index=False, middle=False, ring=False, pinky=False, dx=0.0):
    """21 MediaPipe landmarks, (x, y) with y going down, for a hand held up facing the camera."""
    points = [(0.50, 0.80), (0.42, 0.75), (0.38, 0.70)]
    points += [(0.34, 0.62), (0.28, 0.50)] if thumb else [(0.42, 0.66), (0.47, 0.63)]
    for base, up in zip(MCP, (index, middle, ring, pinky), strict=True):
        x, y = MCP[base]
        offsets = (0, -0.08, -0.13, -0.17) if up else (0, -0.06, -0.02, 0.02)
        points += [(x, y + o) for o in offsets]
    return [(x + dx, y) for x, y in points]


@pytest.mark.parametrize("shape, label", [
    ({"thumb": True, "index": True, "middle": True, "ring": True, "pinky": True}, "open_palm"),
    ({"thumb": True}, "thumbs_up"),
    ({}, "fist"),
    ({"index": True}, "fingers_1"),
    ({"index": True, "middle": True}, "fingers_2"),
    ({"index": True, "middle": True, "ring": True}, "fingers_3"),
])
def test_a_hand_shape_gets_its_label(shape, label):
    assert hands.classify(hand(**shape)) == label


def test_a_shape_that_means_nothing_has_no_label():
    assert hands.classify(hand(thumb=True, pinky=True)) is None
    assert hands.classify([(0.5, 0.5)] * 5) is None


def frames(recognizer, shape_at, start, end, fps=10):
    """Feed the recognizer a hand at fps from start to end; the labels it fires, with their times."""
    fired, n = [], int(round((end - start) * fps))
    for i in range(n):
        t = start + i / fps
        label = recognizer.step(shape_at(t), t)
        if label:
            fired.append((label, round(t, 2)))
    return fired


def test_a_gesture_fires_once_when_held():
    r = hands.Recognizer(hold_seconds=0.6, cooldown_seconds=1.0)
    palm = hand(True, True, True, True, True)
    fired = frames(r, lambda t: [palm], 0, 3)
    assert fired == [("open_palm", 0.6)]


def test_a_gesture_shown_briefly_does_not_fire():
    r = hands.Recognizer(hold_seconds=0.6, cooldown_seconds=1.0)
    assert frames(r, lambda t: [hand(thumb=True)], 0, 0.4) == []
    assert frames(r, lambda t: [], 0.4, 1.0) == []
    assert frames(r, lambda t: [hand(thumb=True)], 1.0, 1.4) == []


def test_a_new_gesture_waits_for_the_cooldown():
    r = hands.Recognizer(hold_seconds=0.3, cooldown_seconds=1.0)
    assert frames(r, lambda t: [hand(thumb=True)], 0, 0.5) == [("thumbs_up", 0.3)]
    assert frames(r, lambda t: [hand()], 0.5, 2.0) == [("fist", 1.3)]


def test_a_hand_moving_sideways_is_a_swipe():
    r = hands.Recognizer()
    palm = hand(True, True, True, True, True)
    fired = frames(r, lambda t: [[(x + t, y) for x, y in palm]], 0, 0.5)   # 1 frame width per second
    assert [label for label, _ in fired] == ["swipe_right"]
    r = hands.Recognizer()
    fired = frames(r, lambda t: [[(x - t, y) for x, y in palm]], 0, 0.5)
    assert [label for label, _ in fired] == ["swipe_left"]


def test_two_hands_moving_apart_or_together_zoom():
    r = hands.Recognizer()
    fired = frames(r, lambda t: [hand(dx=-0.1 - t / 2), hand(dx=0.1 + t / 2)], 0, 0.6)
    assert [label for label, _ in fired] == ["zoom_in"]
    r = hands.Recognizer()
    fired = frames(r, lambda t: [hand(dx=-0.3 + t / 2), hand(dx=0.3 - t / 2)], 0, 0.6)
    assert [label for label, _ in fired] == ["zoom_out"]


# ---------------------------------------------------------------------- the tracker process

def test_the_tracker_sends_only_labels():
    palm = hand(True, True, True, True, True)
    sent = []
    clock = iter(i / 10 for i in range(100))
    tracker.track(["image"] * 20, lambda image: [palm], sent.append, hands.Recognizer(), clock=lambda: next(clock))
    assert sent == ["open_palm"]


def test_a_label_is_posted_with_the_session_token(monkeypatch):
    import httpx

    calls = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: calls.append((url, kw)))
    tracker.post_label("http://127.0.0.1:8765/api/gesture", "s" * 40, "fist")
    url, kw = calls[0]
    assert url.endswith("/api/gesture") and kw["json"] == {"gesture": "fist"}
    assert kw["headers"] == {"X-Rafiq-Gesture-Token": "s" * 40} and kw["timeout"]


# ---------------------------------------------------------------------- what a gesture does

class FakeDesktop:
    def __init__(self):
        self.done = []

    def media(self, command):
        self.done.append(("media", command))

    def keys(self, name):
        self.done.append(("keys", name))

    def volume(self, change):
        self.done.append(("volume", change))


class FakeProcess:
    def __init__(self, args, env=None, **kwargs):
        self.args, self.env, self.stopped = args, env, False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def control():
    desktop, stopped, events, spawned, now = FakeDesktop(), [], [], [], [0.0]

    def spawn(args, **kwargs):
        spawned.append(FakeProcess(args, **kwargs))
        return spawned[-1]

    c = GestureControl(desktop, lambda: stopped.append(True), lambda kind, data: events.append((kind, data)),
                       url="http://127.0.0.1:8765/api/gesture", spawn=spawn, clock=lambda: now[0])
    c.start()
    return c, desktop, stopped, events, spawned, now


def test_palm_pauses_thumbs_up_plays_and_a_fist_cuts_rafiq_off(control):
    c, desktop, stopped, events, _, _ = control
    assert c.handle("open_palm") == "pause"
    assert c.handle("thumbs_up") == "play"
    assert c.handle("fist") == "stop_speech"
    assert desktop.done == [("media", "pause"), ("media", "play")] and stopped == [True]
    assert ("gesture", {"gesture": "fist", "action": "stop_speech"}) in events


def test_finger_counts_arm_a_mode_for_swipes(control):
    c, desktop, _, _, _, now = control
    assert c.handle("swipe_right") is None                  # no mode armed: nothing
    assert c.handle("fingers_1") == "mode:tabs"
    assert c.handle("swipe_right") == "next_tab"
    assert c.handle("swipe_left") == "previous_tab"
    c.handle("fingers_2")
    assert c.handle("swipe_right") == "volume_up"
    assert c.handle("swipe_left") == "volume_down"
    now[0] += gestures.MODE_SECONDS + 1
    assert c.handle("swipe_left") is None                    # the mode ran out
    assert desktop.done == [("keys", "next_tab"), ("keys", "previous_tab"), ("volume", 10), ("volume", -10)]


def test_two_hands_zoom(control):
    c, desktop, *_ = control
    c.handle("zoom_in")
    c.handle("zoom_out")
    assert desktop.done == [("keys", "zoom_in"), ("keys", "zoom_out")]


def test_labels_outside_the_allowlist_do_nothing(control):
    c, desktop, stopped, *_ = control
    for label in ("power_off", "shutdown", "", "open_palm; rm -rf /", None, 3):
        assert c.handle(label) is None
    assert desktop.done == [] and stopped == []


def test_nothing_happens_while_gestures_are_off(control):
    c, desktop, _, _, spawned, _ = control
    c.stop()
    assert spawned[0].stopped and not c.enabled
    assert c.handle("open_palm") is None and desktop.done == []


def test_the_tracker_gets_a_new_token_each_start_and_never_on_its_command_line(control):
    c, _, _, _, spawned, _ = control
    first = c.token
    assert spawned[0].env["RAFIQ_GESTURE_TOKEN"] == first and len(first) >= 32
    assert all(first not in arg for arg in spawned[0].args)
    assert spawned[0].args[:3] == [sys.executable, "-m", "rafiq.tracker"]
    c.stop()
    assert not c.token_ok(first)
    c.start()
    assert c.token != first and c.token_ok(c.token) and not c.token_ok("x" * 40)


def test_gestures_need_the_panel_address():
    c = GestureControl(FakeDesktop(), lambda: None, lambda kind, data: None, url=None,
                       spawn=lambda *a, **k: pytest.fail("no tracker without the panel"))
    assert c.start().startswith("error")
    assert not c.enabled


# ---------------------------------------------------------------------- keys on Wayland and X11

def test_keys_go_through_ydotool_on_wayland(monkeypatch):
    runs = []
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(gestures.shutil, "which", lambda name: f"/usr/bin/{name}")
    desktop = SystemDesktop(run=runs.append)
    desktop.keys("next_tab")
    assert runs == [["ydotool", "key", "29:1", "15:1", "15:0", "29:0"]]


def test_keys_go_through_xdotool_on_x11(monkeypatch):
    runs = []
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(gestures.shutil, "which", lambda name: f"/usr/bin/{name}")
    desktop = SystemDesktop(run=runs.append)
    desktop.keys("previous_tab")
    desktop.media("pause")
    assert runs == [["xdotool", "key", "ctrl+shift+Tab"], ["playerctl", "pause"]]


def test_an_unknown_key_name_is_refused():
    with pytest.raises(KeyError):
        SystemDesktop(run=lambda args: None).keys("alt+F4")


# ---------------------------------------------------------------------- settings and calibration

def test_calibration_overrides_the_timing_in_the_settings():
    cfg = config.load()
    assert gestures.settings(cfg)["hold_seconds"] == config.DEFAULTS["gestures"]["hold_seconds"]
    gestures.save_calibration(0.9, 1.5)
    s = gestures.settings(cfg)
    assert (s["hold_seconds"], s["cooldown_seconds"]) == (0.9, 1.5)
    assert json.loads((config.data_dir() / "gestures.json").read_text()) == {"hold_seconds": 0.9,
                                                                               "cooldown_seconds": 1.5}


def test_the_tracker_is_started_with_the_calibrated_timing(control):
    _, _, _, _, spawned, _ = control
    args = spawned[0].args
    assert "--hold" in args and "--cooldown" in args and "--camera" in args


# ---------------------------------------------------------------------- the panel endpoint

class FakeAssistant:
    def __init__(self):
        self.jobs, self.pending_answer = [], queue.Queue()

    def submit(self, kind, **data):
        self.jobs.append((kind, data))


def request(port, path, body, token="t" * 40):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", data=json.dumps(body).encode())
    req.add_header("X-Rafiq-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture
def panel_with_gestures(control):
    c = control[0]
    assistant = FakeAssistant()
    assistant.gestures = c
    server = serve(assistant, "127.0.0.1", 0, "t" * 40)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield assistant, port, control
    server.shutdown()


def gesture_request(port, label, token):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/gesture", method="POST",
                                 data=json.dumps({"gesture": label}).encode())
    req.add_header("X-Rafiq-Gesture-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_the_tracker_posts_a_label_to_the_panel(panel_with_gestures):
    _, port, (c, desktop, *_) = panel_with_gestures
    assert gesture_request(port, "open_palm", c.token) == 200
    assert desktop.done == [("media", "pause")]


def test_a_gesture_without_the_session_token_is_refused(panel_with_gestures):
    _, port, (c, desktop, *_) = panel_with_gestures
    assert gesture_request(port, "open_palm", "t" * 40) == 403     # the panel's token is not enough
    assert gesture_request(port, "open_palm", "x" * 40) == 403
    assert desktop.done == []


def test_the_session_token_cannot_drive_the_rest_of_the_panel(panel_with_gestures):
    assistant, port, (c, *_) = panel_with_gestures
    assert request(port, "/api/text", {"text": "shut down"}, token=c.token) == 403
    assert assistant.jobs == []


def test_the_panel_turns_gestures_on_and_off(panel_with_gestures):
    _, port, (c, _, _, _, spawned, _) = panel_with_gestures
    assert request(port, "/api/gestures", {"on": False}) == 200
    assert not c.enabled
    assert request(port, "/api/gestures", {"on": True}) == 200
    assert c.enabled and len(spawned) == 2


# ---------------------------------------------------------------------- voice and shortcut

def test_the_voice_tool_turns_gestures_on_and_off(control):
    c = control[0]
    ctx = type("Ctx", (), {"gestures": c})()
    assert "off" in pc.hand_gestures(on=False, ctx=ctx) and not c.enabled
    assert "on" in pc.hand_gestures(on=True, ctx=ctx) and c.enabled
    assert pc.hand_gestures(on=True, ctx=type("Ctx", (), {"gestures": None})()).startswith("error")


def test_the_shortcut_command_asks_the_running_rafiq(monkeypatch):
    import httpx

    from rafiq.__main__ import main

    calls = []

    class Resp:
        status_code = 200

    monkeypatch.setattr(httpx, "post", lambda url, **kw: calls.append((url, kw)) or Resp())
    assert main(["gestures", "on"]) == 0
    url, kw = calls[0]
    assert url.endswith("/api/gestures") and kw["json"] == {"on": True}
    assert kw["headers"]["X-Rafiq-Token"] == config.panel_token()
