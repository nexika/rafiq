import stat
import subprocess
import sys
import tarfile
from datetime import UTC, datetime

import pytest

from rafiq import config, maintain
from rafiq.__main__ import main

NOW = datetime(2026, 10, 10, 9, 30, 5, tzinfo=UTC)


def _settings(home):
    (home / "data" / "wake").mkdir(parents=True)
    (home / "data" / "piper").mkdir()
    (home / "google").mkdir()
    (home / "config.toml").write_text('anthropic_api_key = "sk-secret"\n')
    (home / "data" / "alarms.json").write_text("[]")
    (home / "data" / "wake" / "name-0.wav").write_bytes(b"RIFF")
    (home / "data" / "piper" / "en_US-voice.onnx").write_bytes(b"model")
    (home / "google" / "token.json").write_text("{}")
    (home / "panel.token").write_text("x" * 40)


def _names(path):
    with tarfile.open(path) as tar:
        return set(tar.getnames())


def test_backup_keeps_settings_recordings_and_data_but_not_downloads(rafiq_home):
    _settings(rafiq_home)
    path = maintain.backup(now=NOW)
    names = _names(path)
    assert {"rafiq/config.toml", "rafiq/data/alarms.json", "rafiq/data/wake/name-0.wav",
            "rafiq/google/token.json"} <= names
    assert not any("piper" in name or "panel.token" in name or "backups" in name for name in names)
    assert path.name == "rafiq-backup-20261010-093005.tar.gz"


def test_backup_is_readable_only_by_its_owner(rafiq_home):
    _settings(rafiq_home)
    path = maintain.backup(now=NOW)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_a_second_backup_does_not_contain_the_first(rafiq_home):
    _settings(rafiq_home)
    maintain.backup(now=NOW)
    second = maintain.backup(now=NOW.replace(second=6))
    assert not any(name.endswith(".tar.gz") for name in _names(second))


def test_backup_to_another_folder(rafiq_home, tmp_path):
    _settings(rafiq_home)
    path = maintain.backup(tmp_path / "usb", now=NOW)
    assert path.parent == tmp_path / "usb" and path.is_file()


def test_backup_never_follows_links_out_of_the_settings_folder(rafiq_home, tmp_path):
    _settings(rafiq_home)
    (tmp_path / "elsewhere.txt").write_text("private")
    (rafiq_home / "data" / "link.txt").symlink_to(tmp_path / "elsewhere.txt")
    assert not any("link.txt" in name for name in _names(maintain.backup(now=NOW)))


def test_backup_of_nothing_is_an_error(rafiq_home):
    with pytest.raises(maintain.MaintenanceError, match="nothing to back up"):
        maintain.backup(now=NOW)


class FakeRun:
    """Records the commands instead of running them; fails the ones named in `fail`."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = fail

    def __call__(self, args, cwd):
        self.calls.append(args)
        code = 1 if args[0] in self.fail else 0
        return subprocess.CompletedProcess(args, code, stdout="out\n", stderr="err\n" if code else "")


def test_update_pulls_then_installs_the_dependencies(tmp_path):
    (tmp_path / ".git").mkdir()
    run = FakeRun()
    maintain.update(tmp_path, run=run, extras=["google"])
    assert run.calls == [
        ["git", "pull", "--ff-only"],
        [sys.executable, "-m", "pip", "install", "--upgrade", "-e", f"{tmp_path}[google]"],
    ]


def test_update_without_extras_installs_the_plain_package(tmp_path):
    (tmp_path / ".git").mkdir()
    run = FakeRun()
    maintain.update(tmp_path, run=run, extras=[])
    assert run.calls[-1][-1] == str(tmp_path)


def test_update_stops_when_git_pull_fails(tmp_path):
    (tmp_path / ".git").mkdir()
    run = FakeRun(fail=("git",))
    with pytest.raises(maintain.MaintenanceError, match="git pull"):
        maintain.update(tmp_path, run=run, extras=[])
    assert len(run.calls) == 1


def test_update_refuses_outside_a_git_checkout(tmp_path):
    run = FakeRun()
    with pytest.raises(maintain.MaintenanceError, match="git checkout"):
        maintain.update(tmp_path, run=run, extras=[])
    assert run.calls == []


def test_installed_extras_are_the_ones_whose_packages_are_all_present():
    requires = [
        "httpx>=0.27",
        'google-api-python-client>=2.100; extra == "google"',
        'google-auth-oauthlib>=1.2; extra == "google"',
        'piper-tts>=1.2; extra == "offline-voice"',
        'pytest>=8; extra == "dev"',
    ]
    present = {"google-api-python-client", "google-auth-oauthlib", "pytest"}
    assert maintain.installed_extras(requires, present.__contains__) == ["dev", "google"]


def test_cli_backup_prints_where_it_saved(rafiq_home, capsys):
    _settings(rafiq_home)
    assert main(["backup"]) == 0
    out = capsys.readouterr().out
    assert str(config.home() / "backups") in out

