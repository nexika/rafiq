from types import SimpleNamespace

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
