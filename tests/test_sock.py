"""Rafiq's own programs reach the panel over a Unix socket in a folder only this user can open (#234). The
server asks the kernel whose program connected (SO_PEERCRED, LOCAL_PEERCRED on macOS), and a client never sends
the token to a socket this user doesn't own. On macOS the TCP port takes no token at all: there is no /proc to
say whose program connected, so it serves only the browser's sessions."""

import os
import shutil
import socket
import stat
import struct
import sys
import tempfile
import threading
from pathlib import Path

import httpx
import pytest
from test_brain_panel import FakeAssistant

from rafiq import panel, sock

TOKEN = "t" * 40
REAL_TCP_PROGRAMS = panel.tcp_programs   # conftest pins it for the TCP tests; this is the real one


@pytest.fixture
def home(monkeypatch):
    """A short RAFIQ_HOME: a socket path may be at most about 100 bytes, and pytest's own folders are longer."""
    folder = tempfile.mkdtemp(prefix="rq", dir="/tmp")
    monkeypatch.setenv("RAFIQ_HOME", folder)
    yield Path(folder)
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def served(home):
    """The panel on TCP and on its socket, as `rafiq run` starts them."""
    assistant = FakeAssistant()
    tcp = panel.serve(assistant, "127.0.0.1", 0, TOKEN, screen_token="s" * 40)
    port = tcp.server_address[1]
    unix = panel.serve_socket(tcp)
    for server in (tcp, unix):
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield assistant, port
    for server in (tcp, unix):
        server.shutdown()
        server.server_close()


def over_socket(port, path, body=None, token=TOKEN):
    headers = {"X-Rafiq-Token": token} if token else {}
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(sock.path())), timeout=5) as client:
        url = f"http://127.0.0.1:{port}{path}"
        resp = client.post(url, json=body, headers=headers) if body is not None else client.get(url, headers=headers)
    return resp.status_code


def over_tcp(port, path, body=None, headers=None):
    url = f"http://127.0.0.1:{port}{path}"
    headers = {"X-Rafiq-Token": TOKEN, **(headers or {})}
    resp = httpx.post(url, json=body, headers=headers, timeout=5) if body is not None else httpx.get(
        url, headers=headers, timeout=5)
    return resp.status_code


# ---------------------------------------------------------------------- where the socket is, and whose


def test_the_socket_is_in_its_own_folder_under_the_settings(home):
    assert sock.path() == home / "run" / "panel.sock"


def test_the_server_makes_the_folder_for_this_user_only(served, home):
    folder = home / "run"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700 and folder.stat().st_uid == os.getuid()
    assert stat.S_ISSOCK(sock.path().stat().st_mode) and sock.ours(sock.path())


def test_a_folder_others_can_open_is_not_ours(served, home):
    (home / "run").chmod(0o755)
    assert not sock.ours(sock.path())


def test_a_folder_owned_by_another_account_is_not_ours(served, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 12345)
    assert not sock.ours(sock.path())


def test_a_linked_folder_is_not_ours(home):
    real = Path(tempfile.mkdtemp(prefix="rq", dir="/tmp"))
    try:
        real.chmod(0o700)
        (home / "run").symlink_to(real)
        assert not sock.ours(sock.path())
    finally:
        shutil.rmtree(real, ignore_errors=True)


def test_a_plain_file_in_the_folder_is_not_a_socket(home):
    (home / "run").mkdir(mode=0o700)
    sock.path().write_text("")
    assert not sock.ours(sock.path())


def test_a_path_too_long_for_a_socket_says_to_shorten_rafiq_home(monkeypatch, tmp_path):
    monkeypatch.setenv("RAFIQ_HOME", str(tmp_path / ("x" * 120)))
    with pytest.raises(ValueError, match="RAFIQ_HOME"):
        panel.serve_socket(Tcp())


def test_a_stale_socket_from_a_crash_is_replaced(home):
    (home / "run").mkdir(mode=0o700)
    old = socket.socket(socket.AF_UNIX)
    old.bind(str(sock.path()))
    old.close()                          # the file stays, as after a crash
    server = panel.serve_socket(Tcp())
    try:
        assert sock.ours(sock.path())
    finally:
        server.server_close()


def test_the_socket_file_is_for_this_user_only(served):
    assert stat.S_IMODE(sock.path().stat().st_mode) & 0o077 == 0


# ---------------------------------------------------------------------- whose program connected


def test_linux_peer_credentials_give_the_uid():
    assert sock.uid_from_ucred(struct.pack("3i", 4242, 1000, 1000)) == 1000


def test_macos_peer_credentials_give_the_uid():
    xucred = struct.pack("=Ii", 0, 501) + struct.pack("=h", 1) + b"\0\0" + struct.pack("=16I", *([20] * 16))
    assert sock.uid_from_xucred(xucred) == 501


def test_macos_credentials_of_an_unknown_version_are_not_trusted():
    assert sock.uid_from_xucred(struct.pack("=Ii", 7, 501) + bytes(68)) is None
    assert sock.uid_from_xucred(b"\0\0") is None


def test_this_user_s_program_is_let_in(served):
    assistant, port = served
    assert over_socket(port, "/api/text", {"text": "what time is it"}) == 200
    assert assistant.jobs == [("text", {"text": "what time is it"})]


def test_the_kernel_is_asked_whose_program_connected(served, monkeypatch):
    _, port = served
    seen, real = [], sock.peer_uid
    monkeypatch.setattr(sock, "peer_uid", lambda conn: seen.append(conn.family) or real(conn))
    assert over_socket(port, "/api/text", {"text": "hi"}) == 200
    assert seen == [socket.AF_UNIX]


def test_another_account_s_program_is_refused_even_with_the_token(served, monkeypatch):
    assistant, port = served
    monkeypatch.setattr(sock, "peer_uid", lambda conn: os.getuid() + 1)
    assert over_socket(port, "/api/text", {"text": "send my files"}) == 403
    assert assistant.jobs == []


def test_a_peer_whose_owner_can_t_be_told_is_refused(served, monkeypatch):
    assistant, port = served
    monkeypatch.setattr(sock, "peer_uid", lambda conn: None)
    assert over_socket(port, "/api/text", {"text": "hi"}) == 403


def test_the_socket_still_needs_the_token(served):
    assistant, port = served
    assert over_socket(port, "/api/text", {"text": "hi"}, token=None) == 403
    assert over_socket(port, "/api/text", {"text": "hi"}, token="x" * 40) == 403
    assert assistant.jobs == []


def test_the_peer_uid_of_a_real_connection_is_this_user_s():
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert sock.peer_uid(a) == os.getuid()
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------- the TCP port on macOS


def test_on_macos_the_tcp_port_takes_no_token(served, monkeypatch):
    assistant, port = served
    monkeypatch.setattr(panel, "tcp_programs", lambda: False)
    assert over_tcp(port, "/api/text", {"text": "hi"}) == 403
    assert over_tcp(port, "/api/panel-key", {}) == 403
    assert httpx.get(f"http://127.0.0.1:{port}/events?token={TOKEN}", timeout=5).status_code == 403
    assert assistant.jobs == []


def test_on_macos_the_tcp_port_takes_no_gesture_token(served, monkeypatch):
    assistant, port = served
    assistant.gestures = Gestures()
    assert over_tcp(port, "/api/gesture", {"gesture": "palm"}, {"X-Rafiq-Gesture-Token": "g" * 40}) == 200
    monkeypatch.setattr(panel, "tcp_programs", lambda: False)
    assert over_tcp(port, "/api/gesture", {"gesture": "palm"}, {"X-Rafiq-Gesture-Token": "g" * 40}) == 403


def test_on_macos_the_browser_s_session_still_works_over_tcp(served, monkeypatch):
    assistant, port = served
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(sock.path())), timeout=5) as client:
        key = client.post(f"http://127.0.0.1:{port}/api/panel-key", json={},
                          headers={"X-Rafiq-Token": TOKEN}).json()["key"]
    monkeypatch.setattr(panel, "tcp_programs", lambda: False)
    resp = httpx.post(f"http://127.0.0.1:{port}/api/panel-key/redeem", json={"key": key},
                      headers={"X-Rafiq-Panel": "1"}, timeout=5)
    assert resp.status_code == 200
    session = resp.json()["session"]
    resp = httpx.post(f"http://127.0.0.1:{port}/api/text", json={"text": "hi"},
                      headers={"X-Rafiq-Session": session}, timeout=5)
    assert resp.status_code == 200 and assistant.jobs == [("text", {"text": "hi"})]


def test_on_linux_the_tcp_port_still_takes_the_token(served):
    assistant, port = served
    assert over_tcp(port, "/api/text", {"text": "hi"}) == 200


@pytest.mark.parametrize("platform, allowed", [("linux", True), ("darwin", False), ("freebsd14", False)])
def test_only_linux_lets_rafiq_s_programs_use_the_port(platform, allowed, monkeypatch):
    monkeypatch.setattr(sys, "platform", platform)
    assert REAL_TCP_PROGRAMS() is allowed


# ---------------------------------------------------------------------- Rafiq's own programs


def test_a_client_talks_over_the_socket(served):
    _, port = served
    resp = sock.post(f"http://127.0.0.1:{port}/api/text", json={"text": "hi"}, headers={"X-Rafiq-Token": TOKEN},
                     timeout=5)
    assert resp.status_code == 200


def test_a_client_never_sends_to_a_socket_that_is_not_ours(served, home, monkeypatch):
    assistant, port = served
    (home / "run").chmod(0o755)
    with pytest.raises(httpx.ConnectError):
        sock.post(f"http://127.0.0.1:{port}/api/text", json={"text": "hi"}, headers={"X-Rafiq-Token": TOKEN})
    assert assistant.jobs == []


def test_with_no_socket_the_client_says_rafiq_is_not_running(home):
    with pytest.raises(httpx.ConnectError):
        sock.post("http://127.0.0.1:8765/api/text", json={"text": "hi"})


def test_a_client_never_falls_back_to_tcp(home, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("sent over TCP"))
    with pytest.raises(httpx.ConnectError):
        sock.post("http://127.0.0.1:8765/api/text", json={"text": "hi"})


@pytest.mark.parametrize("command", [["panel"], ["gestures", "on"]])
def test_the_cli_reaches_rafiq_through_its_socket(command, monkeypatch):
    from rafiq import __main__ as cli

    sent = []

    class Resp:
        status_code = 200

        def json(self):
            return {"key": "k" * 40}

    monkeypatch.setattr(sock, "post", lambda url, **k: sent.append((url, k)) or Resp())
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("sent over TCP"))
    monkeypatch.setattr(cli, "_open_browser", lambda url: None)
    cli.main(command)
    assert sent and "X-Rafiq-Token" in sent[0][1]["headers"]


def test_mcp_reaches_rafiq_through_its_socket(monkeypatch):
    from rafiq import config, mcp_server

    sent = []

    class Resp:
        status_code = 200

        def json(self):
            return {"result": "done"}

    monkeypatch.setattr(sock, "post", lambda url, **k: sent.append(url) or Resp())
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("sent over TCP"))
    bridge = mcp_server.Server(config.load(), tools={})
    assert bridge.forward("get_time", {}) == "done"
    assert sent and sent[0].endswith("/api/tool")


def test_the_gesture_tracker_posts_through_the_socket(monkeypatch):
    from rafiq import tracker

    sent = []
    monkeypatch.setattr(sock, "post", lambda url, **k: sent.append((url, k)))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("sent over TCP"))
    tracker.post_label("http://127.0.0.1:8765/api/gesture", "g" * 40, "palm")
    assert sent[0][1]["json"] == {"gesture": "palm"} and sent[0][1]["headers"]["X-Rafiq-Gesture-Token"] == "g" * 40


def test_rafiq_run_serves_the_socket_beside_the_port(monkeypatch):
    from rafiq import __main__ as cli

    started = []
    tcp = object()
    monkeypatch.setattr(panel, "serve_socket", lambda server: started.append(server) or Server())
    assert cli._start_socket(tcp) and started == [tcp]


def test_rafiq_run_goes_on_when_the_socket_can_t_be_made(monkeypatch, capsys):
    from rafiq import __main__ as cli

    def fails(*a):
        raise ValueError("the socket path is too long; set a shorter RAFIQ_HOME")

    monkeypatch.setattr(panel, "serve_socket", fails)
    assert cli._start_socket(object()) is None
    assert "RAFIQ_HOME" in capsys.readouterr().err


class Server:
    def serve_forever(self):
        pass


class Tcp:
    """Only what serve_socket takes from the TCP server: its handler."""
    RequestHandlerClass = panel.make_handler(FakeAssistant(), TOKEN, 8765)


class Gestures:
    def token_ok(self, given):
        return given == "g" * 40

    def handle(self, label):
        pass


# ---------------------------------------------------------------------- found in review


def test_a_client_sends_nothing_when_the_kernel_says_the_listener_is_another_account_s(served, monkeypatch):
    assistant, port = served
    monkeypatch.setattr(sock, "peer_uid", lambda conn: os.getuid() + 1)
    with pytest.raises(httpx.ConnectError):
        sock.post(f"http://127.0.0.1:{port}/api/text", json={"text": "hi"}, headers={"X-Rafiq-Token": TOKEN})
    assert assistant.jobs == []


def test_a_reply_that_never_comes_is_a_read_timeout(home):
    (home / "run").mkdir(mode=0o700)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(sock.path()))
    listener.listen(1)
    try:
        with pytest.raises(httpx.ReadTimeout):
            sock.post("http://127.0.0.1:8765/api/text", json={"text": "hi"}, timeout=0.3)
    finally:
        listener.close()


def test_the_answer_comes_back_as_a_response(served):
    _, port = served
    resp = sock.post(f"http://127.0.0.1:{port}/api/text", json={"text": "hi"}, headers={"X-Rafiq-Token": TOKEN},
                     timeout=httpx.Timeout(5, connect=2))
    assert resp.status_code == 200 and isinstance(resp.json(), dict)


def test_the_event_stream_flows_over_the_socket(served):
    assistant, port = served
    assistant.privacy = lambda: {"local": True}
    conn = socket.socket(socket.AF_UNIX)
    conn.settimeout(5)
    conn.connect(str(sock.path()))
    try:
        conn.sendall(f"GET /events?token={TOKEN} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        seen = b""
        while b'"kind": "state"' not in seen:
            chunk = conn.recv(4096)
            if not chunk:
                break
            seen += chunk
    finally:
        conn.close()
    assert seen.split(b"\r\n")[0].endswith(b" 200 OK") and b'"kind": "state"' in seen


def test_preparing_refuses_a_file_that_is_not_a_socket(home):
    (home / "run").mkdir(mode=0o700)
    sock.path().write_text("keep me")
    with pytest.raises(ValueError, match="isn't a socket"):
        sock.prepare(sock.path())
    assert sock.path().read_text() == "keep me"


def test_preparing_refuses_a_linked_folder(home):
    real = Path(tempfile.mkdtemp(prefix="rq", dir="/tmp"))
    try:
        (home / "run").symlink_to(real)
        with pytest.raises(ValueError, match="this user's"):
            sock.prepare(sock.path())
    finally:
        shutil.rmtree(real, ignore_errors=True)


def test_preparing_never_takes_the_socket_of_a_rafiq_that_is_running(served):
    with pytest.raises(ValueError, match="another Rafiq"):
        sock.prepare(sock.path())
    assert sock.ours(sock.path())


@pytest.mark.parametrize("parse, data", [
    (sock.uid_from_ucred, struct.pack("iII", 4242, 2**31 + 5, 0)),
    (sock.uid_from_xucred, struct.pack("=II", 0, 2**31 + 5) + bytes(68)),
])
def test_a_uid_past_two_to_the_31_is_read_as_it_is(parse, data):
    assert parse(data) == 2**31 + 5


def test_preparing_closes_a_folder_left_open_to_others(home):
    (home / "run").mkdir()
    (home / "run").chmod(0o755)
    sock.prepare(sock.path())
    assert stat.S_IMODE((home / "run").stat().st_mode) == 0o700


def test_rafiq_run_starts_the_socket_with_the_panel(monkeypatch):
    import argparse
    import signal

    from rafiq import __main__ as cli

    class Running:
        gestures = type("G", (), {"url": ""})()

        def __init__(self, cfg):
            pass

        def run(self):
            pass

    tcp = type("Tcp", (), {"server_address": ("127.0.0.1", 9000), "serve_forever": lambda self: None})()
    started = []
    monkeypatch.setattr("rafiq.assistant.Assistant", Running)
    monkeypatch.setattr(panel, "serve", lambda *a, **k: tcp)
    monkeypatch.setattr(cli, "_start_socket", started.append)
    monkeypatch.setattr(cli, "_start_hub", lambda cfg, assistant: None)
    old = signal.getsignal(signal.SIGTERM)
    try:
        assert cli.cmd_run(argparse.Namespace(brain=None, mode=None, server=False, no_panel=False)) == 0
    finally:
        signal.signal(signal.SIGTERM, old)
    assert started == [tcp]
