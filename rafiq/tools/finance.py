"""Bank statements: import a CSV from the bank, sort the transactions into categories, money in and out
per month, and the charges that come back every week or month.

Everything stays on this computer, in data/finance.json (readable only by you). There is no bank sync.
A correction ("put Uber under transport") becomes a rule that is remembered and applied to the earlier
transactions too. The tools are private: amounts and payees never go to the event log or the panel.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .. import config, subscriptions
from . import tool


@dataclass
class Transaction:
    date: str           # ISO, YYYY-MM-DD
    payee: str          # the bank's description, as written
    amount: float       # negative: money out
    category: str = "other"


@dataclass
class Recurring:
    payee: str
    every: str          # "weekly", "monthly" or "yearly"
    amount: float       # the usual amount taken
    last: str           # date of the latest charge


def _plain(text: str) -> str:
    """Lower case without accents, so 'Libellé' and 'libelle' are the same column."""
    text = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(c for c in text if not unicodedata.combining(c))


# ---------------------------------------------------------------------- categories

# Keyword -> category, for a first sort before any correction. The user's rules always win, and the
# longest keyword that matches wins among each set ("uber eats" before "uber").
DEFAULT_RULES = {
    "uber": "transport", "bolt": "transport", "lyft": "transport", "careem": "transport", "taxi": "transport",
    "sncf": "transport", "ratp": "transport", "navigo": "transport", "renfe": "transport", "db bahn": "transport",
    "parking": "transport", "esso": "transport", "shell": "transport",
    "carrefour": "groceries", "lidl": "groceries", "aldi": "groceries", "auchan": "groceries",
    "leclerc": "groceries", "monoprix": "groceries", "intermarche": "groceries", "rewe": "groceries",
    "edeka": "groceries", "mercadona": "groceries", "tesco": "groceries", "sainsbury": "groceries",
    "netflix": "subscriptions", "spotify": "subscriptions", "disney": "subscriptions", "deezer": "subscriptions",
    "apple.com": "subscriptions", "icloud": "subscriptions", "youtube": "subscriptions",
    "uber eats": "eating out", "deliveroo": "eating out", "just eat": "eating out", "talabat": "eating out",
    "mcdonald": "eating out", "starbucks": "eating out", "restaurant": "eating out", "cafe": "eating out",
    "amazon": "shopping", "ikea": "shopping", "zara": "shopping",
    "rent": "housing", "loyer": "housing", "miete": "housing", "alquiler": "housing",
    "edf": "utilities", "engie": "utilities", "electricity": "utilities", "vodafone": "utilities",
    "orange": "utilities", "internet": "utilities",
    "pharmacy": "health", "pharmacie": "health", "apotheke": "health", "farmacia": "health",
    "salary": "income", "salaire": "income", "payroll": "income", "gehalt": "income", "nomina": "income",
    "atm": "cash", "retrait": "cash",
}


def _matches(keyword: str, payee: str) -> bool:
    # The keyword starts a word: "uber" finds "UBER *TRIP" and "Uber BV", not "tuber".
    return re.search(r"(?<!\w)" + re.escape(keyword), payee) is not None


def categorize(payee: str, rules: dict[str, str]) -> str:
    text = _plain(payee)
    for table in (rules, DEFAULT_RULES):
        found = [k for k in table if _matches(k, text)]
        if found:
            return table[max(found, key=len)]
    return "other"


# ---------------------------------------------------------------------- reading a CSV

# Column names in English, French, Spanish, German and Arabic, compared without accents.
DATE_NAMES = ("date", "datum", "fecha", "buchungstag", "booking", "تاريخ")
PAYEE_NAMES = ("payee", "merchant", "beneficiaire", "empfanger", "beguenstigter", "description", "libelle",
               "details", "narrative", "concepto", "descripcion", "verwendungszweck", "name", "الوصف", "البيان")
AMOUNT_NAMES = ("amount", "montant", "importe", "betrag", "المبلغ")
DEBIT_NAMES = ("debit", "withdrawal", "paid out", "money out", "soll", "cargo", "مدين")
CREDIT_NAMES = ("credit", "deposit", "paid in", "money in", "haben", "abono", "دائن")


def _find(header: list[str], names: tuple, taken: set) -> int | None:
    """The first column whose name holds one of the names, trying the names in order."""
    for name in names:
        for i, column in enumerate(header):
            if i not in taken and name in column:
                return i
    return None


def detect_columns(header: list[str]) -> dict[str, int] | None:
    """Which column is the date, the payee and the amount (or the debit and credit pair); None when
    this row is not a header."""
    header = [_plain(h) for h in header]
    found: dict[str, int] = {}
    for key, names in (("date", DATE_NAMES), ("amount", AMOUNT_NAMES), ("debit", DEBIT_NAMES),
                       ("credit", CREDIT_NAMES), ("payee", PAYEE_NAMES)):
        i = _find(header, names, set(found.values()))
        if i is not None:
            found[key] = i
    has_amount = "amount" in found or ("debit" in found and "credit" in found)
    return found if "date" in found and "payee" in found and has_amount else None


def parse_amount(text: str) -> float | None:
    """'1.234,56', '1,234.56', '-12.50', '(7.00)', '12.50-', '€ 3,10' -> a number; None when empty."""
    text = text.strip()
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[^\d,.+-]", "", text)
    negative = negative or text.startswith("-") or text.endswith("-")
    text = text.strip("+-")
    if not re.search(r"\d", text):
        return None
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if decimal == "," else ",", "").replace(",", ".")
    elif "," in text:
        whole, _, rest = text.rpartition(",")
        # One comma with 1 or 2 digits after it is a decimal comma; otherwise commas group thousands.
        text = f"{whole.replace(',', '')}.{rest}" if text.count(",") == 1 and len(rest) != 3 else text.replace(",", "")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    value = float(text)
    return -value if negative else value


_ISO = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_OTHER = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})")


def parse_dates(texts: list[str]) -> list[str | None]:
    """ISO dates as they are; 05/09/2026 is day first unless a value in the column shows the month comes
    first (07/25/2026). Day first is the default: most of Rafiq's languages write dates that way."""
    parts = [_OTHER.match(t.strip()) for t in texts]
    month_first = (not any(int(p[1]) > 12 for p in parts if p)
                   and any(int(p[2]) > 12 for p in parts if p))
    out: list[str | None] = []
    for text, part in zip(texts, parts, strict=True):
        try:
            if m := _ISO.match(text.strip()):
                out.append(date(int(m[1]), int(m[2]), int(m[3])).isoformat())
            elif part:
                a, b, year = int(part[1]), int(part[2]), int(part[3])
                year += 2000 if year < 100 else 0
                day, month = (b, a) if month_first else (a, b)
                out.append(date(year, month, day).isoformat())
            else:
                out.append(None)
        except ValueError:
            out.append(None)
    return out


def _delimiter(sample: str) -> str:
    counts = {d: sample.count(d) for d in (";", "\t", "|")}
    best = max(counts, key=counts.get)
    # Commas also appear inside European amounts, so another separator wins when it is used at all.
    return best if counts[best] else ","


def parse_csv(text: str, rules: dict[str, str] | None = None) -> list[Transaction]:
    """The transactions of a bank CSV, whatever its columns are called; lines before the header (account
    number, period) and lines without a date or an amount (totals) are skipped."""
    rules = rules or {}
    rows = list(csv.reader(io.StringIO(text), delimiter=_delimiter("\n".join(text.splitlines()[:30]))))
    start, columns = next(((i, c) for i, row in enumerate(rows[:30]) if (c := detect_columns(row))), (0, None))
    if columns is None:
        raise ValueError("no date, description and amount columns found in this file")
    body = [r for r in rows[start + 1:] if len(r) > max(columns.values())]
    dates = parse_dates([r[columns["date"]] for r in body])
    out = []
    for row, day in zip(body, dates, strict=True):
        if "amount" in columns:
            amount = parse_amount(row[columns["amount"]])
        else:
            debit, credit = parse_amount(row[columns["debit"]]), parse_amount(row[columns["credit"]])
            amount = None if debit is None and credit is None else (credit or 0.0) - abs(debit or 0.0)
        payee = " ".join(row[columns["payee"]].split())
        if day is None or amount is None or not payee:
            continue
        out.append(Transaction(day, payee, round(amount, 2), categorize(payee, rules)))
    return out


# ---------------------------------------------------------------------- the ledger on disk

def _payee_key(payee: str) -> str:
    """The payee without reference numbers and punctuation, so 'UBER *TRIP 1234' and 'UBER *TRIP 9876'
    are the same."""
    return " ".join(re.sub(r"[\d\W_]+", " ", _plain(payee)).split())


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.transactions: list[Transaction] = []
        self.rules: dict[str, str] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.transactions = [Transaction(**t) for t in data.get("transactions", [])]
            self.rules = dict(data.get("rules", {}))
        except (OSError, ValueError, TypeError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"transactions": [asdict(t) for t in self.transactions], "rules": self.rules}, f,
                      ensure_ascii=False)
        tmp.replace(self.path)

    def add(self, new: list[Transaction]) -> tuple[int, int]:
        """Add a statement's transactions, skipping those already there; two identical coffees on one day
        are both kept. Returns (added, already there)."""
        def key(t):
            return t.date, t.payee, t.amount

        have = Counter(key(t) for t in self.transactions)
        added = 0
        for t in new:
            if have[key(t)]:
                have[key(t)] -= 1
                continue
            t.category = categorize(t.payee, self.rules)
            self.transactions.append(t)
            added += 1
        self.transactions.sort(key=lambda t: t.date)
        self.save()
        return added, len(new) - added

    def set_rule(self, keyword: str, category: str) -> int:
        """Remember that payees with this keyword go under this category, and re-sort the earlier
        transactions. Returns how many of them it covers."""
        keyword, category = _plain(keyword), category.strip().lower()
        self.rules[keyword] = category
        for t in self.transactions:
            t.category = categorize(t.payee, self.rules)
        self.save()
        return sum(_matches(keyword, _plain(t.payee)) for t in self.transactions)

    def monthly(self) -> dict[str, dict[str, float]]:
        months: dict[str, dict[str, float]] = defaultdict(lambda: {"in": 0.0, "out": 0.0})
        for t in self.transactions:
            months[t.date[:7]]["in" if t.amount > 0 else "out"] += abs(t.amount)
        return {m: {k: round(v, 2) for k, v in months[m].items()} for m in sorted(months)}

    def by_category(self, month: str) -> dict[str, float]:
        """Money out in a month (YYYY-MM), per category."""
        totals: dict[str, float] = defaultdict(float)
        for t in self.transactions:
            if t.amount < 0 and t.date.startswith(month):
                totals[t.category] += -t.amount
        return {c: round(v, 2) for c, v in sorted(totals.items(), key=lambda kv: -kv[1])}

    def recurring(self) -> list[Recurring]:
        """Payees that take about the same amount at a steady rhythm, three times or more."""
        groups: dict[str, list[Transaction]] = defaultdict(list)
        for t in self.transactions:
            if t.amount < 0:
                groups[_payee_key(t.payee)].append(t)
        found = []
        for items in groups.values():
            if len(items) < 3:
                continue
            items.sort(key=lambda t: t.date)
            days = [date.fromisoformat(t.date).toordinal() for t in items]
            gaps = [b - a for a, b in zip(days, days[1:], strict=False)]
            gap = statistics.median(gaps)
            every = next((name for name, low, high in (("weekly", 5, 9), ("monthly", 26, 35), ("yearly", 350, 380))
                          if low <= gap <= high), None)
            amounts = [-t.amount for t in items]
            usual = statistics.median(amounts)
            if (every and all(0.5 * gap <= g <= 1.5 * gap for g in gaps)
                    and all(abs(a - usual) <= 0.2 * usual for a in amounts)):
                found.append(Recurring(items[-1].payee, every, usual, items[-1].date))
        return sorted(found, key=lambda r: -r.amount)


def ledger() -> Ledger:
    return Ledger(config.data_dir() / "finance.json")


# ---------------------------------------------------------------------- tools

MAX_CSV_BYTES = 20 * 1024 * 1024


def _statement(path: str) -> Path | None:
    """The file itself, or the newest CSV of a folder (e.g. Downloads)."""
    target = Path(path).expanduser()
    if target.is_dir():
        files = sorted(target.glob("*.csv"), key=lambda p: p.stat().st_mtime)
        return files[-1] if files else None
    return target if target.is_file() else None


@tool("bank_import", "Import a bank statement CSV (any bank: the columns are detected). Local only.",
      {"path": {"type": "string", "description": "the CSV file, or a folder such as ~/Downloads to take "
                                                 "its newest CSV"}},
      required=("path",), level="act", private=True)
def bank_import(path: str) -> str:
    file = _statement(path)
    if file is None:
        return f"error: no CSV file at {path!r}"
    if file.stat().st_size > MAX_CSV_BYTES:
        return "error: that file is too large for a bank statement"
    raw = file.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    book = ledger()
    try:
        rows = parse_csv(text, book.rules)
    except ValueError as exc:
        return f"error: {exc}"
    if not rows:
        return "error: no transactions found in that file"
    added, known = book.add(rows)
    dates = [t.date for t in rows]
    return (f"imported {added} new transactions from {file.name} ({known} were already there), "
            f"{min(dates)} to {max(dates)}")


@tool("bank_categorize", "Put a payee's transactions under a category, e.g. 'put Uber under transport'. "
      "Remembered for new imports and applied to the earlier transactions too.",
      {"payee": {"type": "string", "description": "a word of the payee as the bank writes it, e.g. uber"},
       "category": {"type": "string", "description": "e.g. transport, groceries, subscriptions, eating out"}},
      required=("payee", "category"), level="act", private=True)
def bank_categorize(payee: str, category: str) -> str:
    if not payee.strip() or not category.strip():
        return "error: give both a payee and a category"
    moved = ledger().set_rule(payee, category)
    return f"from now on {payee.strip()} goes under {category.strip().lower()}; {moved} earlier transactions moved"


@tool("bank_summary", "Money in and out per month from the imported bank statements, and spending per "
      "category for one month.",
      {"month": {"type": "string", "description": "YYYY-MM for one month in detail; empty for the last "
                                                  "six months"}},
      level="private", private=True)
def bank_summary(month: str = "") -> str:
    book = ledger()
    if not book.transactions:
        return "no transactions yet: import a bank statement first"
    months = book.monthly()
    if month:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            return "error: give the month as YYYY-MM"
        if month not in months:
            return f"no transactions in {month}"
        spent = ", ".join(f"{c} {v:.2f}" for c, v in book.by_category(month).items()) or "nothing"
        return (f"{month}: in {months[month]['in']:.2f}, out {months[month]['out']:.2f}\n"
                f"spending by category: {spent}")
    return "\n".join(f"{m}: in {v['in']:.2f}, out {v['out']:.2f}" for m, v in list(months.items())[-6:])


@tool("bank_recurring", "Recurring charges (subscriptions, rent, bills) found in the imported bank "
      "statements.", level="private", private=True)
def bank_recurring() -> str:
    found = ledger().recurring()
    if not found:
        return "no recurring charges found"
    return "\n".join(f"{r.payee}: about {r.amount:.2f} {r.every}, last on {r.last}" for r in found)


@tool("subscriptions", "The user's subscriptions: monthly and yearly totals, each one with its next charge, "
      "and alerts (charged tomorrow, amount changed). Only when the user asks about them.",
      level="private")
def list_subscriptions() -> str:
    return "Subscriptions (the user's own records):\n" + subscriptions.describe(subscriptions.default_store())
