import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import numpy as np
import pytest

from rafiq import config, setup
from rafiq.tts import PIPER_FILES


class Fake:
    """Records what the wizard asked the machine to do; nothing touches the network or a device."""

    def __init__(self, level=0.2, fail=()):
        self.log, self.level, self.fail = [], level, set(fail)

    def _maybe_fail(self, step):
        if step in self.fail:
            raise RuntimeError(f"{step} broke")

    def download_voices(self, names, folder):
        self._maybe_fail("voices")
        self.log.append(("voices", list(names), folder))

    def download_whisper(self, size):
        self._maybe_fail("whisper")
        self.log.append(("whisper", size))

    def pull_ollama(self, model):
        self.log.append(("ollama", model))
        return True

    def record(self, seconds):
        self._maybe_fail("microphone")
        self.log.append(("microphone", seconds))
        return np.full(int(seconds * 16000), self.level, dtype="float32")

    def check(self):
        self.log.append(("check",))
        return 0

    def enroll(self):
        self.log.append(("enroll",))
        return 0

    def speak(self, cfg, text, tag):
        self.log.append(("speak", text, tag))
        return True

    def actions(self, ollama=True):
        return setup.Actions(download_voices=self.download_voices, download_whisper=self.download_whisper,
                             pull_ollama=self.pull_ollama if ollama else None, record=self.record,
                             check=self.check, enroll=self.enroll, speak=self.speak)

    def steps(self):
        return [entry[0] for entry in self.log]


def answers(*replies):
    queue = list(replies)

    def ask(_prompt=""):
        return queue.pop(0) if queue else ""

    return ask


def run(fake, *replies, secrets=(), **kwargs):
    said = []
    code = setup.run(ask=answers(*replies), ask_secret=answers(*secrets), say=said.append,
                     actions=fake.actions(kwargs.pop("ollama", True)), **kwargs)
    return code, said


def saved():
    return tomllib.loads((config.home() / "config.toml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- writing the settings


def test_toml_round_trips_nested_tables_and_values():
    data = {"user_name": 'Loai "L"', "brain": {"mode": "claude", "max_tokens": 600},
            "speech": {"languages": ["en", "ar"], "follow_up_seconds": 6.5},
            "voice": {"voices": {"ar-EG": "ar-EG-SalmaNeural"}}, "google": {"enabled": False}}
    assert tomllib.loads(setup.to_toml(data)) == data


def test_toml_quotes_keys_that_are_not_bare():
    data = {"voice": {"piper_voices": {"ar EG": "/x.onnx"}}}
    assert tomllib.loads(setup.to_toml(data)) == data


def test_toml_keeps_arabic_as_written():
    text = setup.to_toml({"user_name": "رفيق"})
    assert "رفيق" in text
    assert tomllib.loads(text) == {"user_name": "رفيق"}


# ---------------------------------------------------------------- the wizard


def test_defaults_everywhere_write_a_working_config():
    fake = Fake()
    code, _ = run(fake)
    assert code == 0
    cfg = saved()
    assert cfg["brain"]["mode"] == "auto"
    assert config.load()["brain"]["mode"] == "auto"


def test_answers_are_saved_in_the_config():
    fake = Fake()
    run(fake, "Loai", "claude", "westeurope", "Cairo", "ar-EG", secrets=("sk-ant-test", "azure-key"))
    cfg = saved()
    assert cfg["user_name"] == "Loai"
    assert cfg["brain"]["mode"] == "claude"
    assert cfg["anthropic_api_key"] == "sk-ant-test"
    assert cfg["voice"]["azure_key"] == "azure-key"
    assert cfg["voice"]["azure_region"] == "westeurope"
    assert cfg["place"]["city"] == "Cairo"


def test_the_config_with_keys_is_readable_only_by_the_user():
    run(Fake(), secrets=("sk-ant-test",))
    mode = stat.S_IMODE((config.home() / "config.toml").stat().st_mode)
    assert mode == 0o600


def test_a_wrong_choice_is_asked_again():
    fake = Fake()
    run(fake, "", "gpt", "ollama")
    assert saved()["brain"]["mode"] == "ollama"


def test_existing_settings_are_kept_and_offered_as_defaults():
    path = config.home() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('user_name = "Sara"\n[brain]\nmode = "ollama"\nollama_model = "qwen2.5:3b"\n'
                    '[google]\nenabled = true\n', encoding="utf-8")
    run(Fake())
    cfg = saved()
    assert cfg["user_name"] == "Sara"
    assert cfg["brain"] == {"mode": "ollama", "ollama_model": "qwen2.5:3b"}
    assert cfg["google"]["enabled"] is True


def test_a_key_already_in_the_environment_is_not_asked_or_written(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    run(Fake(), "", "", "westeurope", secrets=("azure-key",))
    cfg = saved()
    assert "anthropic_api_key" not in cfg
    assert cfg["voice"]["azure_key"] == "azure-key"


def test_an_empty_key_keeps_the_one_already_saved():
    path = config.home() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('anthropic_api_key = "sk-old"\n', encoding="utf-8")
    run(Fake())
    assert saved()["anthropic_api_key"] == "sk-old"


def test_keys_are_never_printed():
    _, said = run(Fake(), "", "", "westeurope", secrets=("sk-ant-secret", "azure-secret"))
    assert not any("sk-ant-secret" in line or "azure-secret" in line for line in said)


def test_only_missing_piper_voices_are_downloaded():
    folder = config.data_dir() / "piper"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{PIPER_FILES['en']}.onnx").write_bytes(b"voice")
    fake = Fake()
    run(fake)
    (_, names, where), = [e for e in fake.log if e[0] == "voices"]
    assert where == folder
    assert PIPER_FILES["en"] not in names
    assert set(names) == set(PIPER_FILES.values()) - {PIPER_FILES["en"]}


def test_no_voice_download_when_every_voice_is_there():
    folder = config.data_dir() / "piper"
    folder.mkdir(parents=True, exist_ok=True)
    for name in PIPER_FILES.values():
        (folder / f"{name}.onnx").write_bytes(b"voice")
    fake = Fake()
    run(fake)
    assert "voices" not in fake.steps()


def test_both_whisper_models_are_downloaded():
    fake = Fake()
    run(fake)
    assert ("whisper", "small") in fake.log
    assert ("whisper", "tiny") in fake.log


def test_ollama_model_is_pulled_unless_claude_only():
    fake = Fake()
    run(fake, "", "ollama")
    assert ("ollama", "qwen2.5:7b") in fake.log
    fake = Fake()
    run(fake, "", "claude")
    assert "ollama" not in fake.steps()


def test_without_ollama_installed_the_wizard_says_how_to_get_it():
    _, said = run(Fake(), ollama=False)
    assert any("ollama.com" in line for line in said)


@pytest.mark.parametrize("level, word", [(0.2, "ok"), (0.0005, "quiet")])
def test_the_microphone_level_is_reported(level, word):
    _, said = run(Fake(level=level))
    assert any("microphone" in line.lower() and word in line.lower() for line in said)


def test_one_step_failing_does_not_stop_the_others():
    fake = Fake(fail={"voices", "whisper", "microphone"})
    code, said = run(fake)
    assert code == 0
    assert fake.steps()[-3:] == ["check", "enroll", "speak"]
    assert any("voices broke" in line for line in said)


def test_enroll_can_be_skipped_by_answer_or_flag():
    fake = Fake()
    run(fake, "", "", "", "", "", "n")   # name, brain, city, language, microphone, enroll
    assert "enroll" not in fake.steps()
    fake = Fake()
    run(fake, enroll=False)
    assert "enroll" not in fake.steps()


def test_it_ends_with_a_spoken_welcome_in_the_chosen_language():
    fake = Fake()
    run(fake, "Loai", "", "", "ar-EG")
    kind, text, tag = fake.log[-1]
    assert (kind, tag) == ("speak", "ar-EG")
    assert "Loai" in text


def test_the_welcome_is_spoken_with_the_new_settings():
    seen = {}
    fake = Fake()
    fake.speak = lambda cfg, text, tag: seen.update(cfg=cfg) or True
    run(fake, "", "", "westeurope", secrets=("", "azure-key"))
    assert seen["cfg"]["voice"]["azure_key"] == "azure-key"


# ---------------------------------------------------------------- install.sh


INSTALL = Path(__file__).resolve().parent.parent / "install.sh"


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_install_script_is_valid_bash():
    assert INSTALL.is_file()
    assert subprocess.run(["bash", "-n", str(INSTALL)], timeout=10).returncode == 0


def test_install_script_is_executable():
    assert INSTALL.stat().st_mode & stat.S_IXUSR


def link_only(tmp_path, on_path=True):
    """Run `install.sh --link-only` from a copy of the checkout with a fake .venv, in a fresh home."""
    checkout, home = tmp_path / "rafiq", tmp_path / "home"
    (checkout / ".venv" / "bin").mkdir(parents=True)
    (checkout / ".venv" / "bin" / "rafiq").write_text("#!/bin/sh\n")
    (checkout / ".venv" / "bin" / "rafiq").chmod(0o755)
    shutil.copy(INSTALL, checkout / "install.sh")
    home.mkdir(exist_ok=True)
    path = (f"{home}/.local/bin:" if on_path else "") + "/usr/bin:/bin"
    done = subprocess.run(["bash", str(checkout / "install.sh"), "--link-only"], capture_output=True, text=True,
                          env={"HOME": str(home), "PATH": path}, timeout=10)
    return done, checkout / ".venv" / "bin" / "rafiq", home / ".local" / "bin" / "rafiq"


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_install_puts_rafiq_on_the_path_so_the_guides_work_as_written(tmp_path):
    done, venv_rafiq, link = link_only(tmp_path)
    assert done.returncode == 0
    assert link.is_symlink() and link.resolve() == venv_rafiq.resolve()
    assert "rafiq doctor" in done.stdout


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_install_says_how_to_add_local_bin_when_it_is_not_on_the_path(tmp_path):
    done, venv_rafiq, link = link_only(tmp_path, on_path=False)
    assert done.returncode == 0
    assert link.is_symlink()
    assert "PATH" in done.stdout and ".venv/bin/rafiq" in done.stdout


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_install_replaces_a_link_to_an_older_checkout(tmp_path):
    old = tmp_path / "home" / ".local" / "bin" / "rafiq"
    old.parent.mkdir(parents=True)
    old.symlink_to(tmp_path / "old-checkout" / ".venv" / "bin" / "rafiq")
    done, venv_rafiq, link = link_only(tmp_path)
    assert done.returncode == 0
    assert link.resolve() == venv_rafiq.resolve()


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_install_leaves_a_rafiq_that_is_not_a_link_alone(tmp_path):
    mine = tmp_path / "home" / ".local" / "bin" / "rafiq"
    mine.parent.mkdir(parents=True)
    mine.write_text("#!/bin/sh\necho mine\n")
    done, venv_rafiq, link = link_only(tmp_path)
    assert done.returncode == 0
    assert not link.is_symlink() and "echo mine" in link.read_text()
    assert ".venv/bin/rafiq" in done.stdout


def test_rafiq_setup_runs_the_wizard(monkeypatch):
    from rafiq import __main__ as cli

    seen = {}
    monkeypatch.setattr(setup, "run", lambda **kw: seen.update(kw) or 0)
    assert cli.main(["setup", "--no-enroll"]) == 0
    assert seen == {"enroll": False}
