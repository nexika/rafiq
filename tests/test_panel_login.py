"""The panel's pages never carry its token (#222). `rafiq panel` opens /#key=<one-time key>; the page trades the
key for a session secret it keeps in this tab's sessionStorage (scoped to the panel's port, unlike a cookie)
and sends it as a header. Rafiq's own programs keep sending the token; another user's program is refused."""

import http.client
import json
import threading
from importlib import resources

import pytest
from test_brain_panel import FakeAssistant

from rafiq import __main__ as cli
from rafiq import config, panel
from rafiq.panel import serve

TOKEN = "t" * 40


@pytest.fixture
def site():
    assistant = FakeAssistant()
    server = serve(assistant, "127.0.0.1", 0, TOKEN, screen_token="s" * 40)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield assistant, server.server_address[1]
    server.shutdown()


def call(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


def new_key(port):
    status, _, body = call(port, "POST", "/api/panel-key", {"X-Rafiq-Token": TOKEN, "Content-Type": "application/json"},
                           "{}")
    assert status == 200
    return json.loads(body)["key"]


def redeem(port, key):
    return call(port, "POST", "/api/panel-key/redeem", {"X-Rafiq-Panel": "1", "Content-Type": "application/json"},
                json.dumps({"key": key}))


def session(port):
    status, _, body = redeem(port, new_key(port))
    assert status == 200
    return json.loads(body)["session"]


def as_page(secret, **more):
    return {"X-Rafiq-Session": secret, "Content-Type": "application/json", **more}


# ---------------------------------------------------------------------- the pages

@pytest.mark.parametrize("path", ["/", "/subscriptions"])
def test_the_pages_are_served_without_the_token_or_anything_secret(site, path):
    _, port = site
    status, headers, body = call(port, "GET", path)
    assert status == 200 and TOKEN not in body and "__TOKEN__" not in body
    assert "Set-Cookie" not in headers


@pytest.mark.parametrize("name", ["panel.html", "subscriptions.html"])
def test_the_pages_take_the_key_from_after_the_hash_and_keep_the_session_in_this_tab(name):
    page = resources.files("rafiq").joinpath(name).read_text(encoding="utf-8")
    assert "X-Rafiq-Token" not in page and "token=" not in page and "__TOKEN__" not in page
    assert "location.hash" in page and "history.replaceState" in page and "sessionStorage" in page
    assert "X-Rafiq-Session" in page and "rafiq panel" in page
    assert "EventSource" not in page and "document.cookie" not in page


# ---------------------------------------------------------------------- keys and sessions

def test_a_key_becomes_a_session_once(site):
    _, port = site
    key = new_key(port)
    status, headers, body = redeem(port, key)
    assert status == 200 and len(json.loads(body)["session"]) >= 32 and "Set-Cookie" not in headers
    assert redeem(port, key)[0] == 403                            # used: gone


def test_each_key_gets_a_session_of_its_own(site):
    _, port = site
    assert session(port) != session(port)


def test_a_key_not_used_within_two_minutes_is_gone(site, monkeypatch):
    _, port = site
    now = [1000.0]
    monkeypatch.setattr(panel.time, "monotonic", lambda: now[0])
    key = new_key(port)
    now[0] += panel.KEY_SECONDS + 1
    assert redeem(port, key)[0] == 403


def test_a_wrong_key_or_one_without_the_page_s_header_is_refused(site):
    _, port = site
    assert redeem(port, "x" * 32)[0] == 403
    key = new_key(port)
    assert call(port, "POST", "/api/panel-key/redeem", {"Content-Type": "application/json"},
                json.dumps({"key": key}))[0] == 403


def test_only_the_token_gets_a_key_and_a_session_can_t_make_more(site):
    _, port = site
    assert call(port, "POST", "/api/panel-key", {"Content-Type": "application/json"}, "{}")[0] == 403
    assert call(port, "POST", "/api/panel-key", as_page(session(port)), "{}")[0] == 403


def test_a_session_ends_after_its_time(site, monkeypatch):
    assistant, port = site
    now = [1000.0]
    monkeypatch.setattr(panel.time, "monotonic", lambda: now[0])
    secret = session(port)
    now[0] += panel.SESSION_SECONDS + 1
    assert call(port, "POST", "/api/text", as_page(secret), '{"text": "hi"}')[0] == 403
    assert assistant.jobs == []


# ---------------------------------------------------------------------- requests from the pages

def test_the_page_s_requests_go_through_with_its_session(site):
    assistant, port = site
    assert call(port, "POST", "/api/text", as_page(session(port)), '{"text": "hello"}')[0] == 200
    assert assistant.jobs == [("text", {"text": "hello"})]


def test_a_wrong_session_is_refused(site):
    assistant, port = site
    assert call(port, "POST", "/api/text", as_page("x" * 43), '{"text": "hello"}')[0] == 403
    assert assistant.jobs == []


def test_a_cookie_opens_nothing(site):
    """A cookie is shared by every port of 127.0.0.1, so any local listener could collect one: none is used."""
    assistant, port = site
    secret = session(port)
    status = call(port, "POST", "/api/text", {"Cookie": f"rafiq_session={secret}", "X-Rafiq-Panel": "1",
                                              "Content-Type": "application/json"}, '{"text": "hi"}')[0]
    assert status == 403 and assistant.jobs == []


def test_the_event_stream_opens_with_the_session_header_and_not_without(site):
    _, port = site
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/events", headers={"Host": f"127.0.0.1:{port}", "X-Rafiq-Session": session(port)})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"
    conn.close()
    assert call(port, "GET", "/events")[0] == 403


def test_history_and_subscriptions_read_with_the_session(site):
    _, port = site
    assert call(port, "GET", "/api/history?q=x")[0] == 403
    assert call(port, "GET", "/api/subscriptions")[0] == 403


def test_rafiq_s_own_programs_still_use_the_token(site):
    assistant, port = site
    status = call(port, "POST", "/api/text", {"X-Rafiq-Token": TOKEN, "Content-Type": "application/json"},
                  '{"text": "hi"}')[0]
    assert status == 200 and assistant.jobs == [("text", {"text": "hi"})]


# ---------------------------------------------------------------------- other accounts

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def row(n, local, remote, uid):
    return f"   {n}: {local} {remote} 01 00000000:00000000 00:00000000 00000000  {uid}        0 {n} 1\n"


def test_the_owner_is_the_one_whose_end_matches_both_addresses_and_ports():
    table = HEADER + row(0, "0100007F:223D", "0100007F:9C40", 1000) + row(1, "0100007F:9C40", "0100007F:223D", 1001)
    assert panel.peer_uid(table, ("127.0.0.1", 0x9C40), ("127.0.0.1", 0x223D)) == 1001
    assert panel.peer_uid(table, ("127.0.0.1", 0x9C41), ("127.0.0.1", 0x223D)) is None


def test_a_client_on_the_panel_s_own_port_number_can_t_borrow_the_panel_s_row():
    """Another user binds 127.0.0.5:8765 and connects to 127.0.0.1:8765: both ends have port 8765, and the panel's
    accepted socket (Rafiq's uid) must not be taken for the client's."""
    table = (HEADER + row(0, "0100007F:223D", "0500007F:223D", 1000)       # the panel's end, Rafiq's user
             + row(1, "0500007F:223D", "0100007F:223D", 1001))            # the client's end, another user
    assert panel.peer_uid(table, ("127.0.0.5", 8765), ("127.0.0.1", 8765)) == 1001


def test_ipv6_loopback_ends_are_read_too():
    v6 = "00000000000000000000000001000000"                                 # ::1 as /proc writes it
    table = HEADER + row(0, f"{v6}:9C40", f"{v6}:223D", 1001)
    assert panel.peer_uid(table, ("::1", 0x9C40), ("::1", 0x223D)) == 1001


def test_another_account_s_connection_is_refused(site, monkeypatch):
    assistant, port = site
    monkeypatch.setattr(panel, "connection_owner", lambda client, server: 4242)
    status = call(port, "POST", "/api/text", {"X-Rafiq-Token": TOKEN, "Content-Type": "application/json"},
                  '{"text": "hi"}')[0]
    assert status == 403 and assistant.jobs == []


def test_on_linux_a_connection_whose_owner_can_t_be_found_is_refused(site, monkeypatch):
    assistant, port = site
    monkeypatch.setattr(panel, "connection_owner", lambda client, server: None)
    monkeypatch.setattr(panel.sys, "platform", "linux")
    assert call(port, "POST", "/api/text", {"X-Rafiq-Token": TOKEN, "Content-Type": "application/json"},
                '{"text": "hi"}')[0] == 403


def test_this_account_s_connection_is_let_through(site):
    """A real connection from this test, looked up in the real /proc/net/tcp."""
    _, port = site
    assert call(port, "POST", "/api/text", {"X-Rafiq-Token": TOKEN, "Content-Type": "application/json"},
                '{"text": "hi"}')[0] == 200


# ---------------------------------------------------------------------- rafiq panel

class Answer:
    def __init__(self, status, data=None):
        self.status_code, self.data = status, data or {}

    def json(self):
        return self.data


def test_rafiq_panel_opens_the_browser_with_the_key_after_the_hash(monkeypatch):
    """After "#", the key is never sent to a server (or another program listening on 127.0.0.1)."""
    import httpx

    posted, opened = [], []
    monkeypatch.setattr(httpx, "post", lambda url, json=None, headers=None, timeout=None:
                        posted.append((url, headers)) or Answer(200, {"key": "k1"}))
    monkeypatch.setattr(cli, "_open_browser", opened.append)
    assert cli.main(["panel"]) == 0
    assert posted[0][0] == "http://127.0.0.1:8765/api/panel-key"
    assert posted[0][1]["X-Rafiq-Token"] == config.panel_token()
    assert opened == ["http://127.0.0.1:8765/#key=k1"]


def test_rafiq_panel_print_shows_the_address_without_opening_anything(monkeypatch, capsys):
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Answer(200, {"key": "k2"}))
    monkeypatch.setattr(cli, "_open_browser", lambda url: pytest.fail("opened a browser"))
    assert cli.main(["panel", "--print"]) == 0
    assert "http://127.0.0.1:8765/#key=k2" in capsys.readouterr().out


def test_rafiq_panel_says_when_rafiq_is_not_running(monkeypatch, capsys):
    import httpx

    def refused(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", refused)
    assert cli.main(["panel"]) == 1
    assert "rafiq run" in capsys.readouterr().err


def test_rafiq_run_no_longer_prints_an_address_that_opens_without_a_key():
    source = open(cli.__file__, encoding="utf-8").read()
    assert 'print(f"Panel: http://{host}:{port}/"' not in source


@pytest.mark.parametrize("header", ["X-Rafiq-Session", "X-Rafiq-Token"])
def test_a_secret_header_that_is_not_ascii_is_refused_not_a_crash(site, header):
    assistant, port = site
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/api/text", skip_host=True)
    conn.putheader("Host", f"127.0.0.1:{port}")
    conn.putheader(header, "caf\xe9".encode("latin-1"))
    conn.putheader("Content-Length", "2")
    conn.endheaders(b"{}")
    assert conn.getresponse().status == 403 and assistant.jobs == []
    conn.close()
