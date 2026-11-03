import json
import stat
import subprocess

import pytest

from rafiq import config, pointer
from rafiq.pointer import PointerError, Stream

PORTAL_CLICK = pointer.portal_click      # as defined: conftest.py replaces it, and every test here gives a fake bus


@pytest.fixture(autouse=True)
def the_real_portal_click_with_a_fake_bus(monkeypatch):
    monkeypatch.setattr(pointer, "portal_click", PORTAL_CLICK)


def test_a_point_on_a_scaled_screen_is_in_logical_pixels():
    streams = [Stream(node=40, x=0, y=0, width=1920, height=1080)]
    assert pointer.to_stream(1920, 1080, 3840, 2160, streams) == (streams[0], 960, 540)


def test_a_point_on_the_second_monitor_is_in_that_monitor():
    left, right = Stream(40, 0, 0, 1920, 1080), Stream(41, 1920, 0, 1280, 1024)
    assert pointer.to_stream(2000, 100, 3200, 1080, [left, right]) == (right, 80, 100)


@pytest.mark.parametrize("streams", [[], [Stream(40, 0, 0, 1920, 1080), Stream(41, 1920, 0, 1280, 1024)]])
def test_a_point_on_no_monitor_is_none(streams):
    # Below the shorter right-hand monitor, where there is no screen at all.
    assert pointer.to_stream(2000, 1060, 3200, 1080, streams) is None


class Bus:
    """The portal as the test wants it: answers each request by method, records every call."""

    def __init__(self, devices=2, refuse=None, restore="new-token"):
        self.calls, self.closed = [], []
        self.devices, self.refuse, self.restore = devices, refuse, restore

    def request(self, interface, method, signature, args, timeout=10):
        self.calls.append((interface, method, args))
        if method == self.refuse:
            raise PointerError("the remote desktop portal was refused")
        if method == "CreateSession":
            return {"session_handle": ("s", "/org/freedesktop/portal/desktop/session/1_1/rafiq")}
        if method == "Start":
            results = {"devices": ("u", self.devices),
                       "streams": ("a(ua{sv})", [(40, {"position": ("(ii)", (0, 0)), "size": ("(ii)", (1920, 1080))})])}
            if self.restore:
                results["restore_token"] = ("s", self.restore)
            return results
        return {}

    def call(self, interface, method, signature, args):
        self.calls.append((interface, method, args))

    def close_session(self, session):
        self.closed.append(session)

    def methods(self):
        return [method for _, method, _ in self.calls]


SESSION = "/org/freedesktop/portal/desktop/session/1_1/rafiq"


def test_the_portal_clicks_once_where_asked_and_closes_the_session():
    bus = Bus()
    pointer.portal_click(1920, 1080, 3840, 2160, bus=bus)
    assert bus.methods() == ["CreateSession", "SelectDevices", "SelectSources", "Start",
                             "NotifyPointerMotionAbsolute", "NotifyPointerButton", "NotifyPointerButton"]
    calls = {(m, i): a for i, (_, m, a) in enumerate(bus.calls)}
    assert calls[("NotifyPointerMotionAbsolute", 4)] == (SESSION, {}, 40, 960.0, 540.0)
    assert calls[("NotifyPointerButton", 5)] == (SESSION, {}, pointer.BTN_LEFT, 1)
    assert calls[("NotifyPointerButton", 6)] == (SESSION, {}, pointer.BTN_LEFT, 0)
    assert bus.closed == [SESSION]


def test_the_portal_asks_for_the_pointer_and_every_monitor_and_to_be_remembered():
    bus = Bus()
    pointer.portal_click(10, 10, 1920, 1080, bus=bus)
    devices = dict(bus.calls[1][2][1])
    sources = dict(bus.calls[2][2][1])
    assert devices["types"] == ("u", pointer.POINTER) and devices["persist_mode"] == ("u", 2)
    assert sources["types"] == ("u", 1) and sources["multiple"] == ("b", True)
    assert "restore_token" not in devices


def test_the_restore_token_is_kept_readable_only_by_the_user_and_used_next_time():
    pointer.portal_click(10, 10, 1920, 1080, bus=Bus(restore="first"))
    path = config.home() / "remote_desktop.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"restore_token": "first"}
    bus = Bus(restore="second")
    pointer.portal_click(10, 10, 1920, 1080, bus=bus)
    assert dict(bus.calls[1][2][1])["restore_token"] == ("s", "first")
    assert json.loads(path.read_text()) == {"restore_token": "second"}


def test_no_pointer_granted_means_no_click_and_the_session_is_closed():
    bus = Bus(devices=1)          # the keyboard only
    with pytest.raises(PointerError):
        pointer.portal_click(10, 10, 1920, 1080, bus=bus)
    assert "NotifyPointerButton" not in bus.methods() and bus.closed == [SESSION]


def test_a_refused_portal_dialog_means_no_click_and_the_session_is_closed():
    bus = Bus(refuse="Start")
    with pytest.raises(PointerError):
        pointer.portal_click(10, 10, 1920, 1080, bus=bus)
    assert "NotifyPointerButton" not in bus.methods() and bus.closed == [SESSION]


def test_a_point_on_no_monitor_is_not_clicked():
    bus = Bus()
    with pytest.raises(PointerError):
        pointer.portal_click(5000, 10, 1920, 1080, bus=bus)
    assert "NotifyPointerButton" not in bus.methods() and bus.closed == [SESSION]


# ---------------------------------------------------------------------- which way to click

def test_on_wayland_the_portal_clicks():
    seen = []
    pointer.click(100, 200, 1920, 1080, env={"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
                  portal=lambda *a, ready: seen.append(a), which=lambda p: "/usr/bin/" + p)
    assert seen == [(100, 200, 1920, 1080)]


def test_on_x11_xdotool_clicks_with_an_argument_list_and_a_timeout():
    runs = []

    def run(args, **kwargs):
        runs.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    pointer.click(100.4, 199.6, 1920, 1080, env={"DISPLAY": ":0"}, which=lambda p: "/usr/bin/" + p, run=run)
    args, kwargs = runs[0]
    assert args == ["xdotool", "mousemove", "--sync", "100", "200", "click", "1"]
    assert kwargs["timeout"] and not kwargs.get("shell")


def test_on_x11_a_failed_xdotool_is_an_error():
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "no display")

    with pytest.raises(PointerError):
        pointer.click(1, 1, 10, 10, env={"DISPLAY": ":0"}, which=lambda p: "/usr/bin/" + p, run=run)


@pytest.mark.parametrize("env", [{"DISPLAY": ":0"}, {}])
def test_without_a_way_to_click_it_says_what_to_install(env):
    with pytest.raises(PointerError, match="xdotool"):
        pointer.click(1, 1, 10, 10, env=env, which=lambda p: None)


def test_how_clicking_works_here_for_doctor():
    assert pointer.method({"WAYLAND_DISPLAY": "w"}, lambda p: None) == "portal"
    assert pointer.method({"DISPLAY": ":0"}, lambda p: "/usr/bin/xdotool") == "xdotool"
    assert pointer.method({"DISPLAY": ":0"}, lambda p: None) is None


def test_monitors_at_different_scales_are_not_clicked():
    # A 4K laptop at 200 % beside a 1080p monitor at 100 %: the screenshot is not the logical layout scaled.
    laptop, monitor = Stream(40, 0, 0, 1920, 1080), Stream(41, 1920, 0, 1920, 1080)
    assert pointer.to_stream(100, 100, 5760, 2160, [laptop, monitor]) is None


# ---------------------------------------------------------------------- after the code review

class ClosingBus(Bus):
    def __init__(self, created=None, **kwargs):
        super().__init__(**kwargs)
        self.created, self.bus_closed = created, False

    def request(self, interface, method, signature, args, timeout=10):
        if method == "CreateSession" and self.created is not None:
            self.calls.append((interface, method, args))
            if isinstance(self.created, Exception):
                raise self.created
            return self.created
        return super().request(interface, method, signature, args, timeout)

    def close(self):
        self.bus_closed = True


@pytest.mark.parametrize("created", [PointerError("no RemoteDesktop here"), {}, {"session_handle": "bad"}])
def test_a_session_that_never_opened_still_closes_the_bus(created):
    bus = ClosingBus(created=created)
    with pytest.raises(PointerError):
        pointer.portal_click(10, 10, 1920, 1080, bus=bus)
    assert bus.bus_closed and bus.closed == []


def test_the_screen_is_checked_after_gnome_asked_and_before_the_pointer_moves():
    bus = Bus()
    seen = []

    def ready():
        seen.append(bus.methods()[-1])        # what had happened by the time of the check
        return False

    with pytest.raises(pointer.NotReady):
        pointer.portal_click(10, 10, 1920, 1080, bus=bus, ready=ready)
    assert seen == ["Start"] and "NotifyPointerMotionAbsolute" not in bus.methods() and bus.closed == [SESSION]


def test_on_x11_the_screen_is_checked_before_xdotool_runs():
    runs = []
    with pytest.raises(pointer.NotReady):
        pointer.click(1, 1, 10, 10, env={"DISPLAY": ":0"}, which=lambda p: "/usr/bin/" + p,
                      run=lambda *a, **k: runs.append(a), ready=lambda: False)
    assert runs == []


def test_with_several_monitors_one_without_a_position_is_not_guessed():
    value = ("a(ua{sv})", [(40, {"position": ("(ii)", (0, 0)), "size": ("(ii)", (1920, 1080))}),
                           (41, {"size": ("(ii)", (1920, 1080))})])
    with pytest.raises(PointerError):
        pointer._streams(value)


def test_a_single_monitor_without_a_position_is_at_the_origin():
    assert pointer._streams(("a(ua{sv})", [(40, {"size": ("(ii)", (1920, 1080))})])) == [Stream(40, 0, 0, 1920, 1080)]
