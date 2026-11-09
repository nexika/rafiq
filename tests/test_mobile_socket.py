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
OWN = ("100.64.0.5", "fd7a:115c:a1e0::5")       # this computer's Tailscale addresses, as `tailscale status` says
PHONE = "100.64.0.9"
REAL_TARGETS = mobile.serve_targets     # the fixture below replaces it in every test; this is the real one


@pytest.fixture(autouse=True)
def no_real_tailscale(monkeypatch):
    """No test asks the real Tailscale: by default it sends the phone to the socket, and a test that wants another
    answer sets its own."""
    monkeypatch.setattr(mobile, "serve_targets", lambda run=None: [f"unix:{mobile.socket_path()}"])


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
    server = mobile.serve_socket(assistant, PORT, mobile.Devices(home / "mobile"), own_ips=lambda: OWN)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield assistant
    server.shutdown()
    server.server_close()


def get(path="/", headers=None):
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(mobile.socket_path())), timeout=5) as client:
        return client.get(f"http://{HOST}{path}", headers=headers).status_code


def through_tailscale(source, **more):
    """The headers tailscaled's `tailscale serve` sets (ipn/ipnlocal/serve.go): X-Forwarded-For is set, never
    appended to, to the connection's source address."""
    return {"Host": "desk.tail1234.ts.net", "X-Forwarded-For": source, "X-Forwarded-Proto": "https", **more}


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
    assert get(headers=through_tailscale(PHONE)) == 200


def test_another_account_s_program_is_refused_on_the_socket(served, monkeypatch):
    monkeypatch.setattr(sock, "peer_uid", lambda conn: os.getuid() + 1)
    assert get() == 403


def test_a_peer_whose_owner_can_t_be_told_is_refused(served, monkeypatch):
    monkeypatch.setattr(sock, "peer_uid", lambda conn: None)
    assert get() == 403


def test_a_live_socket_is_never_taken_over(served, home):
    with pytest.raises(ValueError, match="another Rafiq"):
        mobile.serve_socket(FakeAssistant(), PORT, mobile.Devices(home / "mobile"))


# ---------------------------------------------------------------------- through tailscaled, from this computer (#241)


@pytest.fixture
def paired(served, home, monkeypatch):
    """A paired phone's token, and tailscaled (root) bringing each request."""
    monkeypatch.setattr(sock, "peer_uid", lambda conn: 0)
    devices = mobile.Devices(home / "mobile")
    return devices.redeem(devices.start_pairing("Sam's iPhone"))[1]


def stop(token, headers):
    """POST /api/stop with the phone's token and the given headers (a list keeps repeated ones)."""
    if isinstance(headers, dict):
        headers = list(headers.items())
    headers = [*headers, ("Authorization", f"Bearer {token}")]
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(mobile.socket_path())), timeout=5) as client:
        return client.post(f"http://{HOST}/api/stop", headers=headers, json={}).status_code


def test_a_phone_s_token_sent_from_this_computer_s_own_tailnet_address_is_refused(paired):
    # Another account here sending to https://desk.tail1234.ts.net/: tailscaled's own listener on this
    # computer's Tailscale address takes it, so that address is the source.
    for own in OWN:
        assert stop(paired, through_tailscale(own)) == 403


def test_a_phone_s_token_sent_from_this_computer_s_loopback_is_refused(paired):
    for here in ("127.0.0.1", "127.8.9.1", "::1", "::ffff:127.0.0.1"):
        assert stop(paired, through_tailscale(here)) == 403, here


def test_the_paired_phone_still_gets_in_through_tailscale(paired):
    assert stop(paired, through_tailscale(PHONE)) == 200
    assert stop(paired, through_tailscale("fd7a:115c:a1e0::9")) == 200


def test_tailscaled_without_saying_where_from_is_refused(paired):
    assert stop(paired, {"Host": "desk.tail1234.ts.net"}) == 403


def test_a_source_tailscaled_could_not_have_set_is_refused(paired):
    for source in ("", "phone", f"{PHONE}, {PHONE}", f"{PHONE}:41641", "0.0.0.0", "::"):
        assert stop(paired, through_tailscale(source)) == 403, source


def test_two_forwarded_for_headers_are_refused(paired):
    headers = [("Host", "desk.tail1234.ts.net"), ("X-Forwarded-For", OWN[0]), ("X-Forwarded-For", PHONE)]
    assert stop(paired, headers) == 403


def test_the_page_itself_is_refused_from_this_computer_s_tailnet_address_too(paired):
    assert get(headers=through_tailscale(OWN[0])) == 403


def test_a_request_through_tailscale_funnel_is_refused(paired):
    # tailscaled marks Funnel (public internet) traffic, and strips the header from anything else.
    assert stop(paired, through_tailscale(PHONE, **{"Tailscale-Funnel-Request": "?1"})) == 403


def test_with_this_computer_s_tailnet_addresses_unknown_every_proxied_request_is_refused(home, monkeypatch):
    server = mobile.serve_socket(FakeAssistant(), PORT, mobile.Devices(home / "mobile"), own_ips=lambda: ())
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    monkeypatch.setattr(sock, "peer_uid", lambda conn: 0)
    try:
        assert get(headers=through_tailscale(PHONE)) == 403
    finally:
        server.shutdown()
        server.server_close()


def test_this_user_s_own_program_still_opens_the_page_without_tailscale(served):
    assert get() == 200                                 # the peer is this user, with no X-Forwarded-For


def test_a_tailscaled_running_as_this_user_is_checked_too(served):
    # The Mac App Store's Tailscale runs as the user, not root: the header still says where from.
    assert get(headers=through_tailscale(OWN[0])) == 403
    assert get(headers=through_tailscale(PHONE)) == 200


def test_another_address_of_this_computer_is_refused_as_a_source(paired):
    # Another account binds its connection to the LAN, a docker bridge or an IPv6 address of this computer before
    # sending to its own ts.net name; tailscaled then says that address (#241 review). A phone is always on the
    # tailnet's own ranges.
    for source in ("10.10.220.192", "192.168.1.10", "172.17.0.1", "fc00:f853:ccd:e793::1", "fe80::1",
                   "fe80::1%eth0", "8.8.8.8"):
        assert stop(paired, through_tailscale(source)) == 403, source


def test_this_computer_s_addresses_are_asked_again_until_tailscale_says_them():
    answers = iter([(), ("100.64.0.5",)])
    times = iter([0.0, 1.0, 40.0, 50.0])
    own = mobile.OwnAddresses(lambda: next(answers), clock=lambda: next(times))
    assert own() == ()
    assert own() == ()                                  # not asked again within 30 s
    assert own() == ("100.64.0.5",)
    assert own() == ("100.64.0.5",)                     # known: not asked again within a minute


def test_this_computer_s_addresses_are_asked_again_when_known_so_a_new_one_is_refused_too():
    # `tailscale switch` or a new login gives this computer another address while Rafiq runs.
    answers = iter([("100.80.1.2",), ()])
    times = iter([100.0, 200.0])
    own = mobile.OwnAddresses(lambda: next(answers), clock=lambda: next(times), known=("100.64.0.5",), asked=0.0)
    assert own() == ("100.80.1.2",)
    assert own() == ("100.80.1.2",)                     # tailscaled restarting says nothing: the last ones stand


def test_rafiq_run_on_the_port_refuses_what_comes_from_this_computer_s_tailnet_address(monkeypatch):
    monkeypatch.setattr(cli, "tailnet_self", lambda: ("desk.tail1234.ts.net", OWN))
    monkeypatch.setattr(mobile, "check_start", lambda cfg, targets=None: None)
    server = cli._start_mobile(mobile_cfg("port", 0), FakeAssistant())
    monkeypatch.setattr(mobile, "owner_known", lambda: True)
    monkeypatch.setattr(panel, "connection_owner", lambda client, server: 0)
    try:
        assert tcp_get(server.server_address[1], through_tailscale(OWN[0])) == 403
        assert tcp_get(server.server_address[1], through_tailscale(PHONE)) == 200
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------- the port, opted into


@pytest.fixture
def on_port(tmp_path):
    server = mobile.serve(FakeAssistant(), "127.0.0.1", 0, mobile.Devices(tmp_path / "mobile"), own_ips=lambda: OWN)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def tcp_get(port, headers=None):
    return httpx.get(f"http://127.0.0.1:{port}/", headers=headers, timeout=5).status_code


def test_on_linux_another_account_s_program_is_refused_on_the_port(on_port, monkeypatch):
    monkeypatch.setattr(mobile, "owner_known", lambda: True)
    monkeypatch.setattr(panel, "connection_owner", lambda client, server: os.getuid() + 1)
    assert tcp_get(on_port) == 403


def test_on_linux_tailscaled_and_this_user_are_let_in_on_the_port(on_port, monkeypatch):
    monkeypatch.setattr(mobile, "owner_known", lambda: True)
    monkeypatch.setattr(panel, "connection_owner", lambda client, server: 0)
    assert tcp_get(on_port, through_tailscale(PHONE)) == 200
    monkeypatch.setattr(panel, "connection_owner", lambda client, server: os.getuid())
    assert tcp_get(on_port) == 200


def test_on_the_port_too_tailscaled_bringing_this_computer_s_own_request_is_refused(on_port, monkeypatch):
    monkeypatch.setattr(mobile, "owner_known", lambda: True)
    monkeypatch.setattr(panel, "connection_owner", lambda client, server: 0)
    assert tcp_get(on_port, through_tailscale(OWN[0])) == 403
    assert tcp_get(on_port) == 403                      # root without saying where from


def test_where_a_port_s_owner_can_t_be_found_the_header_is_still_checked(on_port, monkeypatch):
    monkeypatch.setattr(mobile, "owner_known", lambda: False)                  # macOS, on the port
    assert tcp_get(on_port, through_tailscale(OWN[0])) == 403
    assert tcp_get(on_port, through_tailscale(PHONE)) == 200


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
    monkeypatch.setattr(cli, "tailnet_self", lambda: ("", ()))
    server = cli._start_mobile(mobile_cfg("socket"), FakeAssistant())
    try:
        assert server.socket.family == socket.AF_UNIX and sock.ours(mobile.socket_path())
    finally:
        server.shutdown()
        server.server_close()


def test_rafiq_run_refuses_what_comes_from_this_computer_s_tailnet_address(home, monkeypatch):
    monkeypatch.setattr(cli, "tailnet_self", lambda: ("desk.tail1234.ts.net", OWN))
    server = cli._start_mobile(mobile_cfg("socket"), FakeAssistant())
    monkeypatch.setattr(sock, "peer_uid", lambda conn: 0)
    try:
        assert get(headers=through_tailscale(OWN[0])) == 403
        assert get(headers=through_tailscale(PHONE)) == 200
    finally:
        server.shutdown()
        server.server_close()


def test_a_taken_port_stops_rafiq_with_a_clear_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "tailnet_self", lambda: ("", ()))
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
    monkeypatch.setattr(cli, "tailnet_self", lambda: ("", ()))
    with pytest.raises(SystemExit):
        cli._start_mobile(mobile_cfg("socket"), FakeAssistant())
    assert "RAFIQ_HOME" in capsys.readouterr().err


def test_an_unknown_listen_setting_stops_rafiq(monkeypatch, capsys):
    monkeypatch.setattr(cli, "tailnet_self", lambda: ("", ()))
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


def test_starting_on_the_socket_refuses_when_tailscale_can_t_be_asked(home):
    """Not knowing isn't "nothing on the old port": an old setup would stay open to whoever takes the port."""
    with pytest.raises(ValueError, match="tailscale serve status --json"):
        mobile.check_start(mobile_cfg("socket"), targets=lambda: None)


def test_starting_on_the_socket_goes_on_when_tailscale_sends_nothing_to_the_port(home):
    mobile.check_start(mobile_cfg("socket"), targets=lambda: [])


def test_a_tcp_forward_to_the_port_counts_as_the_old_target():
    status = '{"TCP": {"443": {"TCPForward": "127.0.0.1:8768"}}}'
    found = REAL_TARGETS(run=lambda args, **kw: Done(0, status))
    assert found == ["127.0.0.1:8768"] and mobile.on_port(found, 8768)


def test_the_port_starts_again_right_after_a_restart():
    """A connection the old Rafiq closed lingers in TIME_WAIT for about 60 s; the real server binds past it, and so
    must the check, or a restart would stop with exit 78 and tell the user to unpair every phone."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    conn, _ = listener.accept()
    conn.close()                    # the server closes first, so its side waits in TIME_WAIT
    client.close()
    listener.close()
    mobile.check_start(mobile_cfg("port", port), targets=lambda: [])


def test_doctor_says_when_tailscale_can_t_be_asked(served, monkeypatch):
    monkeypatch.setattr(mobile, "serve_targets", lambda run=None: None)
    check = phone_check(mobile_cfg("socket"))
    assert not check.ok and "tailscale serve status --json" in check.fix


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


OLD_UNIT = "[Service]\nExecStart=/home/a/venv/bin/python -P -m rafiq run\nRestart=on-failure\nRestartSec=5\n"


def test_a_unit_from_before_learns_not_to_restart_on_a_setting_to_fix(tmp_path, monkeypatch):
    from rafiq import service

    unit = tmp_path / "rafiq.service"
    unit.write_text(OLD_UNIT)
    monkeypatch.setattr(service, "unit_path", lambda: unit)
    calls = []
    assert service.refresh(run=lambda args: calls.append(args) or Done(0, "")) is True
    assert unit.read_text() == OLD_UNIT.replace("RestartSec=5\n", "RestartSec=5\nRestartPreventExitStatus=78\n")
    assert calls == [["daemon-reload"]]


def test_refreshing_leaves_a_current_unit_or_none_alone(tmp_path, monkeypatch):
    from rafiq import service

    unit = tmp_path / "rafiq.service"
    monkeypatch.setattr(service, "unit_path", lambda: unit)
    calls = []
    assert service.refresh(run=lambda args: calls.append(args) or Done(0, "")) is False
    unit.write_text(service.unit_text("/usr/bin/python3"))
    assert service.refresh(run=lambda args: calls.append(args) or Done(0, "")) is False and calls == []


def test_a_setting_to_fix_refreshes_the_unit_before_rafiq_stops(monkeypatch, capsys):
    from rafiq import service

    refreshed = []
    monkeypatch.setattr(service, "refresh", lambda run=None: refreshed.append(True) or True)
    with pytest.raises(SystemExit):
        cli._mobile_cannot_start(ValueError("x"))
    assert refreshed == [True]


def test_a_paired_phone_s_token_from_another_account_is_refused_on_the_socket(served, home, monkeypatch):
    devices = mobile.Devices(home / "mobile")
    _, token = devices.redeem(devices.start_pairing("Sam's iPhone"))
    monkeypatch.setattr(sock, "peer_uid", lambda conn: os.getuid() + 1)
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(mobile.socket_path())), timeout=5) as client:
        resp = client.post(f"http://{HOST}/api/ask", json={"text": "unlock the door"},
                           headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403 and served.asked == []


@pytest.mark.parametrize("server", [mobile.PhoneServer, mobile.PhoneSocketServer])
def test_the_listener_s_queue_holds_every_allowed_connection(server):
    """macOS refuses a Unix connect when the queue is full, where Linux waits; socketserver's default queue is 5."""
    assert server.request_queue_size >= mobile.MAX_CONNECTIONS


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
