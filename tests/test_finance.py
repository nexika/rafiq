import pytest

from rafiq import config
from rafiq.brain import Brain
from rafiq.tools import REGISTRY, Tool, finance, load_all

ENGLISH = """Date,Description,Amount,Balance
2026-07-01,UBER *TRIP 1234,-14.20,985.80
2026-07-03,ACME PAYROLL,2500.00,3485.80
2026-07-05,NETFLIX.COM,-13.99,3471.81
2026-08-01,Uber BV trip,-9.80,3462.01
2026-08-05,NETFLIX.COM,-13.99,3448.02
2026-09-05,NETFLIX.COM,-13.99,3434.03
"""

# A French bank: semicolons, day-first dates, comma decimals, separate debit and credit columns.
FRENCH = """Date opération;Libellé;Débit;Crédit
05/09/2026;CARREFOUR MARKET;1 234,56;
12/09/2026;VIREMENT SALAIRE;;2 000,00
"""


@pytest.fixture
def ledger(tmp_path):
    return finance.Ledger(tmp_path / "finance.json")


def test_columns_are_detected_and_amounts_signed():
    rows = finance.parse_csv(ENGLISH)
    assert len(rows) == 6
    assert rows[0] == finance.Transaction("2026-07-01", "UBER *TRIP 1234", -14.20, "transport")
    assert rows[1].amount == 2500.0


def test_debit_and_credit_columns_with_european_numbers():
    rows = finance.parse_csv(FRENCH)
    assert [(r.date, r.amount) for r in rows] == [("2026-09-05", -1234.56), ("2026-09-12", 2000.0)]
    assert rows[0].payee == "CARREFOUR MARKET"


@pytest.mark.parametrize("text, amount", [
    ("-12.50", -12.5), ("12,50", 12.5), ("1.234,56", 1234.56), ("1,234.56", 1234.56), ("(7.00)", -7.0),
    ("€ -3,10", -3.1), ("+45", 45.0), ("12.50-", -12.5),
])
def test_parse_amount(text, amount):
    assert finance.parse_amount(text) == pytest.approx(amount)


def test_month_first_dates_are_recognised_from_the_column():
    text = "Date,Payee,Amount\n07/25/2026,SHOP,-1\n08/02/2026,SHOP,-2\n"
    assert [r.date for r in finance.parse_csv(text)] == ["2026-07-25", "2026-08-02"]


def test_a_file_without_the_needed_columns_is_refused():
    with pytest.raises(ValueError, match="column"):
        finance.parse_csv("Name,Age\nAli,3\n")


def test_importing_the_same_statement_twice_adds_nothing(ledger):
    assert ledger.add(finance.parse_csv(ENGLISH)) == (6, 0)
    assert ledger.add(finance.parse_csv(ENGLISH)) == (0, 6)
    assert len(ledger.transactions) == 6


def test_identical_charges_on_one_day_are_both_kept(ledger):
    text = "Date,Description,Amount\n2026-07-01,COFFEE,-3\n2026-07-01,COFFEE,-3\n"
    assert ledger.add(finance.parse_csv(text)) == (2, 0)


def test_a_correction_is_remembered_and_applied_backwards(tmp_path, ledger):
    ledger.add(finance.parse_csv(ENGLISH))
    assert ledger.set_rule("netflix", "fun") == 3
    assert {t.category for t in ledger.transactions if "NETFLIX" in t.payee} == {"fun"}
    again = finance.Ledger(tmp_path / "finance.json")
    assert again.rules == {"netflix": "fun"}
    again.add(finance.parse_csv("Date,Description,Amount\n2026-10-05,NETFLIX.COM,-13.99\n"))
    assert again.transactions[-1].category == "fun"


def test_money_in_and_out_per_month(ledger):
    ledger.add(finance.parse_csv(ENGLISH))
    months = ledger.monthly()
    assert months["2026-07"] == pytest.approx({"in": 2500.0, "out": 28.19})
    assert months["2026-09"] == pytest.approx({"in": 0.0, "out": 13.99})
    assert ledger.by_category("2026-07") == pytest.approx({"transport": 14.20, "subscriptions": 13.99})


def test_recurring_charges_are_spotted(ledger):
    ledger.add(finance.parse_csv(ENGLISH))
    found = ledger.recurring()
    assert [(r.payee, r.every, round(r.amount, 2)) for r in found] == [("NETFLIX.COM", "monthly", 13.99)]


def test_irregular_payees_are_not_recurring(ledger):
    text = "Date,Description,Amount\n2026-07-01,SHOP,-10\n2026-07-02,SHOP,-80\n2026-09-20,SHOP,-15\n"
    ledger.add(finance.parse_csv(text))
    assert ledger.recurring() == []


def test_tools_import_categorize_and_report(tmp_path):
    statement = tmp_path / "statement.csv"
    statement.write_text(ENGLISH)
    assert finance.bank_import(str(statement)).startswith("imported 6")
    assert "3 earlier" in finance.bank_categorize("Netflix", "Fun")
    summary = finance.bank_summary("2026-07")
    assert "in 2500.00" in summary and "out 28.19" in summary and "fun 13.99" in summary
    assert "NETFLIX.COM" in finance.bank_recurring()
    assert (config.data_dir() / "finance.json").stat().st_mode & 0o077 == 0


def test_import_takes_the_newest_csv_of_a_folder(tmp_path):
    import os

    (tmp_path / "old.csv").write_text("Date,Description,Amount\n2026-01-01,A,-1\n")
    (tmp_path / "new.csv").write_text(ENGLISH)
    os.utime(tmp_path / "old.csv", (1, 1))
    assert finance.bank_import(str(tmp_path)).startswith("imported 6")


def test_import_errors_are_short(tmp_path):
    assert finance.bank_import(str(tmp_path / "missing.csv")).startswith("error")
    assert finance.bank_summary("").startswith("no transactions")


def test_bank_tools_are_registered_and_private():
    tools = load_all({})
    for name in ("bank_import", "bank_categorize", "bank_summary", "bank_recurring"):
        assert tools[name].private, name
        assert REGISTRY[name].action({}) is None


def test_private_tools_keep_amounts_and_payees_out_of_events():
    events = []
    tool = Tool("bank_summary", "money", lambda: "NETFLIX -13.99", private=True)
    cfg = config.load()
    brain = Brain(cfg, {"bank_summary": tool}, lambda a, t: True, on_event=lambda k, d: events.append(d))
    assert brain._run_tool("bank_summary", {"payee": "Netflix"}, "en", None, brain.tools) == "NETFLIX -13.99"
    assert "13.99" not in str(events) and "Netflix" not in str(events)
    assert events[0]["name"] == "bank_summary"
