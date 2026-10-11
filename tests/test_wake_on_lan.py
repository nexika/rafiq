import pytest

from rafiq import config
from rafiq.tools import REGISTRY, load_all, pc


def ctx_with(machines, **wol):
    cfg = config.load()
    cfg["wake_on_lan"].update(wol, machines=machines)
    return type("Ctx", (), {"cfg": cfg})()


@pytest.mark.parametrize("mac", ["aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "aabbccddeeff"])
def test_magic_packet_is_six_ff_then_the_address_sixteen_times(mac):
    packet = pc.magic_packet(mac)
    assert len(packet) == 102
    assert packet == b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16


@pytest.mark.parametrize("mac", ["", "aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee:gg", "aa:bb:cc:dd:ee:ff:00"])
def test_a_bad_address_is_refused(mac):
    with pytest.raises(ValueError):
        pc.magic_packet(mac)


def test_find_machine_by_spoken_name():
    machines = {"Gaming PC": "aa:bb:cc:dd:ee:ff", "nas": "11:22:33:44:55:66"}
    assert pc.find_machine("the gaming pc", machines) == "Gaming PC"
    assert pc.find_machine("gaming", machines) == "Gaming PC"
    assert pc.find_machine("NAS", machines) == "nas"
    assert pc.find_machine("laptop", machines) is None
    assert pc.find_machine("", machines) is None


def test_wake_sends_the_packet_to_the_broadcast_address():
    sent = []
    ctx = ctx_with({"gaming pc": "aa:bb:cc:dd:ee:ff"})
    result = pc.wake_computer("gaming PC", ctx=ctx, send=lambda *a: sent.append(a))
    assert result == "sent the wake-up packet to gaming pc"
    assert sent == [(pc.magic_packet("aa:bb:cc:dd:ee:ff"), "255.255.255.255", 9)]


def test_a_machine_can_have_its_own_broadcast_address_and_port():
    sent = []
    ctx = ctx_with({"nas": {"mac": "11:22:33:44:55:66", "broadcast": "192.168.2.255", "port": 7}})
    pc.wake_computer("nas", ctx=ctx, send=lambda *a: sent.append(a))
    assert sent == [(pc.magic_packet("11:22:33:44:55:66"), "192.168.2.255", 7)]


def test_an_unknown_machine_names_the_known_ones():
    ctx = ctx_with({"gaming pc": "aa:bb:cc:dd:ee:ff", "nas": "11:22:33:44:55:66"})
    result = pc.wake_computer("laptop", ctx=ctx, send=lambda *a: pytest.fail("sent"))
    assert result.startswith("error") and "gaming pc" in result and "nas" in result


def test_no_machines_configured_is_an_error():
    result = pc.wake_computer("gaming pc", ctx=ctx_with({}), send=lambda *a: pytest.fail("sent"))
    assert result.startswith("error") and "wake_on_lan.machines" in result


def test_a_bad_address_in_the_config_is_an_error():
    result = pc.wake_computer("pc", ctx=ctx_with({"pc": "nope"}), send=lambda *a: pytest.fail("sent"))
    assert result.startswith("error") and "pc" in result


def test_a_network_error_is_reported():
    def send(*a):
        raise OSError("Network is unreachable")

    result = pc.wake_computer("pc", ctx=ctx_with({"pc": "aa:bb:cc:dd:ee:ff"}), send=send)
    assert result.startswith("error") and "unreachable" in result


def test_waking_a_computer_is_confirmed_first():
    load_all({})
    assert REGISTRY["wake_computer"].action({"name": "gaming pc"}) == "wake gaming pc"
