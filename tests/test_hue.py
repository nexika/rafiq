import json

import pytest

from rafiq import config
from rafiq.__main__ import main
from rafiq.tools import hue, load_all

GROUPS = {"1": {"name": "Living room", "type": "Room"}, "2": {"name": "Kitchen", "type": "Room"},
          "3": {"name": "Chambre", "type": "Room"}, "4": {"name": "Downstairs", "type": "Zone"},
          "5": {"name": "Hue Sync", "type": "Entertainment"}}


class FakeBridge:
    """The Hue bridge's local API, as far as Rafiq uses it: rooms in, group actions out."""

    def __init__(self):
        self.sent = []

    def request(self, method, url, body=None):
        if method == "GET" and url.endswith("/groups"):
            return GROUPS
        if method == "PUT" and "/groups/" in url:
            self.sent.append((url.split("/groups/")[1].split("/")[0], body))
            return [{"success": {k: v}} for k, v in body.items()]
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def bridge():
    fake = FakeBridge()
    return fake, hue.Bridge("192.168.1.20", "secret-user", request=fake.request)


def test_rooms_are_rooms_and_zones_plus_all(bridge):
    _, b = bridge
    assert b.rooms() == {"Living room": "1", "Kitchen": "2", "Chambre": "3", "Downstairs": "4"}


@pytest.mark.parametrize("spoken, expected", [
    ("living room", "Living room"), ("the living room", "Living room"), ("LIVING", "Living room"),
    ("kitchen", "Kitchen"), ("kitchn", "Kitchen"), ("la chambre", "Chambre"), ("downstairs", "Downstairs"),
    ("garage", None),
])
def test_room_names_are_fuzzy_matched(spoken, expected):
    assert hue.match_room(spoken, ["Living room", "Kitchen", "Chambre", "Downstairs"]) == expected


def test_room_names_in_another_language_come_from_aliases():
    names = ["Living room", "Kitchen"]
    aliases = {"الصالة": "Living room", "cuisine": "Kitchen"}
    assert hue.match_room("الصاله", names, aliases) == "Living room"
    assert hue.match_room("la cuisine", names, aliases) == "Kitchen"


@pytest.mark.parametrize("color", ["blue", "bleu", "azul", "blau", "أزرق", "ازرق", "#0000ff"])
def test_colors_are_understood_in_every_language(color):
    state = hue.color_state(color)
    x, y = state["xy"]
    assert x < 0.2 and y < 0.1


def test_white_is_a_colour_temperature_and_unknown_colours_are_errors():
    assert "ct" in hue.color_state("warm white")
    with pytest.raises(ValueError):
        hue.color_state("plaid")


def test_turn_on_the_living_room(bridge):
    fake, b = bridge
    text = hue.lights("living room", on=True, bridge=b)
    assert fake.sent == [("1", {"on": True})]
    assert "Living room" in text and not text.startswith("error")


def test_kitchen_at_30_percent_turns_it_on_at_that_brightness(bridge):
    fake, b = bridge
    hue.lights("kitchen", brightness=30, bridge=b)
    assert fake.sent == [("2", {"on": True, "bri": 76})]


def test_zero_percent_turns_it_off(bridge):
    fake, b = bridge
    hue.lights("kitchen", brightness=0, bridge=b)
    assert fake.sent == [("2", {"on": False})]


def test_make_the_bedroom_blue(bridge):
    fake, b = bridge
    aliases = {"bedroom": "Chambre"}
    ctx = type("Ctx", (), {"cfg": {"hue": {"rooms": aliases}}})()
    hue.lights("bedroom", color="blue", ctx=ctx, bridge=b)
    group, body = fake.sent[0]
    assert group == "3" and body["on"] is True and "xy" in body


def test_every_light_is_group_zero(bridge):
    fake, b = bridge
    hue.lights("all", on=False, bridge=b)
    assert fake.sent == [("0", {"on": False})]


def test_an_unknown_room_lists_the_rooms_so_the_model_can_retry(bridge):
    fake, b = bridge
    text = hue.lights("garage", on=True, bridge=b)
    assert text.startswith("error") and "Living room" in text and "Kitchen" in text
    assert fake.sent == []


def test_nothing_to_change_is_an_error(bridge):
    _, b = bridge
    assert hue.lights("kitchen", bridge=b).startswith("error")


def test_bridge_errors_are_reported(bridge):
    _, b = bridge
    b.request = lambda method, url, body=None: (
        GROUPS if method == "GET" else [{"error": {"type": 201, "description": "parameter, bri, is not modifiable"}}])
    assert "not modifiable" in hue.lights("kitchen", brightness=50, bridge=b)


def test_lights_without_pairing_says_how_to_set_up():
    assert "rafiq hue-setup" in hue.lights("kitchen", on=True)


def test_pairing_waits_for_the_link_button_and_saves_the_user():
    answers = iter([[{"error": {"type": 101, "description": "link button not pressed"}}],
                    [{"success": {"username": "new-user"}}]])
    calls = []

    def request(method, url, body=None):
        calls.append((method, url, body))
        return next(answers)

    message = hue.setup("192.168.1.20", request=request, sleep=lambda s: None, say=lambda s: None)
    assert calls[0][:2] == ("POST", "http://192.168.1.20/api") and "devicetype" in calls[0][2]
    saved = json.loads((config.home() / "hue.json").read_text())
    assert saved == {"host": "192.168.1.20", "username": "new-user"}
    assert (config.home() / "hue.json").stat().st_mode & 0o077 == 0
    assert "192.168.1.20" in message
    assert hue.load_bridge().username == "new-user"


def test_pairing_gives_up_when_the_button_is_never_pressed():
    request = lambda method, url, body=None: [{"error": {"type": 101, "description": "link button not pressed"}}]  # noqa: E731
    message = hue.setup("192.168.1.20", request=request, sleep=lambda s: None, say=lambda s: None, tries=3)
    assert "button" in message
    assert not (config.home() / "hue.json").exists()


def test_setup_without_a_host_finds_the_bridge_on_the_network():
    def request(method, url, body=None):
        if "discovery" in url:
            return [{"id": "x", "internalipaddress": "192.168.1.33"}]
        return [{"success": {"username": "u"}}]

    hue.setup("", request=request, sleep=lambda s: None, say=lambda s: None)
    assert hue.load_bridge().host == "192.168.1.33"


def test_lights_load_only_when_hue_is_enabled():
    assert "lights" not in load_all({})
    tools = load_all({"hue": {"enabled": True}})
    assert "lights" in tools and tools["lights"].action({}) is None


def test_hue_setup_command(monkeypatch):
    seen = {}
    monkeypatch.setattr(hue, "setup", lambda host="", **kw: seen.setdefault("host", host) and "paired")
    assert main(["hue-setup", "--host", "10.0.0.2"]) == 0
    assert seen["host"] == "10.0.0.2"
