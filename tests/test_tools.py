import pytest

from rafiq import config
from rafiq.tools import REGISTRY, everyday, load_all, pc, web


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
    for name in ("get_time", "weather", "web_search", "read_page", "volume", "open_app"):
        assert REGISTRY[name].action({}) is None, name


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


# ---------------------------------------------------------------------- app catalogue

@pytest.fixture
def launched(monkeypatch):
    """Record what would be started instead of starting it."""
    calls = []
    monkeypatch.setattr(pc, "_spawn", calls.append)
    monkeypatch.setattr(pc, "desktop_entries", lambda dirs=None: [
        {"id": "org.mozilla.firefox", "name": "Firefox", "names": ["firefox"], "keywords": []}])
    return calls


def ctx_with(apps):
    return type("Ctx", (), {"cfg": {"apps": apps}})()


@pytest.mark.parametrize("spec, args", [
    ({"steam": 49520}, ["xdg-open", "steam://rungameid/49520"]),
    ({"steam": "steam://rungameid/49520"}, ["xdg-open", "steam://rungameid/49520"]),
    ({"flatpak": "com.mojang.Minecraft"}, ["flatpak", "run", "com.mojang.Minecraft"]),
    ({"command": ["retroarch", "--fullscreen"]}, ["retroarch", "--fullscreen"]),
])
def test_catalogue_entries_become_argument_lists(spec, args):
    assert pc.launch_args(spec) == args


@pytest.mark.parametrize("spec", [
    {}, {"steam": "abc"}, {"steam": "steam://install/1"}, {"flatpak": "minecraft; rm -rf ~"},
    {"command": "retroarch --fullscreen"}, {"command": []}, {"command": ["a", 3]},
    {"steam": 1, "flatpak": "com.a.B"}, {"uri": "file:///etc/passwd"},
])
def test_bad_catalogue_entries_are_refused(spec):
    with pytest.raises(ValueError):
        pc.launch_args(spec)


def test_open_app_launches_a_game_from_the_config(launched):
    text = pc.open_app("borderlands", ctx=ctx_with({"Borderlands 2": {"steam": 49520}}))
    assert text == "opened Borderlands 2"
    assert launched == [["xdg-open", "steam://rungameid/49520"]]


def test_open_app_still_opens_desktop_apps(launched):
    assert pc.open_app("firefox", ctx=ctx_with({"Borderlands": {"steam": 49520}})) == "opened Firefox"
    assert launched == [["gtk-launch", "org.mozilla.firefox"]]


def test_a_broken_config_entry_does_not_hide_the_others(launched):
    ctx = ctx_with({"Broken": {"steam": "nope"}, "Minecraft": {"flatpak": "com.mojang.Minecraft"}})
    assert pc.open_app("minecraft", ctx=ctx) == "opened Minecraft"
    assert "Minecraft" in pc.list_apps(ctx=ctx) and "Broken" not in pc.list_apps(ctx=ctx)


def test_adding_an_app_asks_first_and_names_what_it_runs():
    load_all({})
    tool = REGISTRY["add_app"]
    assert tool.action({"name": "Borderlands", "steam": "49520"}) == \
        "add an app called Borderlands that opens Steam game 49520"
    assert "/usr/bin/retroarch --fullscreen" in tool.action(
        {"name": "Retro", "command": ["/usr/bin/retroarch", "--fullscreen"]})


def test_an_added_app_is_remembered_and_opens(launched, rafiq_home):
    ctx = ctx_with({})
    assert pc.add_app("Borderlands", steam="49520", ctx=ctx) == "added Borderlands"
    assert (rafiq_home / "apps.json").is_file()
    assert pc.open_app("Borderlands", ctx=ctx) == "opened Borderlands"
    assert launched == [["xdg-open", "steam://rungameid/49520"]]
    assert "Borderlands" in pc.list_apps(ctx=ctx)


def test_adding_a_command_checks_the_program_exists(launched):
    import sys

    ctx = ctx_with({})
    assert pc.add_app("Nothing", command=["no-such-program-rafiq"], ctx=ctx).startswith("error")
    assert pc.add_app("Py", command=[sys.executable, "-V"], ctx=ctx) == "added Py"
    pc.open_app("py", ctx=ctx)
    assert launched == [[sys.executable, "-V"]]


def test_adding_needs_exactly_one_way_to_launch():
    ctx = ctx_with({})
    assert pc.add_app("X", ctx=ctx).startswith("error")
    assert pc.add_app("X", steam="1", flatpak="com.a.B", ctx=ctx).startswith("error")
    assert pc.add_app("", steam="1", ctx=ctx).startswith("error")


def test_an_app_in_the_config_is_not_replaced_by_voice():
    ctx = ctx_with({"Borderlands": {"steam": 49520}})
    assert pc.add_app("borderlands", steam="1", ctx=ctx).startswith("error")
