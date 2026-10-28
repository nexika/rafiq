import base64
import io
import subprocess

import pytest

from rafiq import config
from rafiq.tools import REGISTRY, Tool, load_all, screen

PNG = b"\x89PNG\r\n\x1a\nfake"


@pytest.fixture(autouse=True)
def never_the_real_screen(monkeypatch, tmp_path):
    """No test here may reach the portal or a screenshot command of the computer it runs on."""
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))


class Ctx:
    def __init__(self, cfg, tag="en"):
        self.cfg, self.tag = cfg, tag


def cfg_with(mode="auto", key="", local=""):
    cfg = config.load()
    cfg["brain"]["mode"] = mode
    cfg["anthropic_api_key"] = key
    cfg["screen"]["ollama_model"] = local
    return cfg


@pytest.mark.parametrize("mode, key, local, expected", [
    ("auto", "sk-test", "", "claude"),
    ("claude", "sk-test", "", "claude"),
    ("auto", "", "", None),                     # no key: offline
    ("ollama", "sk-test", "", None),            # offline by choice: the screen never goes to the cloud
    ("ollama", "sk-test", "qwen2.5vl:7b", "ollama"),
    ("auto", "", "qwen2.5vl:7b", "ollama"),
])
def test_where_the_screen_goes(mode, key, local, expected):
    assert screen.backend(cfg_with(mode, key, local)) == expected


def test_offline_without_a_local_vision_model_never_takes_a_screenshot():
    def capture():
        raise AssertionError("no screenshot offline")

    text = screen.look(cfg_with("ollama", "sk-test"), "what's this error?", "en", capture=capture)
    assert text.startswith("error") and "offline" in text


def test_the_question_and_the_language_go_with_the_screenshot():
    seen = {}

    def vision(png, prompt):
        seen.update(png=png, prompt=prompt)
        return "It says the file was not found."

    text = screen.look(cfg_with("auto", "sk-test"), "what's this error?", "ar-EG",
                       capture=lambda: PNG, vision=vision, shrink=lambda png, side: png)
    assert text == "It says the file was not found."
    assert seen["png"] == PNG
    assert "what's this error?" in seen["prompt"] and "Egyptian Arabic" in seen["prompt"]


def test_a_failed_screenshot_is_reported():
    def capture():
        raise screen.ScreenError("the screenshot was refused")

    text = screen.look(cfg_with("auto", "sk-test"), "read this", "en", capture=capture,
                       vision=lambda png, prompt: "never")
    assert text == "error: the screenshot was refused"


def test_sending_the_screen_to_the_cloud_is_confirmed_first():
    load_all({})
    tool = REGISTRY["look_at_screen"]
    action = tool.action({"question": "read this"}, Ctx(cfg_with("auto", "sk-test")))
    assert "screen" in action and "cloud" in action
    assert tool.action({}, Ctx(cfg_with("ollama", "", "qwen2.5vl:7b"))) is None   # stays on this computer


def test_the_cloud_confirmation_can_be_turned_off():
    cfg = cfg_with("auto", "sk-test")
    cfg["screen"]["ask_first"] = False
    assert REGISTRY["look_at_screen"].action({}, Ctx(cfg)) is None


def test_a_confirm_function_without_ctx_still_works():
    tool = Tool("t", "d", lambda: "ok", confirm=lambda args: f"do {args['x']}")
    assert tool.action({"x": "it"}, object()) == "do it"


def test_claude_gets_the_image_as_base64():
    calls = []

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            block = type("Block", (), {"type": "text", "text": "A terminal with a Python traceback."})()
            return type("Resp", (), {"content": [block]})()

    client = type("Client", (), {"messages": Messages()})()
    text = screen.claude_vision(cfg_with("auto", "sk-test"), client=client)(PNG, "describe")
    assert text == "A terminal with a Python traceback."
    image, prompt = calls[0]["messages"][0]["content"]
    assert image["source"] == {"type": "base64", "media_type": "image/png",
                               "data": base64.b64encode(PNG).decode()}
    assert prompt == {"type": "text", "text": "describe"}


def test_a_local_vision_model_gets_the_image_through_ollama():
    requests = []

    def post(url, json=None, timeout=None):
        requests.append((url, json))
        return type("Resp", (), {"raise_for_status": lambda self: None,
                                 "json": lambda self: {"message": {"content": "Hello"}}})()

    text = screen.ollama_vision(cfg_with("ollama", "", "qwen2.5vl:7b"), post=post)(PNG, "read this")
    assert text == "Hello"
    url, body = requests[0]
    assert url.endswith("/api/chat") and body["model"] == "qwen2.5vl:7b"
    assert body["messages"][0]["images"] == [base64.b64encode(PNG).decode()]


def test_the_portal_is_tried_first_then_a_screenshot_command():
    def portal():
        raise screen.ScreenError("no portal")

    def run(args, **kwargs):
        if args[0] == "grim":
            return subprocess.CompletedProcess(args, 0, stdout=PNG, stderr=b"")
        raise FileNotFoundError(args[0])

    assert screen.capture(portal=portal, run=run) == PNG


def test_no_way_to_take_a_screenshot_is_an_error():
    def portal():
        raise screen.ScreenError("no portal")

    def run(args, **kwargs):
        raise FileNotFoundError(args[0])

    with pytest.raises(screen.ScreenError):
        screen.capture(portal=portal, run=run)


def test_a_big_screenshot_is_shrunk_to_the_long_side():
    image = pytest.importorskip("PIL.Image")
    out = io.BytesIO()
    image.new("RGB", (3840, 2160), "white").save(out, "PNG")
    small = image.open(io.BytesIO(screen.shrink_png(out.getvalue(), 1568)))
    assert small.size == (1568, 882)


def test_a_small_screenshot_is_left_alone():
    image = pytest.importorskip("PIL.Image")
    out = io.BytesIO()
    image.new("RGB", (800, 600), "white").save(out, "PNG")
    assert screen.shrink_png(out.getvalue(), 1568) == out.getvalue()
