"""The panel, the phone bridge and the agent hub refuse a body length they can't trust before reading (#215)."""

import socket

import pytest
from test_brain_panel import panel  # noqa: F401  (fixture)
from test_phone import TOKEN, ollama, phone  # noqa: F401  (fixtures)
from test_remote import server  # noqa: F401  (fixture)

from rafiq.http_body import BodyError, body_length


def test_a_length_within_the_limit_is_read_as_given():
    assert body_length("120", 10_000) == 120


def test_no_length_means_an_empty_body():
    assert body_length(None, 10_000) == 0 and body_length("", 10_000) == 0


@pytest.mark.parametrize("value", ["-1", "abc", "1.5", "12 34", "0x10"])
def test_a_length_that_is_not_a_whole_number_from_zero_is_refused_as_bad(value):
    with pytest.raises(BodyError) as refused:
        body_length(value, 10_000)
    assert refused.value.status == 400


def test_a_length_over_the_limit_is_refused_as_too_large():
    with pytest.raises(BodyError) as refused:
        body_length("10001", 10_000)
    assert refused.value.status == 413


def answer_to(port: int, path: str, headers: dict[str, str]) -> int:
    """Send only the request line and headers, and keep the connection open: a server that waits for the
    body it was promised never answers, and the read times out."""
    with socket.create_connection(("127.0.0.1", port), timeout=3) as conn:
        lines = [f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", *(f"{k}: {v}" for k, v in headers.items())]
        conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        status_line = conn.makefile("rb").readline().decode()
    return int(status_line.split()[1])


@pytest.mark.parametrize("length", ["-1", "abc"])
def test_the_panel_refuses_a_bad_length_without_reading(panel, length):  # noqa: F811
    assistant, port = panel
    assert answer_to(port, "/api/text", {"X-Rafiq-Token": "t" * 40, "Content-Length": length}) == 400
    assert assistant.jobs == []


@pytest.mark.parametrize("length", ["-1", "abc"])
def test_the_phone_bridge_refuses_a_bad_length_without_reading(phone, length):  # noqa: F811
    _, port = phone
    assert answer_to(port, "/api/inbox", {"Authorization": f"Bearer {TOKEN}", "Content-Length": length}) == 400


@pytest.mark.parametrize("length", ["-1", "abc"])
def test_the_agent_hub_refuses_a_bad_length_without_reading(server, length):  # noqa: F811
    url, token, _, _ = server
    port = int(url.rsplit(":", 1)[1])
    headers = {"X-Rafiq-Agent": "desk", "Authorization": f"Bearer {token}", "Content-Length": length}
    assert answer_to(port, "/poll", headers) == 400


def test_a_body_too_large_is_still_refused(panel):  # noqa: F811
    _, port = panel
    assert answer_to(port, "/api/text", {"X-Rafiq-Token": "t" * 40, "Content-Length": "999999"}) == 413
