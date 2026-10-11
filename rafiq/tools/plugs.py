"""Smart plugs and energy meters: turn a plug on or off, and say how much power it draws.

Plugs are listed in the config under [[plugs]], each with a name the user says and how to reach it:
Shelly (Gen2 and later, or Gen1) and Tasmota over their local HTTP API, no cloud needed, or any
switch Home Assistant knows. The model only ever picks a plug by its name: it never chooses an
address, so it cannot point Rafiq at another device on the network.

A plug marked ask_before_off (the one the computer is on, a freezer) is confirmed out loud before it
is turned off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from . import tool

KINDS = ("shelly", "shelly-gen1", "tasmota", "home-assistant")
ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


class PlugError(Exception):
    """A plug that did not answer, or answered something we cannot read."""


class Http(Protocol):
    def get(self, url: str, params: dict | None = None, headers: dict | None = None): ...
    def post(self, url: str, json: dict | None = None, headers: dict | None = None): ...


class HttpxClient:
    """The real network, with a short timeout: a plug on the local network answers fast or not at all."""

    def __init__(self, timeout: float = 5):
        import httpx

        self.client = httpx.Client(timeout=timeout, follow_redirects=False)

    def get(self, url, params=None, headers=None):
        return self._send("GET", url, params=params, headers=headers)

    def post(self, url, json=None, headers=None):
        return self._send("POST", url, json=json, headers=headers)

    def _send(self, method, url, **kwargs):
        import httpx

        try:
            resp = self.client.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise PlugError(f"no answer ({type(exc).__name__})") from exc
        except ValueError as exc:
            raise PlugError("the answer was not JSON") from exc


@dataclass
class Reading:
    on: bool | None
    watts: float | None = None
    kwh: float | None = None       # energy used in total, as the plug counts it


def _base(host: str) -> str:
    host = host.strip().rstrip("/")
    return host if host.startswith(("http://", "https://")) else f"http://{host}"


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Plug:
    def __init__(self, name: str, http: Http, ask_before_off: bool = False):
        self.name, self.http, self.ask_before_off = name, http, ask_before_off

    def switch(self, on: bool) -> None:
        raise NotImplementedError

    def read(self) -> Reading:
        raise NotImplementedError


class Shelly(Plug):
    """Shelly Gen2 and later (Plus, Pro, Gen3): the RPC API, switch 0."""

    def __init__(self, name, http, host, ask_before_off=False):
        super().__init__(name, http, ask_before_off)
        self.base = _base(host)

    def switch(self, on):
        self.http.get(f"{self.base}/rpc/Switch.Set", params={"id": 0, "on": "true" if on else "false"})

    def read(self):
        data = self.http.get(f"{self.base}/rpc/Switch.GetStatus", params={"id": 0})
        total = _number((data.get("aenergy") or {}).get("total"))      # Wh
        return Reading(data.get("output"), _number(data.get("apower")), total / 1000 if total is not None else None)


class ShellyGen1(Plug):
    """Shelly Gen1 (Plug S, 1PM): the /relay and /status API, relay 0."""

    def __init__(self, name, http, host, ask_before_off=False):
        super().__init__(name, http, ask_before_off)
        self.base = _base(host)

    def switch(self, on):
        self.http.get(f"{self.base}/relay/0", params={"turn": "on" if on else "off"})

    def read(self):
        data = self.http.get(f"{self.base}/status")
        relay = (data.get("relays") or [{}])[0]
        meter = (data.get("meters") or [{}])[0]
        total = _number(meter.get("total"))                             # watt-minutes
        return Reading(relay.get("ison"), _number(meter.get("power")), total / 60000 if total is not None else None)


class Tasmota(Plug):
    """Tasmota: commands over /cm; the energy readings of a plug that has a meter."""

    def __init__(self, name, http, host, ask_before_off=False):
        super().__init__(name, http, ask_before_off)
        self.base = _base(host)

    def _command(self, command: str) -> dict:
        return self.http.get(f"{self.base}/cm", params={"cmnd": command})

    def switch(self, on):
        self._command("Power On" if on else "Power Off")

    def read(self):
        power = str(self._command("Power").get("POWER", "")).upper()
        energy = (self._command("Status 8").get("StatusSNS") or {}).get("ENERGY") or {}
        on = {"ON": True, "OFF": False}.get(power)
        return Reading(on, _number(energy.get("Power")), _number(energy.get("Total")))


class HomeAssistant(Plug):
    """Any switch Home Assistant knows, with an optional power sensor (W or kW) and energy sensor (kWh)."""

    def __init__(self, name, http, ha: dict, entity, power_entity="", energy_entity="", ask_before_off=False):
        super().__init__(name, http, ask_before_off)
        self.api = _base(ha["url"]) + "/api"
        self.headers = {"Authorization": f"Bearer {ha['token']}"}
        self.entity, self.power_entity, self.energy_entity = entity, power_entity, energy_entity

    def switch(self, on):
        self.http.post(f"{self.api}/services/homeassistant/turn_{'on' if on else 'off'}",
                       json={"entity_id": self.entity}, headers=self.headers)

    def _state(self, entity: str) -> dict:
        return self.http.get(f"{self.api}/states/{entity}", headers=self.headers)

    def read(self):
        on = {"on": True, "off": False}.get(self._state(self.entity).get("state"))
        watts = kwh = None
        if self.power_entity:
            state = self._state(self.power_entity)
            watts = _number(state.get("state"))
            unit = (state.get("attributes") or {}).get("unit_of_measurement", "W")
            if watts is not None and unit == "kW":
                watts *= 1000
        if self.energy_entity:
            kwh = _number(self._state(self.energy_entity).get("state"))
        return Reading(on, watts, kwh)


def make_plug(entry: dict, ha: dict, http: Http) -> Plug:
    """A plug from its [[plugs]] settings; ValueError says what is wrong with them."""
    name = str(entry.get("name", "")).strip()
    if not name:
        raise ValueError("a plug needs a name")
    kind, guard = entry.get("kind", ""), bool(entry.get("ask_before_off", False))
    if kind not in KINDS:
        raise ValueError(f"kind is one of {', '.join(KINDS)}")
    if kind == "home-assistant":
        entities = [entry.get(k, "") for k in ("entity", "power_entity", "energy_entity")]
        if not ENTITY.match(entities[0]) or any(e and not ENTITY.match(e) for e in entities[1:]):
            raise ValueError("entity must look like switch.heater")
        if not ha.get("url") or not ha.get("token"):
            raise ValueError("Home Assistant needs a url and a token under [home_assistant]")
        return HomeAssistant(name, http, ha, *entities, ask_before_off=guard)
    if not entry.get("host"):
        raise ValueError("a plug needs a host")
    cls = {"shelly": Shelly, "shelly-gen1": ShellyGen1, "tasmota": Tasmota}[kind]
    return cls(name, http, str(entry["host"]), ask_before_off=guard)


# The plugs from the settings, in their order, and the ones whose settings are wrong (name -> why).
PLUGS: list[Plug] = []
BROKEN: dict[str, str] = {}


def configure(cfg: dict, http: Http | None = None) -> None:
    http = http or HttpxClient()
    PLUGS.clear()
    BROKEN.clear()
    for i, entry in enumerate(cfg.get("plugs") or []):
        try:
            PLUGS.append(make_plug(entry, cfg.get("home_assistant") or {}, http))
        except ValueError as exc:
            BROKEN[str(entry.get("name") or f"plug {i + 1}")] = str(exc)


def find_plug(name: str) -> Plug | None:
    """The plug with that name, else the only one whose name contains it."""
    q = name.casefold().strip()
    for plug in PLUGS:
        if plug.name.casefold() == q:
            return plug
    matches = [p for p in PLUGS if q and q in p.name.casefold()]
    return matches[0] if len(matches) == 1 else None


def _unknown(name: str) -> str:
    return f"error: no plug called {name!r}; the plugs are {', '.join(p.name for p in PLUGS) or 'none'}"


def describe(plug: Plug) -> str:
    try:
        reading = plug.read()
    except PlugError as exc:
        return f"{plug.name}: error: {exc}"
    parts = [{True: "on", False: "off", None: "state unknown"}[reading.on]]
    if reading.watts is not None:
        parts.append(f"drawing {reading.watts:.0f} W")
    if reading.kwh is not None:
        parts.append(f"{reading.kwh:.1f} kWh in total")
    return f"{plug.name}: {', '.join(parts)}"


def _confirm_off(args: dict) -> str | None:
    plug = find_plug(str(args.get("name", "")))
    if plug and plug.ask_before_off and not args.get("on", True):
        return f"turn off the {plug.name} plug"
    return None


@tool("plug_switch", "Turn a smart plug on or off, by its name.",
      {"name": {"type": "string", "description": "the plug's name, e.g. 'PC' or 'desk lamp'"},
       "on": {"type": "boolean", "description": "true to turn it on, false to turn it off"}},
      required=("name", "on"), confirm=_confirm_off, level="critical")
def plug_switch(name: str, on: bool) -> str:
    plug = find_plug(name)
    if not plug:
        return _unknown(name)
    try:
        plug.switch(bool(on))
    except PlugError as exc:
        return f"error: {plug.name}: {exc}"
    return f"{plug.name} is {'on' if on else 'off'}"


@tool("plug_status", "Whether smart plugs are on and how much power they draw now (watts) and have used (kWh).",
      {"name": {"type": "string", "description": "one plug's name; empty for every plug"}}, level="private")
def plug_status(name: str = "") -> str:
    if name:
        plug = find_plug(name)
        return describe(plug) if plug else _unknown(name)
    lines = [describe(p) for p in PLUGS] + [f"{n}: not set up: {why}" for n, why in BROKEN.items()]
    return "\n".join(lines) or "no plugs are set up"
