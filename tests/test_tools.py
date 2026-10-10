import pytest

from rafiq import config, lang
from rafiq import tools as tools_pkg
from rafiq.tools import CARDS, LEVELS, REGISTRY, everyday, load_all, pc, web


def test_every_tool_has_a_description_and_a_valid_schema():
    tools = load_all({"google": {"enabled": True}})
    assert len(tools) >= 20
    for tool in tools.values():
        assert tool.description and tool.schema()["type"] == "object"
        assert set(tool.required) <= set(tool.params), tool.name


def test_actions_that_change_something_are_confirmed_first():
    load_all({"google": {"enabled": True}})
    for name in ("power", "email_send", "calendar_add"):
        assert REGISTRY[name].action({"action": "shutdown", "to": "a@b.c", "subject": "Hi", "title": "x",
                                      "start": "2026-10-11T09:00"}), name
    assert REGISTRY["power"].action({"action": "shutdown"}) == "shut down the computer"
    assert "a@b.c" in REGISTRY["email_send"].action({"to": "a@b.c", "subject": "Hi"})
    for name in ("get_time", "weather", "web_search", "read_page", "volume", "open_app", "audio_output"):
        assert REGISTRY[name].action({}) is None, name


def test_every_tool_declares_its_level_speed_exposure_and_card():
    for t in load_all({"google": {"enabled": True}}).values():
        assert t.level in LEVELS and t.card in CARDS, t.name
        assert isinstance(t.slow, bool) and isinstance(t.expose, bool), t.name
        if t.slow:
            assert set(t.waiting_phrase) == set(lang.TAGS) and all(t.waiting_phrase.values()), t.name
        else:
            assert not t.waiting_phrase, t.name


def test_only_critical_tools_ask_first_and_every_critical_tool_does():
    for t in load_all({"google": {"enabled": True}}).values():
        assert (t.confirm is not None) == (t.level == "critical"), t.name
    assert {n for n, t in REGISTRY.items() if t.level == "critical"} == {"power", "email_send", "calendar_add",
                                                                        "calendar_delete"}


def test_only_safe_tools_are_exposed_over_mcp():
    tools = load_all({"google": {"enabled": True}})
    for t in tools.values():
        assert not t.expose or t.level == "safe", t.name
    assert tools["web_search"].expose and tools["calculate"].expose
    for name in ("power", "email_read", "email_list", "calendar_events", "open_app", "volume"):
        assert not tools[name].expose, name


def test_reading_personal_data_is_private():
    tools = load_all({"google": {"enabled": True}})
    for name in ("email_list", "email_read", "calendar_events"):
        assert tools[name].level == "private", name


def test_a_slow_tool_has_a_waiting_phrase_in_the_users_language():
    search = load_all({})["web_search"]
    assert search.slow
    assert search.waiting("fr") == search.waiting_phrase["fr"] != search.waiting("en")
    assert search.waiting("xx") == search.waiting_phrase["en"]
    assert load_all({})["get_time"].waiting("en") is None


@pytest.mark.parametrize("kwargs", [
    {"level": "god"},                                                  # unknown level
    {"level": "safe", "card": "hologram"},                             # unknown card
    {"level": "critical"},                                             # critical without asking first
    {"level": "safe", "confirm": "do it"},                             # asks first but is not critical
    {"level": "act", "expose": True},                                  # only safe tools go over MCP
    {"level": "safe", "slow": True},                                   # slow with nothing to say meanwhile
    {"level": "safe", "slow": True, "waiting_phrase": {"en": "Hold on."}},   # not in every language
    {"level": "safe", "waiting_phrase": dict.fromkeys(lang.TAGS, "x")},      # a phrase on a quick tool
])
def test_a_tool_that_declares_itself_badly_is_refused(kwargs):
    with pytest.raises(ValueError):
        tools_pkg.tool("bad_tool", "never registered", **kwargs)(lambda: "")
    assert "bad_tool" not in REGISTRY


@pytest.mark.parametrize("expr, value", [
    ("15/100*240", 36), ("2**10", 1024), ("sqrt(16)+1", 5), ("-3+4", 1), ("10 % 3", 1), ("2^3", 8), ("pi", 3.14159),
])
def test_calculate(expr, value):
    assert everyday.safe_eval(expr) == pytest.approx(value, rel=1e-4)


@pytest.mark.parametrize("attack", [
    "__import__('os').system('id')", "open('/etc/passwd').read()", "(1).__class__", "[1,2,3]",
    "lambda: 1", "9**9**9", "a", "sqrt(x=4)",
])
def test_calculate_never_runs_code(attack):
    with pytest.raises((ValueError, SyntaxError)):
        everyday.safe_eval(attack)
    assert everyday.calculate(attack).startswith("error")


@pytest.mark.parametrize("value, a, b, expected", [
    (5, "miles", "km", 8.04672), (100, "c", "f", 212), (32, "°F", "celsius", 0), (1, "kg", "lb", 2.20462),
    (1, "gallon", "l", 3.78541), (100, "km/h", "mph", 62.1371),
])
def test_unit_conversion(value, a, b, expected):
    assert everyday.convert_value(value, a, b) == pytest.approx(expected, rel=1e-4)


def test_units_of_different_kinds_do_not_convert():
    assert everyday.convert_units(1, "kg", "km").startswith("error")


def test_unknown_time_zone_is_an_error():
    assert everyday.get_time("Mars/Olympus").startswith("error")
    assert "UTC+09" in everyday.get_time("Asia/Tokyo").replace("UTC+0900", "UTC+09")


def test_weather_reads_open_meteo():
    def get(url, params):
        if "geocoding" in url:
            return {"results": [{"name": "Cairo", "country": "Egypt", "latitude": 30.0, "longitude": 31.2}]}
        return {"current": {"temperature_2m": 29, "apparent_temperature": 31, "weather_code": 0,
                            "wind_speed_10m": 12, "relative_humidity_2m": 40},
                "daily": {"time": ["2026-10-10"], "weather_code": [1], "temperature_2m_min": [21],
                          "temperature_2m_max": [31], "precipitation_probability_max": [0]}}

    text = everyday.weather("Cairo", 1, get=get)
    assert "Cairo, Egypt: now 29°C" in text and "clear sky" in text and "21 to 31°C" in text


def test_weather_needs_a_city():
    ctx = type("Ctx", (), {"cfg": config.load()})()
    assert everyday.weather("", ctx=ctx).startswith("error")


def test_find_app_prefers_exact_then_prefix_then_keyword(tmp_path):
    (tmp_path / "org.mozilla.firefox.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Firefox\nName[ar]=فايرفوكس\nKeywords=web;browser;\n")
    (tmp_path / "org.gnome.Calculator.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Calculator\nGenericName=Calculator\n")
    (tmp_path / "hidden.desktop").write_text("[Desktop Entry]\nType=Application\nName=Hidden\nNoDisplay=true\n")
    entries = pc.desktop_entries([str(tmp_path)])
    assert {e["name"] for e in entries} == {"Firefox", "Calculator"}
    assert pc.find_app("firefox", entries)["id"] == "org.mozilla.firefox"
    assert pc.find_app("فايرفوكس", entries)["id"] == "org.mozilla.firefox"
    assert pc.find_app("calc", entries)["id"] == "org.gnome.Calculator"
    assert pc.find_app("browser", entries)["id"] == "org.mozilla.firefox"
    assert pc.find_app("hidden", entries) is None


def test_parse_volume():
    assert pc.parse_volume("Volume: 0.45") == (45, False)
    assert pc.parse_volume("Volume: 0.30 [MUTED]") == (30, True)


def fake_resolver(addresses):
    def resolve(host, port):
        return [(None, None, None, None, (a, port)) for a in addresses[host]]
    return resolve


@pytest.mark.parametrize("url, addresses, ok", [
    ("https://example.com/page", {"example.com": ["93.184.216.34"]}, True),
    ("http://127.0.0.1:8765/", {"127.0.0.1": ["127.0.0.1"]}, False),
    ("http://router.local/", {"router.local": ["192.168.1.1"]}, False),
    ("http://evil.example/", {"evil.example": ["93.184.216.34", "10.0.0.5"]}, False),   # any private one refuses
    ("http://[::1]/", {"::1": ["::1"]}, False),
    ("file:///etc/passwd", {}, False),
    ("ftp://example.com/", {}, False),
])
def test_only_the_public_internet_is_fetched(url, addresses, ok):
    assert (web.public_url(url, resolve=fake_resolver(addresses)) is None) is ok


def test_open_in_browser_only_opens_web_addresses():
    assert web.open_in_browser("file:///etc/passwd").startswith("error")
    assert web.open_in_browser("javascript:alert(1)").startswith("error")


class FakeShutdownCtx:
    def __init__(self, delay=30):
        self.cfg = config.load()
        self.cfg["pc"]["shutdown_delay"] = delay
        self.tag = "fr"
        self.said = []

    def say(self, text, tag):
        self.said.append((text, tag))


def countdown(cancel_at=None):
    """A shutdown whose waits return at once and whose commands are only recorded."""
    ran, waited = [], []
    shutdown = pc.DelayedShutdown(run=lambda args: ran.append(args) or type("R", (), {"returncode": 0})(),
                                  spawn=lambda fn: fn())

    def wait(seconds):
        waited.append(seconds)
        if cancel_at is not None and len(waited) == cancel_at:
            shutdown.cancel()
        return shutdown.cancelled.is_set()

    shutdown.wait = wait
    return shutdown, ran, waited


def test_shutdown_waits_warns_then_powers_off(monkeypatch):
    shutdown, ran, waited = countdown()
    monkeypatch.setattr(pc, "SHUTDOWN", shutdown)
    ctx = FakeShutdownCtx()
    assert "30 seconds" in pc.power("shutdown", ctx=ctx)
    assert waited == [20, 10]
    assert ctx.said == [("Extinction dans 10 secondes. Dis « annule l'extinction » pour l'arrêter.", "fr")]
    assert ran == [["systemctl", "poweroff"]]
    assert not shutdown.pending


def test_cancel_the_shutdown_stops_the_countdown(monkeypatch):
    shutdown, ran, waited = countdown(cancel_at=1)
    monkeypatch.setattr(pc, "SHUTDOWN", shutdown)
    pc.power("shutdown", ctx=FakeShutdownCtx())
    assert ran == [] and waited == [20]


def test_cancel_shutdown_tool(monkeypatch):
    shutdown = pc.DelayedShutdown(run=lambda args: None)
    monkeypatch.setattr(pc, "SHUTDOWN", shutdown)
    assert pc.cancel_shutdown() == "no shutdown is pending"
    shutdown.start(60, ["systemctl", "poweroff"], lambda left: None)
    assert shutdown.pending
    assert pc.cancel_shutdown() == "the shutdown is cancelled"
    shutdown.thread.join(timeout=2)
    assert not shutdown.pending


def test_a_second_shutdown_does_not_start_a_second_countdown(monkeypatch):
    shutdown = pc.DelayedShutdown(run=lambda args: None)
    monkeypatch.setattr(pc, "SHUTDOWN", shutdown)
    pc.power("shutdown", ctx=FakeShutdownCtx())
    assert "already" in pc.power("shutdown", ctx=FakeShutdownCtx())
    shutdown.cancel()


def test_a_zero_delay_shuts_down_at_once(monkeypatch):
    ran = []
    monkeypatch.setattr(pc, "_run", lambda args: ran.append(args) or type("R", (), {"returncode": 0})())
    assert pc.power("shutdown", ctx=FakeShutdownCtx(delay=0)) == "done"
    assert ran == [["systemctl", "poweroff"]]


def test_cancelling_the_shutdown_is_not_asked_first():
    load_all({})
    assert REGISTRY["cancel_shutdown"].action({}) is None
