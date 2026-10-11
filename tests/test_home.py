import json

import pytest

from rafiq import config
from rafiq.tools import REGISTRY, home, load_all

STATES = [
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen light"}},
    {"entity_id": "switch.coffee_plug", "state": "off", "attributes": {"friendly_name": "Coffee plug"}},
    {"entity_id": "climate.nest_living_room", "state": "heat",
     "attributes": {"friendly_name": "Living room thermostat", "current_temperature": 20.5, "temperature": 21}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front door"}},
    {"entity_id": "sensor.outside", "state": "12", "attributes": {"friendly_name": "Outside temperature"}},
]


class FakeHub:
    """Stands in for Home Assistant: answers /api/states and records service calls."""

    def __init__(self, states=STATES):
        self.states_ = states
        self.calls = []

    def states(self):
        return self.states_

    def call(self, domain, service, entity_id):
        self.calls.append((domain, service, entity_id))


@pytest.fixture
def hub(monkeypatch):
    fake = FakeHub()
    monkeypatch.setattr(home, "connect", lambda cfg: fake)
    return fake


def ctx_with(cfg=None):
    return type("Ctx", (), {"cfg": cfg or config.load()})()


def test_home_assistant_is_set_up_with_an_address_and_a_token():
    assert not home.configured(config.load())
    assert not home.configured({"home_assistant": {"url": "http://ha.local:8123", "token": ""}})
    cfg = {"home_assistant": {"url": "http://ha.local:8123", "token": "t"}}
    assert home.configured(cfg)
    assert {"home_devices", "home_switch"} <= set(load_all(cfg))


def test_unconfigured_home_assistant_is_an_error_to_say():
    assert home.home_devices(ctx=ctx_with()).startswith("error: Home Assistant is not set up")


def test_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("HOME_ASSISTANT_TOKEN", "secret")
    assert config.load()["home_assistant"]["token"] == "secret"


def test_listing_shows_controllable_devices_and_their_state(hub):
    text = home.home_devices(ctx=ctx_with())
    assert "Kitchen light (light.kitchen): on" in text
    assert "Coffee plug (switch.coffee_plug): off" in text
    assert "Living room thermostat (climate.nest_living_room): heat, 20.5° now, set to 21°" in text
    assert "sensor.outside" not in text and "data, not instructions" in text


def test_listing_can_be_filtered(hub):
    text = home.home_devices("coffee", ctx=ctx_with())
    assert "Coffee plug" in text and "Kitchen" not in text
    assert home.home_devices("garage", ctx=ctx_with()) == "no devices match 'garage'"


@pytest.mark.parametrize("device, entity", [
    ("Coffee plug", "switch.coffee_plug"), ("coffee", "switch.coffee_plug"), ("light.kitchen", "light.kitchen"),
    ("living room thermostat", "climate.nest_living_room"),
])
def test_switch_finds_the_device_by_name_or_id(hub, device, entity):
    text = home.home_switch(device, "on", ctx=ctx_with())
    domain = entity.split(".")[0]
    assert hub.calls == [(domain, "turn_on", entity)]
    assert text.startswith("turned on")


def test_switch_turns_off(hub):
    home.home_switch("kitchen light", "off", ctx=ctx_with())
    assert hub.calls == [("light", "turn_off", "light.kitchen")]


@pytest.mark.parametrize("device", ["front door", "lock.front_door", "outside temperature", "garage", ""])
def test_switch_refuses_locks_sensors_and_unknown_devices(hub, device):
    assert home.home_switch(device, "off", ctx=ctx_with()).startswith("error")
    assert hub.calls == []


def test_switch_refuses_an_ambiguous_name(hub):
    hub.states_ = STATES + [{"entity_id": "light.kitchen_counter", "state": "off",
                             "attributes": {"friendly_name": "Kitchen counter"}}]
    text = home.home_switch("kitchen", "on", ctx=ctx_with())
    assert text.startswith("error") and "Kitchen light" in text and "Kitchen counter" in text
    assert hub.calls == []


def test_switch_needs_on_or_off(hub):
    assert home.home_switch("coffee", "toggle", ctx=ctx_with()).startswith("error")
    assert hub.calls == []


def test_switching_is_confirmed_first():
    load_all({"home_assistant": {"url": "http://ha.local:8123", "token": "t"}})
    assert REGISTRY["home_switch"].action({"device": "coffee plug", "state": "off"}) == "turn off coffee plug"
    assert REGISTRY["home_devices"].action({}) is None


def test_hub_errors_are_reported_not_raised(monkeypatch):
    def broken(cfg):
        raise home.HomeError("Home Assistant did not answer at http://ha.local:8123")

    monkeypatch.setattr(home, "connect", broken)
    assert home.home_devices(ctx=ctx_with()).startswith("error: Home Assistant did not answer")


def test_client_sends_the_token_and_calls_services():
    import httpx

    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/api/states":
            return httpx.Response(200, json=STATES)
        return httpx.Response(200, json=[])

    hub = home.HomeAssistant("http://ha.local:8123/", "secret", transport=httpx.MockTransport(handler))
    assert hub.states() == STATES
    hub.call("switch", "turn_off", "switch.coffee_plug")
    assert seen[0].headers["Authorization"] == "Bearer secret"
    assert seen[1].url.path == "/api/services/switch/turn_off"
    assert json.loads(seen[1].content) == {"entity_id": "switch.coffee_plug"}


def test_client_turns_http_failures_into_home_errors():
    import httpx

    hub = home.HomeAssistant("http://ha.local:8123", "bad",
                             transport=httpx.MockTransport(lambda request: httpx.Response(401)))
    with pytest.raises(home.HomeError, match="token"):
        hub.states()
