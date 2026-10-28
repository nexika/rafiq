"""How long a request body may be, checked before reading it. The panel, the phone bridge and the agent hub
share it: `rfile.read(-1)` reads until the client closes the connection, so a negative length would let a
client with a token keep a handler thread and its memory for as long as it kept sending (#215)."""

from __future__ import annotations


class BodyError(ValueError):
    """The length can't be used: status 400 when it isn't a whole number from 0, 413 when it's over the limit."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def body_length(value: str | None, limit: int) -> int:
    """The Content-Length header as a number of bytes to read; no header means an empty body."""
    if not value:
        return 0
    if not value.isascii() or not value.isdigit():      # "-1", "1.5", "0x10" and "١٢" are all refused
        raise BodyError(400, "bad length")
    length = int(value)
    if length > limit:
        raise BodyError(413, "too large")
    return length
