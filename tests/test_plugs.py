import pytest

from rafiq import config
from rafiq.tools import REGISTRY, load_all, plugs


class FakeHttp:
    """Answers like the devices would, and remembers every request."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def get(self, url, params=None, headers=None):
        self.calls.append(("GET", url, params, headers))
        return self._answer(url, params)

    def post(self, url, json=None, headers=None):
        self.calls.append(("POST", url, json, headers))
        return self._answer(url, json)

    def _answer(self, url, data):
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer(data) if callable(answer) else answer


def test_shelly_switches_and_reads_power():
    http = FakeHttp({
        "http://192.168.1.40/rpc/Switch.Set": {"was_on": False},
        "http://192.168.1.40/rpc/Switch.GetStatus": {"output": True, "apower": 84.6, "aenergy": {"total": 12345.0}},
    })
    plug = plugs.make_plug({"name": "PC", "kind": "shelly", "host": "192.168.1.40"}, {}, http)
    plug.switch(True)
    assert http.calls[0][2] == {"id": 0, "on": "true"}
    reading = plug.read()
    assert reading == plugs.Reading(on=True, watts=84.6, kwh=pytest.approx(12.345))


def test_shelly_gen1_switches_and_reads_power():
    http = FakeHttp({
        "http://plug.lan/relay/0": {"ison": False},
        "http://plug.lan/status": {"relays": [{"ison": False}], "meters": [{"power": 0.0, "total": 120000}]},
    })
    plug = plugs.make_plug({"name": "fan", "kind": "shelly-gen1", "host": "http://plug.lan/"}, {}, http)
    plug.switch(False)
    assert http.calls[0][2] == {"turn": "off"}
    assert plug.read() == plugs.Reading(on=False, watts=0.0, kwh=pytest.approx(2.0))


def test_tasmota_switches_and_reads_power():
    def command(params):
        return {"POWER": "ON"} if params["cmnd"].startswith("Power") else \
            {"StatusSNS": {"ENERGY": {"Total": 3.21, "Power": 40}}}

    http = FakeHttp({"http://10.0.0.7/cm": command})
    plug = plugs.make_plug({"name": "lamp", "kind": "tasmota", "host": "10.0.0.7"}, {}, http)
    plug.switch(True)
    assert http.calls[0][2] == {"cmnd": "Power On"}
    assert plug.read() == plugs.Reading(on=True, watts=40.0, kwh=pytest.approx(3.21))


def test_home_assistant_switches_and_reads_power_with_the_token():
    base = "http://ha.local:8123/api"
    http = FakeHttp({
        f"{base}/services/homeassistant/turn_off": [],
        f"{base}/states/switch.heater": {"state": "on"},
        f"{base}/states/sensor.heater_power": {"state": "1520.5", "attributes": {"unit_of_measurement": "W"}},
    })
    ha = {"url": "http://ha.local:8123/", "token": "secret"}
    plug = plugs.make_plug({"name": "heater", "kind": "home-assistant", "entity": "switch.heater",
                            "power_entity": "sensor.heater_power"}, ha, http)
    plug.switch(False)
    method, url, body, headers = http.calls[0]
    assert (method, url, body) == ("POST", f"{base}/services/homeassistant/turn_off", {"entity_id": "switch.heater"})
    assert headers["Authorization"] == "Bearer secret"
    assert plug.read() == plugs.Reading(on=True, watts=1520.5, kwh=None)


def test_home_assistant_power_in_kilowatts_is_converted():
    base = "http://ha/api"
    http = FakeHttp({f"{base}/states/switch.pc": {"state": "on"},
                     f"{base}/states/sensor.pc_power": {"state": "0.25", "attributes": {"unit_of_measurement": "kW"}}})
    plug = plugs.make_plug({"name": "PC", "kind": "home-assistant", "entity": "switch.pc",
                            "power_entity": "sensor.pc_power"}, {"url": "http://ha", "token": "t"}, http)
    assert plug.read().watts == pytest.approx(250)


@pytest.mark.parametrize("entry, why", [
    ({"name": "x", "kind": "zigbee", "host": "a"}, "kind"),
    ({"name": "x", "kind": "shelly"}, "host"),
    ({"name": "x", "kind": "home-assistant", "entity": "switch.x"}, "url"),
    ({"name": "x", "kind": "home-assistant", "entity": "../config"}, "entity"),
    ({"kind": "shelly", "host": "a"}, "name"),
])
def test_bad_plug_settings_are_refused_clearly(entry, why):
    with pytest.raises(ValueError, match=why):
        plugs.make_plug(entry, {"url": "", "token": ""} if "url" in why else {"url": "http://ha", "token": "t"},
                        FakeHttp({}))


@pytest.fixture
def two_plugs(monkeypatch):
    http = FakeHttp({
        "http://pc-plug/rpc/Switch.Set": {"was_on": True},
        "http://pc-plug/rpc/Switch.GetStatus": {"output": True, "apower": 85.0, "aenergy": {"total": 4200.0}},
        "http://lamp/cm": lambda p: {"POWER": "OFF"} if p["cmnd"].startswith("Power") else {"StatusSNS": {}},
    })
    cfg = {"plugs": [{"name": "PC", "kind": "shelly", "host": "pc-plug", "ask_before_off": True},
                     {"name": "Desk lamp", "kind": "tasmota", "host": "lamp"}]}
    plugs.configure(cfg, http)
    return http


def test_plug_status_answers_how_much_the_pc_draws(two_plugs):
    assert plugs.plug_status("pc") == "PC: on, drawing 85 W, 4.2 kWh in total"
    assert plugs.plug_status("lamp") == "Desk lamp: off"


def test_plug_status_without_a_name_lists_every_plug(two_plugs):
    assert plugs.plug_status() == "PC: on, drawing 85 W, 4.2 kWh in total\nDesk lamp: off"


def test_unknown_plug_names_the_ones_there_are(two_plugs):
    assert plugs.plug_switch("kettle", True) == "error: no plug called 'kettle'; the plugs are PC, Desk lamp"


def test_plug_switch_reports_what_it_did(two_plugs):
    assert plugs.plug_switch("desk lamp", False) == "Desk lamp is off"
    assert two_plugs.calls[-1][2] == {"cmnd": "Power Off"}


def test_a_plug_that_does_not_answer_is_an_error_not_a_crash(two_plugs):
    two_plugs.answers["http://pc-plug/rpc/Switch.GetStatus"] = plugs.PlugError("timed out")
    assert plugs.plug_status("PC") == "PC: error: timed out"


def test_turning_off_a_guarded_plug_is_confirmed_first(two_plugs):
    tool = REGISTRY["plug_switch"]
    assert tool.action({"name": "pc", "on": False}) == "turn off the PC plug"
    assert tool.action({"name": "pc", "on": True}) is None
    assert tool.action({"name": "desk lamp", "on": False}) is None
    assert REGISTRY["plug_status"].action({}) is None


def test_plug_tools_load_only_when_plugs_are_set_up():
    tools = load_all({"plugs": [{"name": "PC", "kind": "shelly", "host": "pc-plug"}]})
    assert {"plug_switch", "plug_status"} <= set(tools)


def test_home_assistant_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("HASS_TOKEN", "from-env")
    assert config.load()["home_assistant"]["token"] == "from-env"


def test_a_plug_with_wrong_settings_is_listed_with_the_reason():
    plugs.configure({"plugs": [{"name": "Fridge", "kind": "shelly"}]}, FakeHttp({}))
    assert plugs.plug_status() == "Fridge: not set up: a plug needs a host"
