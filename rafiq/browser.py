"""Rafiq's own browser: Chrome or Chromium with a separate profile, whose tabs Rafiq can list, read and bring
to the front through the Chrome DevTools Protocol (#50).

`rafiq browser` starts it with --user-data-dir under data/browser and a debugging port on 127.0.0.1 (port 0:
Chrome picks a free one and writes it to the profile's DevToolsActivePort). You sign in there only to the
sites you want Rafiq to read; your everyday browser is never touched. Since Chrome 136 the debugging port
works only with such a separate profile anyway.

Rafiq talks only to 127.0.0.1 on the port its own profile names, once the process listening there is this
account's browser started with that profile and /json/version agrees, and builds every address itself
(never from what the browser's JSON says). It only ever evaluates one fixed expression, TEXT, which reads
the page's text, in an isolated world the page's own scripts can't see or change. Page text and titles are
data, never instructions. While this browser is open, any program on this computer, under any account, can
reach the same port: that is how the protocol works, so it is a separate profile, and not for a computer
other people log into.

Firefox (WebDriver BiDi) is to come as a second Backend.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from . import config

# Google's packages first: Ubuntu's snap Chromium may refuse a profile inside the hidden ~/.config.
PROGRAMS = ("google-chrome-stable", "google-chrome", "chromium-browser", "chromium")
TEXT = "document.body ? document.body.innerText.slice(0, 200000) : ''"
_ID = re.compile(r"[A-Za-z0-9]{1,64}")
START = "start it with `rafiq browser`"


class BrowserError(Exception):
    """Why Rafiq's browser could not be reached or asked, in words the model can pass on."""


@dataclass
class Tab:
    id: str
    title: str
    url: str

    @property
    def site(self) -> str:
        return urlparse(self.url).hostname or ""


class Backend(Protocol):
    def tabs(self) -> list[Tab]: ...

    def text(self, tab_id: str) -> str: ...

    def activate(self, tab_id: str) -> None: ...


def _http_get(url: str) -> str:
    import httpx

    try:
        resp = httpx.get(url, timeout=3, trust_env=False)    # trust_env: no proxy ever sees these calls
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise OSError(type(exc).__name__) from None
    return resp.text


def _ws_evaluate(ws_url: str, expression: str) -> str:
    """`expression` on one tab, in an isolated world: the page's scripts can't see it run or change what it
    reads. No Origin header is sent, so Chrome's own origin check needs no --remote-allow-origins."""
    import asyncio

    import aiohttp

    async def ask() -> str:
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(ws_url, max_msg_size=8 * 1024 * 1024) as ws:
                calls = 0

                async def call(method: str, params: dict) -> dict:
                    nonlocal calls
                    calls += 1
                    await ws.send_json({"id": calls, "method": method, "params": params})
                    while True:             # events come in between; only our answer counts
                        message = await asyncio.wait_for(ws.receive_json(), 5)
                        if message.get("id") == calls:
                            if "error" in message:
                                raise OSError(str((message["error"] or {}).get("message", "refused"))[:100])
                            return message.get("result") or {}

                frame = (await call("Page.getFrameTree", {}))["frameTree"]["frame"]["id"]
                world = await call("Page.createIsolatedWorld",
                                   {"frameId": frame, "worldName": "rafiq", "grantUniversalAccess": False})
                result = await call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                         "contextId": world["executionContextId"]})
                return str((result.get("result") or {}).get("value") or "")

    try:
        return asyncio.run(ask())
    except (aiohttp.ClientError, KeyError, TypeError) as exc:
        raise OSError(type(exc).__name__) from None


def _listener(port: int) -> tuple[int, list[str]] | None:
    """The account and command line of the process listening on 127.0.0.1:`port`; None when there is none,
    or it is another account's (whose sockets don't show which process holds them)."""
    import psutil

    try:
        for conn in psutil.net_connections("tcp"):
            if (conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port and conn.pid
                    and conn.laddr.ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")):
                process = psutil.Process(conn.pid)
                return process.uids().real, process.cmdline()
    except (psutil.Error, OSError):
        return None
    return None


class Chrome:
    """The browser `rafiq browser` started. `get(url) -> str` and `evaluate(ws_url, expression) -> str` are
    the network, passed in so tests need neither a browser nor a socket."""

    def __init__(self, profile: Path, get=_http_get, evaluate=_ws_evaluate, listener=_listener):
        self.profile, self.get, self.evaluate, self.listener = profile, get, evaluate, listener

    def _port(self) -> int:
        try:
            lines = (self.profile / "DevToolsActivePort").read_text(encoding="utf-8").split()
            port, path = int(lines[0]), lines[1]
        except (OSError, ValueError, IndexError):
            raise BrowserError(f"Rafiq's browser is not running: {START}") from None
        if not 0 < port < 65536:
            raise BrowserError(f"Rafiq's browser is not running: {START}")
        owner = self.listener(port)
        if owner is None:                   # the file outlives a crash: nobody may be listening any more
            raise BrowserError(f"Rafiq's browser is not running: {START}")
        uid, cmdline = owner
        if uid != os.getuid() or f"--user-data-dir={self.profile}" not in cmdline:
            raise BrowserError(f"the program on port {port} is not Rafiq's browser: {START}")
        try:
            version = json.loads(self.get(f"http://127.0.0.1:{port}/json/version"))
        except (OSError, ValueError):
            raise BrowserError(f"Rafiq's browser is not running: {START}") from None
        if not str(version.get("webSocketDebuggerUrl", "")).endswith(path):
            raise BrowserError(f"the program on port {port} is not Rafiq's browser: {START}")
        return port

    def tabs(self) -> list[Tab]:
        """The web pages open, the one in front first (Chrome lists them by last use)."""
        port = self._port()
        try:
            targets = json.loads(self.get(f"http://127.0.0.1:{port}/json/list"))
        except (OSError, ValueError):
            raise BrowserError("Rafiq's browser did not answer") from None
        return [Tab(str(t.get("id", "")), str(t.get("title") or ""), str(t.get("url") or ""))
                for t in targets if isinstance(t, dict) and t.get("type") == "page"
                and urlparse(str(t.get("url") or "")).scheme in ("http", "https")]

    def _checked(self, tab_id: str) -> str:
        if not _ID.fullmatch(tab_id):
            raise BrowserError("that tab can't be reached")
        return tab_id

    def text(self, tab_id: str) -> str:
        tab_id = self._checked(tab_id)
        port = self._port()
        try:
            return self.evaluate(f"ws://127.0.0.1:{port}/devtools/page/{tab_id}", TEXT)
        except (OSError, TimeoutError):
            raise BrowserError("the tab did not answer; bring it to the front and ask again") from None

    def activate(self, tab_id: str) -> None:
        tab_id = self._checked(tab_id)
        port = self._port()
        try:
            self.get(f"http://127.0.0.1:{port}/json/activate/{tab_id}")
        except OSError:
            raise BrowserError("Rafiq's browser did not answer") from None


def profile_dir() -> Path:
    path = config.data_dir() / "browser" / "chrome"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def backend(cfg: dict) -> Backend:
    return Chrome(profile_dir())


def running(listener=_listener) -> bool:
    """Whether Rafiq's browser is open: its port file names a port this account's browser listens on."""
    try:
        port = int((profile_dir() / "DevToolsActivePort").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return False
    owner = listener(port)
    return owner is not None and owner[0] == os.getuid()


def find_chrome(cfg: dict, which=shutil.which) -> str | None:
    chosen = str(cfg.get("browser", {}).get("chrome") or "")
    for name in ([chosen] if chosen else PROGRAMS):
        found = which(name)
        if found:
            return found
    return None


def launch(cfg: dict, url: str = "", which=shutil.which, popen=subprocess.Popen) -> list[str]:
    """Start Rafiq's browser, on `url` when it is a web address; returns the command it ran."""
    program = find_chrome(cfg, which)
    if not program:
        raise BrowserError("no Chrome or Chromium found: install google-chrome or chromium "
                           "(or set [browser] chrome)")
    port = cfg.get("browser", {}).get("port", 0)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port < 65536:
        raise BrowserError(f"[browser] port must be a number from 0 to 65535, not {port!r}")
    args = [program, f"--user-data-dir={profile_dir()}", f"--remote-debugging-port={port}", "--no-first-run",
            "--no-default-browser-check"]
    if urlparse(url).scheme in ("http", "https"):
        args.append(url)
    popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
          start_new_session=True)
    return args
