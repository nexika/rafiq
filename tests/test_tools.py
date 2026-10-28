from types import SimpleNamespace

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
    cfg = {"google": {"enabled": True}, "home_assistant": {"url": "http://ha.local", "token": "t"},
           "obs": {"enabled": True}, "spotify": {"enabled": True},
           "plugs": [{"name": "PC", "kind": "shelly", "host": "192.168.1.40"}], "browser": {"enabled": True}}
    for t in load_all(cfg).values():
        assert (t.confirm is not None) == (t.level == "critical"), t.name
    assert {n for n, t in REGISTRY.items() if t.level == "critical"} == {"power", "email_send", "calendar_add",
                                                                        "calendar_delete", "add_app", "wake_computer",
                                                                        "home_switch", "look_at_screen", "email_reply",
                                                                        "email_trash", "obs_stream", "obs_record",
                                                                        "plug_switch", "spotify_add_to_playlist",
                                                                        "print_paper", "close_browser_tabs",
                                                                        "close_other_browser_tabs", "record_learning"}


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
    assert everyday.weather("", ctx=ctx, here=lambda cfg, get: None).startswith("error")


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



def fake_amd_card(drm, name="card1", vendor="0x1002", busy="7", temp="51000",
                  vram=("755228672", "1073741824")):
    device = drm / name / "device"
    (device / "hwmon" / "hwmon3").mkdir(parents=True)
    (device / "vendor").write_text(vendor + "\n")
    (device / "gpu_busy_percent").write_text(busy + "\n")
    (device / "hwmon" / "hwmon3" / "temp1_input").write_text(temp + "\n")
    (device / "mem_info_vram_used").write_text(vram[0] + "\n")
    (device / "mem_info_vram_total").write_text(vram[1] + "\n")
    return device


def test_amd_gpu_is_read_from_sysfs(tmp_path):
    fake_amd_card(tmp_path)
    (tmp_path / "card1-eDP-1").mkdir()  # a screen connector, not a card
    fake_amd_card(tmp_path, "card0", vendor="0x8086")  # an Intel card is not read here
    [gpu] = pc.amd_gpus(tmp_path)
    assert gpu == pc.Gpu("AMD GPU", load=7, temperature=51, memory_used=755228672, memory_total=1073741824)
    assert pc.describe_gpu(gpu) == "AMD GPU 7% busy, 51°C, video memory 0.7 of 1.0 GB used"


def test_amd_gpu_with_missing_figures_says_what_it_has(tmp_path):
    device = fake_amd_card(tmp_path)
    for name in ("gpu_busy_percent", "mem_info_vram_used", "mem_info_vram_total"):
        (device / name).unlink()
    [gpu] = pc.amd_gpus(tmp_path)
    assert pc.describe_gpu(gpu) == "AMD GPU 51°C"


def test_no_drm_folder_means_no_amd_gpu(tmp_path):
    assert pc.amd_gpus(tmp_path / "missing") == []


class FakeNvml:
    """The few pynvml calls Rafiq uses, for one card."""
    NVML_TEMPERATURE_GPU = 0

    class NVMLError(Exception):
        pass

    def __init__(self, fail=False):
        self.fail, self.shut = fail, False

    def nvmlInit(self):
        if self.fail:
            raise self.NVMLError("no driver")

    def nvmlShutdown(self):
        self.shut = True

    def nvmlDeviceGetCount(self):
        return 1

    def nvmlDeviceGetHandleByIndex(self, i):
        return i

    def nvmlDeviceGetName(self, handle):
        return "NVIDIA GeForce RTX 3060"

    def nvmlDeviceGetUtilizationRates(self, handle):
        return SimpleNamespace(gpu=42, memory=10)

    def nvmlDeviceGetTemperature(self, handle, sensor):
        return 63

    def nvmlDeviceGetMemoryInfo(self, handle):
        return SimpleNamespace(used=3 * 2**30, total=12 * 2**30)


def test_nvidia_gpu_is_read_through_nvml():
    nvml = FakeNvml()
    [gpu] = pc.nvidia_gpus(nvml)
    assert pc.describe_gpu(gpu) == "NVIDIA GeForce RTX 3060 42% busy, 63°C, video memory 3.0 of 12.0 GB used"
    assert nvml.shut


def test_no_nvidia_driver_means_no_nvidia_gpu():
    assert pc.nvidia_gpus(FakeNvml(fail=True)) == []


def test_gpu_status_without_a_gpu(monkeypatch):
    monkeypatch.setattr(pc, "gpus", lambda: [])
    assert pc.gpu_status() == "no graphics card figures available"


def test_system_status_includes_the_gpu(monkeypatch):
    monkeypatch.setattr(pc, "gpus", lambda: [pc.Gpu("AMD GPU", load=7, temperature=51)])
    assert pc.system_status().endswith("AMD GPU 7% busy, 51°C")


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


class FakePlayerctl:
    """Stands in for playerctl: each player has a status and what it is playing; records the commands."""

    def __init__(self, players: dict[str, tuple[str, str]]):
        self.players, self.sent = players, []

    def __call__(self, args):
        assert args[0] == "playerctl"
        if args[1:] == ["--list-all"]:
            out = "".join(f"{p}\n" for p in self.players) or "No players found\n"
            return type("Result", (), {"returncode": 0 if self.players else 1, "stdout": out, "stderr": ""})()
        assert args[1] == "--player"
        status, playing = self.players[args[2]]
        if args[3] == "status":
            out = status
        elif args[3] == "metadata":
            out = playing
        else:
            self.sent.append((args[2], args[3]))
            out = ""
        return type("Result", (), {"returncode": 0, "stdout": out + "\n", "stderr": ""})()


def test_media_controls_the_player_that_is_playing():
    run = FakePlayerctl({"vlc": ("Paused", "Old film"), "spotify": ("Playing", "Fairuz - Kifak Inta")})
    assert pc.media("pause", run=run) == "paused spotify"
    assert run.sent == [("spotify", "pause")]


def test_media_resumes_a_paused_player_when_nothing_plays():
    run = FakePlayerctl({"firefox": ("Stopped", ""), "vlc": ("Paused", "Old film")})
    assert pc.media("play", run=run) == "playing vlc"
    assert run.sent == [("vlc", "play")]


@pytest.mark.parametrize("action, command", [
    ("next", "next"), ("previous", "previous"), ("toggle", "play-pause"), ("stop", "stop"),
])
def test_media_actions_map_to_playerctl(action, command):
    run = FakePlayerctl({"spotify": ("Playing", "Fairuz - Kifak Inta")})
    pc.media(action, run=run)
    assert run.sent == [("spotify", command)]


def test_media_says_what_is_playing():
    run = FakePlayerctl({"spotify": ("Playing", "Fairuz - Kifak Inta")})
    assert pc.media("status", run=run) == "spotify is playing: Fairuz - Kifak Inta"
    assert run.sent == []


def test_media_without_a_player_or_with_a_bad_action_is_an_error():
    assert pc.media("pause", run=FakePlayerctl({})).startswith("error")
    assert pc.media("rewind", run=FakePlayerctl({"vlc": ("Playing", "x")})).startswith("error")


def test_media_without_playerctl_installed_is_an_error():
    def missing(args):
        raise FileNotFoundError("playerctl")
    assert "playerctl" in pc.media("pause", run=missing)


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
