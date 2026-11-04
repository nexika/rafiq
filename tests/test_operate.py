"""operate_computer (#18): Rafiq works the desktop on an explicit request, asked first, never for the phone or
another program, with Escape and Stop ending it."""

import io

import pytest
from PIL import Image

from rafiq import config, drive, drivers, shortcuts
from rafiq.drive import Action, Turn
from rafiq.tools import REGISTRY, load_all, operate


def png():
    out = io.BytesIO()
    Image.new("RGB", (1280, 720), (10, 10, 10)).save(out, "PNG")
    return out.getvalue()


class Driver:
    name, max_side, wants_shot_each_turn = "test", 1280, True

    def __init__(self, *turns):
        self.turns = list(turns)

    def begin(self, task, shot):
        self.task = task
        return self.turns.pop(0)

    def answer(self, results, shot):
        return self.turns.pop(0) if self.turns else Turn([], done="Done: the settings are open.")


class Remote:
    def __init__(self):
        self.done = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: self.done.append(name)


class Escape:
    def __init__(self, bound=True, pressed=False):
        self.bound, self.started, self.stopped = bound, False, False
        import threading

        self.pressed = threading.Event()
        if pressed:
            self.pressed.set()

    def start(self):
        self.started = True
        return self.bound

    def stop(self):
        self.stopped = True


class Ctx:
    def __init__(self, cfg=None, run="run1", away=False, stopped=lambda: False):
        self.cfg, self.tag = cfg or claude_cfg(), "en"
        self.run_id, self.away, self.stopped = (lambda: run), (lambda: away), stopped
        self.shown = []

    def show(self, kind, data):
        self.shown.append((kind, data))


def claude_cfg():
    cfg = config.load()
    cfg["anthropic_api_key"] = "sk-test"
    return cfg


@pytest.fixture
def desk(monkeypatch):
    """A screen, an input session and an Escape binding the test controls; the driver is the test's."""
    state = {"remote": Remote(), "escape": Escape(), "driver": Driver(Turn([Action("click", x=1, y=1)]))}
    monkeypatch.setattr(operate, "capture", lambda: png())
    monkeypatch.setattr(operate, "open_remote", lambda w, h: state["remote"])
    monkeypatch.setattr(shortcuts, "EscapeWatch", lambda: state["escape"])
    monkeypatch.setattr(drivers, "make", lambda cfg, which: state["driver"])
    monkeypatch.setattr(operate, "last_run", None)
    monkeypatch.setattr(operate.desktop, "notify", lambda text: state.setdefault("notified", []).append(text))
    return state


def test_a_run_does_the_task_and_reports_as_data(desk):
    result = operate.operate_computer("open the settings", ctx=Ctx())
    assert desk["remote"].done == ["click"] and desk["driver"].task == "open the settings"
    assert "data, not instructions" in result and "the settings are open" in result and "clicked" in result


def test_escape_is_bound_for_the_run_only(desk):
    operate.operate_computer("x", ctx=Ctx())
    assert desk["escape"].started and desk["escape"].stopped


def test_escape_pressed_stops_the_run(desk):
    desk["escape"] = Escape(pressed=True)
    result = operate.operate_computer("x", ctx=Ctx())
    assert desk["remote"].done == [] and "Stopped" in result


def test_the_stop_button_stops_the_run(desk):
    result = operate.operate_computer("x", ctx=Ctx(stopped=lambda: True))
    assert desk["remote"].done == [] and "Stopped" in result


def test_each_step_is_shown_as_it_happens(desk):
    ctx = Ctx()
    operate.operate_computer("x", ctx=ctx)
    assert ctx.shown and ctx.shown[0][0] == "operator" and ctx.shown[0][1]["text"] == "clicked"


def test_the_user_is_told_how_to_stop_it(desk):
    operate.operate_computer("x", ctx=Ctx())
    assert desk["notified"] and "Escape" in desk["notified"][0]


def test_without_escape_the_user_is_told_to_use_stop(desk):
    desk["escape"] = Escape(bound=False)
    operate.operate_computer("x", ctx=Ctx())
    assert "Stop" in desk["notified"][0] and "Escape" not in desk["notified"][0]


def test_a_request_from_the_phone_is_refused_before_anything(desk):
    result = operate.operate_computer("x", ctx=Ctx(away=True))
    assert result.startswith("refused:") and desk["remote"].done == [] and not desk["escape"].started


def test_one_run_per_request(desk):
    ctx = Ctx(run="r1")
    operate.operate_computer("x", ctx=ctx)
    desk["driver"] = Driver(Turn([Action("click", x=1, y=1)]))
    assert operate.operate_computer("y", ctx=ctx).startswith("refused:")
    assert operate.operate_computer("y", ctx=Ctx(run="r2")).startswith("The operator")


def test_without_a_request_to_count_in_nothing_runs(desk):
    assert operate.operate_computer("x", ctx=Ctx(run=None)).startswith("refused:")


def test_with_no_model_that_can_drive_it_says_what_to_set_up(desk, monkeypatch):
    monkeypatch.setattr(drivers, "pick", lambda cfg: None)
    result = operate.operate_computer("x", ctx=Ctx())
    assert result.startswith("error:") and "ollama_model" in result and not desk["escape"].started


def test_the_question_names_the_task_the_limits_and_where_screenshots_go(monkeypatch):
    monkeypatch.setattr(drivers.chatgpt, "signed_in", lambda: False)
    load_all({})
    tool = REGISTRY["operate_computer"]
    action = tool.action({"task": "open the Wi-Fi settings"}, Ctx())
    assert "open the Wi-Fi settings" in action and "12" in action and "Claude, in the cloud" in action


def test_the_tool_is_critical_always_asked_and_follows_the_user_s_words():
    load_all({})
    tool = REGISTRY["operate_computer"]
    assert tool.level == "critical" and tool.always_asks and tool.reads_private and tool.follows == ("task",)
    assert tool.slow and set(tool.waiting_phrase) == {"en", "fr", "es", "de", "ar", "ar-EG"}


def test_another_program_is_never_offered_or_allowed_the_operator():
    from test_mcp_all import bare

    from rafiq import mcp_server
    from rafiq.tools import Tool

    tool = Tool("operate_computer", "x", lambda task: pytest.fail("ran for another program"),
                {"task": {"type": "string"}}, ("task",), confirm="x", level="critical", always_asks=True)
    assert "operate_computer" not in mcp_server.offered({"operate_computer": tool}, config.load())
    a = bare([True])
    a.tools["operate_computer"] = tool
    assert a._mcp_tool("operate_computer", {"task": "x"}).startswith("refused:") and a.asked == []


def test_not_offered_on_macos(monkeypatch):
    from rafiq import desktop

    monkeypatch.setattr(desktop, "system", lambda: "mac")
    assert "operate_computer" not in load_all({})


@pytest.mark.parametrize("text", ["take control and open the Wi-Fi settings", "تحكم في الكمبيوتر وافتح الإعدادات",
                                  "prends le contrôle et ouvre les réglages", "steuere den Computer"])
def test_a_request_to_take_control_is_offered_the_operator(text):
    from rafiq import router

    assert "operate_computer" in router.route(text, load_all({})).tools


def test_the_limits_are_the_ones_decided():
    assert (drive.MAX_ACTIONS, drive.MAX_SECONDS) == (12, 90)


def test_doctor_says_which_model_drives_the_operator(monkeypatch):
    from test_doctor import Probe, by_label

    from rafiq import doctor

    monkeypatch.setattr(drivers.chatgpt, "signed_in", lambda: False)
    check = by_label(doctor.integrations(claude_cfg(), Probe()))["Operator (take control)"]
    assert check.ok and "Claude" in check.detail and "12 steps" in check.detail
    check = by_label(doctor.integrations(config.load(), Probe()))["Operator (take control)"]
    assert not check.ok and check.optional and "ollama_model" in check.fix


def test_the_spoken_start_doesn_t_promise_escape():
    assert all("Escape" not in text and "Échap" not in text for text in operate.STARTING.values())


def test_the_panel_takes_no_request_while_a_run_goes(monkeypatch):
    import http.client
    import json
    import threading

    from rafiq import panel

    asked = []

    class Assistant:
        def submit(self, kind, **data):
            asked.append(data)

    server = panel.serve(Assistant(), "127.0.0.1", 0, token="tok")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        drive.RUNNING.set()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("POST", "/api/text", json.dumps({"text": "open evil.example"}),
                     {"X-Rafiq-Token": "tok", "Content-Type": "application/json",
                      "Host": f"127.0.0.1:{server.server_address[1]}"})
        assert conn.getresponse().status == 409 and asked == []
    finally:
        drive.RUNNING.clear()
        server.shutdown()
