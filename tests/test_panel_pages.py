import json
import threading

import pytest
from test_brain_panel import FakeAssistant, request, tools_with

from rafiq import config
from rafiq.brain import Brain
from rafiq.models import Models, fits
from rafiq.panel import serve
from rafiq.permissions import Approvals, levels
from rafiq.status import check_all
from rafiq.tools import Tool


class FakeOllama:
    """Ollama's API as far as the models page uses it; `up = False` is Ollama not running."""

    def __init__(self, installed=()):
        self.installed, self.up, self.calls = list(installed), True, []

    def tags(self):
        if not self.up:
            raise ConnectionError("refused")
        return list(self.installed)

    def pull(self, name):
        self.calls.append(("pull", name))
        self.installed.append(name)

    def delete(self, name):
        self.calls.append(("delete", name))
        self.installed.remove(name)

    def generate(self, name, prompt):
        self.calls.append(("generate", name))
        return {"eval_count": 50, "eval_duration": 2_000_000_000}


def make_models(installed=("qwen2.5:7b",), ram_gb=16.0, whisper=("small",), tmp_path=None, swapped=None):
    cfg = config.load()
    events = []
    cache = tmp_path / "hub"
    for size in whisper:
        (cache / f"models--Systran--faster-whisper-{size}").mkdir(parents=True)
    models = Models(cfg, ollama=FakeOllama(installed), ram_gb=ram_gb, whisper_cache=cache,
                    on_event=lambda kind, data: events.append((kind, data)),
                    on_whisper=(swapped.append if swapped is not None else None), background=lambda fn: fn())
    return cfg, models, events


# ---------------------------------------------------------------------- models

@pytest.mark.parametrize("size_gb, ram_gb, ok", [(4.7, 16, True), (4.7, 6, False), (2.0, 4, True), (40, 32, False)])
def test_a_model_fits_when_it_leaves_room_for_the_rest(size_gb, ram_gb, ok):
    assert fits(size_gb, ram_gb) is ok


def test_the_catalogue_shows_what_fits_what_is_installed_and_what_is_active(tmp_path):
    _, models, _ = make_models(installed=("qwen2.5:7b", "mistral:latest"), ram_gb=8.0, tmp_path=tmp_path)
    page = models.ollama_page()
    rows = {m["name"]: m for m in page["models"]}
    assert page["reachable"] and page["active"] == "qwen2.5:7b"
    assert rows["qwen2.5:7b"]["installed"] and rows["qwen2.5:7b"]["active"]
    assert rows["qwen2.5:3b"]["fits"] and not rows["qwen2.5:3b"]["installed"]
    assert not rows["qwen2.5:32b"]["fits"]
    assert rows["mistral:latest"]["installed"]          # installed by hand, not in the catalogue


def test_the_models_page_says_when_ollama_is_down(tmp_path):
    _, models, _ = make_models(tmp_path=tmp_path)
    models.ollama.up = False
    page = models.ollama_page()
    assert not page["reachable"] and not any(m["installed"] for m in page["models"])


def test_install_benchmark_activate_and_delete(tmp_path):
    cfg, models, events = make_models(tmp_path=tmp_path)
    models.install("qwen2.5:3b")
    models.benchmark("qwen2.5:3b")
    models.activate("qwen2.5:3b")
    assert cfg["brain"]["ollama_model"] == "qwen2.5:3b"
    assert config.load()["brain"]["ollama_model"] == "qwen2.5:3b"      # kept for the next start
    row = next(m for m in models.ollama_page()["models"] if m["name"] == "qwen2.5:3b")
    assert row["tokens_per_second"] == 25.0
    models.delete("qwen2.5:7b")
    assert ("delete", "qwen2.5:7b") in models.ollama.calls
    assert [d["action"] for k, d in events if k == "model"] == ["install", "benchmark", "activate", "delete"]


def test_only_catalogue_models_install_and_only_installed_ones_change(tmp_path):
    _, models, _ = make_models(tmp_path=tmp_path)
    for act, name in [(models.install, "evil; rm -rf"), (models.install, "someone/unknown:1b"),
                      (models.delete, "qwen2.5:3b"), (models.activate, "qwen2.5:3b"),
                      (models.benchmark, "llama3.2:3b")]:
        with pytest.raises(ValueError):
            act(name)
    assert models.ollama.calls == []


def test_the_active_model_cannot_be_deleted(tmp_path):
    _, models, _ = make_models(tmp_path=tmp_path)
    with pytest.raises(ValueError):
        models.delete("qwen2.5:7b")


def test_a_failed_install_is_reported_not_raised(tmp_path):
    _, models, events = make_models(tmp_path=tmp_path)

    def broken(name):
        raise ConnectionError("refused")

    models.ollama.pull = broken
    models.install("qwen2.5:3b")
    assert events[-1] == ("model", {"action": "install", "name": "qwen2.5:3b", "ok": False,
                                    "result": "ConnectionError: refused"})


def test_whisper_models_show_downloads_and_switch(tmp_path):
    swapped = []
    cfg, models, _ = make_models(whisper=("small", "tiny"), tmp_path=tmp_path, swapped=swapped)
    rows = {m["name"]: m for m in models.whisper_page()["models"]}
    assert rows["small"]["active"] and rows["small"]["downloaded"] and not rows["medium"]["downloaded"]
    models.activate_whisper("medium")
    assert swapped == ["medium"] and cfg["speech"]["whisper_model"] == "medium"
    assert config.load()["speech"]["whisper_model"] == "medium"
    with pytest.raises(ValueError):
        models.activate_whisper("huge")


# ---------------------------------------------------------------------- permissions

def test_each_tool_has_a_level(tmp_path):
    approvals = Approvals(tmp_path / "approvals.json")
    approvals.remember("send_email")
    tools = {**tools_with([]), "send_email": Tool("send_email", "send", lambda: "", confirm="send it")}
    rows = {r["name"]: r["level"] for r in levels(tools, approvals)}
    assert rows == {"power": "asks", "get_time": "free", "send_email": "remembered"}


def test_approvals_are_kept_and_revoked(tmp_path):
    path = tmp_path / "approvals.json"
    Approvals(path).remember("power")
    assert Approvals(path).allowed("power")
    Approvals(path).revoke("power")
    assert not Approvals(path).allowed("power") and json.loads(path.read_text()) == []


def test_a_damaged_approvals_file_means_nothing_is_approved(tmp_path):
    path = tmp_path / "approvals.json"
    path.write_text("{not json")
    assert Approvals(path).names() == []


def test_always_remembers_the_approval_and_stops_asking(tmp_path, monkeypatch):
    import httpx

    script = [{"role": "assistant", "tool_calls": [{"function": {"name": "power", "arguments": {}}}]},
              {"role": "assistant", "content": "Done."}] * 2

    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Resp({"message": script.pop(0)}))
    cfg = config.load()
    cfg["brain"]["mode"] = "ollama"
    log, asked = [], []
    approvals = Approvals(tmp_path / "approvals.json")
    brain = Brain(cfg, tools_with(log), lambda action, tag: asked.append(action) or "always", approvals=approvals)
    brain.think("shut down", "en")
    brain.think("shut down again", "en")
    assert len(asked) == 1 and log == ["shutdown ran", "shutdown ran"] and approvals.allowed("power")


# ---------------------------------------------------------------------- status

class FakeSpeaker:
    def __init__(self, engines, piper=None):
        self._engines, self.piper = engines, piper

    def engines(self):
        return self._engines

    def piper_path(self, tag):
        return self.piper

    def stop(self):
        pass


def test_status_shows_each_part_up_or_down(tmp_path, rafiq_home):
    cfg, models, _ = make_models(tmp_path=tmp_path)
    cfg["google"]["enabled"] = True
    parts = {p["part"]: p for p in check_all(cfg, models, FakeSpeaker(["piper"], piper=tmp_path / "en.onnx"))}
    assert parts["ollama"]["up"] is True
    assert parts["whisper"]["up"] is True
    assert parts["voice"]["up"] is True
    assert parts["google"]["up"] is False          # enabled, but `rafiq google-setup` never ran
    assert parts["claude"]["up"] is None           # no key: not used
    assert parts["satellites"]["up"] is None


def test_status_when_ollama_is_down_and_no_voice_is_found(tmp_path):
    cfg, models, _ = make_models(tmp_path=tmp_path, whisper=())
    models.ollama.up = False
    parts = {p["part"]: p for p in check_all(cfg, models, FakeSpeaker(["piper"]))}
    assert parts["ollama"]["up"] is False and parts["whisper"]["up"] is False and parts["voice"]["up"] is False


# ---------------------------------------------------------------------- the panel pages

class PagesAssistant(FakeAssistant):
    def __init__(self, tmp_path):
        super().__init__()
        self.cfg, self.models, self.events_seen = make_models(tmp_path=tmp_path)
        self.tools = tools_with([])
        self.approvals = Approvals(tmp_path / "approvals.json")
        self.speaker = FakeSpeaker(["piper"])


@pytest.fixture
def pages(tmp_path):
    assistant = PagesAssistant(tmp_path)
    server = serve(assistant, "127.0.0.1", 0, "t" * 40)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield assistant, port
    server.shutdown()


def get(port, path, token="t" * 40):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token:
        req.add_header("X-Rafiq-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def test_the_pages_read_models_status_and_permissions(pages):
    _, port = pages
    status, models = get(port, "/api/models")
    assert status == 200 and models["ollama"]["active"] == "qwen2.5:7b" and models["whisper"]["active"] == "small"
    status, parts = get(port, "/api/status")
    assert status == 200 and {p["part"] for p in parts["parts"]} >= {"ollama", "voice", "google", "satellites"}
    status, perms = get(port, "/api/permissions")
    assert status == 200 and {t["name"] for t in perms["tools"]} == {"power", "get_time"}


def test_the_pages_need_the_token(pages):
    _, port = pages
    for path in ("/api/models", "/api/status", "/api/permissions"):
        assert get(port, path, token=None)[0] == 403
        assert get(port, path, token="x" * 40)[0] == 403


def test_the_panel_changes_models(pages):
    assistant, port = pages
    assert request(port, "/api/models/install", {"name": "qwen2.5:3b"})[0] == 200
    assert request(port, "/api/models/activate", {"name": "qwen2.5:3b"})[0] == 200
    assert request(port, "/api/whisper/activate", {"name": "base"})[0] == 200
    assert assistant.cfg["brain"]["ollama_model"] == "qwen2.5:3b"
    assert assistant.cfg["speech"]["whisper_model"] == "base"


def test_the_panel_refuses_names_off_the_allowlist(pages):
    assistant, port = pages
    assert request(port, "/api/models/install", {"name": "../../etc"})[0] == 400
    assert request(port, "/api/models/delete", {"name": 3})[0] == 400
    assert request(port, "/api/models/install", {"name": "qwen2.5:3b"}, token=None)[0] == 403
    assert assistant.models.ollama.calls == []


def test_revoke_from_the_panel(pages):
    assistant, port = pages
    assistant.approvals.remember("power")
    assert request(port, "/api/permissions/revoke", {"name": "power"})[0] == 200
    assert not assistant.approvals.allowed("power")


def test_always_from_the_panel_answers_yes_and_remembers(pages):
    assistant, port = pages
    assert request(port, "/api/confirm", {"yes": True, "always": True})[0] == 200
    assert assistant.pending_answer.get_nowait() == "always"
