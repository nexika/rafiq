from types import SimpleNamespace

import pytest
from test_brain_panel import panel, request  # noqa: F401  (fixture)

from rafiq import config, lang
from rafiq.tasks import AgentResult, Cancelled, ClaudeAgent, Tasks, TooManyTasks, cost_of
from rafiq.tools import Tool, background, load_all


def run_now(fn):
    """Run a task on the calling thread, so a test sees it finish."""
    fn()


class FakeAgent:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.goals = result, error, []

    def run(self, goal, tag, cancelled=None):
        self.goals.append((goal, tag))
        if cancelled is not None and cancelled.is_set():
            raise Cancelled(0.004)
        if self.error:
            raise self.error
        return self.result


def make_tasks(agent, **kwargs):
    done, events = [], []
    tasks = Tasks(agent, on_done=done.append, on_event=lambda kind, data: events.append((kind, data)),
                  spawn=kwargs.pop("spawn", run_now), clock=lambda: 1000.0, **kwargs)
    return tasks, done, events


# ---------------------------------------------------------------------- the task list

def test_a_finished_task_keeps_its_result_and_cost_and_is_handed_back():
    agent = FakeAgent(AgentResult("The Cowboy 4 is the best under 2000 euros.", 0.0123))
    tasks, done, _ = make_tasks(agent)
    task = tasks.start("best e-bike under 2000 euros", "en")
    assert agent.goals == [("best e-bike under 2000 euros", "en")]
    assert task.state == "done" and task.result.startswith("The Cowboy 4") and task.cost == 0.0123
    assert done == [task]


def test_a_failing_task_is_marked_failed_not_lost():
    tasks, done, _ = make_tasks(FakeAgent(error=RuntimeError("network down")))
    task = tasks.start("research something", "fr")
    assert task.state == "failed" and "network down" in task.error
    assert done == [task]


def test_every_change_is_shown_in_the_panel():
    tasks, _, events = make_tasks(FakeAgent(AgentResult("done", 0.5)))
    tasks.start("look into it", "en")
    states = [data["state"] for kind, data in events if kind == "task"]
    assert states == ["running", "done"]
    assert events[-1][1]["cost"] == 0.5 and events[-1][1]["goal"] == "look into it"


def test_only_a_few_tasks_run_at_once():
    tasks, _, _ = make_tasks(FakeAgent(), spawn=lambda fn: None, limit=2)   # never finish
    tasks.start("one", "en")
    tasks.start("two", "en")
    with pytest.raises(TooManyTasks):
        tasks.start("three", "en")
    assert [t.goal for t in tasks.all()] == ["one", "two"]


# ---------------------------------------------------------------------- the tools the brain uses

def ctx_with(tasks):
    return SimpleNamespace(tasks=tasks, tag="ar-EG")


def test_starting_a_task_from_the_brain_uses_the_request_language():
    agent = FakeAgent(AgentResult("ok", 0.01))
    tasks, _, _ = make_tasks(agent, spawn=lambda fn: None)
    answer = background.start_background_task("best e-bike", ctx=ctx_with(tasks))
    assert answer.startswith("started") and agent.goals == [] and tasks.all()[0].tag == "ar-EG"


def test_the_status_names_each_task_its_state_and_cost():
    tasks, _, _ = make_tasks(FakeAgent(AgentResult("The Cowboy 4.", 0.0123)))
    tasks.start("best e-bike", "en")
    status = background.background_tasks(ctx=ctx_with(tasks))
    assert "best e-bike" in status and "done" in status and "$0.0123" in status and "Cowboy" in status


def test_with_no_tasks_the_status_says_so():
    tasks, _, _ = make_tasks(FakeAgent())
    assert background.background_tasks(ctx=ctx_with(tasks)) == "no background tasks"


def test_without_claude_the_tools_say_background_tasks_are_off():
    ctx = SimpleNamespace(tasks=None, tag="en")
    assert background.start_background_task("x", ctx=ctx).startswith("error")
    assert background.background_tasks(ctx=ctx).startswith("error")


def test_the_task_tools_are_registered_and_run_without_asking():
    tools = load_all({})
    for name in ("start_background_task", "background_tasks"):
        assert tools[name].action({"goal": "x"}) is None


def test_the_finished_task_phrase_exists_in_every_language():
    for tag in lang.NAMES:
        assert "Cowboy" in lang.phrase("task_done", tag, goal="e-bike", result="Cowboy")
        assert "e-bike" in lang.phrase("task_failed", tag, goal="e-bike")


# ---------------------------------------------------------------------- cost

def test_cost_uses_the_price_per_million_tokens():
    prices = {"claude-haiku-5-5": [0.10, 0.50]}
    assert cost_of("claude-haiku-5-5", 1_000_000, 200_000, prices) == pytest.approx(0.20)


def test_an_unknown_model_has_no_cost():
    assert cost_of("claude-new", 1000, 1000, {}) is None


# ---------------------------------------------------------------------- the Claude agent loop

def block(kind, **fields):
    data = {"type": kind, **fields}
    return SimpleNamespace(**data, model_dump=lambda: data)


class FakeClaude:
    """A scripted client: each call returns the next response; the requests are kept."""
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def response(content, inp, out):
    return SimpleNamespace(content=content, usage=SimpleNamespace(input_tokens=inp, output_tokens=out))


def agent_tools(log):
    def search(query):
        log.append(query)
        return "- Cowboy 4: an e-bike at 1990 euros"

    return {"web_search": Tool("web_search", "search", search, {"query": {"type": "string"}}),
            "power": Tool("power", "power off", lambda: "off", confirm="shut down the computer")}


def test_the_agent_searches_then_answers_and_adds_up_the_cost():
    log = []
    client = FakeClaude([
        response([block("tool_use", id="t1", name="web_search", input={"query": "e-bike under 2000"})], 1000, 100),
        response([block("text", text="The Cowboy 4, at 1990 euros.")], 3000, 200),
    ])
    cfg = config.load()
    cfg["tasks"]["model"] = "claude-haiku-5-5"
    agent = ClaudeAgent(cfg, agent_tools(log), client=client)
    result = agent.run("best e-bike under 2000 euros", "en")
    assert result.text == "The Cowboy 4, at 1990 euros." and log == ["e-bike under 2000"]
    assert result.cost == pytest.approx((4000 * 0.10 + 300 * 0.50) / 1_000_000)
    assert client.requests[1]["messages"][2]["content"][0]["content"].startswith("- Cowboy 4")


def test_the_agent_never_gets_tools_that_change_something():
    client = FakeClaude([response([block("text", text="ok")], 10, 10)])
    ClaudeAgent(config.load(), agent_tools([]), client=client).run("x", "en")
    assert [t["name"] for t in client.requests[0]["tools"]] == ["web_search"]


def test_the_agent_answers_in_the_request_language():
    client = FakeClaude([response([block("text", text="ok")], 10, 10)])
    ClaudeAgent(config.load(), agent_tools([]), client=client).run("x", "ar-EG")
    assert "Egyptian Arabic" in client.requests[0]["messages"][0]["content"]


def test_the_agent_gives_up_after_too_many_steps():
    endless = [response([block("tool_use", id=f"t{i}", name="web_search", input={"query": "q"})], 1, 1)
               for i in range(50)]
    with pytest.raises(RuntimeError):
        ClaudeAgent(config.load(), agent_tools([]), client=FakeClaude(endless)).run("x", "en")


def test_a_background_task_reports_under_its_own_run_id():
    tasks, _, events = make_tasks(FakeAgent(AgentResult("done", None)))
    task = tasks.start("find a flight", "en")
    assert {d["run_id"] for k, d in events if k == "task"} == {f"task-{task.id}"}


# ---------------------------------------------------------------------- cancelling

def later():
    """A spawn that keeps the task's work for the test to run, so it can be cancelled first."""
    waiting = []
    return waiting, waiting.append


def test_a_cancelled_task_ends_cancelled_with_its_cost_so_far_and_is_handed_back():
    waiting, spawn = later()
    tasks, done, events = make_tasks(FakeAgent(AgentResult("never", 1.0)), spawn=spawn)
    task = tasks.start("find a flight", "fr")
    assert tasks.cancel(task.id) is True
    waiting[0]()
    assert task.state == "cancelled" and task.cost == 0.004 and done == [task]
    last = [d for k, d in events if k == "task"][-1]
    assert last["state"] == "cancelled" and last["run_id"] == f"task-{task.id}"


def test_cancelling_a_finished_or_unknown_task_changes_nothing():
    tasks, _, _ = make_tasks(FakeAgent(AgentResult("done", 0.01)))
    task = tasks.start("find a flight", "en")
    assert tasks.cancel(task.id) is False and task.state == "done"
    assert tasks.cancel("nope") is False


def test_the_agent_makes_no_further_model_call_once_cancelled():
    import threading

    cancelled = threading.Event()
    cancelled.set()
    client = FakeClaude([response([block("text", text="ok")], 10, 10)])
    with pytest.raises(Cancelled) as stopped:
        ClaudeAgent(config.load(), agent_tools([]), client=client).run("x", "en", cancelled)
    assert client.requests == [] and stopped.value.cost in (0, 0.0, None)


def test_the_agent_finishes_the_tool_it_is_running_then_runs_no_other_and_keeps_the_cost():
    import threading

    cancelled, log = threading.Event(), []
    tools = agent_tools(log)
    search = tools["web_search"].fn

    def search_then_cancel(query):
        cancelled.set()                  # cancelled while this search ran
        return search(query)

    tools["web_search"].fn = search_then_cancel
    client = FakeClaude([
        response([block("tool_use", id="t1", name="web_search", input={"query": "one"}),
                  block("tool_use", id="t2", name="web_search", input={"query": "two"})], 1000, 100),
        response([block("text", text="never")], 1, 1),
    ])
    cfg = config.load()
    cfg["tasks"]["model"] = "claude-haiku-5-5"
    with pytest.raises(Cancelled) as stopped:
        ClaudeAgent(cfg, tools, client=client).run("x", "en", cancelled)
    assert log == ["one"] and len(client.requests) == 1
    assert stopped.value.cost == pytest.approx((1000 * 0.10 + 100 * 0.50) / 1_000_000)


def test_the_cancel_tool_cancels_the_task_named():
    waiting, spawn = later()
    tasks, _, _ = make_tasks(FakeAgent(AgentResult("x", 0.0)), spawn=spawn)
    task = tasks.start("find a flight", "en")
    ctx = SimpleNamespace(tasks=tasks, tag="en")
    tools = load_all(config.load())
    assert "cancelling" in tools["cancel_task"].run({"id": task.id}, ctx)
    assert tools["cancel_task"].run({"id": "nope"}, ctx).startswith("error")


def test_a_cancelled_task_is_said_in_its_language():
    from rafiq.assistant import Assistant
    from rafiq.tasks import Task

    a = object.__new__(Assistant)
    a.jobs = []
    a.submit = lambda kind, **data: a.jobs.append((kind, data))
    a._task_done(Task("ab12cd", "trouver un vol", "fr", 0.0, state="cancelled"))
    assert a.jobs == [("say", {"text": lang.phrase("task_cancelled", "fr", goal="trouver un vol"), "tag": "fr"})]


# ---------------------------------------------------------------------- in the panel

class CancellingTasks:
    def __init__(self):
        self.cancelled = []

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        return task_id == "ab12cd"


@pytest.fixture
def panel_with_tasks(panel):  # noqa: F811
    assistant, port = panel
    assistant.ctx = SimpleNamespace(tasks=CancellingTasks())
    return assistant, port


def test_the_panel_cancels_a_task_and_says_whether_there_was_one(panel_with_tasks):
    assistant, port = panel_with_tasks
    assert request(port, "/api/task/cancel", {"id": "ab12cd"}) == (200, '{"cancelled": true}')
    assert request(port, "/api/task/cancel", {"id": "zz99zz"}) == (200, '{"cancelled": false}')
    assert assistant.ctx.tasks.cancelled == ["ab12cd", "zz99zz"]


def test_the_panel_refuses_to_cancel_without_the_token_or_from_another_host(panel_with_tasks):
    assistant, port = panel_with_tasks
    assert request(port, "/api/task/cancel", {"id": "ab12cd"}, token=None)[0] == 403
    assert request(port, "/api/task/cancel", {"id": "ab12cd"}, host="evil.example")[0] == 403
    assert request(port, "/api/task/cancel", {"id": 3})[0] == 400
    assert assistant.ctx.tasks.cancelled == []


def test_stop_leaves_the_background_tasks_running(panel_with_tasks):
    assistant, port = panel_with_tasks
    assert request(port, "/api/stop", {})[0] == 200
    assert assistant.ctx.tasks.cancelled == []
