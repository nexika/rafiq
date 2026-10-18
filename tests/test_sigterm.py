import argparse
import signal
import subprocess
import sys
import textwrap

import pytest

from rafiq import __main__ as cli


@pytest.fixture
def keep_sigterm():
    """cmd_run installs a process-wide handler; put the old one back after the test."""
    old = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, old)


def _run_args(**kw):
    return argparse.Namespace(brain=None, mode=None, server=False, no_panel=True, **kw)


def test_sigterm_during_run_stops_the_assistant_and_exits_0(monkeypatch, keep_sigterm):
    calls = []

    class FakeAssistant:
        def __init__(self, cfg):
            pass

        def run(self):
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler), "rafiq run left SIGTERM at its default, which skips atexit hooks"
            handler(signal.SIGTERM, None)
            calls.append("kept running")

        def stop(self):
            calls.append("stop")

    monkeypatch.setattr("rafiq.assistant.Assistant", FakeAssistant)

    assert cli.cmd_run(_run_args()) == 0
    assert calls == ["stop"]


def test_sigterm_during_run_server_exits_0(monkeypatch, keep_sigterm):
    class FakeServer:
        server_address = ("127.0.0.1", 0)
        closed = False

        def serve_forever(self):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        def server_close(self):
            FakeServer.closed = True

    monkeypatch.setattr("rafiq.remote.serve", lambda *a, **kw: FakeServer())

    args = _run_args()
    args.server = True
    assert cli.cmd_run(args) == 0
    assert FakeServer.closed


def test_sigterm_runs_atexit_hooks_in_a_real_process(tmp_path):
    """systemctl stop sends SIGTERM; the hook that ends Firefox's session must still run."""
    marker = tmp_path / "hook-ran"
    script = textwrap.dedent(f"""
        import atexit, os, signal, sys, time
        import rafiq.assistant
        from rafiq import __main__ as cli

        atexit.register(lambda: open({str(marker)!r}, "w").write("yes"))

        class FakeAssistant:
            def __init__(self, cfg):
                pass
            def run(self):
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(10)
            def stop(self):
                pass

        rafiq.assistant.Assistant = FakeAssistant
        sys.exit(cli.main(["run", "--no-panel"]))
    """)

    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)

    assert done.returncode == 0, done.stderr
    assert marker.read_text() == "yes"
