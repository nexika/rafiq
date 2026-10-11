import subprocess
from pathlib import Path

import pytest

from rafiq import service
from rafiq.__main__ import main


class FakeSystemctl:
    """Records the systemctl commands instead of running them."""

    def __init__(self, fail_on: str | None = None):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(args)
        code = 1 if self.fail_on and self.fail_on in args else 0
        return subprocess.CompletedProcess(args, code, "", "boom" if code else "")


@pytest.fixture
def units(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "systemd" / "user"


def test_the_unit_runs_rafiq_with_this_python_and_restarts_on_failure():
    text = service.unit_text(python="/opt/venv/bin/python")
    assert "ExecStart=/opt/venv/bin/python -m rafiq run" in text
    assert "Restart=on-failure" in text
    assert "WantedBy=default.target" in text


def test_the_unit_reads_secrets_from_an_optional_env_file_and_never_holds_them(rafiq_home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    text = service.unit_text(python="/usr/bin/python3")
    assert f"EnvironmentFile=-{rafiq_home / 'rafiq.env'}" in text
    assert "sk-secret" not in text


def test_the_unit_keeps_a_moved_settings_folder(rafiq_home):
    assert f"Environment=RAFIQ_HOME={rafiq_home}" in service.unit_text(python="/usr/bin/python3")


def test_a_path_with_spaces_is_quoted():
    text = service.unit_text(python="/home/me/my env/bin/python")
    assert 'ExecStart="/home/me/my env/bin/python" -m rafiq run' in text


def test_the_unit_goes_in_the_systemd_user_folder(units):
    assert service.unit_path() == units / "rafiq.service"


def test_install_writes_the_unit_then_enables_and_starts_it(units):
    systemctl = FakeSystemctl()
    service.install(systemctl, python="/usr/bin/python3")
    assert (units / "rafiq.service").read_text().startswith("[Unit]")
    assert systemctl.calls == [["daemon-reload"], ["enable", "rafiq.service"], ["restart", "rafiq.service"]]


def test_installing_twice_keeps_one_unit_and_one_rafiq(units):
    systemctl = FakeSystemctl()
    service.install(systemctl, python="/usr/bin/python3")
    service.install(systemctl, python="/usr/bin/python3")
    assert [p.name for p in units.iterdir()] == ["rafiq.service"]
    assert systemctl.calls[-1] == ["restart", "rafiq.service"]     # restart, not a second start


def test_install_reports_a_systemctl_failure(units):
    with pytest.raises(service.ServiceError, match="boom"):
        service.install(FakeSystemctl(fail_on="enable"), python="/usr/bin/python3")


def test_uninstall_stops_disables_and_removes_the_unit(units):
    service.install(FakeSystemctl(), python="/usr/bin/python3")
    systemctl = FakeSystemctl()
    assert service.uninstall(systemctl) is True
    assert not (units / "rafiq.service").exists()
    assert systemctl.calls == [["disable", "--now", "rafiq.service"], ["daemon-reload"]]


def test_uninstall_without_a_unit_does_nothing(units):
    systemctl = FakeSystemctl()
    assert service.uninstall(systemctl) is False
    assert systemctl.calls == []


def test_the_commands_are_on_the_cli(units, monkeypatch, capsys):
    systemctl = FakeSystemctl()
    monkeypatch.setattr(service, "systemctl", systemctl)
    assert main(["install-service"]) == 0
    assert "journalctl --user -u rafiq" in capsys.readouterr().out
    assert main(["uninstall-service"]) == 0
    assert not Path(units / "rafiq.service").exists()


def test_the_cli_says_when_systemd_is_missing(units, monkeypatch, capsys):
    def missing(_args):
        raise service.ServiceError("systemctl not found: Rafiq can start at login only on systemd")

    monkeypatch.setattr(service, "systemctl", missing)
    assert main(["install-service"]) == 1
    assert "systemd" in capsys.readouterr().err
