"""Rafiq's harmless tools for other programs (Claude Desktop, Claude Code, other agents), as an MCP server
over stdio: `rafiq mcp`. Only safe tools marked expose=True are offered, and never one that asks the user first,
since no one is there to answer. Every call is logged.

The protocol is JSON-RPC 2.0, one message per line on stdin and stdout; the few methods a tools-only server
needs are written out here rather than pulling in the MCP SDK."""

from __future__ import annotations

import json
import logging
import sys
from types import SimpleNamespace
from typing import TextIO

from . import __version__, config
from .scheduler import Scheduler
from .tools import Tool, load_all

logger = logging.getLogger(__name__)

# Newest first: a client asking for one of these gets it, any other gets the newest.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


def safe_tools(tools: dict[str, Tool]) -> dict[str, Tool]:
    return {name: t for name, t in tools.items() if t.expose and t.level == "safe" and t.confirm is None}


class Server:
    def __init__(self, cfg: dict, tools: dict[str, Tool] | None = None):
        self.cfg = cfg
        self.tools = safe_tools(tools if tools is not None else load_all(cfg))

    def context(self) -> SimpleNamespace:
        # Read the saved timers afresh on each call; nothing here adds, cancels or rings them, so the
        # running Rafiq stays the only one writing the file.
        alarms = Scheduler(config.data_dir() / "alarms.json", lambda alarm: None)
        return SimpleNamespace(cfg=self.cfg, scheduler=alarms, tag="en")

    def handle(self, message) -> dict | None:
        """Answer one JSON-RPC message; None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST,
                          "not a JSON-RPC 2.0 request")
        if "id" not in message:
            return None
        msg_id, method = message["id"], message["method"]
        params = message.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _result(msg_id, {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "rafiq", "version": __version__},
                "instructions": "Rafiq's read-only tools: time, weather, web search and pages, system status, "
                                "arithmetic and unit conversion."})
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.schema(),
                 "annotations": {"readOnlyHint": True, "destructiveHint": False}} for t in self.tools.values()]})
        if method == "tools/call":
            return self.call(msg_id, params)
        return _error(msg_id, METHOD_NOT_FOUND, f"unknown method {method!r}")

    def call(self, msg_id, params: dict) -> dict:
        name, args = params.get("name"), params.get("arguments") or {}
        found = self.tools.get(name) if isinstance(name, str) else None
        if found is None or not isinstance(args, dict):
            logger.warning("refused %r %s", name, _short(args))
            return _error(msg_id, INVALID_PARAMS, f"no tool {name!r} is offered here")
        try:
            text = found.run(args, self.context())
        except Exception as exc:  # a broken tool answers with an error, the server keeps running
            logger.exception("%s %s failed", name, _short(args))
            return _result(msg_id, _content(f"error: {type(exc).__name__}: {exc}", True))
        logger.info("%s %s -> %s", name, _short(args), _short(text))
        return _result(msg_id, _content(text, text.startswith("error")))

    def serve(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                reply = _error(None, PARSE_ERROR, "not JSON")
            else:
                reply = self.handle(message)
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


def _result(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _content(text: str, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _short(value, limit: int = 200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "..."


def run() -> int:
    """`rafiq mcp`: stdout carries the protocol, so the log goes to stderr and to data/mcp.log."""
    handlers = [logging.StreamHandler(sys.stderr),
                logging.FileHandler(config.data_dir() / "mcp.log", encoding="utf-8")]
    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        server = Server(config.load())
        logger.info("serving %s over stdio", ", ".join(server.tools))
        server.serve()
    except KeyboardInterrupt:
        pass
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()
    return 0
