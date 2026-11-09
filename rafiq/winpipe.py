"""Rafiq's own programs on Windows (#242): a named pipe only this user can open, in place of the panel's Unix socket
(#234), since CPython has no AF_UNIX there.

The rule is the socket's: a program never sends the token to something it can't prove is this user's.
- The pipe is \\\\.\\pipe\\rafiq-panel-<SID>, made with a security descriptor that grants this user's SID alone
  (nothing inherited, not even SYSTEM), as the first instance of its name, so a pipe another account made first
  under that name is never shared: Rafiq says so and runs without it. It refuses clients on other computers.
- One instance always waits: the next is made before a connection is handed on, so while Rafiq runs the name is
  never free for another account to take.
- The server asks Windows whose program connected (GetNamedPipeClientProcessId, then that process's token) and
  serves only this user's. The client asks whose program is listening (GetNamedPipeServerProcessId) and sends
  nothing to another account's, and it opens the pipe at the identification level, so the server can't act as it.

Every Win32 call is in `Win32`, made only on Windows; the tests give a stand-in (`_api`).
"""

from __future__ import annotations

import io
import sys
import threading
import time

FAMILY = "windows-pipe"                 # a PipeConnection's `family`, beside socket.AF_UNIX and AF_INET
POLL_SECONDS = 0.01                     # between looks for data on a pipe, while a read waits
WAKE_SECONDS = 2                        # for shutdown() to reach the waiting instance
BUFFER = 65536


def pipe_name(sid: str, what: str = "panel") -> str:
    return rf"\\.\pipe\rafiq-{what}-{sid}"


def sddl(sid: str) -> str:
    """This user's SID alone, with full access; P: nothing inherited."""
    return f"D:P(A;;GA;;;{sid})"


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
    """This user's Rafiq on its pipe, once Windows says the listening program is this user's. FileNotFoundError
    when no Rafiq runs, TimeoutError when it doesn't take the connection in time, PermissionError when the program
    on the pipe isn't this user's, or this user's SID isn't known: nothing is sent then."""
    if not me:
        raise PermissionError("Windows didn't say which user this is, so nothing is sent")
    handle = api().open(name, timeout)
    try:
        owner = api().server_sid(handle)
    except OSError:
        owner = None
    if owner != me:
        api().close(handle)
        raise PermissionError("the program on Rafiq's pipe isn't this user's")
    return PipeConnection(handle, owner, peer_is_me=True)


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
                handle, self._waiting = self._waiting, None
                try:
                    api().connect(handle)
                except OSError:
                    api().close(handle)
                    if self._stop.is_set() or not self._next():
                        break
                    continue
                if self._stop.is_set():
                    api().close(handle)
                    break
                # The next instance first: with one always there, the name is never free for another program. One
                # that can't be made ends the pipe, and this connection goes unanswered, rather than serve on with
                # the name free once it closes.
                if not self._next():
                    api().close(handle)
                    break
                threading.Thread(target=self._serve, args=(handle,), name="rafiq-pipe", daemon=True).start()
        finally:
            self._stopped.set()

    def _next(self) -> bool:
        try:
            self._waiting = api().create(self.name, self.me, False)
        except OSError as exc:
            print(f"Rafiq's pipe stopped, so its own programs (rafiq ask, rafiq panel, rafiq mcp) can't reach it until "
                  f"it restarts: {exc.strerror or exc}", file=sys.stderr, flush=True)
            return False
        return True

    def _serve(self, handle) -> None:
        try:
            sid = api().client_sid(handle)
        except OSError:
            sid = None
        conn = PipeConnection(handle, sid, peer_is_me=sid is not None and sid == self.me)
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
        self.FlushFileBuffers = fn(k32, "FlushFileBuffers", w.BOOL, w.HANDLE)
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
        self.GetNamedPipeServerProcessId = fn(k32, "GetNamedPipeServerProcessId", w.BOOL, w.HANDLE,
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

    def server_sid(self, handle) -> str | None:
        pid = self.w.ULONG()
        if not self.GetNamedPipeServerProcessId(handle, self.ctypes.byref(pid)):
            return None
        return self._process_sid(pid.value)

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

    def close(self, handle) -> None:
        self.FlushFileBuffers(handle)       # what was written reaches the other end before it is cut off
        self.DisconnectNamedPipe(handle)    # fails harmlessly on a client's end
        self.CloseHandle(handle)
