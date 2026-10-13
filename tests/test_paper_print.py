import subprocess
from datetime import date

import pytest

from rafiq import config, lang
from rafiq import paper_print as paper_mod
from rafiq.__main__ import main
from rafiq.paper_print import CupsPrinter, DailyLock, MorningPaper, PrintError, timer_units
from rafiq.tools import load_all

DAY = date(2026, 10, 10)


class FakePrinter:
    def __init__(self, fail=False):
        self.printed, self.fail = [], fail

    def print_file(self, path):
        if self.fail:
            raise PrintError("printer is out of paper")
        self.printed.append(path)
        return "office-12"


def make(tmp_path, fail=False, **settings):
    cfg = config.load()
    cfg["paper"].update(settings)
    printer = FakePrinter(fail)
    paper = MorningPaper(cfg, printer=printer, lock=DailyLock(tmp_path / "paper.lock"),
                         folder=tmp_path / "paper")
    return paper, printer


def write_pdf(paper, day=DAY):
    path = paper.pdf_for(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n")
    return path


def test_the_paper_prints_after_a_yes(tmp_path):
    paper, printer = make(tmp_path)
    pdf = write_pdf(paper)
    asked = []
    result = paper.print_today(lambda action: asked.append(action) or True, DAY)
    assert result.startswith("printed") and "office-12" in result
    assert printer.printed == [pdf]
    assert asked == ["print today's morning paper"]


def test_nothing_prints_without_a_yes(tmp_path):
    paper, printer = make(tmp_path)
    write_pdf(paper)
    assert paper.print_today(lambda _action: False, DAY) == "declined"
    assert printer.printed == []
    # A no today does not use up the day: a later yes still prints.
    assert paper.print_today(lambda _action: True, DAY).startswith("printed")


def test_the_paper_never_prints_twice_in_a_day(tmp_path):
    paper, printer = make(tmp_path)
    write_pdf(paper)
    paper.print_today(lambda _action: True, DAY)
    asked = []
    assert paper.print_today(lambda action: asked.append(action) or True, DAY) == "already printed today"
    assert len(printer.printed) == 1 and asked == []        # and it does not even ask


def test_the_lock_is_shared_by_every_rafiq_process(tmp_path):
    """The timer, the voice and the terminal each make their own MorningPaper; the lock file is one."""
    first, printer = make(tmp_path)
    write_pdf(first)
    first.print_today(lambda _action: True, DAY)
    second, other_printer = make(tmp_path)
    assert second.print_today(lambda _action: True, DAY) == "already printed today"
    assert other_printer.printed == []


def test_a_new_day_prints_again(tmp_path):
    paper, printer = make(tmp_path)
    write_pdf(paper, DAY)
    write_pdf(paper, date(2026, 10, 11))
    paper.print_today(lambda _action: True, DAY)
    assert paper.print_today(lambda _action: True, date(2026, 10, 11)).startswith("printed")
    assert len(printer.printed) == 2


def test_no_paper_for_today_is_said_before_asking(tmp_path):
    paper, printer = make(tmp_path)
    asked = []
    result = paper.print_today(lambda action: asked.append(action) or True, DAY)
    assert result.startswith("error: no paper for today") and asked == [] and printer.printed == []


def test_a_failed_print_can_be_tried_again(tmp_path):
    paper, _ = make(tmp_path, fail=True)
    write_pdf(paper)
    result = paper.print_today(lambda _action: True, DAY)
    assert result.startswith("error") and "out of paper" in result
    paper.printer = FakePrinter()
    assert paper.print_today(lambda _action: True, DAY).startswith("printed")


def test_the_file_setting_names_where_the_paper_is(tmp_path):
    paper, _ = make(tmp_path, file=str(tmp_path / "news-{date}.pdf"))
    assert paper.pdf_for(DAY) == tmp_path / "news-2026-10-10.pdf"


def test_after_the_brief_only_when_asked_for(tmp_path):
    paper, printer = make(tmp_path)
    write_pdf(paper)
    assert paper.after_brief(lambda _action: True, DAY) is None and printer.printed == []
    paper, printer = make(tmp_path, after_brief=True)
    assert paper.after_brief(lambda _action: True, DAY).startswith("printed")
    assert paper.after_brief(lambda _action: True, DAY) == "already printed today"     # the second brief


def test_cups_runs_lp_with_an_argument_list():
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "request id is Office-12 (1 file(s))\n", "")

    assert CupsPrinter("Office", run=run).print_file("/tmp/p.pdf") == "Office-12"
    args, kwargs = calls[0]
    assert args == ["lp", "-d", "Office", "--", "/tmp/p.pdf"] and kwargs["timeout"] and not kwargs.get("shell")
    CupsPrinter(run=run).print_file("/tmp/p.pdf")
    assert calls[1][0] == ["lp", "--", "/tmp/p.pdf"]          # the default printer


@pytest.mark.parametrize("outcome", [
    subprocess.CompletedProcess([], 1, "", "lp: The printer or class does not exist."),
    FileNotFoundError("lp"),
    subprocess.TimeoutExpired("lp", 30),
])
def test_cups_failures_are_print_errors(outcome):
    def run(args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with pytest.raises(PrintError):
        CupsPrinter(run=run).print_file("/tmp/p.pdf")


def test_the_timer_offers_at_the_chosen_time():
    units = timer_units("07:30", "/usr/bin/python3")
    assert "OnCalendar=*-*-* 07:30:00" in units["rafiq-paper.timer"]
    assert "ExecStart=/usr/bin/python3 -P -m rafiq paper offer" in units["rafiq-paper.service"]


@pytest.mark.parametrize("bad", ["7", "25:00", "07:60", "seven", "07:30; rm -rf ~"])
def test_the_timer_needs_a_real_time(bad):
    with pytest.raises(ValueError):
        timer_units(bad, "/usr/bin/python3")


def test_rafiq_paper_timer_writes_the_systemd_units(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert main(["paper", "timer", "--at", "06:45"]) == 0
    timer = tmp_path / "xdg" / "systemd" / "user" / "rafiq-paper.timer"
    assert "06:45:00" in timer.read_text()
    assert "systemctl --user enable --now rafiq-paper.timer" in capsys.readouterr().out


def test_rafiq_paper_print_asks_in_the_terminal(tmp_path, monkeypatch, capsys):
    printer = FakePrinter()
    monkeypatch.setattr(paper_mod, "CupsPrinter", lambda name="": printer)
    write_pdf(MorningPaper(config.load(), printer=printer), date.today())
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert main(["paper", "print"]) == 1 and printer.printed == []
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert main(["paper", "print"]) == 0 and len(printer.printed) == 1


def test_the_print_tool_asks_first_and_prints(tmp_path, monkeypatch):
    tools = load_all(config.load())
    tool = tools["print_paper"]
    assert tool.action({}) == "print today's morning paper"
    printer = FakePrinter()
    monkeypatch.setattr(paper_mod, "CupsPrinter", lambda name="": printer)
    cfg = config.load()
    write_pdf(MorningPaper(cfg, printer=printer), date.today())
    ctx = type("Ctx", (), {"cfg": cfg})()
    assert tool.run({}, ctx).startswith("printed") and len(printer.printed) == 1


def test_the_paper_phrases_exist_in_every_language():
    for key in ("paper_printed", "paper_failed"):
        for tag in lang.TAGS:
            assert key in lang.PHRASES and tag in lang.PHRASES[key], (key, tag)


@pytest.mark.parametrize("printed, said", [("printed (job 1)", "paper_printed"),
                                           ("error: printing failed: jam", "paper_failed"),
                                           ("already printed today", None), ("declined", None)])
def test_the_running_rafiq_says_how_the_offer_went(printed, said):
    from rafiq.assistant import Assistant, Events

    spoken = []
    fake = type("A", (), {})()
    fake.tag, fake.events = "en", Events()
    fake.paper = type("P", (), {"print_today": lambda self, confirm: printed})()
    fake.confirm = lambda action, tag: True
    fake.say = lambda text, tag: spoken.append(text)
    Assistant.offer_paper(fake)
    assert spoken == ([lang.phrase(said, "en")] if said else [])
