"""Time, timers and reminders, weather, calculations and unit conversion, and Rafiq's mode."""

from __future__ import annotations

import ast
import math
import operator
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import location, modes
from . import tool


def now() -> datetime:
    return datetime.now().astimezone()


@tool("get_time", "The current date and time, here or in another time zone.",
      {"timezone": {"type": "string", "description": "IANA time zone such as Europe/Paris or Africa/Cairo; "
                                                     "empty for the user's own"}}, level="safe", expose=True)
def get_time(timezone: str = "") -> str:
    when = now()
    if timezone:
        try:
            when = when.astimezone(ZoneInfo(timezone))
        except (ZoneInfoNotFoundError, ValueError):
            return f"error: unknown time zone {timezone!r}"
    return when.strftime("%A %d %B %Y, %H:%M (%Z, UTC%z)")


# ---------------------------------------------------------------------- timers and reminders

def _describe(alarm, current: datetime) -> str:
    due = datetime.fromtimestamp(alarm.due).astimezone()
    left = max(0, int((due - current).total_seconds()))
    return f"{alarm.kind} {alarm.id} '{alarm.label}' at {due:%a %H:%M} (in {left // 60} min {left % 60} s)"


@tool("set_timer", "Start a countdown timer.",
      {"seconds": {"type": "integer", "description": "length of the timer in seconds"},
       "label": {"type": "string", "description": "what it is for, in the user's language"}},
      required=("seconds",), level="act")
def set_timer(seconds: int, label: str = "", ctx=None) -> str:
    seconds = int(seconds)
    if not 1 <= seconds <= 7 * 24 * 3600:
        return "error: a timer is between 1 second and 7 days"
    alarm = ctx.scheduler.add(now().timestamp() + seconds, label or "timer", "timer", ctx.tag)
    return "set: " + _describe(alarm, now())


@tool("set_reminder", "Remind the user of something at a date and time.",
      {"when": {"type": "string", "description": "local date and time, ISO format YYYY-MM-DDTHH:MM"},
       "label": {"type": "string", "description": "what to remind them of, in the user's language"}},
      required=("when", "label"), level="act")
def set_reminder(when: str, label: str, ctx=None) -> str:
    try:
        due = datetime.fromisoformat(when)
    except ValueError:
        return "error: give the time as YYYY-MM-DDTHH:MM"
    if due.tzinfo is None:
        due = due.astimezone()
    if due <= now():
        return "error: that time has already passed"
    alarm = ctx.scheduler.add(due.timestamp(), label, "reminder", ctx.tag)
    return "set: " + _describe(alarm, now())


@tool("list_timers", "The timers and reminders that are still pending.", level="private", card="list")
def list_timers(ctx=None) -> str:
    pending = [a for a in ctx.scheduler.pending() if a.kind != "prayer-plan"]   # Rafiq's own, not the user's
    return "\n".join(_describe(a, now()) for a in pending) if pending else "none pending"


@tool("cancel_timer", "Cancel a timer or reminder by its id or by words in its label.",
      {"id": {"type": "string"}, "label": {"type": "string"}}, level="act")
def cancel_timer(id: str = "", label: str = "", ctx=None) -> str:  # noqa: A002 (the model's word)
    gone = ctx.scheduler.cancel(id or None, label or None)
    return f"cancelled {len(gone)}: " + ", ".join(a.label for a in gone) if gone else "nothing matched"


# ---------------------------------------------------------------------- weather (Open-Meteo, no key)

WEATHER_CODES = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
                 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
                 65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
                 81: "rain showers", 82: "violent rain showers", 95: "thunderstorm", 96: "thunderstorm with hail",
                 99: "thunderstorm with hail"}


def geocode(city: str, get) -> dict | None:
    data = get("https://geocoding-api.open-meteo.com/v1/search",
               params={"name": city, "count": 1, "format": "json"})
    results = data.get("results") or []
    return results[0] if results else None


def _get_json(url: str, params: dict) -> dict:
    import httpx

    resp = httpx.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# Said while the forecast is fetched.
CHECKING_WEATHER = {"en": "Let me check the weather.", "fr": "Je regarde la météo.", "es": "Miro el tiempo.",
                    "de": "Ich schaue mir das Wetter an.", "ar": "دعني أتحقق من الطقس.",
                    "ar-EG": "ثواني أشوفلك الجو."}


@tool("weather", "Current weather and the forecast for the next days.",
      {"city": {"type": "string", "description": "city name; empty for where the user is"},
       "days": {"type": "integer", "description": "days of forecast, 1 to 7"}},
      level="safe", slow=True, waiting_phrase=CHECKING_WEATHER, expose=True, card="facts")
def weather(city: str = "", days: int = 2, ctx=None, get=_get_json, here=location.here) -> str:
    cfg = ctx.cfg if ctx else {}
    city = city or cfg.get("place", {}).get("city") or ""
    if city:
        place = geocode(city, get)
        if not place:
            return f"error: no place called {city!r}"
    else:
        found = here(cfg, get)
        if not found:
            return ("error: no city given, none set in the config (place.city) and the location service "
                    "gave none; the user can set place.city, or allow an IP lookup with place.ip_lookup = true")
        place = {"latitude": found.latitude, "longitude": found.longitude, "country": found.country,
                 "name": found.name or f"your location ({found.latitude:.2f}, {found.longitude:.2f})"}
    days = max(1, min(int(days or 1), 7))
    data = get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": days})
    cur, daily = data.get("current") or {}, data.get("daily") or {}
    where = ", ".join(p for p in (place["name"], place.get("country")) if p)
    lines = [f"{where}: now {cur.get('temperature_2m')}°C "
             f"(feels {cur.get('apparent_temperature')}°C), {WEATHER_CODES.get(cur.get('weather_code'), 'unknown')}, "
             f"wind {cur.get('wind_speed_10m')} km/h, humidity {cur.get('relative_humidity_2m')}%"]
    for i, day in enumerate(daily.get("time") or []):
        lines.append(f"{day}: {WEATHER_CODES.get(daily['weather_code'][i], 'unknown')}, "
                     f"{daily['temperature_2m_min'][i]} to {daily['temperature_2m_max'][i]}°C, "
                     f"rain chance {daily['precipitation_probability_max'][i]}%")
    return "\n".join(lines)


# ---------------------------------------------------------------------- calculations

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log,
          "log10": math.log10, "exp": math.exp, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil}
_CONSTS = {"pi": math.pi, "e": math.e}


def safe_eval(expression: str) -> float:
    """Arithmetic only: numbers, + - * / // % **, a few math functions. No names, attributes or calls
    to anything else, so a transcript can never run code."""
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("exponent too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS
                and not node.keywords):
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ValueError("only arithmetic is allowed")

    if len(expression) > 200:
        raise ValueError("expression too long")
    return ev(ast.parse(expression.replace("^", "**").replace("×", "*").replace("÷", "/"), mode="eval"))


@tool("calculate", "Work out an arithmetic expression, e.g. '15/100*240' or 'sqrt(2)*3'.",
      {"expression": {"type": "string"}}, required=("expression",), level="safe", expose=True)
def calculate(expression: str) -> str:
    try:
        value = safe_eval(expression)
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError) as exc:
        return f"error: {exc}"
    return f"{expression} = {round(value, 10):g}" if isinstance(value, float) else f"{expression} = {value}"


# ---------------------------------------------------------------------- unit conversion

UNITS = {
    # length, in metres
    "mm": ("length", 0.001), "cm": ("length", 0.01), "m": ("length", 1.0), "km": ("length", 1000.0),
    "in": ("length", 0.0254), "ft": ("length", 0.3048), "yd": ("length", 0.9144), "mi": ("length", 1609.344),
    # mass, in kilograms
    "mg": ("mass", 1e-6), "g": ("mass", 0.001), "kg": ("mass", 1.0), "t": ("mass", 1000.0),
    "oz": ("mass", 0.028349523125), "lb": ("mass", 0.45359237),
    # volume, in litres
    "ml": ("volume", 0.001), "l": ("volume", 1.0), "cup": ("volume", 0.2365882365),
    "floz": ("volume", 0.0295735295625), "gal": ("volume", 3.785411784),
    # speed, in metres per second
    "m/s": ("speed", 1.0), "km/h": ("speed", 1 / 3.6), "mph": ("speed", 0.44704), "kn": ("speed", 0.514444),
    # area, in square metres
    "m2": ("area", 1.0), "km2": ("area", 1e6), "ha": ("area", 1e4), "acre": ("area", 4046.8564224),
    "ft2": ("area", 0.09290304),
}
ALIASES = {"meter": "m", "metre": "m", "meters": "m", "metres": "m", "kilometer": "km", "kilometers": "km",
           "kilometre": "km", "mile": "mi", "miles": "mi", "foot": "ft", "feet": "ft", "inch": "in",
           "inches": "in", "yard": "yd", "yards": "yd", "gram": "g", "grams": "g", "kilogram": "kg",
           "kilograms": "kg", "kilo": "kg", "kilos": "kg", "pound": "lb", "pounds": "lb", "lbs": "lb",
           "ounce": "oz", "ounces": "oz", "liter": "l", "litre": "l", "liters": "l", "litres": "l",
           "milliliter": "ml", "gallon": "gal", "gallons": "gal", "cups": "cup", "kmh": "km/h", "kph": "km/h",
           "knots": "kn", "hectare": "ha", "hectares": "ha", "acres": "acre",
           "celsius": "c", "°c": "c", "fahrenheit": "f", "°f": "f", "kelvin": "k"}
TEMPERATURE = {"c", "f", "k"}


def convert_value(value: float, from_unit: str, to_unit: str) -> float:
    a = ALIASES.get(from_unit.lower().strip(), from_unit.lower().strip())
    b = ALIASES.get(to_unit.lower().strip(), to_unit.lower().strip())
    if a in TEMPERATURE and b in TEMPERATURE:
        celsius = {"c": value, "f": (value - 32) * 5 / 9, "k": value - 273.15}[a]
        return {"c": celsius, "f": celsius * 9 / 5 + 32, "k": celsius + 273.15}[b]
    if a not in UNITS or b not in UNITS:
        raise ValueError(f"unknown unit {from_unit if a not in UNITS else to_unit!r}")
    if UNITS[a][0] != UNITS[b][0]:
        raise ValueError(f"cannot convert {UNITS[a][0]} to {UNITS[b][0]}")
    return value * UNITS[a][1] / UNITS[b][1]


@tool("convert_units", "Convert between units of length, mass, volume, speed, area or temperature.",
      {"value": {"type": "number"}, "from_unit": {"type": "string", "description": "e.g. km, lb, °F, cup"},
       "to_unit": {"type": "string"}}, required=("value", "from_unit", "to_unit"), level="safe", expose=True)
def convert_units(value: float, from_unit: str, to_unit: str) -> str:
    try:
        result = convert_value(float(value), from_unit, to_unit)
    except ValueError as exc:
        return f"error: {exc}"
    return f"{value:g} {from_unit} = {round(result, 4):g} {to_unit}"


# ---------------------------------------------------------------------- mode

@tool("set_mode", "Switch Rafiq's mode: everyday (fast), quality (the strongest model, slower and costlier) "
      "or offline (only local models and voices, nothing leaves the computer). Empty: say the current mode.",
      {"mode": {"type": "string", "enum": list(modes.MODES), "description": "the mode to switch to"}},
      level="act")
def set_mode(mode: str = "", ctx=None) -> str:
    if not mode:
        return f"the current mode is {modes.current(ctx.cfg)}"
    try:
        mode = modes.switch(ctx.cfg, mode)
    except ValueError as exc:
        return f"error: {exc}"
    if ctx.on_mode:
        ctx.on_mode(mode)
    return f"switched to {mode} mode; it applies from the next request"
