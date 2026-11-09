"""The panel's Unix socket, for Rafiq's own programs (#234). On macOS nothing says whose program opened a TCP
connection (there is no /proc), so `rafiq ask`, `rafiq panel`, `rafiq mcp`, the gesture tracker and the widget
talk over a socket in `<settings>/run`, a folder only this user can open. Both ends ask the kernel who is at the
other end: the server refuses another account's program, and a client sends nothing until the kernel says the
listening program is this user's, so the token never reaches a program of another account that took the
panel's place while Rafiq was down.

Windows has no AF_UNIX in CPython: there they use a named pipe only this user can open, with the same checks at
both ends (rafiq/winpipe.py, #242). Nothing falls back to TCP, where the token could reach another account's
program.
"""

from __future__ import annotations

import errno
import http.client
import json as jsonlib
import os
import socket
import socketserver
import stat
import struct
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import config, winpipe

# sun_path holds 104 bytes on macOS and 108 on Linux, the final NUL included.
MAX_PATH = 103 if sys.platform == "darwin" else 107
# LOCAL_PEERCRED on macOS: level SOL_LOCAL (0), option 1, struct xucred (version, uid, ngroups, 16 groups).
SOL_LOCAL, LOCAL_PEERCRED, XUCRED_VERSION = 0, 1, 0
XUCRED_SIZE = 76                                # u_int, uid_t, short, 2 bytes of padding, gid_t[16]
UCRED = "iII"                                   # Linux struct ucred: pid, uid, gid (uid_t is unsigned)
NAME = "panel.sock"
# The base of the socket servers. Windows has no UnixStreamServer: the class is still defined there, so the modules
# import, but it is never made there (unix_available() is False; the panel uses the pipe).
UnixServer = getattr(socketserver, "UnixStreamServer", socketserver.BaseServer)
NOT_YET = ("This command can't reach the running Rafiq here: it needs a socket only you can open, and this Python "
           "has none. Say it to Rafiq, or use the panel `rafiq run` opens in a terminal.")


class Unavailable(httpx.ConnectError):
    """No socket or pipe on this platform: what `rafiq listen` and the others print, instead of "Rafiq is not
    running"."""


def _windows() -> bool:
    from . import desktop

    return desktop.system() == "windows"


def available() -> bool:
    """Can Rafiq's own programs reach the running Rafiq here: a Unix socket, or on Windows its pipe (#242)?"""
    return _windows() or hasattr(socket, "AF_UNIX")


def unix_available() -> bool:
    """A Unix socket, which the phone page needs for `tailscale serve` (not on Windows yet)."""
    return not _windows() and hasattr(socket, "AF_UNIX")


def path() -> Path:
    return config.home() / "run" / NAME


def _closed_to_others(info: os.stat_result) -> bool:
    return info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0


def ours(where: Path) -> bool:
    """The folder is a real one (not a link), this user's, and closed to everyone else; the socket in it is a
    socket of this user's. A first check for a clear answer: what decides is the kernel's, once connected."""
    try:
        folder, file = where.parent.lstat(), where.lstat()
    except OSError:
        return False
    return (stat.S_ISDIR(folder.st_mode) and _closed_to_others(folder)
            and stat.S_ISSOCK(file.st_mode) and file.st_uid == os.getuid())


def _in_use(where: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(str(where))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def prepare(where: Path) -> None:
    """Make the folder for this user only and clear a socket left by a crash. Everything after the folder is
    opened goes through its descriptor, so a folder swapped in meanwhile changes nothing. ValueError when the
    path is too long to bind, the folder isn't safely this user's, or another Rafiq answers on the socket."""
    if len(os.fsencode(where)) > MAX_PATH:
        raise ValueError(f"the socket path {where} is longer than {MAX_PATH} bytes; set a shorter RAFIQ_HOME")
    folder = where.parent
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError(f"{folder} isn't a folder of this user's") from exc
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise ValueError(f"{folder} isn't a folder of this user's")
        os.fchmod(fd, 0o700)
        try:
            old = os.lstat(where.name, dir_fd=fd)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(old.st_mode):
            raise ValueError(f"{where} is there and isn't a socket")
        if _in_use(where):
            raise ValueError("another Rafiq already answers on its socket")
        os.unlink(where.name, dir_fd=fd)
    finally:
        os.close(fd)


def uid_from_ucred(data: bytes) -> int | None:
    if len(data) < struct.calcsize(UCRED):
        return None
    return struct.unpack_from(UCRED, data)[1]


def uid_from_xucred(data: bytes) -> int | None:
    if len(data) < 8:
        return None
    version, uid = struct.unpack_from("=II", data)
    return uid if version == XUCRED_VERSION else None


def peer_uid(conn: socket.socket) -> int | None:
    """Whose program is at the other end of a Unix connection, as the kernel says; None when it can't be told."""
    try:
        if sys.platform == "darwin":
            return uid_from_xucred(conn.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, XUCRED_SIZE))
        return uid_from_ucred(conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(UCRED)))
    except (OSError, AttributeError):
        return None


def local(conn) -> bool:
    """A connection on Rafiq's own socket or pipe, where whose program it is can be told."""
    return getattr(conn, "family", None) in (getattr(socket, "AF_UNIX", None), winpipe.FAMILY)


def peer_is_me(conn) -> bool:
    """Is the program at the other end of this local connection this user's: on the pipe as Windows said when it
    connected, on a Unix socket as the kernel says."""
    if getattr(conn, "family", None) == winpipe.FAMILY:
        return conn.peer_is_me is True
    return peer_uid(conn) == os.getuid()


class _Connection(http.client.HTTPConnection):
    """HTTP over the panel's socket, sent only once the kernel says the listening program is this user's."""

    def __init__(self, where: Path, host: str, read: float | None, connect: float | None):
        super().__init__(host, timeout=read)
        self.where, self.connect_timeout = where, connect

    def connect(self) -> None:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.settimeout(self.connect_timeout)
            conn.connect(str(self.where))
            if peer_uid(conn) != os.getuid():
                raise PermissionError(errno.EPERM, "the program on Rafiq's socket isn't this user's")
            conn.settimeout(self.timeout)
        except BaseException:
            conn.close()
            raise
        self.sock = conn


class _PipeConnection(http.client.HTTPConnection):
    """HTTP over Rafiq's pipe on Windows (#242), sent only once Windows says the listening program is this user's."""

    def __init__(self, name: str, me: str | None, host: str, read: float | None, connect: float | None):
        super().__init__(host, timeout=read)
        self.name, self.me, self.connect_timeout = name, me, connect

    def connect(self) -> None:
        conn = winpipe.connect(self.name, self.me, self.connect_timeout)
        conn.settimeout(self.timeout)
        self.sock = conn


def _timeouts(timeout) -> tuple[float | None, float | None]:
    if timeout is None or isinstance(timeout, (int, float)):
        return timeout, timeout
    return timeout.read, timeout.connect        # an httpx.Timeout


def post(url: str, *, json=None, headers: dict | None = None, timeout=5.0, me: str | None = None,
         name: str | None = None):
    """A POST to the running Rafiq over its socket (its pipe on Windows), answered as an httpx.Response; `url` still
    names 127.0.0.1 and the port, for the panel's Host check. Raises httpx.ConnectError, as for a Rafiq that isn't
    running, when the socket or pipe isn't there or isn't this user's: the token is never sent to it, and nothing
    falls back to TCP. A reply that doesn't come in time raises httpx.ReadTimeout. `me` (this user's SID) and
    `name` (the pipe's) are for tests; by default Windows says them."""
    request = httpx.Request("POST", url)
    if not available():
        raise Unavailable(NOT_YET, request=request)
    parts = urlsplit(url)
    read, connect = _timeouts(timeout)
    if _windows():
        me = me or winpipe.my_sid()
        conn = _PipeConnection(name or winpipe.pipe_name(me or ""), me, parts.netloc, read, connect)
    else:
        where = path()
        if not ours(where):
            raise httpx.ConnectError("Rafiq's socket isn't there, or isn't this user's", request=request)
        conn = _Connection(where, parts.netloc, read, connect)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    body = jsonlib.dumps(json).encode() if json is not None else b""
    sent = {"Host": parts.netloc, "Content-Type": "application/json", **(headers or {})}
    try:
        try:
            conn.connect()
        except TimeoutError as exc:
            raise httpx.ConnectTimeout("Rafiq didn't answer on its socket", request=request) from exc
        except OSError as exc:
            raise httpx.ConnectError(f"Rafiq's socket: {exc.strerror or exc}", request=request) from exc
        try:
            conn.request("POST", target or "/", body, sent)
            reply = conn.getresponse()
            content = reply.read()
        except TimeoutError as exc:
            raise httpx.ReadTimeout("no answer in time", request=request) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise httpx.ReadError(f"Rafiq stopped answering ({type(exc).__name__})", request=request) from exc
    finally:
        conn.close()
    kind = reply.getheader("Content-Type", "application/json")
    return httpx.Response(reply.status, headers={"Content-Type": kind}, content=content, request=request)
