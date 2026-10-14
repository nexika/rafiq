import json
import os
import stat

import pytest

from rafiq import browser, config, doctor
from rafiq.tools import load_all

PORT = 35511
ARTICLE = {"context": "29fa56f2-2b1a-49a7-b919-fd208c32e070", "url": "https://news.example.com/olive",
           "title": "Olive oil - an article", "visible": False, "text": "Olive oil is pressed from olives."}
INBOX = {"context": "8f5f16c9-25f1-4ae4-9da8-005cc5882cba", "url": "https://mail.example.com/inbox",
         "title": "Inbox (3) - Mail", "visible": True, "text": "Three new messages."}
PRIVATE = {"context": "0a1b2c3d-0000-4000-8000-000000000001", "url": "about:preferences", "title": "Settings",
           "visible": False, "text": ""}


@pytest.fixture(autouse=True)
def no_kept_sessions():
    """Rafiq keeps one BiDi session per Firefox; each test starts without one."""
    browser._sessions.clear()
    yield
    browser._sessions.clear()


class FakeFirefox:
    """What Firefox's WebDriver BiDi answers (as seen on a real Firefox 158), without a browser or a socket.
    As in the real one, each session gives the tabs new ids: an id from one session means nothing in another."""

    def __init__(self, tabs=(ARTICLE, INBOX, PRIVATE), fail_on=None, taken=False, exit_error=None):
        self.tabs, self.fail_on, self.taken, self.exit_error = [dict(t) for t in tabs], fail_on, taken, exit_error
        self.calls, self.sessions, self.ports = [], [], []

    def connect(self, port):
        self.ports.append(port)
        return Session(self, len(self.ports))


class Session:
    def __init__(self, ff, number):
        self.ff, self.prefix = ff, f"s{number}-"

    def __enter__(self):
        if self.ff.taken:
            raise browser.BiDiRefused("session not created")   # an earlier session is still open in Firefox
        self.ff.sessions.append("new")
        return self

    def __exit__(self, *exc):
        self.ff.sessions.append("end")
        if self.ff.exit_error:
            raise self.ff.exit_error
        return False

    def call(self, method, params):
        self.ff.calls.append((method, params))
        if method == self.ff.fail_on:
            raise OSError("connection lost")              # the socket, not Firefox, failed
        if method == "browsingContext.getTree":
            return {"contexts": [{"context": self.prefix + t["context"], "url": t["url"], "children": None}
                                 for t in self.ff.tabs]}
        asked = (params.get("target") or params).get("context", "")
        tab = next((t for t in self.ff.tabs if self.prefix + t["context"] == asked), None)
        if tab is None:
            raise browser.BiDiRefused("no such frame")    # Firefox answers: no such tab (closed, or another session's)
        if method == "script.evaluate":
            if params["expression"] == browser.FIREFOX_INFO:
                value = [{"type": "string", "value": tab["title"]},
                         {"type": "string", "value": "visible" if tab["visible"] else "hidden"}]
                return {"type": "success", "result": {"type": "array", "value": value}}
            return {"type": "success", "result": {"type": "string", "value": tab["text"]}}
        if method == "browsingContext.activate":
            for t in self.ff.tabs:
                t["visible"] = t is tab
            return {}
        if method == "browsingContext.close":
            self.ff.tabs.remove(tab)
            return {}
        raise AssertionError(method)


def ff_profile(rafiq_home, host="127.0.0.1", port=PORT):
    profile = browser.profile_dir("firefox")
    (profile / "WebDriverBiDiServer.json").write_text(json.dumps({"ws_host": host, "ws_port": port}), encoding="utf-8")
    return profile


def ours(profile):
    return lambda port: (os.getuid(), ["/usr/lib64/firefox/firefox", "--no-remote", "--profile", str(profile)])


def make(profile, fake, listener=None):
    return browser.Firefox(profile, connect=fake.connect, listener=listener or ours(profile))


@pytest.fixture
def firefox(rafiq_home):
    fake = FakeFirefox()
    return fake, make(ff_profile(rafiq_home), fake)


def cfg_on(engine="firefox"):
    cfg = config.load()
    cfg["browser"].update(enabled=True, engine=engine)
    return cfg


# ---------------------------------------------------------------- the backend


def test_web_tabs_are_listed_with_the_one_in_front_first(firefox):
    fake, backend = firefox
    tabs = backend.tabs()
    assert [t.title for t in tabs] == ["Inbox (3) - Mail", "Olive oil - an article"]
    assert all(t.url.startswith("https://") for t in tabs)


def listed(backend, words):
    return next(t for t in backend.tabs() if words in t.title)


def test_titles_and_text_are_read_in_a_sandbox_with_fixed_expressions_only(firefox):
    fake, backend = firefox
    backend.text(listed(backend, "Olive").id)
    evaluated = [p for m, p in fake.calls if m == "script.evaluate"]
    assert evaluated and all(p["target"]["sandbox"] == "rafiq" for p in evaluated)
    assert {p["expression"] for p in evaluated} <= {browser.FIREFOX_INFO, browser.TEXT}


def test_text_is_read_from_the_tab_asked_for(firefox):
    fake, backend = firefox
    assert backend.text(listed(backend, "Olive").id) == ARTICLE["text"]


def test_switching_and_closing_reach_the_tab_listed(firefox):
    fake, backend = firefox
    backend.activate(listed(backend, "Olive").id)
    assert listed(backend, "Olive") == backend.tabs()[0]
    backend.close(listed(backend, "Inbox").id)
    assert [t["context"] for t in fake.tabs] == [ARTICLE["context"], PRIVATE["context"]]


def test_one_session_serves_every_call_so_listed_ids_stay_valid(firefox):
    fake, backend = firefox
    tab = listed(backend, "Olive")
    backend.text(tab.id)
    backend.activate(tab.id)
    other = make(backend.profile, fake)                  # a new backend, as each tool call makes, shares it
    assert other.text(tab.id) == ARTICLE["text"]
    assert fake.sessions == ["new"]


def test_a_failed_call_ends_the_session_and_the_next_call_opens_a_new_one(rafiq_home):
    fake = FakeFirefox(fail_on="browsingContext.activate")
    backend = make(ff_profile(rafiq_home), fake)
    with pytest.raises(browser.BrowserError):
        backend.activate(listed(backend, "Olive").id)
    assert fake.sessions == ["new", "end"]
    backend.tabs()
    assert fake.sessions == ["new", "end", "new"]


def test_firefox_gone_drops_the_kept_session(rafiq_home):
    fake = FakeFirefox()
    backend = make(ff_profile(rafiq_home), fake)
    backend.tabs()
    (backend.profile / "WebDriverBiDiServer.json").unlink()
    with pytest.raises(browser.BrowserError, match="not running"):
        backend.tabs()
    assert fake.sessions == ["new", "end"] and not browser._sessions


def test_kept_sessions_end_when_rafiq_exits(rafiq_home):
    fake = FakeFirefox()
    make(ff_profile(rafiq_home), fake).tabs()
    browser._forget_all()
    assert fake.sessions == ["new", "end"]


def test_a_new_firefox_on_another_port_gets_a_new_session(rafiq_home):
    fake = FakeFirefox()
    backend = make(ff_profile(rafiq_home), fake)
    backend.tabs()
    ff_profile(rafiq_home, port=PORT + 1)                # Firefox restarted
    backend.tabs()
    assert fake.ports == [PORT, PORT + 1] and fake.sessions == ["new", "end", "new"]


@pytest.mark.parametrize("tab_id", ["../x", "a b", "x" * 65, ""])
def test_a_context_id_with_odd_characters_is_refused(firefox, tab_id):
    fake, backend = firefox
    with pytest.raises(browser.BrowserError):
        backend.text(tab_id)
    assert not fake.ports


# ---------------------------------------------------------------- the port


def test_firefox_not_running_says_to_start_it(rafiq_home):
    fake = FakeFirefox()
    backend = make(browser.profile_dir("firefox"), fake)
    with pytest.raises(browser.BrowserError, match="rafiq browser"):
        backend.tabs()
    assert not fake.ports


@pytest.mark.parametrize("host, port", [("0.0.0.0", PORT), ("evil.example.com", PORT), ("127.0.0.1", 0),
                                        ("127.0.0.1", "9222"), ("127.0.0.1", 70000)])
def test_a_port_file_pointing_anywhere_else_is_refused(rafiq_home, host, port):
    fake = FakeFirefox()
    backend = make(ff_profile(rafiq_home, host, port), fake)
    with pytest.raises(browser.BrowserError):
        backend.tabs()
    assert not fake.ports


def test_a_port_held_by_another_account_or_program_is_refused(rafiq_home):
    profile = ff_profile(rafiq_home)
    for owner in ((12345678, ["firefox", "--profile", str(profile)]), (os.getuid(), ["python3", "fake.py"]), None):
        fake = FakeFirefox()
        with pytest.raises(browser.BrowserError):
            make(profile, fake, lambda port, owner=owner: owner).tabs()
        assert not fake.ports


def test_only_the_port_in_the_profile_is_contacted(firefox):
    fake, backend = firefox
    backend.tabs()
    assert fake.ports == [PORT]


# ---------------------------------------------------------------- the tools on Firefox


def test_the_tools_read_and_close_through_firefox(rafiq_home, monkeypatch):
    fake = FakeFirefox()
    backend = make(ff_profile(rafiq_home), fake)
    monkeypatch.setattr(browser, "backend", lambda cfg: backend)
    tools, ctx = load_all({"browser": {"enabled": True}}), type("Ctx", (), {"cfg": cfg_on()})()
    assert "Three new messages." in tools["read_browser_tab"].run({}, ctx)
    args = {"tab": "olive"}
    assert "news.example.com" in tools["close_browser_tabs"].action(args, ctx)
    assert "closed 1 tab" in tools["close_browser_tabs"].run(args, ctx)
    assert [t["context"] for t in fake.tabs] == [INBOX["context"], PRIVATE["context"]]


# ---------------------------------------------------------------- which browser


def test_the_engine_setting_picks_the_backend(rafiq_home):
    assert isinstance(browser.backend(cfg_on("firefox")), browser.Firefox)
    assert isinstance(browser.backend(cfg_on("chrome")), browser.Chrome)


def test_auto_uses_firefox_when_rafiqs_firefox_is_the_one_running(rafiq_home, monkeypatch):
    monkeypatch.setattr(browser, "running", lambda engine="chrome", listener=None: engine == "firefox")
    assert isinstance(browser.backend(cfg_on("auto")), browser.Firefox)
    monkeypatch.setattr(browser, "running", lambda engine="chrome", listener=None: False)
    assert isinstance(browser.backend(cfg_on("auto")), browser.Chrome)


def test_firefox_is_running_only_when_this_account_listens_on_its_port(rafiq_home):
    profile = ff_profile(rafiq_home)
    assert browser.running("firefox", listener=ours(profile))
    assert not browser.running("firefox", listener=lambda port: None)


# ---------------------------------------------------------------- starting Rafiq's Firefox


class Launched:
    def __call__(self, args, **kwargs):
        self.args, self.kwargs = args, kwargs


def which_of(*found):
    return lambda name: f"/usr/bin/{name}" if name in found else None


def test_launch_uses_rafiqs_own_firefox_profile_and_a_loopback_port(rafiq_home):
    popen = Launched()
    browser.launch(cfg_on("firefox"), "https://example.com/", which=which_of("firefox"), popen=popen)
    args = popen.args
    assert args[0] == "/usr/bin/firefox" and "--no-remote" in args
    assert args[args.index("--profile") + 1] == str(browser.profile_dir("firefox"))
    assert args[args.index("--remote-debugging-port") + 1] == "0"
    assert not any(a.startswith(("--remote-allow-hosts", "--remote-allow-origins")) for a in args)
    assert args[-1] == "https://example.com/" and not popen.kwargs.get("shell")


def test_the_firefox_profile_is_private_and_skips_the_first_run_pages(rafiq_home):
    browser.launch(cfg_on("firefox"), which=which_of("firefox"), popen=Launched())
    profile = browser.profile_dir("firefox")
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700
    assert "browser.aboutwelcome.enabled" in (profile / "user.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "--remote-allow-hosts=*", "javascript:1"])
def test_firefox_opens_only_web_addresses(rafiq_home, url):
    popen = Launched()
    browser.launch(cfg_on("firefox"), url, which=which_of("firefox"), popen=popen)
    assert url not in popen.args


def test_auto_starts_chrome_when_installed_and_firefox_otherwise(rafiq_home):
    chrome, firefox = Launched(), Launched()
    browser.launch(cfg_on("auto"), which=which_of("google-chrome", "firefox"), popen=chrome)
    browser.launch(cfg_on("auto"), which=which_of("firefox"), popen=firefox)
    assert chrome.args[0].endswith("google-chrome") and firefox.args[0].endswith("firefox")


def test_firefox_asked_for_but_missing_says_what_to_install(rafiq_home):
    with pytest.raises(browser.BrowserError, match="firefox"):
        browser.launch(cfg_on("firefox"), which=which_of("google-chrome"), popen=Launched())


def test_a_wrong_engine_is_explained(rafiq_home):
    with pytest.raises(browser.BrowserError, match="engine"):
        browser.launch(cfg_on("safari"), which=which_of("firefox"), popen=Launched())


# ---------------------------------------------------------------- doctor


class Probe(doctor.Probe):
    def __init__(self, programs=()):
        self.programs = set(programs)

    def which(self, program):
        return f"/usr/bin/{program}" if program in self.programs else None


def test_doctor_with_firefox_chosen_and_installed_is_ok():
    check = {c.label: c for c in doctor.integrations(cfg_on("firefox"), Probe({"firefox"}))}["Browser tabs"]
    assert check.ok and "firefox" in check.detail


def test_doctor_with_firefox_chosen_but_missing_says_what_to_install():
    check = {c.label: c for c in doctor.integrations(cfg_on("firefox"), Probe({"chromium"}))}["Browser tabs"]
    assert not check.ok and "firefox" in check.fix


# ---------------------------------------------------------------- after the review of the Firefox backend


def test_the_profile_keeps_safe_browsing_and_tracking_protection_on(rafiq_home):
    """With --remote-debugging-port, Firefox turns off Safe Browsing, tracking protection and revocation
    updates for automation (seen on Firefox 158) unless remote.prefs.recommended is false."""
    browser.launch(cfg_on("firefox"), which=which_of("firefox"), popen=Launched())
    assert 'user_pref("remote.prefs.recommended", false);' in (
        browser.profile_dir("firefox") / "user.js").read_text(encoding="utf-8")


def test_a_tab_closed_by_hand_keeps_the_session_and_the_listed_ids(firefox):
    fake, backend = firefox
    tab = listed(backend, "Olive")
    fake.tabs = [t for t in fake.tabs if t["title"] != ARTICLE["title"]]
    with pytest.raises(browser.BrowserError):
        backend.text(tab.id)
    assert backend.text(listed(backend, "Inbox").id) == INBOX["text"] and fake.sessions == ["new"]


def test_a_session_left_open_by_an_earlier_rafiq_says_how_to_fix_it(rafiq_home):
    fake = FakeFirefox(taken=True)
    with pytest.raises(browser.BrowserError, match="quit that Firefox"):
        make(ff_profile(rafiq_home), fake).tabs()


def test_an_error_while_ending_a_session_never_escapes(rafiq_home):
    fake = FakeFirefox(exit_error=RuntimeError("Concurrent call to receive() is not allowed"))
    make(ff_profile(rafiq_home), fake).tabs()
    browser._forget_all()
    assert fake.sessions == ["new", "end"] and not browser._sessions


def test_listing_gives_up_on_titles_when_its_time_is_up(firefox, monkeypatch):
    fake, backend = firefox
    monkeypatch.setattr(browser, "FIREFOX_TABS_SECONDS", 0)
    tabs = backend.tabs()
    assert len(tabs) == 2 and all(t.title == "" for t in tabs)
    assert not [m for m, _ in fake.calls if m == "script.evaluate"]


def test_auto_ignores_a_leftover_port_file_now_held_by_another_program(rafiq_home):
    ff_profile(rafiq_home)
    assert not browser.running("firefox", listener=lambda port: (os.getuid(), ["node", "dev-server.js"]))


def test_ubuntus_snap_firefox_is_refused_with_the_reason(rafiq_home):
    with pytest.raises(browser.BrowserError, match="snap"):
        browser.launch(cfg_on("firefox"), which=lambda name: "/snap/bin/firefox", popen=Launched())
