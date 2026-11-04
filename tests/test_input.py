"""The operator's input layer (#18): one RemoteDesktop session for a whole run on Wayland, xdotool on X11."""

import subprocess

import pytest

from rafiq import pointer
from rafiq.pointer import PointerError

PORTAL_REMOTE = pointer.PortalRemote      # as defined: conftest.py keeps the real portal out of reach
SESSION = "/org/freedesktop/portal/desktop/session/1_1/rafiq"


class Bus:
    """The portal: answers requests by method and records every call."""

    def __init__(self, devices=3):
        self.calls, self.closed, self.devices = [], [], devices

    def request(self, interface, method, signature, args, timeout=10):
        self.calls.append((method, args))
        if method == "CreateSession":
            return {"session_handle": ("s", SESSION)}
        if method == "Start":
            return {"devices": ("u", self.devices),
                    "streams": ("a(ua{sv})", [(40, {"position": ("(ii)", (0, 0)), "size": ("(ii)", (1920, 1080))})])}
        return {}

    def call(self, interface, method, signature, args):
        self.calls.append((method, args))

    def close_session(self, session):
        self.closed.append(session)

    def close(self):
        self.closed.append("bus")

    def sent(self, method):
        return [args for m, args in self.calls if m == method]


def remote(bus, width=1920, height=1080):
    return PORTAL_REMOTE(width, height, bus=bus)


# ---------------------------------------------------------------------- keysyms

@pytest.mark.parametrize("combo, syms", [
    ("Return", [0xFF0D]), ("ctrl+s", [0xFFE3, ord("s")]), ("ctrl+shift+Tab", [0xFFE3, 0xFFE1, 0xFF09]),
    ("CTRL+L", [0xFFE3, ord("l")]), ("alt+F4", [0xFFE9, 0xFFC1]), ("Escape", [0xFF1B]), ("super", [0xFFEB]),
    ("ArrowDown", [0xFF54]), ("space", [0x20]), ("BackSpace", [0xFF08]),
])
def test_key_combos_become_keysyms_modifiers_first(combo, syms):
    assert pointer.combo_keysyms(combo) == syms


@pytest.mark.parametrize("combo", ["", "ctrl+", "notakey", "ctrl+shift+notakey"])
def test_an_unknown_key_is_an_error(combo):
    with pytest.raises(PointerError):
        pointer.combo_keysyms(combo)


@pytest.mark.parametrize("char, sym", [("a", ord("a")), ("Z", ord("Z")), ("é", 0xE9), ("م", 0x01000000 + 0x0645),
                                       ("\n", 0xFF0D), ("\t", 0xFF09)])
def test_each_character_has_a_keysym_unicode_included(char, sym):
    assert pointer.char_keysym(char) == sym


# ---------------------------------------------------------------------- the portal session

def test_one_session_asks_for_the_pointer_and_the_keyboard_once():
    bus = Bus()
    with remote(bus):
        pass
    devices = dict(bus.sent("SelectDevices")[0][1])
    assert devices["types"] == ("u", pointer.POINTER | pointer.KEYBOARD)
    assert [m for m, _ in bus.calls].count("Start") == 1 and bus.closed == [SESSION]


def test_a_click_moves_then_presses_and_releases_the_button():
    bus = Bus()
    with remote(bus) as r:
        r.click(960, 540, button="right")
    assert bus.sent("NotifyPointerMotionAbsolute") == [(SESSION, {}, 40, 960.0, 540.0)]
    assert bus.sent("NotifyPointerButton") == [(SESSION, {}, 273, 1), (SESSION, {}, 273, 0)]


def test_a_double_click_with_shift_holds_shift_around_both_clicks():
    bus = Bus()
    with remote(bus) as r:
        r.click(10, 10, count=2, mods=("shift",))
    keys = bus.sent("NotifyKeyboardKeysym")
    assert keys[0] == (SESSION, {}, 0xFFE1, 1) and keys[-1] == (SESSION, {}, 0xFFE1, 0)
    assert len(bus.sent("NotifyPointerButton")) == 4


def test_a_key_combo_presses_in_order_and_releases_in_reverse():
    bus = Bus()
    with remote(bus) as r:
        r.key("ctrl+s")
    assert bus.sent("NotifyKeyboardKeysym") == [(SESSION, {}, 0xFFE3, 1), (SESSION, {}, ord("s"), 1),
                                                (SESSION, {}, ord("s"), 0), (SESSION, {}, 0xFFE3, 0)]


def test_typing_sends_one_keysym_per_character_arabic_included():
    bus = Bus()
    with remote(bus) as r:
        r.type("hi م")
    pressed = [args[2] for args in bus.sent("NotifyKeyboardKeysym") if args[3] == 1]
    assert pressed == [ord("h"), ord("i"), 0x20, 0x01000645]


def test_scrolling_sends_discrete_steps_at_the_point():
    bus = Bus()
    with remote(bus) as r:
        r.scroll(100, 200, dx=0, dy=3)
    assert bus.sent("NotifyPointerMotionAbsolute")[-1][3:] == (100.0, 200.0)
    assert bus.sent("NotifyPointerAxisDiscrete") == [(SESSION, {}, 0, 3)]


def test_a_drag_holds_the_button_along_the_path():
    bus = Bus()
    with remote(bus) as r:
        r.drag([(10, 10), (50, 60), (90, 100)])
    assert [a[3:] for a in bus.sent("NotifyPointerMotionAbsolute")] == [(10.0, 10.0), (50.0, 60.0), (90.0, 100.0)]
    assert bus.sent("NotifyPointerButton") == [(SESSION, {}, 272, 1), (SESSION, {}, 272, 0)]


def test_a_scaled_screen_maps_points_to_the_stream():
    bus = Bus()
    with remote(bus, width=3840, height=2160) as r:
        r.move(1920, 1080)
    assert bus.sent("NotifyPointerMotionAbsolute") == [(SESSION, {}, 40, 960.0, 540.0)]


def test_without_the_keyboard_the_run_does_not_start_and_the_session_closes():
    bus = Bus(devices=pointer.POINTER)
    with pytest.raises(PointerError):
        remote(bus).__enter__()
    assert bus.closed == [SESSION]


def test_a_point_on_no_monitor_is_refused():
    with remote(Bus()) as r, pytest.raises(PointerError):
        r.click(5000, 10)


# ---------------------------------------------------------------------- X11

class Runs:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        return subprocess.CompletedProcess(args, self.code, "", "")


def test_x11_clicks_keys_types_and_scrolls_with_xdotool_argument_lists():
    runs = Runs()
    r = pointer.XdoRemote(run=runs)
    r.click(10.4, 20.6, button="right", count=2)
    r.key("ctrl+s")
    r.type("rm -rf; hi")
    r.scroll(5, 5, dx=0, dy=-2)
    args = [a for a, _ in runs.calls]
    assert args[0] == ["xdotool", "mousemove", "--sync", "10", "21", "click", "--repeat", "2", "3"]
    assert args[1] == ["xdotool", "key", "--clearmodifiers", "ctrl+s"]
    assert args[2] == ["xdotool", "type", "--clearmodifiers", "--", "rm -rf; hi"]
    assert args[3] == ["xdotool", "mousemove", "--sync", "5", "5", "click", "--repeat", "2", "4"]
    assert all(k["timeout"] and not k.get("shell") for _, k in runs.calls)


def test_x11_failure_is_an_error():
    with pytest.raises(PointerError):
        pointer.XdoRemote(run=Runs(code=1)).key("Return")


def test_the_remote_is_picked_by_session(monkeypatch):
    monkeypatch.setattr(pointer, "PortalRemote", lambda w, h: ("portal", w, h))
    assert pointer.open_remote(100, 50, env={"WAYLAND_DISPLAY": "w"}, which=lambda p: None) == ("portal", 100, 50)
    assert isinstance(pointer.open_remote(100, 50, env={"DISPLAY": ":0"}, which=lambda p: "/usr/bin/" + p),
                      pointer.XdoRemote)
    with pytest.raises(PointerError):
        pointer.open_remote(100, 50, env={}, which=lambda p: None)


# ---------------------------------------------------------------------- after the reviews

def test_text_the_layout_may_lack_is_pasted_through_the_clipboard():
    bus, copied = Bus(), []
    with PORTAL_REMOTE(1920, 1080, bus=bus, copy=lambda t: copied.append(t) or True) as r:
        note = r.type("مرحبا")
    assert copied == ["مرحبا"] and "pasted" in note
    assert [a[2] for a in bus.sent("NotifyKeyboardKeysym") if a[3] == 1] == [0xFFE3, ord("v")]


def test_without_a_clipboard_tool_the_model_is_told_to_check_the_screen():
    with remote(Bus()) as r:
        assert "check the screen" in r.type("مرحبا")


def test_the_operator_keeps_its_own_permission_from_gnome():
    import json

    from rafiq import config

    class Granting(Bus):
        def request(self, interface, method, signature, args, timeout=10):
            answer = super().request(interface, method, signature, args, timeout)
            if method == "Start":
                answer["restore_token"] = ("s", "keyboard-too")
            return answer

    pointer.save_token("pointer-only")
    bus = Granting()
    with remote(bus):
        pass
    assert "restore_token" not in dict(bus.sent("SelectDevices")[0][1])     # the click's token isn't reused
    assert json.loads((config.home() / pointer.OPERATOR_TOKEN).read_text()) == {"restore_token": "keyboard-too"}
    assert pointer.load_token() == "pointer-only"


def test_x11_an_unknown_modifier_presses_nothing():
    runs = Runs()
    with pytest.raises(PointerError):
        pointer.XdoRemote(run=runs).click(1, 1, mods=("ctrl", "fn"))
    assert runs.calls == []


def test_x11_a_plus_key():
    runs = Runs()
    pointer.XdoRemote(run=runs).key("+")
    assert runs.calls[0][0][-1] == "plus"
