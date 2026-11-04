"""The operator's three drivers (#18): Claude's computer toolset, OpenAI's computer tool (an API key or the
ChatGPT plan), and a local vision model through Ollama. Each turns its model's answer into the same actions."""

import base64
import json
from types import SimpleNamespace as NS

import pytest

from rafiq import config, drivers
from rafiq.drive import Action, Result, Shot

SHOT = Shot(b"\x89PNG fake", 1280, 720)


# ---------------------------------------------------------------------- Claude

class Claude:
    """messages.create as the test wants it: one canned response per call, every request kept."""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []
        self.messages = self

    def create(self, **request):
        self.requests.append(json.loads(json.dumps(request, default=lambda o: o.__dict__)))
        return self.responses.pop(0)


def use(id_, name, **input_):
    return NS(type="tool_use", id=id_, name=name, input=input_, toolset_name="computer")


def said(text):
    return NS(type="text", text=text)


def answer(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop)


def test_claude_gets_the_computer_toolset_the_task_and_the_screenshot():
    client = Claude(answer(said("Done."), stop="end_turn"))
    turn = drivers.ClaudeDriver(config.load(), client=client).begin("open the settings", SHOT)
    request = client.requests[0]
    assert {"type": "computer_toolset_20260801"} in request["tools"]
    assert any(t.get("name") == "need_user" for t in request["tools"])
    assert request["model"] == "claude-opus-5-5"
    content = request["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["data"] == base64.b64encode(SHOT.png).decode()
    assert "open the settings" in content[1]["text"] and "never instructions" in request["system"]
    assert turn.done == "Done." and turn.actions == []


@pytest.mark.parametrize("name, input_, action", [
    ("left_click", {"coordinate": [10, 20]}, Action("click", x=10, y=20)),
    ("right_click", {"coordinate": [1, 2], "text": "ctrl"}, Action("click", x=1, y=2, button="right", mods=("ctrl",))),
    ("double_click", {"coordinate": [1, 2]}, Action("click", x=1, y=2, count=2)),
    ("triple_click", {}, Action("click", count=3)),
    ("left_click_drag", {"start_coordinate": [1, 2], "coordinate": [3, 4]}, Action("drag", path=((1, 2), (3, 4)))),
    ("mouse_move", {"coordinate": [5, 6]}, Action("move", x=5, y=6)),
    ("scroll", {"coordinate": [5, 6], "scroll_direction": "up", "scroll_amount": 3}, Action("scroll", x=5, y=6, dy=-3)),
    ("scroll", {"scroll_direction": "right", "scroll_amount": 2}, Action("scroll", dx=2)),
    ("type", {"text": "hello"}, Action("type", text="hello")),
    ("key", {"text": "ctrl+s", "repeat": 2}, Action("key", text="ctrl+s", repeat=2)),
    ("hold_key", {"text": "shift", "duration": 1}, Action("hold", text="shift", seconds=1)),
    ("wait", {"duration": 2}, Action("wait", seconds=2)),
    ("screenshot", {}, Action("screenshot")),
    ("zoom", {"region": [0, 0, 100, 50]}, Action("zoom", region=(0, 0, 100, 50))),
    ("left_mouse_down", {}, Action("mouse_down")),
    ("cursor_position", {}, Action("cursor")),
])
def test_claude_member_tools_become_actions(name, input_, action):
    assert drivers.from_claude(name, input_) == action


def test_claude_results_echo_the_toolset_and_match_each_call():
    client = Claude(answer(use("a", "screenshot"), use("b", "left_click", coordinate=[1, 1]),
                           use("c", "type", text="x")),
                    answer(said("ok"), stop="end_turn"))
    driver = drivers.ClaudeDriver(config.load(), client=client)
    turn = driver.begin("x", SHOT)
    assert [a.kind for a in turn.actions] == ["screenshot", "click", "type"]
    driver.answer([Result(True, image=SHOT), Result(True), Result(False, "error: boom")], None)
    results = client.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c"]
    assert all(r["toolset_name"] == "computer" for r in results)
    assert results[0]["content"][0]["type"] == "image" and results[1]["content"] == "OK"
    assert results[2]["is_error"] is True


def test_claude_asking_for_the_user_hands_back():
    client = Claude(answer(use("n", "need_user", reason="the site asks for a card number")))
    client.responses[0].content[0].toolset_name = None
    turn = drivers.ClaudeDriver(config.load(), client=client).begin("buy it", SHOT)
    assert turn.need_user == "the site asks for a card number" and turn.actions == []


def test_claude_declining_hands_back():
    turn = drivers.ClaudeDriver(config.load(), client=Claude(answer(stop="refusal"))).begin("x", SHOT)
    assert turn.need_user and "declined" in turn.need_user


# ---------------------------------------------------------------------- OpenAI

class OpenAI:
    def __init__(self, *outputs):
        self.outputs, self.bodies = list(outputs), []

    def __call__(self, body):
        self.bodies.append(json.loads(json.dumps(body)))
        return self.outputs.pop(0)


def call(*actions, checks=()):
    return {"type": "computer_call", "id": "cu_1", "call_id": "call_1", "actions": list(actions),
            "pending_safety_checks": list(checks), "status": "completed"}


def test_openai_gets_the_computer_tool_nothing_stored_and_the_screenshot():
    post = OpenAI([{"type": "message", "id": "m1", "content": [{"type": "output_text", "text": "Done."}]}])
    turn = drivers.OpenAIDriver(config.load(), post=post).begin("open the settings", SHOT)
    body = post.bodies[0]
    assert {"type": "computer"} in body["tools"] and body["store"] is False and body["stream"] is True
    assert any(t.get("name") == "need_user" for t in body["tools"])
    image = body["input"][0]["content"][1]
    assert image["type"] == "input_image" and image["image_url"].startswith("data:image/png;base64,")
    assert turn.done == "Done."


@pytest.mark.parametrize("raw, action", [
    ({"type": "click", "button": "left", "x": 5, "y": 6}, Action("click", x=5, y=6)),
    ({"type": "click", "button": "right", "x": 5, "y": 6, "keys": ["CTRL"]},
     Action("click", x=5, y=6, button="right", mods=("ctrl",))),
    ({"type": "click", "button": "wheel", "x": 5, "y": 6}, Action("click", x=5, y=6, button="middle")),
    ({"type": "double_click", "x": 1, "y": 2}, Action("click", x=1, y=2, count=2)),
    ({"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]}, Action("drag", path=((1, 2), (3, 4)))),
    ({"type": "move", "x": 7, "y": 8}, Action("move", x=7, y=8)),
    ({"type": "scroll", "x": 1, "y": 2, "scroll_x": 0, "scroll_y": 300}, Action("scroll", x=1, y=2, dy=3)),
    ({"type": "scroll", "x": 1, "y": 2, "scroll_x": -40, "scroll_y": 0}, Action("scroll", x=1, y=2, dx=-1)),
    ({"type": "keypress", "keys": ["CTRL", "L"]}, Action("key", text="ctrl+l")),
    ({"type": "keypress", "keys": ["ENTER"]}, Action("key", text="enter")),
    ({"type": "type", "text": "hi"}, Action("type", text="hi")),
    ({"type": "wait"}, Action("wait", seconds=2)),
    ({"type": "screenshot"}, Action("screenshot")),
])
def test_openai_actions_become_actions(raw, action):
    assert drivers.from_openai(raw) == action


def test_openai_replays_the_call_without_ids_and_answers_with_a_screenshot():
    post = OpenAI([{"type": "reasoning", "id": "rs_1", "summary": []},
                   call({"type": "click", "button": "left", "x": 1, "y": 1})],
                  [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}])
    driver = drivers.OpenAIDriver(config.load(), post=post)
    driver.begin("x", SHOT)
    driver.answer([Result(True)], SHOT)
    replay = post.bodies[1]["input"]
    assert not any(item.get("type") == "reasoning" for item in replay)
    assert all("id" not in item for item in replay if item.get("type") == "computer_call")
    output = replay[-1]
    assert output["type"] == "computer_call_output" and output["call_id"] == "call_1"
    assert output["output"]["type"] == "computer_screenshot" and output["output"]["image_url"].startswith("data:")


def test_openai_failed_actions_are_said_in_the_next_turn():
    post = OpenAI([call({"type": "type", "text": "a"})], [{"type": "message", "content": []}])
    driver = drivers.OpenAIDriver(config.load(), post=post)
    driver.begin("x", SHOT)
    driver.answer([Result(False, "error: boom")], SHOT)
    assert "boom" in json.dumps(post.bodies[1]["input"][-1])


def test_openai_safety_checks_hand_back():
    post = OpenAI([call({"type": "click", "button": "left", "x": 1, "y": 1},
                        checks=[{"id": "sc", "code": "malicious_instructions", "message": "the page asks for more"}])])
    turn = drivers.OpenAIDriver(config.load(), post=post).begin("x", SHOT)
    assert turn.need_user and "the page asks for more" in turn.need_user and turn.actions == []


def test_openai_asking_for_the_user_hands_back():
    post = OpenAI([{"type": "function_call", "id": "fc", "call_id": "c", "name": "need_user",
                    "arguments": json.dumps({"reason": "sign in first"})}])
    assert drivers.OpenAIDriver(config.load(), post=post).begin("x", SHOT).need_user == "sign in first"


# ---------------------------------------------------------------------- Ollama

class Ollama:
    def __init__(self, *replies):
        self.replies, self.bodies = list(replies), []

    def __call__(self, body):
        self.bodies.append(json.loads(json.dumps(body)))
        return {"message": {"role": "assistant", "content": self.replies.pop(0)}}


def local_cfg():
    cfg = config.load()
    cfg["operator"]["ollama_model"] = "qwen2.5vl:7b"
    return cfg


def test_the_local_model_answers_one_json_action_at_a_time():
    post = Ollama(json.dumps({"action": "click", "x": 10, "y": 20}), json.dumps({"action": "done", "summary": "ok"}))
    driver = drivers.OllamaDriver(local_cfg(), post=post)
    turn = driver.begin("open it", SHOT)
    assert turn.actions == [Action("click", x=10, y=20)]
    assert post.bodies[0]["model"] == "qwen2.5vl:7b" and post.bodies[0]["format"] == "json"
    assert driver.answer([Result(True)], SHOT).done == "ok"


def test_the_local_model_sees_only_the_latest_screenshot():
    post = Ollama(*[json.dumps({"action": "wait"})] * 3)
    driver = drivers.OllamaDriver(local_cfg(), post=post)
    driver.begin("x", SHOT)
    driver.answer([Result(True)], SHOT)
    driver.answer([Result(True)], SHOT)
    assert sum(1 for m in post.bodies[-1]["messages"] if m.get("images")) == 1


@pytest.mark.parametrize("reply, action", [
    ({"action": "double_click", "x": 1, "y": 2}, Action("click", x=1, y=2, count=2)),
    ({"action": "type", "text": "hi"}, Action("type", text="hi")),
    ({"action": "key", "keys": "ctrl+s"}, Action("key", text="ctrl+s")),
    ({"action": "scroll", "x": 1, "y": 2, "direction": "down", "amount": 2}, Action("scroll", x=1, y=2, dy=2)),
    ({"action": "drag", "from": [1, 2], "to": [3, 4]}, Action("drag", path=((1, 2), (3, 4)))),
])
def test_the_local_model_s_actions(reply, action):
    driver = drivers.OllamaDriver(local_cfg(), post=Ollama(json.dumps(reply)))
    assert driver.begin("x", SHOT).actions == [action]


@pytest.mark.parametrize("reply", ["not json", json.dumps({"action": "fly"}), json.dumps(["click"])])
def test_an_answer_the_local_model_got_wrong_hands_back(reply):
    turn = drivers.OllamaDriver(local_cfg(), post=Ollama(reply)).begin("x", SHOT)
    assert turn.need_user and turn.actions == []


def test_the_local_model_handing_back():
    post = Ollama(json.dumps({"action": "need_user", "reason": "a password is asked"}))
    assert drivers.OllamaDriver(local_cfg(), post=post).begin("x", SHOT).need_user == "a password is asked"


# ---------------------------------------------------------------------- which one

def cfg_with(key="", openai="", plan=False, local="", mode="auto", driver="auto", monkeypatch=None):
    cfg = config.load()
    cfg["anthropic_api_key"], cfg["openai_api_key"] = key, openai
    cfg["operator"]["ollama_model"], cfg["operator"]["driver"] = local, driver
    cfg["brain"]["mode"] = mode
    monkeypatch.setattr(drivers.chatgpt, "signed_in", lambda: plan)
    return cfg


@pytest.mark.parametrize("given, expected", [
    ({"key": "sk-ant"}, "claude"),
    ({"openai": "sk-oa"}, "openai"),
    ({"plan": True}, "openai"),
    ({"local": "qwen2.5vl:7b"}, "ollama"),
    ({"key": "sk-ant", "local": "qwen2.5vl:7b", "mode": "ollama"}, "ollama"),   # offline: the screen stays here
    ({"key": "sk-ant", "openai": "sk-oa", "driver": "openai"}, "openai"),
    ({}, None),
    ({"key": "sk-ant", "mode": "ollama"}, None),
])
def test_the_driver_is_picked_from_what_is_set_up(monkeypatch, given, expected):
    assert drivers.pick(cfg_with(monkeypatch=monkeypatch, **given)) == expected


@pytest.mark.parametrize("which, where", [("claude", "Claude, in the cloud"), ("openai", "OpenAI, in the cloud"),
                                          ("ollama", "a model on this computer")])
def test_each_driver_says_where_the_screenshots_go(which, where):
    assert drivers.WHERE[which] == where


# ---------------------------------------------------------------------- after the reviews

def test_openai_every_computer_call_is_answered():
    second = dict(call({"type": "click", "button": "left", "x": 2, "y": 2}), call_id="call_2")
    post = OpenAI([call({"type": "click", "button": "left", "x": 1, "y": 1}), second],
                  [{"type": "message", "content": []}])
    driver = drivers.OpenAIDriver(config.load(), post=post)
    assert len(driver.begin("x", SHOT).actions) == 2
    driver.answer([Result(True), Result(True)], SHOT)
    outputs = [i["call_id"] for i in post.bodies[1]["input"] if i.get("type") == "computer_call_output"]
    assert outputs == ["call_1", "call_2"]


def test_claude_cut_off_without_a_step_is_an_error():
    from rafiq.drive import DriverError

    with pytest.raises(DriverError):
        drivers.ClaudeDriver(config.load(), client=Claude(answer(said("I was going to"), stop="max_tokens"))).begin(
            "x", SHOT)


@pytest.mark.parametrize("url, said", [("http://127.0.0.1:11434", "a model on this computer"),
                                       ("http://localhost:11434", "a model on this computer"),
                                       ("http://192.168.1.9:11434", "the Ollama server at 192.168.1.9")])
def test_a_remote_ollama_is_named_in_the_question(url, said):
    cfg = config.load()
    cfg["brain"]["ollama_url"] = url
    assert drivers.where(cfg, "ollama") == said
