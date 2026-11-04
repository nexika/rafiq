"""The phone page behind a socket only this user can open (#238): `tailscale serve` reaches it at
`<settings>/run/mobile.sock`, so another account can't take its place while Rafiq is down and collect a phone's
token. Only this user's programs and root (tailscaled) may connect, on the socket or on the port."""

import os
import shutil
import socket
import stat
import tempfile
import threading
from pathlib import Path

import httpx
import pytest
from test_mobile import FakeAssistant

from rafiq import __main__ as cli
from rafiq import config, doctor, mobile, panel, sock

PORT = 8768
HOST = f"127.0.0.1:{PORT}"
REAL_TARGETS = mobile.serve_targets     # the fixture below replaces it in every test; this is the real one


@pytest.fixture(autouse=True)
def no_real_tailscale(monkeypatch):
    """No test asks the real Tailscale; one that wants an answer sets its own."""
    monkeypatch.setattr(mobile, "serve_targets", lambda run=None: None)


@pytest.fixture
def home(monkeypatch):
    """A short RAFIQ_HOME: a socket path may be at most about 100 bytes, and pytest's own folders are longer."""
    folder = tempfile.mkdtemp(prefix="rq", dir="/tmp")
    monkeypatch.setenv("RAFIQ_HOME", folder)
    yield Path(folder)
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def served(home):
    assistant = FakeAssistant()
    server = mobile.serve_socket(assistant, PORT, mobile.Devices(home / "mobile"))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield assistant
    server.shutdown()
    server.server_close()


def get(path="/"):
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(mobile.socket_path())), timeout=5) as client:
        return client.get(f"http://{HOST}{path}").status_code


# ---------------------------------------------------------------------- the socket


def test_the_phone_page_s_socket_is_beside_the_panel_s(home):
    assert mobile.socket_path() == home / "run" / "mobile.sock"


def test_the_socket_is_this_user_s_only(served, home):
    assert stat.S_IMODE((home / "run").stat().st_mode) == 0o700
    assert stat.S_IMODE(mobile.socket_path().stat().st_mode) & 0o077 == 0 and sock.ours(mobile.socket_path())


def test_the_page_is_served_over_the_socket(served):
    assert get() == 200


def test_tailscaled_running_as_root_is_let_in(served, monkeypatch):
    monkeypatch.setattr(sock, "peer_uid", lambda conn: 0)
    assert get() == 200


def test_another_account_s_program_is_refused_on_the_socket(served, monkeypatch):
    monkeypatch.setattr(sock, "peer_uid", lambda conn: os.getuid() + 1)
    assert get() == 403


def test_a_peer_whose_owner_can_t_be_told_is_refused(served, monkeypatch):
    monkeypatch.setattr(sock, "peer_uid", lambda conn: None)
    assert get() == 403


def test_a_live_socket_is_never_taken_over(served, home):
    with pytest.raises(ValueError, match="another Rafiq"):
        mobile.serve_socket(FakeAssistant(), PORT, mobile.Devices(home / "mobile"))


# ---------------------------------------------------------------------- the port, opted into


@pytest.fixture
def on_port(tmp_path):
    server = mobile.serve(FakeAssistant(), "127.0.0.1", 0, mobile.Devices(tmp_path / "mobile"))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def tcp_get(port):
    return httpx.get(f"http://127.0.0.1:{port}/", timeout=5).status_code


def test_on_linux_another_account_s_program_is_refused_on_the_port(on_port, monkeypatch):
    monkeypatch.setattr(mobile, "owner_known", lambda: True)
    monkeypatch.setattr(panel, "connection_owner", lambda client, server: os.getuid() + 1)
    assert tcp_get(on_port) == 403


def test_on_linux_tailscaled_and_this_user_are_let_in_on_the_port(on_port, monkeypatch):
    monkeypatch.setattr(mobile, "owner_known", lambda: True)
    for uid in (0, os.getuid()):
        monkeypatch.setattr(panel, "connection_owner", lambda client, server, uid=uid: uid)
        assert tcp_get(on_port) == 200


def test_on_linux_a_connection_whose_owner_can_t_be_found_is_refused(on_port, monkeypatch):
    monkeypatch.setattr(mobile, "owner_known", lambda: True)
    monkeypatch.setattr(panel, "connection_owner", lambda client, server: None)
    assert tcp_get(on_port) == 403


# ---------------------------------------------------------------------- rafiq run


def mobile_cfg(listen, port=PORT):
    cfg = config.load()
    cfg["mobile"].update(enabled=True, listen=listen, port=port)
    return cfg


def test_socket_is_the_default():
    assert config.load()["mobile"]["listen"] == "socket"


def test_rafiq_run_serves_the_phone_page_on_its_socket(home, monkeypatch):
    monkeypatch.setattr(cli, "tailnet_name", lambda: "")
    server = cli._start_mobile(mobile_cfg("socket"), FakeAssistant())
    try:
        assert server.socket.family == socket.AF_UNIX and sock.ours(mobile.socket_path())
    finally:
        server.shutdown()
        server.server_close()


def test_a_taken_port_stops_rafiq_with_a_clear_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "tailnet_name", lambda: "")
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    try:
        with pytest.raises(SystemExit) as stopped:
            cli._start_mobile(mobile_cfg("port", taken.getsockname()[1]), FakeAssistant())
    finally:
        taken.close()
    assert stopped.value.code != 0 and "phone page" in capsys.readouterr().err.lower()


def test_a_socket_that_can_t_be_made_stops_rafiq_with_a_clear_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("RAFIQ_HOME", str(tmp_path / ("x" * 120)))
    monkeypatch.setattr(cli, "tailnet_name", lambda: "")
    with pytest.raises(SystemExit):
        cli._start_mobile(mobile_cfg("socket"), FakeAssistant())
    assert "RAFIQ_HOME" in capsys.readouterr().err


def test_an_unknown_listen_setting_stops_rafiq(monkeypatch, capsys):
    monkeypatch.setattr(cli, "tailnet_name", lambda: "")
    with pytest.raises(SystemExit):
        cli._start_mobile(mobile_cfg("everywhere"), FakeAssistant())
    assert "listen" in capsys.readouterr().err


# ---------------------------------------------------------------------- rafiq mobile pair


def write_config(text):
    config.home().mkdir(parents=True, exist_ok=True)
    (config.home() / "config.toml").write_text(text)


def test_pairing_says_to_point_tailscale_at_the_socket(monkeypatch, capsys):
    monkeypatch.setattr(cli, "tailnet_name", lambda: "")
    write_config("[mobile]\nenabled = true\n")
    assert cli.main(["mobile", "pair", "Sam's iPhone"]) == 0
    out = capsys.readouterr().out
    assert f"tailscale serve --bg unix:{mobile.socket_path()}" in out and "serve --bg http" not in out
    assert "tailscale serve --https=443 off" in out          # moving a setup from before #238


def test_pairing_on_the_port_warns_that_another_account_could_take_it(monkeypatch, capsys):
    monkeypatch.setattr(cli, "tailnet_name", lambda: "")
    write_config('[mobile]\nenabled = true\nlisten = "port"\n')
    assert cli.main(["mobile", "pair", "Sam's iPhone"]) == 0
    out = capsys.readouterr().out
    assert "tailscale serve --bg http://127.0.0.1:8768" in out and "another account" in out


# ---------------------------------------------------------------------- rafiq doctor


def phone_check(cfg):
    return next(c for c in doctor.integrations(cfg, None) if c.label == "Phone page")


def test_doctor_says_the_phone_page_is_off():
    check = phone_check(config.load())
    assert check.optional and "off" in check.detail


def test_doctor_says_the_phone_page_answers_on_its_socket(served):
    check = phone_check(mobile_cfg("socket"))
    assert check.ok and "socket" in check.detail


def test_doctor_says_when_the_phone_page_isn_t_running(home):
    check = phone_check(mobile_cfg("socket"))
    assert not check.ok and "rafiq run" in check.fix


def test_doctor_never_calls_the_port_safe(on_port):
    check = phone_check(mobile_cfg("port", on_port))
    assert not check.ok and "another account" in check.detail and 'listen = "socket"' in check.fix


def test_doctor_says_when_another_account_holds_the_port(on_port, monkeypatch):
    monkeypatch.setattr(mobile, "owner_known", lambda: True)
    monkeypatch.setattr(panel, "listener_owner", lambda port: os.getuid() + 1)
    check = phone_check(mobile_cfg("port", on_port))
    assert not check.ok and "another account's program" in check.detail and "--https=443 off" in check.fix


def test_doctor_says_when_tailscale_still_sends_the_phone_to_the_port(served, monkeypatch):
    monkeypatch.setattr(mobile, "serve_targets", lambda run=None: [f"http://127.0.0.1:{PORT}"])
    check = phone_check(mobile_cfg("socket"))
    assert not check.ok and "--https=443 off" in check.fix and f"unix:{mobile.socket_path()}" in check.fix


def test_doctor_says_when_tailscale_doesn_t_send_the_phone_to_the_socket(served, monkeypatch):
    monkeypatch.setattr(mobile, "serve_targets", lambda run=None: [])
    check = phone_check(mobile_cfg("socket"))
    assert not check.ok and f"tailscale serve --bg unix:{mobile.socket_path()}" == check.fix


def test_doctor_is_happy_when_tailscale_sends_the_phone_to_the_socket(served, monkeypatch):
    monkeypatch.setattr(mobile, "serve_targets", lambda run=None: [f"unix:{mobile.socket_path()}"])
    assert phone_check(mobile_cfg("socket")).ok


def test_doctor_names_a_listen_setting_it_doesn_t_know(home):
    check = phone_check(mobile_cfg("sockets"))
    assert not check.ok and "sockets" in check.detail


# ---------------------------------------------------------------------- found in review


class Done:
    def __init__(self, code, out):
        self.returncode, self.stdout = code, out


def test_tailscale_s_serve_targets_are_read_from_its_status():
    status = ('{"TCP": {"443": {"HTTPS": true}}, "Web": {"desk.tail1.ts.net:443": {"Handlers": '
              '{"/": {"Proxy": "http://127.0.0.1:8768"}, '
              '"/x": {"Proxy": "unix:/home/a/.config/rafiq/run/mobile.sock"}}}}}')
    calls = []
    found = REAL_TARGETS(run=lambda args, **kw: calls.append((args, kw)) or Done(0, status))
    assert found == ["http://127.0.0.1:8768", "unix:/home/a/.config/rafiq/run/mobile.sock"]
    assert calls[0][0] == ["tailscale", "serve", "status", "--json"] and calls[0][1]["timeout"] == 5


@pytest.mark.parametrize("done", [Done(1, ""), Done(0, "not json")])
def test_a_tailscale_that_can_t_be_asked_gives_no_targets(done):
    assert REAL_TARGETS(run=lambda args, **kw: done) is None


def test_a_missing_tailscale_gives_no_targets():
    def missing(args, **kw):
        raise FileNotFoundError(args[0])

    assert REAL_TARGETS(run=missing) is None


@pytest.mark.parametrize("target, old", [("http://127.0.0.1:8768", True), ("http://localhost:8768/", True),
                                         ("http://127.0.0.1:87680", False), ("unix:/x/mobile.sock", False),
                                         ("http://127.0.0.1:8765", False)])
def test_only_a_target_on_the_phone_page_s_port_is_the_old_one(target, old):
    assert bool(mobile.on_port([target], 8768)) is old


def test_starting_on_the_socket_refuses_while_tailscale_still_sends_the_phone_to_the_port(home):
    with pytest.raises(ValueError, match="--https=443 off"):
        mobile.check_start(mobile_cfg("socket"), targets=lambda: ["http://127.0.0.1:8768"])


def test_starting_on_the_socket_goes_on_when_tailscale_can_t_be_asked(home):
    mobile.check_start(mobile_cfg("socket"), targets=lambda: None)


def test_a_taken_port_says_to_turn_serve_off_and_pair_again():
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    try:
        with pytest.raises(ValueError, match="--https=443 off.*pair each phone again"):
            mobile.check_start(mobile_cfg("port", taken.getsockname()[1]), targets=lambda: None)
    finally:
        taken.close()


def test_rafiq_run_checks_the_phone_page_before_the_models_load(home, monkeypatch, capsys):
    import argparse
    import signal

    monkeypatch.setattr("rafiq.assistant.Assistant", lambda cfg: pytest.fail("loaded the models first"))
    write_config('[mobile]\nenabled = true\nlisten = "sockets"\n')
    old = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit) as stopped:
            cli.cmd_run(argparse.Namespace(brain=None, mode=None, server=False, no_panel=True))
    finally:
        signal.signal(signal.SIGTERM, old)
    assert stopped.value.code == cli.EXIT_CONFIG == 78 and "listen" in capsys.readouterr().err


def test_the_service_isn_t_started_again_on_a_setting_to_fix():
    from rafiq import service

    assert "RestartPreventExitStatus=78" in service.unit_text("/usr/bin/python3")


def test_a_paired_phone_s_token_from_another_account_is_refused_on_the_socket(served, home, monkeypatch):
    devices = mobile.Devices(home / "mobile")
    _, token = devices.redeem(devices.start_pairing("Sam's iPhone"))
    monkeypatch.setattr(sock, "peer_uid", lambda conn: os.getuid() + 1)
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(mobile.socket_path())), timeout=5) as client:
        resp = client.post(f"http://{HOST}/api/ask", json={"text": "unlock the door"},
                           headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403 and served.asked == []


def test_the_socket_server_closes_connections_over_the_limit(served, monkeypatch):
    held = []
    try:
        for _ in range(mobile.MAX_CONNECTIONS):
            conn = socket.socket(socket.AF_UNIX)
            conn.connect(str(mobile.socket_path()))
            held.append(conn)
        extra = socket.socket(socket.AF_UNIX)
        extra.connect(str(mobile.socket_path()))   # blocking: a full backlog waits for the server to accept
        extra.settimeout(3)
        assert extra.recv(1) == b""                # closed at once, nothing sent
        extra.close()
    finally:
        for conn in held:
            conn.close()


def test_whose_program_listens_on_a_port_is_read_from_proc():
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    table = header + "\n".join([
        "   0: 0100007F:2240 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 1 1",
        "   1: 0100007F:2240 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1000        0 1 1",
    ])
    assert panel.listening_uid(table, 0x2240) == 1001 and panel.listening_uid(table, 0x2241) is None
