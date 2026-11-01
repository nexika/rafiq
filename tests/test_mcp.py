import io
import json

import pytest

from rafiq import config, mcp_server
from rafiq.tools import REGISTRY, Tool, load_all


def rpc(server, method, params=None, id=1):
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id is not None:
        message["id"] = id
    return server.handle(message)


@pytest.fixture(autouse=True)
def no_running_rafiq(monkeypatch):
    """`rafiq mcp` asks the running Rafiq first: here none answers, and no test reaches a real one."""
    import httpx

    def refused(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", refused)


@pytest.fixture
def server():
    return mcp_server.Server(config.load())


def test_every_tool_is_offered_since_203(server):
    names = set(server.tools)
    assert {"get_time", "weather", "calculate", "power", "set_timer", "list_timers", "open_app"} <= names


def test_only_safe_tools_marked_expose_run_in_this_process():
    """The rest go to the running Rafiq, which asks before an act or critical one (#203)."""
    load_all({"google": {"enabled": True}})
    marked = dict(REGISTRY, risky=Tool("risky", "x", lambda: "done", expose=True, confirm="do something"))
    assert "risky" not in mcp_server.safe_tools(marked)
    assert "get_time" in mcp_server.safe_tools(marked)


def test_initialize_names_the_server_and_tools_capability(server):
    reply = rpc(server, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "1"}})
    assert reply["id"] == 1
    result = reply["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "rafiq"
    assert "tools" in result["capabilities"]


def test_initialize_with_an_unknown_version_offers_the_latest(server):
    reply = rpc(server, "initialize", {"protocolVersion": "1999-01-01"})
    assert reply["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSIONS[0]


def test_notifications_get_no_reply(server):
    assert rpc(server, "notifications/initialized", id=None) is None


def test_tools_list_gives_schemas(server):
    tools = rpc(server, "tools/list")["result"]["tools"]
    by_name = {t["name"]: t for t in tools}
    assert by_name["get_time"]["inputSchema"]["type"] == "object"
    assert by_name["get_time"]["description"]
    assert by_name["get_time"]["annotations"]["readOnlyHint"] is True


def test_tools_call_runs_the_tool_and_logs_it(server, caplog):
    caplog.set_level("INFO", logger="rafiq.mcp_server")
    reply = rpc(server, "tools/call", {"name": "calculate", "arguments": {"expression": "2**10"}})
    result = reply["result"]
    assert result["isError"] is False
    assert "1024" in result["content"][0]["text"]
    assert any("calculate" in r.getMessage() for r in caplog.records)


def test_a_tool_error_is_reported_as_a_tool_error(server):
    result = rpc(server, "tools/call", {"name": "get_time", "arguments": {"timezone": "Mars/Olympus"}})["result"]
    assert result["isError"] is True and "unknown time zone" in result["content"][0]["text"]


def test_a_tool_that_raises_does_not_stop_the_server(server, monkeypatch):
    def broken():
        raise RuntimeError("sensor gone")

    monkeypatch.setitem(server.tools, "system_status", Tool("system_status", "x", broken, expose=True))
    result = rpc(server, "tools/call", {"name": "system_status", "arguments": {}})["result"]
    assert result["isError"] is True and "sensor gone" in result["content"][0]["text"]


@pytest.mark.parametrize("name", ["nope", "rm_rf"])
def test_calling_a_tool_that_does_not_exist_is_refused(server, name, caplog):
    caplog.set_level("INFO", logger="rafiq.mcp_server")
    reply = rpc(server, "tools/call", {"name": name, "arguments": {"action": "shutdown"}})
    assert reply["error"]["code"] == -32602
    assert any(name in r.getMessage() for r in caplog.records)


def test_a_tool_turned_off_in_the_settings_is_neither_offered_nor_called():
    cfg = config.load()
    cfg["mcp"]["off"] = ["power", "list_timers"]
    server = mcp_server.Server(cfg)
    assert "power" not in server.tools and "list_timers" not in server.tools
    assert rpc(server, "tools/call", {"name": "power", "arguments": {"action": "shutdown"}})["error"]["code"] == -32602


def test_unknown_methods_are_a_json_rpc_error(server):
    assert rpc(server, "resources/list")["error"]["code"] == -32601
    assert rpc(server, "ping")["result"] == {}


def test_serve_reads_and_writes_one_message_per_line(server):
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
             "not json",
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}), ""]
    out = io.StringIO()
    server.serve(io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700
    assert replies[2]["result"]["tools"]


def test_rafiq_mcp_serves_stdio(monkeypatch, capsys, rafiq_home):
    from rafiq.__main__ import main

    request = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                          "params": {"name": "convert_units", "arguments": {"value": 100, "from_unit": "c",
                                                                            "to_unit": "f"}}})
    monkeypatch.setattr("sys.stdin", io.StringIO(request + "\n"))
    assert main(["mcp"]) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["id"] == 7 and "212" in reply["result"]["content"][0]["text"]
    assert "convert_units" in (rafiq_home / "data" / "mcp.log").read_text()
