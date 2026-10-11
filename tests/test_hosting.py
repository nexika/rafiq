from pathlib import Path

import pytest

from rafiq import __main__ as cli
from rafiq import config, hosting


class FakeRun:
    """Answers commands from a table, records what ran, never starts a process."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.ran = []

    def __call__(self, cmd, timeout=60):
        self.ran.append(cmd)
        for prefix, answer in self.answers.items():
            if tuple(cmd[:len(prefix)]) == prefix:
                return answer
        return 0, ""


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "src"
    (root / ".git").mkdir(parents=True)
    return root


def answers_for(root, behind="3", dirty="", active="active"):
    def g(*args):
        return ("git", "-C", str(root), *args)

    return {g("status", "--porcelain"): (0, dirty),
            g("fetch", "--quiet"): (0, ""),
            g("rev-list", "--count", "HEAD..@{u}"): (0, behind),
            g("rev-parse", "--short", "HEAD"): (0, "149c19d"),
            g("rev-parse", "--abbrev-ref", "HEAD"): (0, "main"),
            ("systemctl", "--user", "is-active", "rafiq"): (0 if active == "active" else 3, active)}


def test_panel_on_loopback_counts_as_private():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert hosting.panel_private({"panel": {"host": host, "port": 8765}})
    assert not hosting.panel_private({"panel": {"host": "0.0.0.0", "port": 8765}})


def test_status_reports_running_service_and_source(checkout):
    run = FakeRun(answers_for(checkout))
    checks = hosting.status(config.load(), root=checkout, run=run, probe=lambda host, port: True,
                            has_systemctl=True)
    by_label = {c.label: c for c in checks}
    assert by_label["Rafiq running"].ok
    assert by_label["service"].ok and by_label["service"].detail == "active"
    assert "149c19d" in by_label["version"].detail and "main" in by_label["version"].detail
    assert by_label["panel private"].ok
    assert "disk free" in by_label


def test_status_says_when_rafiq_is_down(checkout):
    run = FakeRun(answers_for(checkout, active="inactive"))
    checks = {c.label: c for c in hosting.status(config.load(), root=checkout, run=run,
                                                 probe=lambda host, port: False, has_systemctl=True)}
    assert not checks["Rafiq running"].ok
    assert not checks["service"].ok and checks["service"].detail == "inactive"


def test_status_accepts_rafiq_started_outside_the_service(checkout):
    run = FakeRun(answers_for(checkout, active="inactive"))
    checks = {c.label: c for c in hosting.status(config.load(), root=checkout, run=run,
                                                 probe=lambda host, port: True, has_systemctl=True)}
    assert checks["service"].ok and "outside the service" in checks["service"].detail


def test_status_without_systemd_or_git_skips_those_lines(tmp_path):
    run = FakeRun()
    checks = {c.label: c for c in hosting.status(config.load(), root=tmp_path, run=run,
                                                 probe=lambda host, port: True, has_systemctl=False)}
    assert "service" not in checks
    assert checks["version"].detail.startswith("0.1.0")
    assert run.ran == []


def test_status_flags_a_panel_open_to_the_network(checkout):
    cfg = config.load()
    cfg["panel"]["host"] = "0.0.0.0"
    checks = {c.label: c for c in hosting.status(cfg, root=checkout, run=FakeRun(answers_for(checkout)),
                                                 probe=lambda host, port: True, has_systemctl=False)}
    assert not checks["panel private"].ok


def test_update_pulls_reinstalls_and_restarts_the_service(checkout):
    run = FakeRun(answers_for(checkout))
    code, lines = hosting.update(root=checkout, run=run, python="/venv/bin/python", has_systemctl=True)
    assert code == 0
    assert ["git", "-C", str(checkout), "pull", "--ff-only", "--quiet"] in run.ran
    assert ["/venv/bin/python", "-m", "pip", "install", "--quiet", "-e", str(checkout)] in run.ran
    assert ["systemctl", "--user", "restart", "rafiq"] in run.ran
    assert any("3 new" in line for line in lines)


def test_update_when_up_to_date_changes_nothing(checkout):
    run = FakeRun(answers_for(checkout, behind="0"))
    code, lines = hosting.update(root=checkout, run=run, python="/venv/bin/python", has_systemctl=True)
    assert code == 0
    assert not any("pull" in cmd for cmd in run.ran)
    assert any("up to date" in line for line in lines)


def test_update_check_only_reports(checkout):
    run = FakeRun(answers_for(checkout))
    code, lines = hosting.update(root=checkout, run=run, python="py", has_systemctl=True, check=True)
    assert code == 0
    assert not any("pull" in cmd or "pip" in cmd for cmd in run.ran)
    assert any("3 new" in line for line in lines)


def test_update_refuses_local_changes(checkout):
    run = FakeRun(answers_for(checkout, dirty=" M rafiq/brain.py"))
    code, lines = hosting.update(root=checkout, run=run, python="py", has_systemctl=True)
    assert code == 1
    assert not any("pull" in cmd for cmd in run.ran)
    assert any("local changes" in line for line in lines)


def test_update_does_not_restart_a_stopped_service(checkout):
    run = FakeRun(answers_for(checkout, active="inactive"))
    code, _ = hosting.update(root=checkout, run=run, python="py", has_systemctl=True)
    assert code == 0
    assert ["systemctl", "--user", "restart", "rafiq"] not in run.ran


def test_update_stops_when_the_pull_fails(checkout):
    answers = answers_for(checkout)
    answers[("git", "-C", str(checkout), "pull")] = (1, "fatal: Not possible to fast-forward")
    run = FakeRun(answers)
    code, lines = hosting.update(root=checkout, run=run, python="py", has_systemctl=True)
    assert code == 1
    assert not any("pip" in cmd for cmd in run.ran)
    assert any("fast-forward" in line for line in lines)


def test_update_outside_a_git_checkout_explains(tmp_path):
    code, lines = hosting.update(root=tmp_path, run=FakeRun(), python="py", has_systemctl=False)
    assert code == 1
    assert any("git" in line for line in lines)


def test_cli_has_status_and_update(monkeypatch, capsys):
    monkeypatch.setattr(hosting, "update", lambda **kw: (0, [f"check={kw['check']}"]))
    assert cli.main(["update", "--check"]) == 0
    assert "check=True" in capsys.readouterr().out
    monkeypatch.setattr(hosting, "status", lambda cfg, **kw: [hosting.Check("Rafiq running", False, "down")])
    assert cli.main(["status"]) == 1
    assert "Rafiq running" in capsys.readouterr().out


def test_service_file_runs_rafiq_from_its_venv():
    unit = (Path(hosting.__file__).parent / "rafiq.service").read_text()
    assert "ExecStart=" in unit and "rafiq run" in unit
    assert "Restart=on-failure" in unit
