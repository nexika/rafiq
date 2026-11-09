"""Rafiq's own programs on Windows (#242): a named pipe only this user can open, in place of the panel's Unix socket
(#234), since CPython has no AF_UNIX there.

The rule is the socket's: a program never sends the token to something it can't prove is this user's.
- The pipe is \\\\.\\pipe\\rafiq-panel-<SID>, made with a security descriptor that this user's SID owns and alone
  may open (nothing inherited, not even SYSTEM), labelled Medium, as the first instance of its name, so a pipe
  another account made first under that name is never shared: Rafiq says so and runs without it. It refuses
  clients on other computers.
- One instance always waits: the next is made before a connection is handed on, and an instance whose client left
  early waits again, so while Rafiq runs the name is never free for another account to take.
- The server asks Windows whose program connected (GetNamedPipeClientProcessId, then that process's token) and
  serves only this user's; the pipe's DACL already keeps other accounts out.
- The client reads the pipe's own security descriptor and sends nothing unless it is the one Rafiq makes: owned by
  this user's SID, open to it alone, Medium or higher. Never the server's process ID, which Windows reuses once
  the program that made the pipe exits. It opens the pipe at the identification level, so the server can't act as
  it.
- The whole request goes in one write, and no larger than the panel reads: a pipe's write waits while its buffer
  is full, and a server that already answered would never empty it.

Every Win32 call is in `Win32`, made only on Windows; the tests give a stand-in (`_api`).
"""

from __future__ import annotations

import io
import re
import sys
import threading
import time

FAMILY = "windows-pipe"                 # a PipeConnection's `family`, beside socket.AF_UNIX and AF_INET
POLL_SECONDS = 0.01                     # between looks for data on a pipe, while a read waits
WAKE_SECONDS = 2                        # for shutdown() to reach the waiting instance
REQUEST_SECONDS = 30                    # for a connection's request to come, on the server
BUFFER = 65536


def pipe_name(sid: str, what: str = "panel") -> str:
    return rf"\\.\pipe\rafiq-{what}-{sid}"


def sddl(sid: str) -> str:
    """Owned by this user's SID (even elevated, whose default owner is Administrators); this SID alone with full
    access, P: nothing inherited; labelled Medium, so a Low-integrity program of this user can't write to it."""
    return f"O:{sid}D:P(A;;GA;;;{sid})S:(ML;;NW;;;ME)"


_SECTION = re.compile(r"([OGDS]):((?:(?![OGDS]:).)*)")
_ACES = re.compile(r"([A-Z]*)((?:\([^()]*\))*)")
_LEVELS = {"ME": 0x2000, "MP": 0x2100, "HI": 0x3000, "SI": 0x4000}
MEDIUM = 0x2000


def _level(trustee: str) -> int:
    if trustee in _LEVELS:
        return _LEVELS[trustee]
    if trustee.startswith("S-1-16-") and trustee[7:].isdigit():
        return int(trustee[7:])
    return 0


_OWNER_ALIAS = re.compile(r"(?<=O:)([A-Z]{2})(?=[GDS]:|$)")
_TRUSTEE_ALIAS = re.compile(r"(?<=;)([A-Z]{2})(?=\))")


def expand_sids(text: str, full_sid) -> str:
    """SDDL with the owner's and each entry's account abbreviation (LA, BA, WD...) written as the full SID, so it
    can be compared with this user's. A label's level (ME, HI) is left: descriptor_ok reads those. An abbreviation
    Windows can't expand is left as it is, and then matches no user's SID."""
    def full(match: re.Match) -> str:
        try:
            return full_sid(match.group(1))
        except OSError:
            return match.group(1)

    sections = _SECTION.findall(text or "")
    out = []
    for key, value in sections:
        if key == "O":
            value = _OWNER_ALIAS.sub(full, f"O:{value}")[2:]
        elif key == "D":
            value = _TRUSTEE_ALIAS.sub(full, value)
        out.append(f"{key}:{value}")
    return "".join(out)


def descriptor_ok(text: str, me: str) -> bool:
    """Is this pipe's security descriptor, as Windows gives it back in SDDL, the one Rafiq makes: owned by this
    user's SID, with one allow for that SID and nothing inherited, labelled Medium or higher? What decides who made
    the pipe is the pipe object itself, never the process ID it reports, which Windows reuses (#242 review):
    another account can't make an object owned by this user's SID, and a Low-integrity program's gets a Low label."""
    sections = dict(_SECTION.findall(text or ""))
    if sections.get("O") != me or "D" not in sections:
        return False
    dacl = _ACES.fullmatch(sections["D"])
    if dacl is None or "P" not in dacl.group(1):
        return False
    aces = re.findall(r"\(([^()]*)\)", dacl.group(2))
    if len(aces) != 1:
        return False
    fields = aces[0].split(";")
    if len(fields) != 6 or fields[0] != "A" or fields[1] or not fields[2] or fields[5] != me:
        return False
    # The label Rafiq always sets, and none lower: a pipe with none isn't one Rafiq made, and a sandboxed program
    # that removed its own Low label mustn't pass as Medium (#242 review).
    label = _ACES.fullmatch(sections.get("S", ""))
    if label is None:
        return False
    levels = [_level(parts[5]) for parts in (ace.split(";") for ace in re.findall(r"\(([^()]*)\)", label.group(2)))
              if len(parts) == 6 and parts[0] == "ML"]
    return bool(levels) and min(levels) >= MEDIUM


_api = None


def api():
    """The Win32 calls; a test's stand-in when it set one."""
    global _api
    if _api is None:
        _api = Win32()
    return _api


def my_sid() -> str | None:
    """This user's SID, from this process's own token; None if Windows doesn't say."""
    try:
        return api().my_sid()
    except OSError:
        return None


# ---------------------------------------------------------------------- one connection


class _Reader(io.RawIOBase):
    def __init__(self, conn: PipeConnection):
        self.conn = conn

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self.conn.recv(len(buffer))
        buffer[:len(data)] = data
        return len(data)


class PipeConnection:
    """One end of a pipe, as much of a socket as http.server and http.client use: sendall, makefile("rb"),
    settimeout, close. `peer_is_me` is what Windows said about the program at the other end."""

    family = FAMILY

    def __init__(self, handle, peer_sid: str | None, peer_is_me: bool):
        self.handle, self.peer_sid, self.peer_is_me = handle, peer_sid, peer_is_me
        self._timeout: float | None = None
        self._closed = False

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def gettimeout(self) -> float | None:
        return self._timeout

    def recv(self, size: int) -> bytes:
        """Up to `size` bytes once some are there; b"" when the other end closed. TimeoutError past the timeout:
        a pipe opened for plain (not overlapped) reads can't stop a ReadFile, so it is called only on data."""
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        while True:
            waiting = api().available(self.handle)
            if waiting is None:
                return b""
            if waiting:
                return api().read(self.handle, min(size, waiting, BUFFER))
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("no answer on Rafiq's pipe in time")
            time.sleep(POLL_SECONDS)

    def sendall(self, data) -> None:
        api().write(self.handle, bytes(data))

    def makefile(self, mode: str = "rb", buffering: int = -1, **_kwargs):
        if "r" not in mode:
            raise ValueError("a pipe connection is read through makefile('rb') and written with sendall")
        return io.BufferedReader(_Reader(self), BUFFER if buffering in (-1, None) or buffering < 1 else buffering)

    def getsockname(self) -> str:
        return ""

    def shutdown(self, _how=None) -> None:
        pass

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            api().close(self.handle)


def connect(name: str, me: str | None, timeout: float | None) -> PipeConnection:
    """This user's Rafiq on its pipe, once the pipe itself says it is one Rafiq made: owned by this user, open to
    this user alone, Medium or higher (descriptor_ok). FileNotFoundError when no Rafiq runs, TimeoutError when it
    doesn't take the connection in time, PermissionError when the pipe isn't this user's Rafiq's, or this user's SID
    isn't known: nothing is sent then."""
    if not me:
        raise PermissionError("Windows didn't say which user this is, so nothing is sent")
    handle = api().open(name, timeout)
    try:
        text = api().pipe_security(handle)
    except OSError:
        text = ""
    if not descriptor_ok(text, me):
        api().close(handle)
        raise PermissionError("the pipe isn't one this user's Rafiq made")
    return PipeConnection(handle, me, peer_is_me=True)


# ---------------------------------------------------------------------- the server


class PipeServer:
    """The panel's handler on the pipe, as socketserver serves it on a socket: serve_forever in a thread,
    shutdown, server_close. Each connection gets a thread; a program of another account is closed unanswered."""

    def __init__(self, name: str, handler_class, me: str):
        self.name, self.RequestHandlerClass, self.me = name, handler_class, me
        self.server_address = name
        self._stop = threading.Event()
        self._stopped = threading.Event()
        self._stopped.set()
        try:
            self._waiting = api().create(name, me, True)
        except OSError as exc:
            raise ValueError(f"another program already has Rafiq's pipe ({name}): is another Rafiq running? "
                             f"({exc.strerror or exc})") from exc

    def serve_forever(self, poll_interval: float | None = None) -> None:
        self._stopped.clear()
        try:
            while not self._stop.is_set():
                handle = self._waiting          # still self._waiting while it waits, so server_close closes it
                if handle is None:
                    break
                try:
                    api().connect(handle)
                except OSError as exc:
                    if self._stop.is_set():
                        break
                    # A client that came and went before the call. The same instance waits again: closing it and
                    # making another would leave the name free in between, for another account's program to take.
                    try:
                        api().disconnect(handle)
                    except OSError:
                        self._end(f"{exc.strerror or exc}")
                        break
                    continue
                if self._stop.is_set():
                    break
                # The next instance first: with one always there, the name is never free for another program. One
                # that can't be made ends the pipe, and this connection goes unanswered, rather than serve on with
                # the name free once it closes.
                try:
                    self._waiting = api().create(self.name, self.me, False)
                except OSError as exc:
                    self._waiting = None
                    api().close(handle)
                    self._end(f"{exc.strerror or exc}")
                    break
                threading.Thread(target=self._serve, args=(handle,), name="rafiq-pipe", daemon=True).start()
        finally:
            self._stopped.set()

    def _end(self, why: str) -> None:
        self._stop.set()
        print(f"Rafiq's pipe stopped, so its own programs (rafiq ask, rafiq panel, rafiq mcp) can't reach it until it "
              f"restarts: {why}", file=sys.stderr, flush=True)

    def _serve(self, handle) -> None:
        try:
            sid = api().client_sid(handle)
        except OSError:
            sid = None
        conn = PipeConnection(handle, sid, peer_is_me=sid is not None and sid == self.me)
        conn.settimeout(REQUEST_SECONDS)    # a program of this user's that connects and says nothing ends, too
        try:
            if conn.peer_is_me:
                self.RequestHandlerClass(conn, ("pipe", sid), self)
        except Exception:                   # noqa: BLE001 (a request that fails ends its connection, not Rafiq)
            pass
        finally:
            conn.close()

    def shutdown(self) -> None:
        """Stop serve_forever: connect once, so the instance waiting for a client returns."""
        self._stop.set()
        if self._stopped.is_set():
            return
        try:
            api().close(api().open(self.name, WAKE_SECONDS))
        except OSError:
            pass
        self._stopped.wait(WAKE_SECONDS)

    def server_close(self) -> None:
        self._stop.set()
        waiting, self._waiting = self._waiting, None
        if waiting is not None:
            try:
                api().close(waiting)
            except OSError:
                pass


# ---------------------------------------------------------------------- the Win32 calls (Windows only)


class Win32:
    """kernel32 and advapi32 through ctypes. Errors are OSError with Windows' code (ctypes.WinError)."""

    PIPE_ACCESS_DUPLEX = 0x3
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    PIPE_REJECT_REMOTE_CLIENTS = 0x8            # with PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT, all 0
    PIPE_UNLIMITED_INSTANCES = 255
    GENERIC_READ_WRITE = 0x80000000 | 0x40000000
    OPEN_EXISTING = 3
    SECURITY_SQOS_PRESENT = 0x00100000
    SECURITY_IDENTIFICATION = 0x00010000        # the server may learn who connected, never act as them
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TOKEN_QUERY = 0x8
    TOKEN_USER = 1
    SE_KERNEL_OBJECT = 6
    OWNER_SECURITY_INFORMATION, DACL_SECURITY_INFORMATION, LABEL_SECURITY_INFORMATION = 0x1, 0x4, 0x10
    ERROR_FILE_NOT_FOUND, ERROR_BROKEN_PIPE, ERROR_PIPE_BUSY, ERROR_PIPE_CONNECTED = 2, 109, 231, 535
    ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED = 232, 233

    def __init__(self):
        import ctypes

        if not hasattr(ctypes, "WinDLL"):
            raise OSError("named pipes are Windows'")
        from ctypes import wintypes as w

        self.ctypes, self.w = ctypes, w
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", w.LPVOID), ("bInheritHandle", w.BOOL)]

        self.SecurityAttributes = SecurityAttributes
        self.INVALID = w.HANDLE(-1).value

        def fn(dll, name, restype, *argtypes):
            f = getattr(dll, name)
            f.restype, f.argtypes = restype, list(argtypes)
            return f

        self.CreateNamedPipeW = fn(k32, "CreateNamedPipeW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD,
                                   w.DWORD, w.DWORD, ctypes.POINTER(SecurityAttributes))
        self.ConnectNamedPipe = fn(k32, "ConnectNamedPipe", w.BOOL, w.HANDLE, w.LPVOID)
        self.DisconnectNamedPipe = fn(k32, "DisconnectNamedPipe", w.BOOL, w.HANDLE)
        self.CloseHandle = fn(k32, "CloseHandle", w.BOOL, w.HANDLE)
        self.CreateFileW = fn(k32, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD,
                              w.HANDLE)
        self.WaitNamedPipeW = fn(k32, "WaitNamedPipeW", w.BOOL, w.LPCWSTR, w.DWORD)
        self.ReadFile = fn(k32, "ReadFile", w.BOOL, w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID)
        self.WriteFile = fn(k32, "WriteFile", w.BOOL, w.HANDLE, w.LPCVOID, w.DWORD, ctypes.POINTER(w.DWORD),
                            w.LPVOID)
        self.PeekNamedPipe = fn(k32, "PeekNamedPipe", w.BOOL, w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD),
                                ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD))
        self.GetNamedPipeClientProcessId = fn(k32, "GetNamedPipeClientProcessId", w.BOOL, w.HANDLE,
                                              ctypes.POINTER(w.ULONG))
        self.OpenProcess = fn(k32, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
        self.GetCurrentProcess = fn(k32, "GetCurrentProcess", w.HANDLE)
        self.LocalFree = fn(k32, "LocalFree", w.HLOCAL, w.HLOCAL)
        self.OpenProcessToken = fn(adv, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
        self.GetTokenInformation = fn(adv, "GetTokenInformation", w.BOOL, w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD,
                                      ctypes.POINTER(w.DWORD))
        self.ConvertSidToStringSidW = fn(adv, "ConvertSidToStringSidW", w.BOOL, w.LPVOID,
                                         ctypes.POINTER(w.LPWSTR))
        self.ConvertSD = fn(adv, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL, w.LPCWSTR, w.DWORD,
                            ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.ULONG))
        self.ConvertStringSidToSidW = fn(adv, "ConvertStringSidToSidW", w.BOOL, w.LPCWSTR, ctypes.POINTER(w.LPVOID))
        self.ConvertSDToString = fn(adv, "ConvertSecurityDescriptorToStringSecurityDescriptorW", w.BOOL, w.LPVOID,
                                    w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), ctypes.POINTER(w.ULONG))
        self.GetSecurityInfo = fn(adv, "GetSecurityInfo", w.DWORD, w.HANDLE, ctypes.c_int, w.DWORD,
                                  ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.LPVOID),
                                  ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.LPVOID))

    def _fail(self, what: str):
        code = self.ctypes.get_last_error()
        error = self.ctypes.WinError(code)
        error.strerror = f"{what}: {error.strerror}"
        return error

    # -- whose program

    def _token_sid(self, process) -> str:
        ctypes, w = self.ctypes, self.w
        token = w.HANDLE()
        if not self.OpenProcessToken(process, self.TOKEN_QUERY, ctypes.byref(token)):
            raise self._fail("OpenProcessToken")
        try:
            size = w.DWORD()
            self.GetTokenInformation(token, self.TOKEN_USER, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not self.GetTokenInformation(token, self.TOKEN_USER, buffer, size, ctypes.byref(size)):
                raise self._fail("GetTokenInformation")
            sid = ctypes.cast(buffer, ctypes.POINTER(w.LPVOID))[0]   # TOKEN_USER starts with the SID's address
            text = w.LPWSTR()
            if not self.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise self._fail("ConvertSidToStringSidW")
            try:
                return text.value
            finally:
                self.LocalFree(ctypes.cast(text, w.HLOCAL))
        finally:
            self.CloseHandle(token)

    def _process_sid(self, pid: int) -> str | None:
        process = self.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return None                     # another account's process: Windows won't open it for this user
        try:
            return self._token_sid(process)
        except OSError:
            return None
        finally:
            self.CloseHandle(process)

    def my_sid(self) -> str:
        return self._token_sid(self.GetCurrentProcess())

    def client_sid(self, handle) -> str | None:
        pid = self.w.ULONG()
        if not self.GetNamedPipeClientProcessId(handle, self.ctypes.byref(pid)):
            return None
        return self._process_sid(pid.value)

    def pipe_security(self, handle) -> str:
        """The pipe's owner, DACL and integrity label, in SDDL, from the pipe object behind this handle."""
        ctypes, w = self.ctypes, self.w
        descriptor = w.LPVOID()
        info = self.OWNER_SECURITY_INFORMATION | self.DACL_SECURITY_INFORMATION | self.LABEL_SECURITY_INFORMATION
        code = self.GetSecurityInfo(handle, self.SE_KERNEL_OBJECT, info, None, None, None, None,
                                    ctypes.byref(descriptor))
        if code:
            raise ctypes.WinError(code)
        try:
            text = w.LPWSTR()
            if not self.ConvertSDToString(descriptor, 1, info, ctypes.byref(text), None):
                raise self._fail("ConvertSecurityDescriptorToStringSecurityDescriptorW")
            try:
                return expand_sids(text.value or "", self.full_sid)
            finally:
                self.LocalFree(ctypes.cast(text, w.HLOCAL))
        finally:
            self.LocalFree(ctypes.cast(descriptor, w.HLOCAL))

    def full_sid(self, alias: str) -> str:
        """An SDDL account abbreviation (LA: this computer's Administrator) as the full SID Windows means by it."""
        ctypes, w = self.ctypes, self.w
        sid = w.LPVOID()
        if not self.ConvertStringSidToSidW(alias, ctypes.byref(sid)):
            raise self._fail(f"the SID {alias}")
        try:
            text = w.LPWSTR()
            if not self.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise self._fail("ConvertSidToStringSidW")
            try:
                return text.value
            finally:
                self.LocalFree(ctypes.cast(text, w.HLOCAL))
        finally:
            self.LocalFree(ctypes.cast(sid, w.HLOCAL))

    # -- the pipe

    def create(self, name: str, sid: str, first: bool):
        ctypes, w = self.ctypes, self.w
        descriptor = w.LPVOID()
        if not self.ConvertSD(sddl(sid), 1, ctypes.byref(descriptor), None):
            raise self._fail("the pipe's security descriptor")
        try:
            attributes = self.SecurityAttributes(ctypes.sizeof(self.SecurityAttributes), descriptor, False)
            mode = self.PIPE_ACCESS_DUPLEX | (self.FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0)
            handle = self.CreateNamedPipeW(name, mode, self.PIPE_REJECT_REMOTE_CLIENTS, self.PIPE_UNLIMITED_INSTANCES,
                                           BUFFER, BUFFER, 0, ctypes.byref(attributes))
            if handle == self.INVALID or not handle:
                raise self._fail("CreateNamedPipeW")
            return handle
        finally:
            self.LocalFree(ctypes.cast(descriptor, w.HLOCAL))

    def connect(self, handle) -> None:
        if not self.ConnectNamedPipe(handle, None):
            if self.ctypes.get_last_error() != self.ERROR_PIPE_CONNECTED:     # a client came before the call
                raise self._fail("ConnectNamedPipe")

    def open(self, name: str, timeout: float | None):
        deadline = time.monotonic() + (timeout if timeout is not None else 3600)
        while True:
            handle = self.CreateFileW(name, self.GENERIC_READ_WRITE, 0, None, self.OPEN_EXISTING,
                                      self.SECURITY_SQOS_PRESENT | self.SECURITY_IDENTIFICATION, None)
            if handle != self.INVALID and handle:
                return handle
            code = self.ctypes.get_last_error()
            if code == self.ERROR_FILE_NOT_FOUND:
                raise FileNotFoundError(2, "Rafiq's pipe isn't there")
            if code != self.ERROR_PIPE_BUSY:
                raise self.ctypes.WinError(code)
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("Rafiq's pipe is busy")
            self.WaitNamedPipeW(name, max(1, int(min(left, 1) * 1000)))

    def available(self, handle) -> int | None:
        waiting = self.w.DWORD()
        if not self.PeekNamedPipe(handle, None, 0, None, self.ctypes.byref(waiting), None):
            if self.ctypes.get_last_error() in (self.ERROR_BROKEN_PIPE, self.ERROR_PIPE_NOT_CONNECTED,
                                                self.ERROR_NO_DATA):
                return None
            raise self._fail("PeekNamedPipe")
        return waiting.value

    def read(self, handle, size: int) -> bytes:
        buffer = self.ctypes.create_string_buffer(size)
        done = self.w.DWORD()
        if not self.ReadFile(handle, buffer, size, self.ctypes.byref(done), None):
            if self.ctypes.get_last_error() == self.ERROR_BROKEN_PIPE:
                return b""
            raise self._fail("ReadFile")
        return buffer.raw[:done.value]

    def write(self, handle, data: bytes) -> None:
        view = memoryview(data)
        while view:
            done = self.w.DWORD()
            chunk = bytes(view[:BUFFER])
            if not self.WriteFile(handle, chunk, len(chunk), self.ctypes.byref(done), None):
                code = self.ctypes.get_last_error()
                raise BrokenPipeError(32, "Rafiq's pipe closed") if code in (
                    self.ERROR_BROKEN_PIPE, self.ERROR_NO_DATA) else self._fail("WriteFile")
            view = view[done.value:]

    def disconnect(self, handle) -> None:
        """Ready this instance for the next client (after one that left before ConnectNamedPipe)."""
        if not self.DisconnectNamedPipe(handle):
            raise self._fail("DisconnectNamedPipe")

    def close(self, handle) -> None:
        # CloseHandle alone: what was written stays for the other end to read. DisconnectNamedPipe would throw it
        # away, and FlushFileBuffers would wait for a client that may never read (#242 review).
        self.CloseHandle(handle)
