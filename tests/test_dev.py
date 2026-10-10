import subprocess

import pytest

from rafiq import config
from rafiq.tools import REGISTRY, dev, load_all


class Runner:
    """Stands in for the Nexika helpers: records each command and answers with a fixed output."""

    def __init__(self, stdout="ok", returncode=0):
        self.stdout, self.returncode, self.calls = stdout, returncode, []

    def __call__(self, args, cwd, timeout):
        self.calls.append((args, cwd, timeout))
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, "")


def ctx_for(project):
    cfg = config.load()
    cfg["dev"]["project"] = str(project)
    return type("Ctx", (), {"cfg": cfg, "tag": "en"})()


def installed(name):
    return f"/usr/bin/{name}"


@pytest.mark.parametrize("call, command", [
    (lambda ctx, **kw: dev.run_check("test", ctx=ctx, **kw), ["barq", "run:test"]),
    (lambda ctx, **kw: dev.run_check("lint", ctx=ctx, **kw), ["barq", "run:lint"]),
    (lambda ctx, **kw: dev.git_status(ctx=ctx, **kw), ["barq", "git-status"]),
    (lambda ctx, **kw: dev.ci_failure(ctx=ctx, **kw), ["tabib", "triage", "--cwd", "PROJECT"]),
    (lambda ctx, **kw: dev.branch_and_pr(ctx=ctx, **kw), ["mizan", "report", "--cwd", "PROJECT"]),
])
def test_each_tool_runs_its_helper_in_the_project_folder(tmp_path, call, command):
    run = Runner("2 passed")
    assert "2 passed" in call(ctx_for(tmp_path), run=run, which=installed)
    args, cwd, timeout = run.calls[0]
    assert args == [installed(command[0])] + [str(tmp_path) if a == "PROJECT" else a for a in command[1:]]
    assert cwd == tmp_path and timeout > 0


def test_only_known_checks_run(tmp_path):
    run = Runner()
    assert dev.run_check("rm -rf /", ctx=ctx_for(tmp_path), run=run, which=installed).startswith("error")
    assert run.calls == []


def test_a_missing_helper_says_which_plugin_to_install(tmp_path):
    run = Runner()
    text = dev.git_status(ctx=ctx_for(tmp_path), run=run, which=lambda name: None)
    assert text.startswith("error") and "barq" in text and run.calls == []


def test_no_project_folder_is_an_error(tmp_path):
    run = Runner()
    cfg = config.load()
    assert dev.git_status(ctx=type("Ctx", (), {"cfg": cfg})(), run=run, which=installed).startswith("error")
    assert dev.git_status(ctx=ctx_for(tmp_path / "gone"), run=run, which=installed).startswith("error")
    assert run.calls == []


def test_the_folder_set_in_the_panel_is_remembered(tmp_path):
    assert dev.set_project(str(tmp_path / "gone")).startswith("error")
    assert dev.set_project(str(tmp_path)) == str(tmp_path)
    assert dev.project_folder(config.load()) == tmp_path   # the settings file names none: the saved one


def test_the_settings_file_wins_over_nothing_saved(tmp_path):
    cfg = config.load()
    assert dev.project_folder(cfg) is None
    cfg["dev"]["project"] = str(tmp_path)
    assert dev.project_folder(cfg) == tmp_path


def test_long_output_is_cut_and_colours_removed(tmp_path):
    run = Runner("\x1b[31mFAIL\x1b[0m test_a\n" + "x" * 10_000)
    text = dev.run_check("test", ctx=ctx_for(tmp_path), run=run, which=installed)
    assert "\x1b" not in text and "FAIL test_a" in text and len(text) < 5000


def test_a_helper_that_hangs_is_an_error(tmp_path):
    def run(args, cwd, timeout):
        raise subprocess.TimeoutExpired(args, timeout)

    assert dev.git_status(ctx=ctx_for(tmp_path), run=run, which=installed).startswith("error")


def test_developer_tools_only_read_and_take_no_command_text():
    load_all({})
    for name in ("dev_check", "dev_git_status", "dev_ci_failure", "dev_pr_status"):
        tool = REGISTRY[name]
        assert tool.action({}) is None, name
        for spec in tool.params.values():
            assert spec.get("enum"), name   # nothing free-form the model could turn into a command
