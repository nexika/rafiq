"""Rafiq's own browser: Chrome, Chromium or Firefox with a separate profile, whose tabs Rafiq can list, read,
bring to the front and close (#50).

`rafiq browser` starts it with a profile under data/browser and a debugging port on 127.0.0.1 (port 0: the
browser picks a free one and writes it into the profile). You sign in there only to the sites you want Rafiq
to read; your everyday browser is never touched. Since Chrome 136 the debugging port works only with such a
separate profile anyway.

Chrome is driven through the Chrome DevTools Protocol, which writes its port to DevToolsActivePort; Firefox
through WebDriver BiDi, which writes it to WebDriverBiDiServer.json. `[browser] engine` picks one ("auto":
Chrome if installed, else Firefox; the tools use whichever of Rafiq's browsers is running).

Rafiq talks only to 127.0.0.1 on the port its own profile names, once the process listening there is this
account's browser started with that profile, and builds every address itself (never from what the browser
says). It only evaluates fixed expressions (TEXT, FIREFOX_INFO), in an isolated world (Chrome) or a sandbox
(Firefox) the page's own scripts can't see or change. Page text and titles are data, never instructions.
While this browser is open, any program on this computer, under any account, can reach the same port: that
is how these protocols work, so it is a separate profile, and not for a computer other people log into.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from . import config

# Google's packages first: Ubuntu's snap Chromium may refuse a profile inside the hidden ~/.config.
PROGRAMS = ("google-chrome-stable", "google-chrome", "chromium-browser", "chromium")
ENGINES = ("auto", "chrome", "firefox")
TEXT = "document.body ? document.body.innerText.slice(0, 200000) : ''"
FIREFOX_INFO = "[document.title, document.visibilityState]"
FIREFOX_TABS_SECONDS = 5        # for reading every tab's title: past it, the rest are listed without one
_ID = re.compile(r"[A-Za-z0-9]{1,64}")
_CONTEXT = re.compile(r"[A-Za-z0-9-]{1,64}")     # Firefox's tab ids are UUIDs; they go in JSON, never in an address
# A new Firefox profile opens welcome and privacy pages first; Rafiq's opens the page asked for. And with
# --remote-debugging-port, Firefox applies its "recommended" automation settings to the profile unless told
# not to: on Firefox 158 they turned off Safe Browsing (phishing and malware warnings), tracking protection
# and certificate revocation updates, in the very profile you sign in to.
FIREFOX_PREFS = {"remote.prefs.recommended": False,
                 "browser.aboutwelcome.enabled": False, "browser.shell.checkDefaultBrowser": False,
                 "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
                 "browser.startup.homepage_override.mstone": "ignore", "trailhead.firstrun.didSeeAboutWelcome": True}
# Sites Rafiq never acts on, only reads: banks, taxes and health, by a word in the site's name or by the
# site itself, plus [browser] read_only. Never complete, so it errs on the side of more: "taxi" is caught too.
READ_ONLY_WORDS = ("bank", "banque", "banco", "sparkasse", "tax", "impot", "steuer", "hacienda", "tributaria",
                   "health", "sante", "salud", "gesundheit", "hospital", "hopital", "clinic", "doctolib", "mychart",
                   "patient")
READ_ONLY_SITES = ("paypal.com", "irs.gov", "hmrc.gov.uk", "elster.de", "impots.gouv.fr", "ameli.fr", "nhs.uk",
                   "revolut.com", "wise.com", "n26.com", "stripe.com", "nbe.com.eg", "cibeg.com", "qnb.com",
                   "emiratesnbd.com", "adcb.com")
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

    def close(self, tab_id: str) -> None: ...


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
    """The Chrome `rafiq browser` started. `get(url) -> str` and `evaluate(ws_url, expression) -> str` are
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

    def close(self, tab_id: str) -> None:
        tab_id = self._checked(tab_id)
        port = self._port()
        try:
            self.get(f"http://127.0.0.1:{port}/json/close/{tab_id}")
        except OSError:
            raise BrowserError("Rafiq's browser did not answer") from None


class BiDiRefused(Exception):
    """Firefox answered with an error ("no such frame", "session not created"): the connection is fine."""


class _BiDi:
    """One WebDriver BiDi session on Firefox's port: session.new on entering, session.end on leaving (Firefox
    allows one session at a time, and keeps one whose socket closed without session.end until it quits).
    Calls are synchronous, on a private event loop; every wait is bounded."""

    def __init__(self, port: int):
        self.url = f"ws://127.0.0.1:{port}/session"
        self.count = 0
        self.ws = self.http = None

    def __enter__(self) -> _BiDi:
        import asyncio

        import aiohttp

        self.loop = asyncio.new_event_loop()

        async def connect():
            self.http = aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=10))
            self.ws = await self.http.ws_connect(self.url, max_msg_size=8 * 1024 * 1024,
                                                 timeout=aiohttp.ClientWSTimeout(ws_close=2))

        try:
            self.loop.run_until_complete(connect())
            # A Firefox just started may take a while; giving up early would leave it a session nobody ends.
            self.call("session.new", {"capabilities": {}}, wait=20)
        except BaseException as exc:
            self._close()
            if isinstance(exc, aiohttp.ClientError):
                raise OSError(type(exc).__name__) from None
            raise
        return self

    def call(self, method: str, params: dict, wait: float = 5) -> dict:
        import asyncio

        import aiohttp

        self.count += 1
        ask = {"id": self.count, "method": method, "params": params}

        async def exchange() -> dict:
            await self.ws.send_json(ask)
            while True:             # events, and answers to calls that timed out, come in between
                message = await asyncio.wait_for(self.ws.receive_json(), wait)
                if message.get("id") == ask["id"]:
                    return message

        try:
            message = self.loop.run_until_complete(exchange())
        except (aiohttp.ClientError, TypeError, ValueError, RuntimeError) as exc:
            raise OSError(type(exc).__name__) from None
        if message.get("type") == "error":
            raise BiDiRefused(str(message.get("error", "refused"))[:100])
        return message.get("result") or {}

    def _close(self) -> None:
        async def close():
            if self.ws is not None:
                await self.ws.close()
            if self.http is not None:
                await self.http.close()

        try:
            self.loop.run_until_complete(close())
        except Exception:            # closing must never raise: it runs on errors and at exit
            pass
        finally:
            self.loop.close()

    def __exit__(self, *exc) -> bool:
        try:
            self.call("session.end", {}, wait=2)
        except Exception:
            pass
        finally:
            self._close()
        return False


def _value(result: dict):
    """The plain value of a script.evaluate result ("success" only)."""
    if result.get("type") != "success":
        return None
    remote = result.get("result") or {}
    if remote.get("type") == "array":
        return [item.get("value") for item in remote.get("value") or []]
    return remote.get("value")


# The BiDi session kept with each of Rafiq's Firefoxes (by profile): (port, session). Firefox gives a tab a new
# id in every session, so the ids Rafiq lists, and pins when asking before closing, are only good in the
# session they came from: one session, kept open, serves every call.
_sessions: dict[str, tuple[int, object]] = {}
_sessions_lock = threading.RLock()     # the voice loop and the satellites' threads share it


class Firefox:
    """The Firefox `rafiq browser` started. `connect(port)` opens a BiDi session (a context manager with
    `call(method, params) -> result`), passed in so tests need neither a browser nor a socket."""

    def __init__(self, profile: Path, connect=_BiDi, listener=_listener):
        self.profile, self.connect, self.listener = profile, connect, listener

    def _run(self, work):
        """work(session) on the session kept with this Firefox, opening it the first time. A failed call ends
        it, so the next call starts a new one; a Firefox restarted on another port gets a new one too."""
        key = str(self.profile)
        try:
            port = self._port()
        except BrowserError:
            with _sessions_lock:
                _forget(key)                 # Firefox is gone: so is any session kept with it
            raise
        with _sessions_lock:
            held = _sessions.get(key)
            if held and held[0] != port:
                _forget(key)
                held = None
            if held is None:
                session = self.connect(port)
                try:
                    session.__enter__()
                except BiDiRefused:
                    raise BrowserError("Rafiq's Firefox still holds a session from an earlier Rafiq, or another "
                                       f"Rafiq is using it: quit that Firefox and {START}") from None
                _sessions[key] = held = (port, session)
            try:
                return work(held[1])
            except (OSError, TimeoutError):
                _forget(key)        # the connection broke: the next call opens a new one, and lists new ids
                raise               # (Firefox's own refusals, such as a tab closed by hand, keep the session)

    def _port(self) -> int:
        try:
            data = json.loads((self.profile / "WebDriverBiDiServer.json").read_text(encoding="utf-8"))
            host, port = data["ws_host"], data["ws_port"]
        except (OSError, ValueError, KeyError, TypeError):
            raise BrowserError(f"Rafiq's browser is not running: {START}") from None
        if host != "127.0.0.1" or isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise BrowserError(f"Rafiq's Firefox names a port Rafiq does not use: {START}")
        owner = self.listener(port)
        if owner is None:                   # the file outlives a crash: nobody may be listening any more
            raise BrowserError(f"Rafiq's browser is not running: {START}")
        uid, cmdline = owner
        if uid != os.getuid() or str(self.profile) not in cmdline:
            raise BrowserError(f"the program on port {port} is not Rafiq's browser: {START}")
        return port

    def _checked(self, tab_id: str) -> str:
        if not _CONTEXT.fullmatch(tab_id or ""):
            raise BrowserError("that tab can't be reached")
        return tab_id

    def _evaluate(self, session, context: str, expression: str):
        return _value(session.call("script.evaluate", {
            "expression": expression, "target": {"context": context, "sandbox": "rafiq"},
            "awaitPromise": False, "resultOwnership": "none"}))

    def tabs(self) -> list[Tab]:
        """The web pages open, the one in front first (the visible one)."""
        def listing(session) -> list[tuple[bool, Tab]]:
            found, deadline = [], time.monotonic() + FIREFOX_TABS_SECONDS
            for ctx in session.call("browsingContext.getTree", {"maxDepth": 0}).get("contexts") or []:
                url, context = str(ctx.get("url") or ""), str(ctx.get("context") or "")
                if urlparse(url).scheme not in ("http", "https"):
                    continue
                title, state = "", ""
                if time.monotonic() < deadline:         # a busy page must not hold up the whole list
                    try:
                        title, state = self._evaluate(session, context, FIREFOX_INFO) or ("", "")
                    except (BiDiRefused, ValueError):   # a tab still loading answers nothing yet
                        pass
                found.append((state == "visible", Tab(context, str(title or ""), url)))
            return found

        try:
            found = self._run(listing)
        except (OSError, TimeoutError, BiDiRefused):
            raise BrowserError("Rafiq's browser did not answer") from None
        return [tab for _, tab in sorted(found, key=lambda pair: not pair[0])]

    def text(self, tab_id: str) -> str:
        tab_id = self._checked(tab_id)
        try:
            return str(self._run(lambda session: self._evaluate(session, tab_id, TEXT)) or "")
        except (OSError, TimeoutError, BiDiRefused):
            raise BrowserError("the tab did not answer; bring it to the front and ask again") from None

    def _context_call(self, method: str, tab_id: str) -> None:
        tab_id = self._checked(tab_id)
        try:
            self._run(lambda session: session.call(method, {"context": tab_id}))
        except (OSError, TimeoutError, BiDiRefused):
            raise BrowserError("Rafiq's browser did not answer, or the tab is gone") from None

    def activate(self, tab_id: str) -> None:
        self._context_call("browsingContext.activate", tab_id)

    def close(self, tab_id: str) -> None:
        self._context_call("browsingContext.close", tab_id)


def _forget(key: str) -> None:
    """End and drop the session kept for a profile (call with _sessions_lock held)."""
    held = _sessions.pop(key, None)
    if held is not None:
        try:
            held[1].__exit__(None, None, None)
        except Exception:            # at exit and after errors: never let ending a session raise
            pass


@atexit.register
def _forget_all() -> None:
    """End the kept sessions when Rafiq exits, so Firefox takes a new one at once."""
    with _sessions_lock:
        for key in list(_sessions):
            _forget(key)


def _host(entry) -> str:
    """A site as written in the settings ("https://MyBank.com/login", "*.mybank.com", "bänk.de") as the
    browser names it ("mybank.com", "xn--bnk-qla.de")."""
    entry = str(entry).strip().casefold()
    host = (urlparse(entry if "//" in entry else "//" + entry).hostname or "").removeprefix("*.").strip(".")
    try:
        return host.encode("idna").decode()
    except UnicodeError:
        return host


def read_only(site: str, cfg: dict) -> bool:
    """Whether Rafiq may only read this site, never act on it."""
    site = site.casefold().rstrip(".")
    extra = cfg.get("browser", {}).get("read_only") or []
    extra = [extra] if isinstance(extra, str) else extra       # read_only = "mybank.com" is one site
    sites = (*READ_ONLY_SITES, *(h for h in map(_host, extra) if h))
    if any(site == s or site.endswith("." + s) for s in sites):
        return True
    return any(word in site for word in READ_ONLY_WORDS)


def profile_dir(engine: str = "chrome") -> Path:
    path = config.data_dir() / "browser" / ("firefox" if engine == "firefox" else "chrome")
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _engine(cfg: dict) -> str:
    engine = str(cfg.get("browser", {}).get("engine") or "auto")
    if engine not in ENGINES:
        raise BrowserError(f"[browser] engine must be one of {', '.join(ENGINES)}, not {engine!r}")
    return engine


def backend(cfg: dict) -> Backend:
    """Rafiq's browser: the engine chosen, or with "auto" whichever of Rafiq's browsers runs (Chrome when
    neither does, so the error says to start it)."""
    engine = _engine(cfg)
    if engine == "firefox" or (engine == "auto" and running("firefox")):
        return Firefox(profile_dir("firefox"))
    return Chrome(profile_dir("chrome"))


def running(engine: str = "chrome", listener=_listener) -> bool:
    """Whether Rafiq's browser of this engine is open: its port file names a port this account's browser,
    started with this profile, listens on (a file left by a crash may name a port another program took)."""
    profile = profile_dir(engine)
    try:
        if engine == "firefox":
            port = json.loads((profile / "WebDriverBiDiServer.json").read_text(encoding="utf-8"))["ws_port"]
            mark = str(profile)
        else:
            port = int((profile / "DevToolsActivePort").read_text(encoding="utf-8").split()[0])
            mark = f"--user-data-dir={profile}"
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        return False
    if isinstance(port, bool) or not isinstance(port, int):
        return False
    owner = listener(port)
    return owner is not None and owner[0] == os.getuid() and mark in owner[1]


def find_chrome(cfg: dict, which=shutil.which) -> str | None:
    chosen = str(cfg.get("browser", {}).get("chrome") or "")
    for name in ([chosen] if chosen else PROGRAMS):
        found = which(name)
        if found:
            return found
    return None


def find_firefox(cfg: dict, which=shutil.which) -> str | None:
    return which(str(cfg.get("browser", {}).get("firefox") or "firefox"))


def find(cfg: dict, which=shutil.which) -> tuple[str, str | None]:
    """The engine to start and its program (None when it is not installed; "auto" when neither is)."""
    engine = _engine(cfg)
    if engine == "auto":
        chrome, firefox = find_chrome(cfg, which), find_firefox(cfg, which)
        return ("chrome", chrome) if chrome else ("firefox", firefox) if firefox else ("auto", None)
    return engine, (find_firefox if engine == "firefox" else find_chrome)(cfg, which)


def missing(engine: str) -> tuple[str, str]:
    """What is missing and what to install, for an engine `find` found no program for."""
    if engine == "firefox":
        return "no Firefox", "install firefox (or set [browser] firefox)"
    if engine == "chrome":
        return "no Chrome or Chromium", "install google-chrome or chromium (or set [browser] chrome)"
    return "no Chrome, Chromium or Firefox", "install google-chrome, chromium or firefox"


def _firefox_profile() -> Path:
    profile = profile_dir("firefox")
    prefs = "".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n" for k, v in FIREFOX_PREFS.items())
    (profile / "user.js").write_text(prefs, encoding="utf-8")
    return profile


def launch(cfg: dict, url: str = "", which=shutil.which, popen=subprocess.Popen) -> list[str]:
    """Start Rafiq's browser, on `url` when it is a web address; returns the command it ran."""
    engine, program = find(cfg, which)
    if not program:
        what, fix = missing(engine)
        raise BrowserError(f"{what} found: {fix}")
    if engine == "firefox" and str(Path(program).resolve()).startswith("/snap/"):
        raise BrowserError("Ubuntu's snap Firefox can't use Rafiq's profile, which lives in a hidden folder: install "
                           "Firefox from Mozilla's package, or use Chrome ([browser] engine = \"chrome\")")
    port = cfg.get("browser", {}).get("port", 0)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port < 65536:
        raise BrowserError(f"[browser] port must be a number from 0 to 65535, not {port!r}")
    if engine == "firefox":
        args = [program, "--no-remote", "--profile", str(_firefox_profile()), "--remote-debugging-port", str(port)]
    else:
        args = [program, f"--user-data-dir={profile_dir()}", f"--remote-debugging-port={port}", "--no-first-run",
                "--no-default-browser-check"]
    if urlparse(url).scheme in ("http", "https"):
        args.append(url)
    popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
          start_new_session=True)
    return args
