import json

import pytest

from rafiq import config, doctor
from rafiq.tools import dmx, load_all

KEY = {"name": "key", "address": 1, "channels": ["dimmer", "cct"], "kelvin": [2700, 6500]}
FILL = {"name": "fill", "address": 10, "channels": ["dimmer16", "cct16"], "kelvin": [3200, 5600]}


def cfg_with(*lights, host="192.168.1.50", universe=0):
    cfg = config.load()
    cfg["dmx"] = {"host": host, "universe": universe, "lights": list(lights)}
    return cfg


class Sent:
    def __init__(self, error=None):
        self.packets, self.error = [], error

    def __call__(self, host, packet):
        if self.error:
            raise self.error
        self.packets.append((host, packet))

    def frame(self):
        """The DMX data of the last packet: index 0 is channel 1."""
        return self.packets[-1][1][18:]


def set_light(cfg, send, **args):
    return dmx.apply(cfg, send=send, **args)


# ---------------------------------------------------------------- Art-Net


def test_an_artdmx_packet_follows_the_art_net_spec():
    packet = dmx.artdmx(universe=0x123, data=bytes([255, 128, 7]), sequence=5)
    assert packet[:8] == b"Art-Net\x00"
    assert packet[8:10] == b"\x00\x50"                  # OpDmx, little-endian
    assert packet[10:12] == b"\x00\x0e"                 # protocol version 14, big-endian
    assert packet[12] == 5 and packet[13] == 0           # sequence, physical
    assert packet[14:16] == b"\x23\x01"                 # SubUni then Net
    assert packet[16:18] == b"\x00\x04"                 # length, big-endian, padded to an even number
    assert packet[18:] == bytes([255, 128, 7, 0])


def test_a_packet_never_carries_more_than_512_channels():
    with pytest.raises(ValueError):
        dmx.artdmx(0, bytes(513))


# ---------------------------------------------------------------- settings


def test_lights_are_read_from_the_settings():
    found = dmx.lights(cfg_with(KEY, FILL))
    assert [light.name for light in found] == ["key", "fill"]
    assert found[1].footprint == 4


@pytest.mark.parametrize("light, why", [
    ({**KEY, "address": 0}, "address"),
    ({**KEY, "address": 512}, "512"),
    ({**KEY, "channels": ["dimmer", "rgb"]}, "rgb"),
    ({**KEY, "kelvin": [6500, 2700]}, "kelvin"),
    ({**KEY, "name": ""}, "name"),
    ({**KEY, "kelvin": 5600}, "kelvin"),
    ({**KEY, "kelvin": [2700, 4000, 6500]}, "kelvin"),
    ({**KEY, "kelvin": "2700-6500"}, "kelvin"),
    ({**KEY, "channels": 5}, "channels"),
    ({**KEY, "channels": [["dimmer"]]}, "channels"),
    ({**KEY, "address": [1]}, "address"),
    ({**KEY, "address": 1.5}, "address"),
    ({**KEY, "address": True}, "address"),
    ({**KEY, "name": "all"}, "all"),
    ("key", "light 1"),
])
def test_a_wrong_light_is_explained(light, why):
    with pytest.raises(ValueError, match=why):
        dmx.lights(cfg_with(light))


def test_two_lights_with_the_same_name_are_refused():
    with pytest.raises(ValueError, match="same name"):
        dmx.lights(cfg_with(KEY, {**FILL, "name": "The Key"}))


@pytest.mark.parametrize("universe", ["one", -1, 32768, 1.5])
def test_a_wrong_universe_is_explained(universe):
    with pytest.raises(ValueError, match="universe"):
        dmx.node(cfg_with(KEY, universe=universe))


def test_two_lights_on_the_same_channels_are_refused():
    with pytest.raises(ValueError, match="overlap"):
        dmx.lights(cfg_with(KEY, {**FILL, "address": 2}))


@pytest.mark.parametrize("host", ["8.8.8.8", "lights.example.com", ""])
def test_the_art_net_node_must_be_on_the_local_network(host):
    sent = Sent()
    said = set_light(cfg_with(KEY, host=host), sent, light="key", brightness=50)
    assert said.startswith("error:") and not sent.packets


# ---------------------------------------------------------------- the tool


def test_brightness_and_colour_temperature_set_the_lights_channels(rafiq_home):
    sent = Sent()
    said = set_light(cfg_with(KEY), sent, light="key", brightness=60, kelvin=5600)
    frame = sent.frame()
    assert sent.packets[-1][0] == "192.168.1.50"
    assert frame[0] == round(0.6 * 255)
    assert frame[1] == round((5600 - 2700) / (6500 - 2700) * 255)
    assert "key" in said and "60%" in said and "5600 K" in said


def test_sixteen_bit_channels_send_coarse_then_fine(rafiq_home):
    sent = Sent()
    set_light(cfg_with(FILL), sent, light="fill", brightness=50)
    frame = sent.frame()
    value = frame[9] * 256 + frame[10]
    assert value == round(0.5 * 65535)


def test_kelvin_outside_the_lights_range_is_kept_inside_it(rafiq_home):
    sent = Sent()
    said = set_light(cfg_with(KEY), sent, light="key", brightness=100, kelvin=10000)
    assert sent.frame()[1] == 255 and "6500 K" in said


def test_changing_one_light_keeps_the_others_as_they_were(rafiq_home):
    sent, cfg = Sent(), cfg_with(KEY, FILL)
    set_light(cfg, sent, light="fill", brightness=40)
    set_light(cfg, sent, light="key", brightness=80)
    frame = sent.frame()
    assert frame[0] == round(0.8 * 255)
    assert frame[9] * 256 + frame[10] == round(0.4 * 65535)


def test_off_then_on_comes_back_at_the_same_brightness(rafiq_home):
    sent, cfg = Sent(), cfg_with(KEY)
    set_light(cfg, sent, light="key", brightness=70)
    set_light(cfg, sent, light="key", on=False)
    assert sent.frame()[0] == 0
    set_light(cfg, sent, light="key", on=True)
    assert sent.frame()[0] == round(0.7 * 255)


def test_all_sets_every_light(rafiq_home):
    sent = Sent()
    set_light(cfg_with(KEY, FILL), sent, light="all", brightness=100)
    frame = sent.frame()
    assert frame[0] == 255 and frame[9] == 255 and frame[10] == 255


def test_the_light_is_found_whatever_the_case_or_article(rafiq_home):
    sent = Sent()
    assert not set_light(cfg_with(KEY), sent, light="The Key light", brightness=10).startswith("error:")


def test_an_unknown_light_lists_the_ones_there_are(rafiq_home):
    sent = Sent()
    said = set_light(cfg_with(KEY, FILL), sent, light="backlight", brightness=10)
    assert said.startswith("error:") and "key" in said and "fill" in said and not sent.packets


def test_a_network_error_is_reported_and_nothing_is_remembered(rafiq_home):
    said = set_light(cfg_with(KEY), Sent(error=OSError("unreachable")), light="key", brightness=60)
    assert said.startswith("error:")
    assert not (config.data_dir() / "dmx.json").exists()


def test_a_broken_state_file_starts_from_dark(rafiq_home):
    (config.data_dir() / "dmx.json").write_text("{not json", encoding="utf-8")
    sent = Sent()
    set_light(cfg_with(KEY), sent, light="key", on=True)
    assert sent.frame()[0] == 255                       # on with no brightness known: full


def test_the_state_is_kept_between_runs(rafiq_home):
    set_light(cfg_with(KEY), Sent(), light="key", brightness=30, kelvin=3200)
    kept = json.loads((config.data_dir() / "dmx.json").read_text(encoding="utf-8"))
    assert kept["key"]["brightness"] == 30 and kept["key"]["kelvin"] == 3200


def test_a_saved_kelvin_outside_a_changed_range_stays_inside_it(rafiq_home):
    set_light(cfg_with(KEY), Sent(), light="key", brightness=100, kelvin=6500)
    sent = Sent()
    said = set_light(cfg_with({**KEY, "kelvin": [3200, 5600]}, FILL), sent, light="fill", brightness=50)
    assert not said.startswith("error:") and sent.frame()[1] == 255


def test_a_light_named_with_light_in_it_can_be_chosen(rafiq_home):
    named = {**KEY, "name": "Key Light"}
    for said in ("key light", "Key Light", "the key"):
        assert not set_light(cfg_with(named), Sent(), light=said, brightness=10).startswith("error:"), said


def test_on_with_brightness_zero_stays_off(rafiq_home):
    sent = Sent()
    set_light(cfg_with(KEY), sent, light="key", on=True, brightness=0)
    assert sent.frame()[0] == 0


def test_off_with_a_brightness_stays_off_and_keeps_the_level_for_later(rafiq_home):
    sent, cfg = Sent(), cfg_with(KEY)
    set_light(cfg, sent, light="key", on=False, brightness=40)
    assert sent.frame()[0] == 0
    set_light(cfg, sent, light="key", on=True)
    assert sent.frame()[0] == round(0.4 * 255)


def test_brightness_zero_then_on_comes_back_at_the_earlier_level(rafiq_home):
    sent, cfg = Sent(), cfg_with(KEY)
    set_light(cfg, sent, light="key", brightness=70)
    set_light(cfg, sent, light="key", brightness=0)
    set_light(cfg, sent, light="key", on=True)
    assert sent.frame()[0] == round(0.7 * 255)


def test_a_light_never_set_stays_dark_and_none_channels_stay_zero(rafiq_home):
    sent = Sent()
    set_light(cfg_with({**KEY, "channels": ["dimmer", "none", "cct"]}, FILL), sent, light="key", brightness=100)
    frame = sent.frame()
    assert frame[0] == 255 and frame[1] == 0 and frame[9:11] == bytes(2)      # the fill's dimmer: dark


def test_the_universe_is_sent_in_the_packet(rafiq_home):
    sent = Sent()
    set_light(cfg_with(KEY, universe=300), sent, light="key", brightness=10)
    assert sent.packets[-1][1][14:16] == (300).to_bytes(2, "little")


def test_two_rooms_changing_lights_at_once_keep_both_changes(rafiq_home):
    import threading

    sent, cfg = Sent(), cfg_with(KEY, FILL)
    barrier = threading.Barrier(2)

    def slow_send(host, packet):
        try:
            barrier.wait(timeout=0.5)        # both inside at once, if nothing stops them
        except threading.BrokenBarrierError:
            pass
        sent(host, packet)

    threads = [threading.Thread(target=set_light, args=(cfg, slow_send), kwargs=kw)
               for kw in ({"light": "key", "brightness": 80}, {"light": "fill", "brightness": 40})]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    kept = json.loads((config.data_dir() / "dmx.json").read_text(encoding="utf-8"))
    assert kept["key"]["brightness"] == 80 and kept["fill"]["brightness"] == 40
    frame = sent.frame()
    assert frame[0] == round(0.8 * 255) and frame[9] * 256 + frame[10] == round(0.4 * 65535)


# ---------------------------------------------------------------- loading and doctor


def test_the_tool_loads_only_with_lights_set():
    assert "video_light" not in load_all({})
    tool = load_all({"dmx": {"host": "192.168.1.50", "lights": [KEY]}})["video_light"]
    assert tool.level == "act" and tool.confirm is None and not tool.expose


def test_the_tool_takes_only_what_its_schema_declares():
    import inspect

    tool = load_all({"dmx": {"host": "192.168.1.50", "lights": [KEY]}})["video_light"]
    assert set(inspect.signature(tool.fn).parameters) == set(tool.params) | {"ctx"}


def dmx_check(cfg):
    return {c.label: c for c in doctor.integrations(cfg, doctor.Probe())}["Video lights (DMX)"]


def test_doctor_without_lights_is_optional():
    assert dmx_check(config.load()).optional


def test_doctor_reports_a_wrong_light_setting():
    check = dmx_check(cfg_with({**KEY, "channels": ["rgb"]}))
    assert not check.ok and "rgb" in check.detail


@pytest.mark.parametrize("lights", [["key"], [{**KEY, "kelvin": 5600}]])
def test_doctor_reports_a_mistyped_setting_instead_of_crashing(lights):
    cfg = config.load()
    cfg["dmx"] = {"host": "192.168.1.50", "universe": 0, "lights": lights}
    assert not dmx_check(cfg).ok


def test_doctor_counts_the_lights():
    check = dmx_check(cfg_with(KEY, FILL))
    assert check.ok and "2 lights" in check.detail
