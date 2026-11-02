"""The chatgpt brain (#196): Rafiq's prompt and tools on the Responses API with the plan's token, streamed."""

import copy
import json
import time

import httpx
import pytest
from test_brain_panel import tools_with

from rafiq import chatgpt, config
from rafiq.brain import Brain


def signed_in(**changes):
    record = {"client_id": "oaiapp_123", "subject": "user-1", "email": "sam@example.com", "access_token": "at-1",
              "refresh_token": "rt-1", "id_token": "id", "expires_at": time.time() + 3600,
              "scopes": chatgpt.SCOPE.split()}
    record.update(changes)
    chatgpt._save(chatgpt.folder() / "account.json", record)


def sse(*events) -> list[str]:
    lines = []
    for event in events:
        lines += [f"event: {event['type']}", f"data: {json.dumps(event)}", ""]
    return lines


def said(text: str) -> list[dict]:
    """A streamed answer in words: two deltas, then the completed response holding the message."""
    half = len(text) // 2
    return sse({"type": "response.created", "response": {"id": "resp_1"}},
               {"type": "response.output_text.delta", "delta": text[:half]},
               {"type": "response.output_text.delta", "delta": text[half:]},
               {"type": "response.completed", "response": {"output": [
                   {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}]}})


def calls(*items) -> list[dict]:
    """A response that asks for tools: a reasoning item, then each (call id, name, arguments)."""
    output = [{"type": "reasoning", "id": "rs_1", "summary": []}]
    output += [{"type": "function_call", "id": f"fc_{i}", "call_id": cid, "name": name, "arguments": args,
                "status": "completed"} for i, (cid, name, args) in enumerate(items)]
    return sse({"type": "response.completed", "response": {"output": output}})


class Stream:
    def __init__(self, status=200, lines=(), body=None):
        self.status_code, self.lines, self.body = status, list(lines), body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        yield from self.lines

    def read(self):
        return json.dumps(self.body).encode() if self.body is not None else b""

    def json(self):
        if self.body is None:
            raise ValueError("no body")
        return self.body


@pytest.fixture
def responses(monkeypatch):
    """A scripted Responses API: each request gets the next Stream; the requests are kept."""
    script, requests = [], []

    def stream(method, url, headers=None, json=None, timeout=None):
        requests.append({"method": method, "url": url, "headers": headers, "body": copy.deepcopy(json)})
        answer = script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, Stream) else Stream(lines=answer)

    monkeypatch.setattr(httpx, "stream", stream)
    return script, requests


def make_brain(mode="chatgpt", confirm=None, **keys):
    cfg = config.load()
    cfg["brain"]["mode"] = mode
    cfg["brain"]["chatgpt_model"] = "gpt-6.1-sol"
    cfg.update(keys)
    events = []
    brain = Brain(cfg, tools_with([]), confirm or (lambda action, tag: True),
                  on_event=lambda kind, data: events.append((kind, data)))
    return brain, events


# ---------------------------------------------------------------------- answering

def test_chatgpt_answers_on_the_plan_streamed_and_unstored(responses):
    script, requests = responses
    script.append(said("It's sunny in Cairo."))
    signed_in()
    brain, _ = make_brain()
    spoken = []
    assert brain.think("weather in Cairo", "en", on_speech=lambda s, tag: spoken.append(s)) == "It's sunny in Cairo."
    assert spoken == ["It's sunny in Cairo."]
    (request,) = requests
    assert request["method"] == "POST" and request["url"] == "https://api.openai.com/v1/responses"
    assert request["headers"]["Authorization"] == "Bearer at-1"
    body = request["body"]
    assert body["store"] is False and body["stream"] is True
    assert body["model"] == "gpt-6.1-sol"
    assert body["instructions"].startswith("You are Rafiq")
    assert isinstance(body["input"], list)
    assert all(item.get("role") != "system" for item in body["input"])
    assert body["input"][-1]["role"] == "user" and "weather in Cairo" in body["input"][-1]["content"]
    # Fields the plan route refuses are never sent.
    for refused in ("max_output_tokens", "temperature", "top_p", "previous_response_id", "user", "metadata",
                    "truncation"):
        assert refused not in body


def test_chatgpt_tools_are_offered_in_a_namespace(responses):
    script, requests = responses
    script.append(said("hi"))
    signed_in()
    brain, _ = make_brain()
    brain.cfg["brain"]["routing"] = False
    brain.think("hello", "en")
    (namespace,) = requests[0]["body"]["tools"]
    assert namespace["type"] == "namespace" and namespace["name"] == "rafiq"
    names = {t["name"] for t in namespace["tools"]}
    assert names == {"power", "get_time", "web_search"}
    assert all(t["type"] == "function" and "parameters" in t for t in namespace["tools"])


def test_chatgpt_tool_calls_run_and_their_results_go_back_without_stored_ids(responses):
    script, requests = responses
    script += [calls(("call_1", "get_time", "{}")), said("It's twenty to four.")]
    signed_in()
    brain, _ = make_brain()
    assert brain.think("what time is it", "en") == "It's twenty to four."
    second = requests[1]["body"]["input"]
    assert second[-2] == {"type": "function_call", "call_id": "call_1", "name": "get_time", "arguments": "{}"}
    assert second[-1] == {"type": "function_call_output", "call_id": "call_1",
                          "output": "Saturday 10 October 2026, 15:40"}
    # Nothing that would point at a stored item: no reasoning item, no item ids.
    assert all(item.get("type") != "reasoning" and "id" not in item for item in second)


def test_a_namespaced_tool_name_runs_the_tool(responses):
    script, _ = responses
    script += [calls(("c1", "rafiq.get_time", "{}")), said("Twenty to four.")]
    signed_in()
    brain, events = make_brain()
    assert brain.think("time?", "en") == "Twenty to four."
    assert any(kind == "tool" and data.get("name") == "get_time" for kind, data in events)


def test_chatgpt_tools_that_change_something_are_confirmed(responses):
    script, _ = responses
    asked = []
    script += [calls(("c", "power", "{}")), said("Okay, I didn't.")]
    signed_in()
    brain, _ = make_brain(confirm=lambda action, tag: asked.append(action) or False)
    assert brain.think("shut down", "en") == "Okay, I didn't."
    assert asked == ["shut down the computer"]


def test_an_answer_cut_off_before_it_completed_is_not_taken_as_an_answer(responses):
    script, _ = responses
    script.append(sse({"type": "response.output_text.delta", "delta": "It's sun"}))
    signed_in()
    brain, events = make_brain()
    answer = brain.think("weather", "en")
    assert answer != "It's sun"
    assert any(kind == "error" for kind, _ in events)


# ---------------------------------------------------------------------- the plan's limit and errors

def test_the_plans_limit_mid_stream_falls_back_and_says_where_to_look(responses, monkeypatch):
    script, _ = responses
    script.append(sse({"type": "response.failed", "response": {"error": {
        "code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}}}))
    signed_in()

    class Ollama:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"role": "assistant", "content": "from ollama"}}

    monkeypatch.setattr(httpx, "post", lambda url, **kw: Ollama())
    brain, events = make_brain("auto")
    assert brain.think("hello", "en") == "from ollama"
    errors = [data for kind, data in events if kind == "error"]
    assert any("chatgpt.com/settings/usage" in json.dumps(data) for data in errors)


def test_the_plans_limit_with_nothing_to_fall_back_on_is_said_plainly(responses):
    script, _ = responses
    script.append(Stream(429, body={"error": {"code": "subscription_sharing_usage_limit_exceeded"}}))
    signed_in()
    brain, _ = make_brain()
    from rafiq import lang

    assert brain.think("hello", "en") == lang.phrase("chatgpt_limit", "en")


def test_an_expired_access_token_is_refreshed_once_and_the_request_retried(responses, monkeypatch):
    script, requests = responses
    script += [Stream(401, body={"detail": "expired"}), said("hi")]
    signed_in()

    class Token:
        status_code = 200

        def json(self):
            return {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600, "scope": chatgpt.SCOPE}

    monkeypatch.setattr(httpx, "post", lambda url, **kw: Token())
    brain, _ = make_brain()
    assert brain.think("hello", "en") == "hi"
    assert [r["headers"]["Authorization"] for r in requests] == ["Bearer at-1", "Bearer at-2"]


def test_errors_never_carry_the_token(responses):
    script, _ = responses
    script.append(Stream(403, body={"error": {"code": "subscription_sharing_user_not_eligible"}}))
    signed_in(access_token="at-secret-123")
    brain, events = make_brain()
    brain.think("hello", "en")
    text = json.dumps(events)
    assert "at-secret-123" not in text and "rt-1" not in text
    assert "Plus or Pro" in text


def test_a_network_error_is_a_brain_error(responses):
    script, _ = responses
    script.append(httpx.ConnectError("down"))
    signed_in()
    brain, events = make_brain()
    from rafiq import lang

    assert brain.think("hello", "en") == lang.phrase("brain_down", "en")


# ---------------------------------------------------------------------- choosing it

def test_auto_tries_the_chatgpt_plan_first_when_signed_in():
    brain, _ = make_brain("auto", anthropic_api_key="a", openai_api_key="k")
    assert brain.backends() == ["claude", "openai", "ollama"]
    signed_in()
    assert brain.backends() == ["chatgpt", "claude", "openai", "ollama"]


def test_a_sign_in_without_plan_usage_is_not_tried():
    signed_in(scopes=["openid", "offline_access"])
    brain, _ = make_brain("auto")
    assert brain.backends() == ["ollama"]


def test_the_status_says_whether_chatgpt_is_ready():
    brain, _ = make_brain("auto")
    assert brain.status()["ready"]["chatgpt"] is False
    signed_in()
    assert brain.status()["ready"]["chatgpt"] is True
    assert brain.status()["models"]["chatgpt"] == "gpt-6.1-sol"


def test_the_models_are_the_ones_the_account_lists(monkeypatch):
    signed_in()
    seen = []

    class Models:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1", "visibility": "list"},
                               {"slug": "internal", "visibility": "hide"},
                               {"slug": "gpt-6-mini", "visibility": "list"}]}

    def get(url, headers=None, timeout=None, params=None):
        seen.append((url, headers))
        return Models()

    monkeypatch.setattr(httpx, "get", get)
    brain, _ = make_brain()
    assert brain.models("chatgpt") == ["gpt-6-mini", "gpt-6.1-sol"]
    assert seen == [("https://api.openai.com/v1/models", {"Authorization": "Bearer at-1"})]


def test_with_no_model_set_the_accounts_first_listed_model_is_used(responses, monkeypatch):
    script, requests = responses
    script.append(said("hi"))
    signed_in()

    class Models:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"slug": "hidden", "visibility": "hide"}, {"slug": "gpt-6.1-sol", "visibility": "list"},
                               {"slug": "gpt-6-mini", "visibility": "list"}]}

    monkeypatch.setattr(httpx, "get", lambda url, **kw: Models())
    brain, _ = make_brain()
    brain.cfg["brain"]["chatgpt_model"] = ""
    brain.think("hello", "en")
    assert requests[0]["body"]["model"] == "gpt-6.1-sol"


def test_complete_uses_the_plan_for_interpreting(responses):
    script, requests = responses
    script.append(said("Bonjour"))
    signed_in()
    brain, _ = make_brain()
    assert brain.translate("Hello", "en", "fr") == "Bonjour"
    body = requests[0]["body"]
    assert "tools" not in body and body["store"] is False and "interpreter" in body["instructions"]


def test_chatgpt_is_a_brain_mode_in_the_config_and_the_cli(monkeypatch):
    assert "chatgpt" in config.CHOICES["brain.mode"]
    from rafiq import __main__ as cli

    seen = []
    monkeypatch.setattr(cli, "cmd_ask", lambda args: seen.append(args.brain) or 0)
    assert cli.main(["ask", "--brain", "chatgpt", "hi"]) == 0
    assert seen == ["chatgpt"]


def test_signing_out_from_the_cli_when_not_signed_in(capsys):
    from rafiq.__main__ import main

    assert main(["chatgpt-logout"]) == 0
    assert "wasn't signed in" in capsys.readouterr().out


# ---------------------------------------------------------------------- after the plan's limit

LIMIT = {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}


def test_the_limit_after_a_tool_ran_doesnt_run_the_request_again_elsewhere(responses, monkeypatch):
    script, _ = responses
    script += [calls(("c", "power", "{}")), sse({"type": "response.failed", "response": {"error": LIMIT}})]
    signed_in()
    posted = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: posted.append(url))
    log = []
    cfg = config.load()
    cfg["brain"]["mode"], cfg["brain"]["chatgpt_model"], cfg["openai_api_key"] = "auto", "gpt-6.1-sol", "sk-test"
    brain = Brain(cfg, tools_with(log), lambda action, tag: True)
    from rafiq import lang

    assert brain.think("shut down", "en") == lang.phrase("chatgpt_limit", "en")
    assert log == ["shutdown ran"]                  # once: OpenAI and Ollama weren't asked to do it again
    assert posted == []


def test_after_the_limit_auto_leaves_the_plan_out_for_a_while(responses, monkeypatch):
    script, _ = responses
    script.append(Stream(429, body={"error": LIMIT}))
    signed_in()
    now = [1000.0]
    brain, _ = make_brain("auto", openai_api_key="k")
    brain.clock = lambda: now[0]

    class Answer:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "from openai"}}]}

    monkeypatch.setattr(httpx, "post", lambda url, **kw: Answer())
    assert brain.think("hello", "en") == "from openai"
    assert brain.backends() == ["openai", "ollama"]
    now[0] += 1799
    assert brain.backends()[0] == "openai"
    now[0] += 2
    assert brain.backends()[0] == "chatgpt"


def test_a_limit_nested_in_a_stream_error_event_is_the_plans_limit(responses):
    script, _ = responses
    script.append(sse({"type": "error", "error": LIMIT}))
    signed_in()
    brain, _ = make_brain()
    from rafiq import lang

    assert brain.think("hello", "en") == lang.phrase("chatgpt_limit", "en")


def test_a_limit_as_a_top_level_stream_error_event_is_the_plans_limit(responses):
    script, _ = responses
    script.append(sse({"type": "error", **LIMIT}))
    signed_in()
    brain, _ = make_brain()
    from rafiq import lang

    assert brain.think("hello", "en") == lang.phrase("chatgpt_limit", "en")


def test_an_answer_that_came_without_streamed_text_is_still_spoken(responses):
    script, _ = responses
    script.append(sse({"type": "response.completed", "response": {"output": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello there."}]}]}}))
    signed_in()
    brain, _ = make_brain()
    spoken = []
    assert brain.think("hi", "en", on_speech=lambda s, tag: spoken.append(s)) == "Hello there."
    assert spoken == ["Hello there."]
