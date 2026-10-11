"""Subscriptions: what you pay for again and again, kept in one local file (data/subscriptions.json).

The panel's /subscriptions page shows the monthly and yearly totals, the charges coming up, alerts
("charged tomorrow", "amount changed") and a split by category. Nothing here goes to the model unless
you ask about your subscriptions (the `subscriptions` tool).

Receipts found in Gmail are only proposals: they are merged after you confirm each one.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta
from email.utils import parseaddr
from pathlib import Path
from typing import Protocol

from . import config

# How many times a month each period charges.
PERIODS = {"weekly": 52 / 12, "monthly": 1.0, "quarterly": 1 / 3, "yearly": 1 / 12}
CHANGED_ALERT_DAYS = 30
GMAIL_QUERY = ('subject:(receipt OR invoice OR subscription OR renewal OR "payment confirmation") '
               "newer_than:3m -in:spam")

CATEGORIES = {
    "entertainment": ("netflix", "spotify", "disney", "youtube", "deezer", "shahid", "hbo", "prime video",
                      "apple tv", "anghami", "twitch", "playstation", "xbox", "nintendo", "audible"),
    "cloud": ("icloud", "dropbox", "google one", "onedrive", "backblaze", "proton"),
    "software": ("adobe", "github", "jetbrains", "microsoft", "notion", "openai", "anthropic", "claude",
                 "1password", "figma", "canva"),
    "news": ("times", "monde", "guardian", "economist", "medium", "substack"),
    "phone & internet": ("orange", "vodafone", "etisalat", "we ", "free mobile", "sfr", "bouygues"),
    "fitness": ("strava", "gym", "fitness", "peloton"),
}

SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "E£": "EGP", "ج.م": "EGP"}
_CODE = r"EUR|USD|GBP|EGP|CHF|CAD|SAR|AED"
_SYMBOL = r"E£|ج\.م|€|\$|£"
_NUMBER = r"\d{1,6}(?:[.,]\d{2})?"
AMOUNT = re.compile(rf"(?P<c1>{_SYMBOL}|\b(?:{_CODE}))\s?(?P<n1>{_NUMBER})\b"
                    rf"|\b(?P<n2>{_NUMBER})\s?(?P<c2>{_SYMBOL}|(?:{_CODE})\b)")
YEARLY = re.compile(r"\b(annual|yearly|per year|a year|/ ?year|12 months)\b|annuel|jährlich|anual|سنوي", re.I)


@dataclass
class Subscription:
    name: str
    amount: float
    currency: str = "EUR"
    period: str = "monthly"
    next_charge: str = ""            # local date, YYYY-MM-DD
    category: str = "other"
    id: str = ""
    previous_amount: float | None = None   # the amount before the last change, for the alert
    changed_on: str = ""
    source: str = ""                 # the Gmail message a proposal came from

    def check(self) -> Subscription:
        """Normalise the fields, or raise ValueError saying which one is wrong."""
        self.name = str(self.name).strip()[:80]
        if not self.name:
            raise ValueError("a subscription needs a name")
        try:
            self.amount = round(float(self.amount), 2)
        except (TypeError, ValueError) as exc:
            raise ValueError("the amount must be a number") from exc
        if not 0 < self.amount <= 1_000_000:
            raise ValueError("the amount must be above zero")
        self.currency = str(self.currency).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("the currency is a three-letter code such as EUR")
        if self.period not in PERIODS:
            raise ValueError(f"the period is one of {', '.join(PERIODS)}")
        try:
            date.fromisoformat(self.next_charge)
        except (TypeError, ValueError) as exc:
            raise ValueError("the next charge is a date, YYYY-MM-DD") from exc
        self.category = str(self.category or "other").strip().lower()[:40] or "other"
        self.id = self.id or secrets.token_hex(4)
        return self

    @classmethod
    def from_dict(cls, data: dict) -> Subscription:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def monthly(self) -> float:
        return self.amount * PERIODS[self.period]


def add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def step(day: date, period: str, start: date | None = None, n: int = 1) -> date:
    """The charge n periods after `day`; months are counted from `start` so the 31st stays the 31st."""
    if period == "weekly":
        return day + timedelta(weeks=n)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}[period]
    return add_months(start or day, months * n)


def due(sub: Subscription, today: date) -> date:
    """The next charge on or after today: a date in the past rolls forward by the period."""
    first = date.fromisoformat(sub.next_charge)
    when, n = first, 0
    while when < today:
        n += 1
        when = step(first, sub.period, first, n)
    return when


# ---------------------------------------------------------------------- the file

class Store:
    """The subscriptions and the proposals waiting for a yes, in one private JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"subscriptions": [], "pending": []}
        return {"subscriptions": data.get("subscriptions", []), "pending": data.get("pending", [])}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def subscriptions(self) -> list[Subscription]:
        return [Subscription.from_dict(d) for d in self._read()["subscriptions"]]

    def pending(self) -> list[Subscription]:
        return [Subscription.from_dict(d) for d in self._read()["pending"]]

    def add(self, sub: Subscription) -> Subscription:
        sub.check()
        with self.lock:
            data = self._read()
            data["subscriptions"].append(asdict(sub))
            self._write(data)
        return sub

    def remove(self, sub_id: str) -> bool:
        with self.lock:
            data = self._read()
            kept = [d for d in data["subscriptions"] if d.get("id") != sub_id]
            if len(kept) == len(data["subscriptions"]):
                return False
            data["subscriptions"] = kept
            self._write(data)
        return True

    def scan(self, inbox: ReceiptSource, max_results: int = 30) -> list[Subscription]:
        """Look for receipts and keep what is new as proposals; nothing is merged yet."""
        found = detect(inbox.search(GMAIL_QUERY, max_results))
        with self.lock:
            data = self._read()
            known = {(d["name"].casefold(), round(d["amount"], 2)) for d in data["subscriptions"] + data["pending"]}
            new = [s.check() for s in found if (s.name.casefold(), s.amount) not in known]
            data["pending"] += [asdict(s) for s in new]
            self._write(data)
        return new

    def confirm(self, sub_id: str, today: date | None = None) -> Subscription:
        """Merge a proposal: a new price for a known name updates it, anything else is added."""
        today = today or date.today()
        with self.lock:
            data = self._read()
            proposal = next((d for d in data["pending"] if d.get("id") == sub_id), None)
            if proposal is None:
                raise ValueError("no such proposal")
            data["pending"].remove(proposal)
            match = next((d for d in data["subscriptions"] if d["name"].casefold() == proposal["name"].casefold()),
                         None)
            if match is None:
                data["subscriptions"].append(proposal)
                merged = proposal
            else:
                if round(match["amount"], 2) != round(proposal["amount"], 2):
                    match["previous_amount"], match["changed_on"] = match["amount"], today.isoformat()
                match.update(amount=proposal["amount"], currency=proposal["currency"], period=proposal["period"],
                             next_charge=proposal["next_charge"], source=proposal["source"])
                merged = match
            self._write(data)
        return Subscription.from_dict(merged)

    def reject(self, sub_id: str) -> bool:
        with self.lock:
            data = self._read()
            kept = [d for d in data["pending"] if d.get("id") != sub_id]
            if len(kept) == len(data["pending"]):
                return False
            data["pending"] = kept
            self._write(data)
        return True


def default_store() -> Store:
    return Store(config.data_dir() / "subscriptions.json")


# ---------------------------------------------------------------------- what the page shows

def summary(store: Store, today: date | None = None, days: int = 30) -> dict:
    today = today or date.today()
    subs = store.subscriptions()
    totals: dict[str, dict] = {}
    categories: dict[str, dict] = {}
    upcoming, alerts = [], []
    for sub in subs:
        money = totals.setdefault(sub.currency, {"monthly": 0.0, "yearly": 0.0})
        money["monthly"] += sub.monthly()
        money["yearly"] += sub.monthly() * 12
        split = categories.setdefault(sub.currency, {})
        split[sub.category] = split.get(sub.category, 0.0) + sub.monthly()
        when = due(sub, today)
        if (when - today).days <= days:
            upcoming.append({"id": sub.id, "name": sub.name, "date": when.isoformat(), "amount": sub.amount,
                             "currency": sub.currency})
        if when == today + timedelta(days=1):
            alerts.append({"kind": "tomorrow", "id": sub.id, "name": sub.name, "amount": sub.amount,
                           "currency": sub.currency})
        if (sub.previous_amount is not None and sub.changed_on and sub.previous_amount != sub.amount
                and (today - date.fromisoformat(sub.changed_on)).days <= CHANGED_ALERT_DAYS):
            alerts.append({"kind": "changed", "id": sub.id, "name": sub.name, "previous": sub.previous_amount,
                           "amount": sub.amount, "currency": sub.currency})
    for money in totals.values():
        money["monthly"], money["yearly"] = round(money["monthly"], 2), round(money["yearly"], 2)
    upcoming.sort(key=lambda u: (u["date"], u["name"]))
    return {"today": today.isoformat(), "totals": totals,
            "categories": {c: {k: round(v, 2) for k, v in s.items()} for c, s in categories.items()},
            "upcoming": upcoming, "alerts": alerts,
            "subscriptions": [asdict(s) | {"due": due(s, today).isoformat()} for s in subs],
            "pending": [asdict(s) for s in store.pending()]}


def describe(store: Store, today: date | None = None) -> str:
    """The summary in a few lines, for the model when the user asks."""
    info = summary(store, today)
    if not info["subscriptions"]:
        return "no subscriptions recorded (add them on the panel's Subscriptions page)"
    lines = [f"Total {c}: {t['monthly']:.2f} a month, {t['yearly']:.2f} a year" for c, t in info["totals"].items()]
    lines += [f"- {s['name']}: {s['amount']:.2f} {s['currency']} {s['period']}, {s['category']}, next {s['due']}"
              for s in info["subscriptions"]]
    for a in info["alerts"]:
        if a["kind"] == "tomorrow":
            lines.append(f"Alert: {a['name']} charges tomorrow ({a['amount']:.2f} {a['currency']})")
        else:
            lines.append(f"Alert: {a['name']} changed from {a['previous']:.2f} to {a['amount']:.2f} {a['currency']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- receipts

@dataclass
class Receipt:
    id: str
    sender: str
    subject: str
    date: str                        # YYYY-MM-DD
    text: str


class ReceiptSource(Protocol):
    def search(self, query: str, max_results: int) -> list[Receipt]: ...


def _amount(text: str) -> tuple[float, str] | None:
    m = AMOUNT.search(text)
    if not m:
        return None
    number, unit = (m["n1"], m["c1"]) if m["n1"] else (m["n2"], m["c2"])
    return float(number.replace(",", ".")), SYMBOLS.get(unit, unit.upper())


def _name(sender: str) -> str:
    display, address = parseaddr(sender)
    display = re.sub(r"\b(billing|receipts?|no-?reply|info|team|support)\b", "", display, flags=re.I).strip(" -|")
    if display:
        return display
    domain = address.rpartition("@")[2].split(".")
    return (domain[-2] if len(domain) >= 2 else domain[0]).capitalize()


def category_of(name: str) -> str:
    low = name.casefold() + " "
    return next((cat for cat, words in CATEGORIES.items() if any(w in low for w in words)), "other")


def detect(receipts: list[Receipt]) -> list[Subscription]:
    """One proposal per sender, from its newest receipt that names an amount."""
    best: dict[str, tuple[str, Subscription]] = {}
    for r in receipts:
        found = _amount(f"{r.subject}\n{r.text}")
        if not found:
            continue
        name = _name(r.sender)
        period = "yearly" if YEARLY.search(f"{r.subject}\n{r.text}") else "monthly"
        try:
            paid = date.fromisoformat(r.date)
        except ValueError:
            continue
        sub = Subscription(name, found[0], found[1], period, step(paid, period).isoformat(), category_of(name),
                           source=r.id)
        key = name.casefold()
        if key not in best or r.date > best[key][0]:
            best[key] = (r.date, sub)
    return [sub for _, sub in best.values()]


class GmailReceipts:
    """Receipts from the user's Gmail (read-only search), when Google is set up."""

    def search(self, query: str, max_results: int) -> list[Receipt]:
        from email.utils import parsedate_to_datetime

        from .tools.google import _body, _header, service

        gmail = service("gmail", "v1")
        found = gmail.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        out = []
        for ref in found.get("messages", []):
            msg = gmail.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            try:
                when = parsedate_to_datetime(_header(msg, "date")).date().isoformat()
            except (TypeError, ValueError):
                continue
            text = _body(msg.get("payload", {})) or msg.get("snippet", "")
            out.append(Receipt(ref["id"], _header(msg, "from"), _header(msg, "subject"), when, text[:4000]))
        return out


# ---------------------------------------------------------------------- panel requests

def handle(data: dict, store: Store, inbox: ReceiptSource | None = None) -> dict:
    """One change asked by the panel page; raises ValueError for a bad request."""
    action = data.get("action")
    if action == "add":
        sub = Subscription.from_dict({k: v for k, v in data.items() if k not in ("action", "id", "source")})
        store.add(sub)
    elif action == "remove":
        if not store.remove(str(data.get("id", ""))):
            raise ValueError("no such subscription")
    elif action == "confirm":
        store.confirm(str(data.get("id", "")))
    elif action == "reject":
        if not store.reject(str(data.get("id", ""))):
            raise ValueError("no such proposal")
    elif action == "scan":
        if inbox is None:
            raise ValueError("Gmail is not set up: enable [google] and run `rafiq google-setup`")
        return {"found": len(store.scan(inbox))}
    else:
        raise ValueError("unknown action")
    return {"ok": True}
