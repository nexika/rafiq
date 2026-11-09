"""Rafiq's own programs on Windows (#242): a named pipe only this user can open, in place of the panel's Unix socket.

Most tests run anywhere, on FakeKernel: a stand-in for the few Win32 calls winpipe makes, which pairs a server's
pipe instance with a client and says whose program is at each end. The tests at the bottom make real pipes and run
only on Windows (CI's windows-latest job)."""

import os
import select
import socket
import sys
import threading
import time

import httpx
import pytest

from rafiq import __main__ as cli
from rafiq import config, desktop, panel, sock, winpipe

PAIR_FAMILY = getattr(socket, "AF_UNIX", socket.AF_INET)   # taken before a test removes AF_UNIX, as on Windows
ME = "S-1-5-21-1111-2222-3333-1001"
OTHER = "S-1-5-21-1111-2222-3333-1002"
NAME = winpipe.pipe_name(ME)


class Handle:
    def __init__(self, name, owner):
        self.name, self.owner = name, owner        # owner: the SID of the program that made or opened it
        self.end: socket.socket | None = None
        self.peer: str | None = None                # the SID of the program at the other end
        self.attached = threading.Event()
        self.closed = False


class FakeKernel:
    """The Win32 calls winpipe makes, on socket pairs. `caller` is whose program makes the next call."""

    def __init__(self):
        self.caller = ME
        self.pipes: dict[str, dict] = {}            # name -> {"dacl": sid, "waiting": [server handles], "open": n}
        self.made: list[tuple[str, str, bool]] = []
        self.fail_next_instance = False
        self.fail_connects = 0
        self.disconnected = 0
        self.writes: list[bytes] = []

    def my_sid(self):
        return self.caller

    def create(self, name, sid, first):
        self.made.append((name, sid, first))
        pipe = self.pipes.get(name)
        if pipe is not None and first:
            raise PermissionError(5, "Access is denied")               # ERROR_ACCESS_DENIED
        if self.fail_next_instance and pipe is not None:
            raise OSError(1450, "Insufficient system resources exist to complete the requested service")
        if pipe is None:
            trustee = "WD" if sid == "everyone" else sid
            # As GetSecurityInfo gives it back: the owner is the maker's, GA reads as FA (all of a pipe's rights).
            pipe = self.pipes[name] = {"dacl": sid, "owner": self.caller, "waiting": [], "open": 0,
                                       "sddl": f"O:{self.caller}D:P(A;;FA;;;{trustee})S:(ML;;NW;;;ME)"}
        handle = Handle(name, pipe["owner"])          # every instance is its maker's, as the first one is
        pipe["waiting"].append(handle)
        pipe["open"] += 1
        return handle

    def connect(self, handle):
        handle.attached.wait()
        if handle.closed:
            raise OSError(6, "The handle is invalid")
        if self.fail_connects:
            self.fail_connects -= 1
            raise OSError(232, "The pipe is being closed")      # a client that came and went before the call

    def disconnect(self, handle):
        """Ready the same instance for the next client."""
        if handle.end is not None:
            handle.end.close()
        handle.end, handle.peer = None, None
        handle.attached.clear()
        self.pipes[handle.name]["waiting"].append(handle)
        self.disconnected += 1

    def pipe_security(self, handle):
        return self.pipes[handle.name]["sddl"]

    def open(self, name, timeout):
        pipe = self.pipes.get(name)
        if pipe is None:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        if pipe["dacl"] not in (self.caller, "everyone"):
            raise PermissionError(5, "Access is denied")
        deadline = time.monotonic() + (timeout or 5)
        while not pipe["waiting"]:
            if time.monotonic() > deadline:
                raise TimeoutError("all pipe instances are busy")
            time.sleep(0.01)
        server = pipe["waiting"].pop(0)
        client = Handle(name, self.caller)
        server.end, client.end = socket.socketpair(PAIR_FAMILY)
        server.peer, client.peer = self.caller, server.owner
        server.attached.set()
        return client

    def client_sid(self, handle):
        return handle.peer


    def available(self, handle):
        try:
            if not select.select([handle.end], [], [], 0)[0]:
                return 0
            data = handle.end.recv(65536, socket.MSG_PEEK)
        except (OSError, ValueError):
            return None
        return len(data) if data else None          # b"": the other end closed

    def read(self, handle, size):
        return handle.end.recv(size)

    def write(self, handle, data):
        self.writes.append(data)
        handle.end.sendall(data)

    def close(self, handle):
        if handle.closed:
            return
        handle.closed = True
        if handle.end is not None:
            handle.end.close()
        pipe = self.pipes.get(handle.name)
        if pipe is not None and handle in pipe["waiting"]:
            pipe["waiting"].remove(handle)
        handle.attached.set()


@pytest.fixture
def windows(monkeypatch):
    """As test_windows.py's: Windows' commands, and no program comes in over the panel's TCP port."""
    monkeypatch.setattr(desktop, "system", lambda: "windows")
    monkeypatch.setattr(panel, "tcp_programs", lambda: False)


@pytest.fixture
def kernel(monkeypatch):
    fake = FakeKernel()
    monkeypatch.setattr(winpipe, "_api", fake)
    return fake


@pytest.fixture
def on_windows(windows, kernel, monkeypatch):
    """Windows, as test_windows.py's fixture says it, with the fake pipe calls."""
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    return kernel


def tcp_panel(token="token"):
    return panel.serve(object(), "127.0.0.1", 0, token)


@pytest.fixture
def served(on_windows):
    """The panel, with its pipe for Rafiq's own programs, as `rafiq run` starts them."""
    tcp = tcp_panel()
    pipe = panel.serve_socket(tcp)
    threading.Thread(target=pipe.serve_forever, daemon=True).start()
    yield tcp, pipe
    pipe.shutdown()
    pipe.server_close()
    tcp.server_close()


def post_key(tcp, token="token"):
    return sock.post(f"http://127.0.0.1:{tcp.server_address[1]}/api/panel-key", json={},
                     headers={"X-Rafiq-Token": token}, timeout=5)


# ---------------------------------------------------------------------- the pipe itself


def test_the_pipe_is_named_for_this_user_s_sid():
    assert winpipe.pipe_name(ME) == rf"\\.\pipe\rafiq-panel-{ME}"


def test_the_pipe_s_security_descriptor_grants_only_this_user():
    # Owned by this user's SID even when Rafiq runs elevated (whose default owner is Administrators), protected
    # (nothing inherited, no Everyone, no SYSTEM), and labelled Medium, so a Low-integrity program can't write to it.
    assert winpipe.sddl(ME) == f"O:{ME}D:P(A;;GA;;;{ME})S:(ML;;NW;;;ME)"


@pytest.mark.parametrize("text", [
    f"O:{ME}D:P(A;;FA;;;{ME})S:(ML;;NW;;;ME)",                     # as Windows gives Rafiq's own back
    f"O:{ME}D:P(A;;0x1f01ff;;;{ME})S:(ML;;NW;;;ME)",               # the rights as a number
    f"O:{ME}D:P(A;;FA;;;{ME})",                                    # no label: Medium, implicitly
    f"O:{ME}D:PAI(A;;FA;;;{ME})S:(ML;;NW;;;HI)",                   # a Rafiq started elevated
])
def test_rafiq_s_own_pipe_reads_as_this_user_s(text):
    assert winpipe.descriptor_ok(text, ME)


@pytest.mark.parametrize("text", [
    f"O:{OTHER}D:P(A;;FA;;;{ME})S:(ML;;NW;;;ME)",                  # another account made it
    f"O:BAD:P(A;;FA;;;{ME})",                                      # Administrators own it
    f"O:{ME}D:(A;;FA;;;{ME})",                                     # not protected: it inherits
    f"O:{ME}D:P(A;;FA;;;{ME})(A;;FA;;;WD)",                        # Everyone too
    f"O:{ME}D:P(A;;FA;;;WD)",                                      # Everyone only
    f"O:{ME}D:P(D;;FA;;;{OTHER})(A;;FA;;;{ME})",                   # anything but the one allow
    f"O:{ME}D:P(A;ID;FA;;;{ME})",                                  # an inherited allow
    f"O:{ME}D:P(A;;FA;;;{ME})S:(ML;;NW;;;LW)",                     # a Low-integrity program of this user made it
    f"O:{ME}D:P(A;;FA;;;{ME})S:(ML;;NW;;;S-1-16-4096)",
    f"O:{ME}D:NO_ACCESS_CONTROL",                                  # no DACL: open to all
    f"O:{ME}",
    "",
])
def test_a_pipe_not_made_as_rafiq_makes_it_is_refused(text):
    assert not winpipe.descriptor_ok(text, ME)


def test_rafiq_s_own_programs_reach_it_on_windows(windows):
    assert sock.available()


def test_the_phone_page_still_needs_a_unix_socket(windows):
    assert not sock.unix_available()


def test_the_pipe_is_made_as_the_first_instance_of_its_name(served, kernel):
    assert kernel.made[0] == (NAME, ME, True)


def test_a_pipe_name_another_account_took_first_stops_the_pipe_with_a_clear_error(on_windows):
    on_windows.caller = OTHER
    on_windows.create(NAME, "everyone", True)          # another account's program, waiting for Rafiq's
    on_windows.caller = ME
    with pytest.raises(ValueError, match="another program"):
        panel.serve_socket(tcp_panel())


def test_an_instance_is_always_waiting_so_the_name_is_never_free(served, kernel):
    tcp, _pipe = served
    for _ in range(3):
        assert post_key(tcp).status_code == 200
        time.sleep(0.05)
        assert kernel.pipes[NAME]["waiting"]            # the next instance was made before the request was handled
    assert all(first is False for _name, _sid, first in kernel.made[1:])


# ---------------------------------------------------------------------- the panel over the pipe


def test_a_pipe_that_can_t_make_its_next_instance_stops_and_says_so(served, kernel, capsys):
    tcp, pipe = served
    kernel.fail_next_instance = True
    with pytest.raises(httpx.HTTPError):
        post_key(tcp)                                   # the connection is closed: a new one can't be waited for
    assert pipe._stopped.wait(2)
    assert "Rafiq's pipe stopped" in capsys.readouterr().err


def test_a_program_of_this_user_gets_a_panel_key_over_the_pipe(served):
    tcp, _pipe = served
    resp = post_key(tcp)
    assert resp.status_code == 200 and resp.json()["key"]


def test_the_wrong_token_is_refused_over_the_pipe_too(served):
    tcp, _pipe = served
    assert post_key(tcp, token="wrong").status_code == 403


def test_the_pipe_refuses_a_program_of_another_account(served, kernel):
    # The DACL keeps another account out; were it let in anyway (an administrator), the server checks whose it is.
    tcp, _pipe = served
    kernel.pipes[NAME]["dacl"] = "everyone"
    kernel.caller = OTHER                               # the program that connects is another account's
    with pytest.raises(httpx.HTTPError):
        # me=ME: the client is told the server's owner is fine, so the server's own check is what is tested.
        sock.post(f"http://127.0.0.1:{tcp.server_address[1]}/api/panel-key", json={}, timeout=2,
                  headers={"X-Rafiq-Token": "token"}, me=ME)


def test_the_server_says_whose_program_connected_from_the_pipe_itself(served, kernel):
    conn = winpipe.PipeConnection(Handle(NAME, ME), OTHER, peer_is_me=False)
    assert sock.peer_is_me(conn) is False
    assert sock.peer_is_me(winpipe.PipeConnection(Handle(NAME, ME), ME, peer_is_me=True)) is True


# ---------------------------------------------------------------------- the client


def test_the_token_is_never_sent_to_a_pipe_of_another_account(on_windows):
    # Another account's program made the pipe first, open to all, while Rafiq was down.
    on_windows.caller = OTHER
    squatter = on_windows.create(NAME, "everyone", True)
    on_windows.caller = ME
    with pytest.raises(httpx.ConnectError):
        sock.post("http://127.0.0.1:8765/api/panel-key", json={}, headers={"X-Rafiq-Token": "secret"}, timeout=2)
    assert squatter.end is not None and squatter.end.recv(100) == b""   # connected, then closed with nothing sent


def test_a_pipe_whose_security_can_t_be_read_is_refused(on_windows, monkeypatch):
    server = on_windows.create(NAME, ME, True)

    def unreadable(handle):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(on_windows, "pipe_security", unreadable)
    with pytest.raises(httpx.ConnectError):
        sock.post("http://127.0.0.1:8765/api/panel-key", json={}, headers={"X-Rafiq-Token": "secret"}, timeout=2)
    assert server.end.recv(100) == b""


def test_a_pipe_a_low_integrity_program_of_this_user_made_gets_nothing(on_windows):
    # A sandboxed program of this user (Low integrity) took the name while Rafiq was down: same SID, Low label.
    squatter = on_windows.create(NAME, ME, True)
    on_windows.pipes[NAME]["sddl"] = f"O:{ME}D:P(A;;FA;;;{ME})S:(ML;;NW;;;LW)"
    with pytest.raises(httpx.ConnectError):
        sock.post("http://127.0.0.1:8765/api/panel-key", json={}, headers={"X-Rafiq-Token": "secret"}, timeout=2)
    assert squatter.end.recv(100) == b""


def test_the_whole_request_goes_in_one_write(served, kernel):
    # Rafiq may answer once it has the headers and close; a body sent after would find the pipe closed.
    tcp, _pipe = served
    assert post_key(tcp).status_code == 200
    request = next(w for w in kernel.writes if w.startswith(b"POST "))
    assert request.endswith(b"\r\n\r\n{}")


def test_a_body_larger_than_rafiq_takes_is_refused_before_connecting(on_windows):
    # Over a pipe a body Rafiq won't read fills its buffer, and the write would wait forever.
    with pytest.raises(httpx.HTTPError, match="too large"):
        sock.post("http://127.0.0.1:8765/api/tool", json={"text": "x" * (panel.MAX_BODY + 1)},
                  headers={"X-Rafiq-Token": "secret"})
    assert on_windows.writes == []


def test_a_client_that_came_and_went_before_the_server_waited_reuses_the_same_instance(served, kernel):
    # Closing it and making another would leave the name free in between, for another account to take.
    tcp, _pipe = served
    made = len(kernel.made)
    kernel.fail_connects = 1
    with pytest.raises(httpx.HTTPError):
        post_key(tcp)
    assert post_key(tcp).status_code == 200
    assert kernel.disconnected == 1 and len(kernel.made) == made + 1       # one new instance, for the request


def test_no_rafiq_running_reads_as_not_running(on_windows, capsys):
    with pytest.raises(httpx.ConnectError):
        sock.post("http://127.0.0.1:8765/api/panel-key", json={}, timeout=2)


def test_nothing_falls_back_to_tcp_on_windows(on_windows, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("fell back to TCP"))
    with pytest.raises(httpx.ConnectError):
        sock.post("http://127.0.0.1:8765/api/ask", json={"text": "hi"}, headers={"X-Rafiq-Token": "secret"})


def test_an_answer_that_doesn_t_come_in_time_is_a_read_timeout(on_windows):
    server = on_windows.create(NAME, ME, True)
    threading.Thread(target=on_windows.connect, args=(server,), daemon=True).start()
    with pytest.raises(httpx.ReadTimeout):
        sock.post("http://127.0.0.1:8765/api/ask", json={}, timeout=httpx.Timeout(0.3, connect=2))


def test_without_this_user_s_sid_nothing_is_sent(on_windows, monkeypatch):
    monkeypatch.setattr(on_windows, "my_sid", lambda: None)
    with pytest.raises(httpx.ConnectError):
        sock.post("http://127.0.0.1:8765/api/panel-key", json={}, headers={"X-Rafiq-Token": "secret"})


# ---------------------------------------------------------------------- the commands


def test_rafiq_run_starts_the_pipe_on_windows(on_windows, capsys):
    tcp = tcp_panel()
    pipe = cli._start_socket(tcp)
    try:
        assert isinstance(pipe, winpipe.PipeServer)
        assert post_key(tcp).status_code == 200
    finally:
        pipe.shutdown()
        pipe.server_close()
        tcp.server_close()


def test_rafiq_ask_refuses_to_answer_here_while_a_rafiq_it_can_t_reach_runs(on_windows, monkeypatch, capsys):
    # Another account holds the pipe's name, so the running Rafiq has none: answering here, next to it, would save
    # timers and facts that it then overwrites from its own copy.
    from rafiq import hosting

    on_windows.caller = OTHER
    on_windows.create(NAME, "everyone", True)
    on_windows.caller = ME
    monkeypatch.setattr(hosting, "panel_up", lambda host, port: True)
    assert cli._ask_through_running(config.load(), "remind me in 10 minutes", None, False) == 1
    assert "can't reach it" in capsys.readouterr().err


def test_rafiq_ask_answers_here_when_no_rafiq_runs_on_windows(on_windows, monkeypatch):
    from rafiq import hosting

    monkeypatch.setattr(hosting, "panel_up", lambda host, port: False)
    assert cli._ask_through_running(config.load(), "hi", None, False) is None


def test_rafiq_run_opens_the_panel_itself_when_the_pipe_can_t_start(on_windows, monkeypatch, capsys):
    on_windows.caller = OTHER
    on_windows.create(NAME, "everyone", True)
    on_windows.caller = ME
    tcp = tcp_panel()
    opened = []
    monkeypatch.setattr(cli, "_open_panel_here", lambda server: opened.append(server))
    try:
        cli._panel_for_programs(tcp)
    finally:
        tcp.server_close()
    assert opened == [tcp] and "another program" in capsys.readouterr().err


def test_rafiq_panel_gets_its_key_through_the_pipe(served, monkeypatch):
    tcp, _pipe = served
    monkeypatch.setattr(config, "panel_token", lambda: "token")
    cfg = config.load()
    cfg["panel"]["port"] = tcp.server_address[1]
    monkeypatch.setattr(config, "load", lambda: cfg)
    opened = []
    monkeypatch.setattr(cli, "_open_browser", opened.append)
    assert cli.main(["panel"]) == 0
    assert opened and opened[0].startswith(f"http://127.0.0.1:{tcp.server_address[1]}/#key=")


def test_doctor_no_longer_lists_rafiq_s_own_programs_as_not_on_windows():
    from rafiq import doctor

    assert "rafiq listen" not in doctor.NOT_ON_WINDOWS and "`mcp`" not in doctor.NOT_ON_WINDOWS
    assert "the phone page" in doctor.NOT_ON_WINDOWS


# ---------------------------------------------------------------------- real pipes, on Windows only

real = pytest.mark.skipif(sys.platform != "win32", reason="real named pipes are Windows'")


@pytest.fixture
def real_windows(windows, monkeypatch, tmp_path):
    monkeypatch.setattr(winpipe, "_api", None)                        # the real Win32 calls
    monkeypatch.setattr(winpipe, "pipe_name", lambda sid, what="panel": rf"\\.\pipe\rafiq-test-{os.getpid()}-{sid}")


@real
def test_on_windows_this_user_s_sid_comes_from_the_process_token(real_windows):
    sid = winpipe.my_sid()
    assert sid and sid.startswith("S-1-5-")


@real
def test_on_windows_a_real_pipe_carries_the_panel_s_requests(real_windows):
    tcp = tcp_panel()
    pipe = panel.serve_socket(tcp)
    threading.Thread(target=pipe.serve_forever, daemon=True).start()
    try:
        resp = post_key(tcp)
        assert resp.status_code == 200 and resp.json()["key"]
        assert post_key(tcp, token="wrong").status_code == 403
    finally:
        pipe.shutdown()
        pipe.server_close()
        tcp.server_close()


@real
def test_on_windows_the_pipe_s_own_descriptor_reads_as_rafiq_s(real_windows, capsys):
    # What decides on the client is the descriptor Windows gives back for the pipe, so check its real form.
    me = winpipe.my_sid()
    tcp = tcp_panel()
    pipe = panel.serve_socket(tcp)
    handle = winpipe.api().open(winpipe.pipe_name(me), 5)
    try:
        text = winpipe.api().pipe_security(handle)
        with capsys.disabled():
            print(f"\nthe pipe's descriptor: {text}")
        assert winpipe.descriptor_ok(text, me), text
        assert not winpipe.descriptor_ok(text, OTHER)
    finally:
        winpipe.api().close(handle)
        pipe.server_close()
        tcp.server_close()


@real
def test_on_windows_a_second_rafiq_can_t_take_the_pipe(real_windows):
    tcp = tcp_panel()
    pipe = panel.serve_socket(tcp)
    try:
        with pytest.raises(ValueError, match="another program"):
            panel.serve_socket(tcp_panel())
    finally:
        pipe.server_close()
        tcp.server_close()


@real
def test_on_windows_the_client_refuses_a_pipe_whose_owner_isn_t_this_user(real_windows, monkeypatch):
    # The pipe is this test's own; the client is told this user is someone else, as when another account owns it.
    tcp = tcp_panel()
    pipe = panel.serve_socket(tcp)
    threading.Thread(target=pipe.serve_forever, daemon=True).start()
    real_sid = winpipe.my_sid()
    try:
        with pytest.raises(httpx.ConnectError):
            sock.post(f"http://127.0.0.1:{tcp.server_address[1]}/api/panel-key", json={}, timeout=5,
                      headers={"X-Rafiq-Token": "token"}, me=OTHER, name=winpipe.pipe_name(real_sid))
    finally:
        pipe.shutdown()
        pipe.server_close()
        tcp.server_close()


@real
def test_on_windows_the_server_refuses_a_client_it_can_t_prove_is_this_user(real_windows, monkeypatch):
    tcp = tcp_panel()
    pipe = panel.serve_socket(tcp)
    pipe.me = OTHER                                      # the server takes this user to be someone else
    threading.Thread(target=pipe.serve_forever, daemon=True).start()
    real_sid = winpipe.my_sid()
    try:
        with pytest.raises(httpx.HTTPError):
            sock.post(f"http://127.0.0.1:{tcp.server_address[1]}/api/panel-key", json={}, timeout=5,
                      headers={"X-Rafiq-Token": "token"}, name=winpipe.pipe_name(real_sid))
    finally:
        pipe.shutdown()
        pipe.server_close()
        tcp.server_close()
