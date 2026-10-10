import subprocess

import httpx
import pytest

from rafiq import config, doctor


class Probe(doctor.Probe):
    """A machine with nothing on it unless the test says so: no network, no programs, no modules."""

    def __init__(self, answers=None, programs=(), modules=(), gpu=""):
        self.answers, self.programs, self.modules, self.gpu = answers or {}, set(programs), set(modules), gpu
        self.asked = []

    def get(self, url, **kwargs):
        self.asked.append((url, kwargs))
        for start, answer in self.answers.items():
            if url.startswith(start):
                if isinstance(answer, Exception):
                    raise answer
                status, body = answer
                return httpx.Response(status, json=body, request=httpx.Request("GET", url))
        raise httpx.ConnectError("no network in tests")

    def which(self, program):
        return f"/usr/bin/{program}" if program in self.programs else None

    def has_module(self, name):
        return name in self.modules

    def run(self, args, timeout=5):
        if args[0] == "nvidia-smi" and self.gpu:
            return subprocess.CompletedProcess(args, 0, self.gpu, "")
        raise FileNotFoundError(args[0])


def by_label(checks):
    return {c.label: c for c in checks}


def test_ollama_with_the_model_installed_is_ok():
    probe = Probe({"http://127.0.0.1:11434/api/tags": (200, {"models": [{"name": "qwen2.5:7b"}]})})
    check = by_label(doctor.integrations(config.load(), probe))["Ollama"]
    assert check.ok and "installed" in check.detail


def test_ollama_without_the_model_says_how_to_get_it():
    probe = Probe({"http://127.0.0.1:11434/api/tags": (200, {"models": [{"name": "llama3:8b"}]})})
    check = by_label(doctor.integrations(config.load(), probe))["Ollama"]
    assert not check.ok and check.fix == "ollama pull qwen2.5:7b"


def test_ollama_not_running_says_how_to_start_it():
    check = by_label(doctor.integrations(config.load(), Probe()))["Ollama"]
    assert not check.ok and "not reachable" in check.detail and "ollama serve" in check.fix


def test_claude_key_never_appears_in_the_report(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    checks = doctor.integrations(config.load(), Probe(modules={"anthropic"}))
    assert by_label(checks)["Claude"].ok
    assert all("sk-ant-secret" not in f"{c.detail} {c.fix}" for c in checks)


def test_claude_without_a_key_says_which_variable_to_set():
    check = by_label(doctor.integrations(config.load(), Probe(modules={"anthropic"})))["Claude"]
    assert not check.ok and "ANTHROPIC_API_KEY" in check.fix


def test_azure_key_is_tried_against_its_region(monkeypatch):
    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-secret")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    probe = Probe({"https://westeurope.tts.speech.microsoft.com/": (200, [])})
    check = by_label(doctor.integrations(config.load(), probe))["Azure voices"]
    assert check.ok and check.detail == "westeurope"
    kwargs = next(kw for url, kw in probe.asked if "microsoft" in url)
    assert kwargs["headers"]["Ocp-Apim-Subscription-Key"] == "azure-secret"
    assert "azure-secret" not in check.detail + check.fix


def test_azure_rejected_key_is_reported(monkeypatch):
    monkeypatch.setenv("AZURE_SPEECH_KEY", "wrong")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    probe = Probe({"https://westeurope.tts.speech.microsoft.com/": (401, {})})
    check = by_label(doctor.integrations(config.load(), probe))["Azure voices"]
    assert not check.ok and "401" in check.detail


def test_azure_not_set_up_is_optional_and_not_tried():
    probe = Probe()
    check = by_label(doctor.integrations(config.load(), probe))["Azure voices"]
    assert not check.ok and check.optional
    assert not any("microsoft" in url for url, _ in probe.asked)


def test_google_needs_the_libraries_then_the_token_then_enabled(rafiq_home):
    cfg = config.load()
    assert "pip install" in by_label(doctor.integrations(cfg, Probe()))["Google"].fix
    probe = Probe(modules={"googleapiclient", "google_auth_oauthlib"})
    assert by_label(doctor.integrations(cfg, probe))["Google"].fix == "rafiq google-setup"
    (rafiq_home / "google").mkdir(parents=True)
    (rafiq_home / "google" / "token.json").write_text("{}")
    google = by_label(doctor.integrations(cfg, probe))["Google"]
    assert not google.ok and "enabled = true" in google.fix
    cfg["google"]["enabled"] = True
    assert by_label(doctor.integrations(cfg, probe))["Google"].ok


def test_weather_without_a_city_still_works_but_suggests_one():
    probe = Probe({"https://geocoding-api.open-meteo.com/": (200, {"results": []})})
    check = by_label(doctor.integrations(config.load(), probe))["Weather"]
    assert check.ok and "[place] city" in check.fix


def test_weather_unreachable_is_reported():
    check = by_label(doctor.integrations(config.load(), Probe()))["Weather"]
    assert not check.ok and "not reachable" in check.detail


def test_programs_the_pc_tools_use_are_checked():
    checks = by_label(doctor.integrations(config.load(), Probe(programs={"wpctl", "xdg-open"})))
    assert checks["Volume"].ok
    assert checks["Open apps and pages"].ok
    assert not checks["Lock screen"].ok and "loginctl" in checks["Lock screen"].detail


def test_web_search_needs_its_libraries():
    assert not by_label(doctor.integrations(config.load(), Probe()))["Web search"].ok
    probe = Probe(modules={"ddgs", "trafilatura"})
    assert by_label(doctor.integrations(config.load(), probe))["Web search"].ok


def test_machine_reads_an_nvidia_gpu():
    machine = doctor.machine(Probe(gpu="NVIDIA GeForce RTX 3060, 12288\n"), ram_gb=32)
    assert machine.gpu == "NVIDIA GeForce RTX 3060" and machine.vram_gb == pytest.approx(12)


def test_machine_without_nvidia_smi_has_no_gpu():
    machine = doctor.machine(Probe(), ram_gb=16)
    assert machine.gpu == "" and machine.vram_gb == 0 and machine.ram_gb == 16


@pytest.mark.parametrize("ram, vram, ollama, whisper", [
    (4, 0, "qwen2.5:1.5b", "base"),
    (8, 0, "qwen2.5:3b", "small"),
    (16, 0, "qwen2.5:7b", "small"),
    (64, 0, "qwen2.5:14b", "small"),
    (16, 6, "qwen2.5:7b", "medium"),
    (16, 4, "qwen2.5:3b", "small"),
    (32, 24, "qwen2.5:14b", "medium"),
])
def test_model_suggestion_fits_the_memory(ram, vram, ollama, whisper):
    advice = doctor.suggest(doctor.Machine(ram_gb=ram, gpu="GPU" if vram else "", vram_gb=vram))
    assert advice.ollama_model == ollama and advice.whisper_model == whisper
    assert advice.reason


def test_report_says_when_the_configured_model_is_too_big():
    cfg = config.load()
    cfg["brain"]["ollama_model"] = "qwen2.5:14b"
    lines = doctor.advice_lines(cfg, doctor.Machine(ram_gb=8))
    text = "\n".join(lines)
    assert "qwen2.5:3b" in text and "ollama_model" in text


def test_report_is_quiet_when_the_configured_model_fits():
    lines = doctor.advice_lines(config.load(), doctor.Machine(ram_gb=16))
    assert not any("ollama_model =" in line for line in lines)
