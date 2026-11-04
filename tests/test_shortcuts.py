"""Escape ends an operator run (#18): bound through the GlobalShortcuts portal only while the run goes."""

import threading

from rafiq import pointer, shortcuts

SESSION = "/org/freedesktop/portal/desktop/session/1_1/rafiq_keys"


class Bus:
    def __init__(self, refuse=False):
        self.calls, self.closed, self.refuse = [], [], refuse
        self.on_signal, self.listening = None, threading.Event()

    def request(self, interface, method, signature, args, timeout=10):
        self.calls.append((interface, method, args))
        if self.refuse and method == "BindShortcuts":
            raise pointer.PointerError("refused")
        if method == "CreateSession":
            return {"session_handle": ("s", SESSION)}
        return {"shortcuts": ("a(sa{sv})", [("stop", {})])}

    def listen(self, interface, member, on_signal, stop):
        self.on_signal = on_signal
        self.listening.set()
        stop.wait(2)

    def close_session(self, session):
        self.closed.append(session)

    def close(self):
        self.closed.append("bus")


def test_escape_is_bound_with_a_description_and_the_preferred_key():
    bus = Bus()
    watch = shortcuts.EscapeWatch(bus=bus)
    assert watch.start() is True
    interface, method, args = bus.calls[1]
    assert (interface, method) == ("GlobalShortcuts", "BindShortcuts")
    (shortcut_id, options), = args[1]
    assert shortcut_id == "stop" and options["preferred_trigger"] == ("s", "Escape")
    assert "Rafiq" in options["description"][1]
    watch.stop()


def test_pressing_escape_sets_pressed_and_stopping_unbinds_it():
    bus = Bus()
    watch = shortcuts.EscapeWatch(bus=bus)
    watch.start()
    assert bus.listening.wait(1)
    bus.on_signal((SESSION, "stop", 0, {}))
    assert watch.pressed.is_set()
    watch.stop()
    assert bus.closed == [SESSION]


def test_a_signal_for_another_session_or_shortcut_is_ignored():
    bus = Bus()
    watch = shortcuts.EscapeWatch(bus=bus)
    watch.start()
    assert bus.listening.wait(1)
    bus.on_signal(("/other/session", "stop", 0, {}))
    bus.on_signal((SESSION, "something-else", 0, {}))
    assert not watch.pressed.is_set()
    watch.stop()


def test_without_the_portal_the_run_goes_on_with_the_stop_button_only():
    bus = Bus(refuse=True)
    watch = shortcuts.EscapeWatch(bus=bus)
    assert watch.start() is False and not watch.pressed.is_set()
    assert bus.closed == [SESSION]
    watch.stop()                                  # nothing left to undo


def test_with_no_session_bus_at_all_it_says_so_and_does_not_fail():
    def no_bus():
        raise pointer.PointerError("no session bus")

    watch = shortcuts.EscapeWatch(bus_factory=no_bus)
    assert watch.start() is False
    watch.stop()


def test_left_unassigned_in_gnome_s_dialog_it_counts_as_not_bound():
    class Unassigned(Bus):
        def request(self, interface, method, signature, args, timeout=10):
            answer = super().request(interface, method, signature, args, timeout)
            return {"shortcuts": ("a(sa{sv})", [])} if method == "BindShortcuts" else answer

    bus = Unassigned()
    watch = shortcuts.EscapeWatch(bus=bus)
    assert watch.start() is False and bus.closed == [SESSION]
