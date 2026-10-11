from datetime import UTC, datetime

import pytest

from rafiq.fallback import Fallback, HubWatch, magic_packet, wake_pc


def test_a_magic_packet_is_six_ff_then_the_address_sixteen_times():
    packet = magic_packet("AA:bb-CC:dd:EE:ff")
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("aabbccddeeff") * 16
    assert len(packet) == 102


@pytest.mark.parametrize("mac", ["", "aa:bb:cc", "zz:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff:00"])
def test_a_bad_address_is_refused(mac):
    with pytest.raises(ValueError):
        magic_packet(mac)


def test_waking_the_pc_broadcasts_the_packet():
    sent = []
    wake_pc("aa:bb:cc:dd:ee:ff", "192.168.1.255", 7, send=lambda data, addr: sent.append((data, addr)))
    assert sent == [(magic_packet("aa:bb:cc:dd:ee:ff"), ("192.168.1.255", 7))]


class Probe:
    def __init__(self):
        self.up = True

    def __call__(self):
        if isinstance(self.up, Exception):
            raise self.up
        return self.up


def test_the_hub_is_down_only_after_several_missed_probes_and_up_after_one_answer():
    probe, changes = Probe(), []
    watch = HubWatch(probe, misses=3, on_change=changes.append)
    assert watch.check() is True
    probe.up = False
    assert [watch.check(), watch.check()] == [True, True]    # one slow answer is not a dead PC
    assert watch.check() is False and changes == [False]
    probe.up = True
    assert watch.check() is True and changes == [False, True]


def test_a_probe_that_raises_counts_as_a_miss():
    probe = Probe()
    probe.up = OSError("no route to host")
    watch = HubWatch(probe, misses=1)
    assert watch.check() is False


def fallback(sent=None):
    sent = [] if sent is None else sent
    return Fallback(mac="aa:bb:cc:dd:ee:ff", send=lambda data, addr: sent.append(addr),
                    now=lambda: datetime(2026, 10, 10, 14, 5, tzinfo=UTC))


@pytest.mark.parametrize("text,tag", [
    ("wake up the computer", "en"),
    ("allume l'ordinateur", "fr"),
    ("enciende el ordenador", "es"),
    ("schalte den Computer ein", "de"),
    ("شغل الكمبيوتر", "ar-EG"),
])
def test_asking_to_start_the_pc_sends_wake_on_lan(text, tag):
    sent = []
    answer = fallback(sent).answer(text, tag)
    assert sent == [("255.255.255.255", 9)]
    assert answer


def test_without_an_address_it_says_it_cannot_start_the_pc():
    fb = Fallback(mac="", send=lambda d, a: pytest.fail("nothing to send"))
    assert "address" in fb.answer("turn on the pc", "en")


@pytest.mark.parametrize("text,tag,expected", [
    ("what time is it", "en", "14:05"),
    ("quelle heure est-il", "fr", "14:05"),
    ("كم الساعة", "ar", "14:05"),
    ("what's the date today", "en", "2026-10-10"),
])
def test_the_time_and_date_are_answered_without_the_pc(text, tag, expected):
    assert expected in fallback().answer(text, tag)


@pytest.mark.parametrize("tag", ["en", "fr", "es", "de", "ar", "ar-EG"])
def test_anything_else_says_the_main_computer_is_off_in_the_users_language(tag):
    answer = fallback().answer("play some jazz", tag)
    assert answer and answer == Fallback.PHRASES["off"][tag]


def test_an_unknown_language_falls_back_to_english():
    assert fallback().answer("play some jazz", "it") == Fallback.PHRASES["off"]["en"]


def test_the_wake_pc_command_sends_the_packet(monkeypatch):
    from rafiq import __main__ as cli
    from rafiq import fallback as fb

    sent = []
    monkeypatch.setattr(fb, "_broadcast", lambda data, addr: sent.append((data, addr)))
    assert cli.main(["wake-pc", "aa:bb:cc:dd:ee:ff", "--broadcast", "10.0.0.255"]) == 0
    assert sent == [(magic_packet("aa:bb:cc:dd:ee:ff"), ("10.0.0.255", 9))]
    assert cli.main(["wake-pc", "nope"]) == 1
