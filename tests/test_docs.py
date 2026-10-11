"""The guides for people and for AI assistants: they exist in English and Arabic, and the commands, files
and install extras they name are real, so a guide pasted into a chatbot doesn't send a beginner to a
command that was renamed."""

import re
import tomllib
from pathlib import Path

import pytest

from rafiq.__main__ import main

ROOT = Path(__file__).resolve().parent.parent
# English guide -> its Arabic version.
GUIDES = {"INSTALL_WITH_AI.md": "INSTALL_WITH_AI.ar.md", "CONTRIBUTING.md": "CONTRIBUTING.ar.md"}
DOCS = [*GUIDES, *GUIDES.values(), "CLAUDE.md", "README.md"]
# A file of this repo named in a guide: rafiq/brain.py, tests/conftest.py, config.example.toml.
FILE = re.compile(r"(?<![\w/])((?:rafiq|tests|\.github)/[\w./-]+\.(?:py|html|yml)|[\w.]+\.example\.toml)")


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("english, arabic", GUIDES.items())
def test_each_guide_has_an_arabic_version_and_they_link_to_each_other(english, arabic):
    assert arabic in read(english)
    assert english in read(arabic)
    assert re.search("[؀-ۿ]{3,}", read(arabic))


def test_the_readme_and_the_ai_context_point_to_the_guides():
    readme = read("README.md")
    for name in ("INSTALL_WITH_AI.md", "CONTRIBUTING.md"):
        assert name in readme
    assert "CONTRIBUTING.md" in read("CLAUDE.md")


def commands(text: str) -> set[str]:
    """`rafiq <command>` as written in code: after a backtick, `.venv/bin/` or at the start of a line."""
    return set(re.findall(r"(?:`|\.venv/bin/|^\s*)rafiq ([a-z][a-z-]*)", text, re.MULTILINE))


def test_commands_are_found_only_in_code():
    assert commands("run `rafiq doctor`, then\n  rafiq enroll\n.venv/bin/rafiq listen; Rafiq answers") == {
        "doctor", "enroll", "listen"}


@pytest.mark.parametrize("name", DOCS)
def test_every_command_a_guide_names_exists(name):
    for command in commands(read(name)):
        with pytest.raises(SystemExit) as exit_:
            main([command, "--help"])
        assert exit_.value.code == 0, f"{name}: rafiq {command}"


@pytest.mark.parametrize("name", ["INSTALL_WITH_AI.md", "INSTALL_WITH_AI.ar.md"])
def test_the_install_guide_checks_the_setup_and_records_the_wake_word(name):
    assert {"doctor", "enroll", "run"} <= commands(read(name))


@pytest.mark.parametrize("name", DOCS)
def test_every_file_a_guide_names_exists(name):
    for path in FILE.findall(read(name)):
        assert (ROOT / path).is_file(), f"{name}: {path}"


@pytest.mark.parametrize("name", DOCS)
def test_every_install_extra_a_guide_names_exists(name):
    extras = tomllib.loads(read("pyproject.toml"))["project"]["optional-dependencies"]
    for group in re.findall(r'-e "\.\[([\w,-]+)\]"', read(name)):
        assert set(group.split(",")) <= set(extras), f"{name}: {group}"
