import json
from datetime import date

import pytest

from rafiq import subscriptions as subs
from rafiq.subscriptions import Receipt, Store, Subscription

TODAY = date(2026, 10, 10)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "subscriptions.json")


def add(store, **fields):
    defaults = {"name": "Netflix", "amount": 12.99, "currency": "EUR", "period": "monthly",
                "next_charge": "2026-10-20", "category": "entertainment"}
    return store.add(Subscription(**{**defaults, **fields}))


def test_totals_count_a_yearly_plan_as_a_twelfth_each_month(store):
    add(store, amount=12.0)
    add(store, name="iCloud", amount=120.0, period="yearly", category="cloud")
    totals = subs.summary(store, TODAY)["totals"]["EUR"]
    assert totals["monthly"] == pytest.approx(22.0) and totals["yearly"] == pytest.approx(264.0)


def test_totals_are_kept_apart_by_currency(store):
    add(store, amount=10.0)
    add(store, name="Shahid", amount=100.0, currency="EGP")
    assert set(subs.summary(store, TODAY)["totals"]) == {"EUR", "EGP"}


def test_a_past_charge_date_rolls_forward_by_the_period(store):
    add(store, next_charge="2026-08-31")
    add(store, name="Domain", period="yearly", next_charge="2025-03-01")
    upcoming = {u["name"]: u["date"] for u in subs.summary(store, TODAY, days=400)["upcoming"]}
    assert upcoming == {"Netflix": "2026-10-31", "Domain": "2027-03-01"}


def test_upcoming_lists_only_the_coming_days_soonest_first(store):
    add(store, name="Later", next_charge="2026-12-01")
    add(store, name="Soon", next_charge="2026-10-12")
    add(store, name="Sooner", next_charge="2026-10-11")
    assert [u["name"] for u in subs.summary(store, TODAY)["upcoming"]] == ["Sooner", "Soon"]


def test_a_charge_tomorrow_raises_an_alert(store):
    add(store, name="Spotify", next_charge="2026-10-11")
    add(store, name="Later", next_charge="2026-10-15")
    alerts = subs.summary(store, TODAY)["alerts"]
    assert [(a["kind"], a["name"]) for a in alerts] == [("tomorrow", "Spotify")]


def test_the_split_by_category_uses_monthly_amounts(store):
    add(store, amount=10.0)
    add(store, name="Disney+", amount=5.0)
    add(store, name="iCloud", amount=36.0, period="yearly", category="cloud")
    split = subs.summary(store, TODAY)["categories"]["EUR"]
    assert split == {"entertainment": pytest.approx(15.0), "cloud": pytest.approx(3.0)}


def test_the_file_is_private_and_survives_a_restart(store, tmp_path):
    add(store)
    path = tmp_path / "subscriptions.json"
    assert path.stat().st_mode & 0o077 == 0
    assert [s.name for s in Store(path).subscriptions()] == ["Netflix"]


def test_remove(store):
    sub = add(store)
    assert store.remove(sub.id) and store.subscriptions() == []
    assert not store.remove("nope")


@pytest.mark.parametrize("bad", [
    {"name": ""}, {"amount": -1}, {"amount": "lots"}, {"period": "daily"}, {"currency": "euros"},
    {"next_charge": "next week"},
])
def test_bad_fields_are_refused(store, bad):
    with pytest.raises(ValueError):
        add(store, **bad)


# ---------------------------------------------------------------------- receipts

def receipt(sender="Netflix <info@account.netflix.com>", subject="Your Netflix receipt",
            text="Thanks! You paid €15.49 for your monthly plan.", when="2026-10-03", rid="m1"):
    return Receipt(rid, sender, subject, when, text)


@pytest.mark.parametrize("text, amount, currency", [
    ("Total: €15.49", 15.49, "EUR"), ("Amount charged $9.99", 9.99, "USD"), ("You paid 120,00 EUR", 120.0, "EUR"),
    ("Total £4.50 per month", 4.5, "GBP"), ("EGP 99.00 renewal", 99.0, "EGP"),
])
def test_detection_reads_the_amount_and_currency(text, amount, currency):
    found = subs.detect([receipt(text=text)])[0]
    assert (found.amount, found.currency) == (pytest.approx(amount), currency)


def test_detection_names_the_sender_and_guesses_period_category_and_next_charge():
    found = subs.detect([receipt()])[0]
    assert (found.name, found.period, found.category, found.next_charge) == (
        "Netflix", "monthly", "entertainment", "2026-11-03")
    yearly = subs.detect([receipt(sender="billing@dropbox.com", text="Annual plan: $119.88")])[0]
    assert (yearly.name, yearly.period, yearly.category, yearly.next_charge) == (
        "Dropbox", "yearly", "cloud", "2027-10-03")


def test_detection_keeps_the_newest_receipt_per_sender_and_skips_ones_without_an_amount():
    found = subs.detect([receipt(rid="old", when="2026-09-03", text="€14.99"), receipt(rid="new"),
                         receipt(sender="news@shop.com", text="Our sale starts now", rid="x")])
    assert [(f.name, f.amount) for f in found] == [("Netflix", pytest.approx(15.49))]


class FakeInbox:
    def __init__(self, receipts):
        self.receipts, self.queries = receipts, []

    def search(self, query, max_results):
        self.queries.append(query)
        return self.receipts


def test_a_scan_only_proposes_nothing_is_merged_before_confirmation(store):
    inbox = FakeInbox([receipt()])
    pending = store.scan(inbox)
    assert [p.name for p in pending] == ["Netflix"] and store.subscriptions() == []
    assert inbox.queries and "receipt" in inbox.queries[0]


def test_confirming_a_new_one_adds_it_and_rejecting_drops_it(store):
    netflix, other = store.scan(FakeInbox([receipt(), receipt(sender="billing@dropbox.com", rid="d")]))
    store.confirm(netflix.id)
    store.reject(other.id)
    assert [s.name for s in store.subscriptions()] == ["Netflix"] and store.pending() == []


def test_confirming_a_new_price_merges_it_and_raises_an_amount_changed_alert(store):
    add(store, amount=12.99, next_charge="2026-10-03")
    proposal = store.scan(FakeInbox([receipt()]))[0]
    store.confirm(proposal.id, today=TODAY)
    [sub] = store.subscriptions()
    assert (sub.amount, sub.previous_amount, sub.category) == (pytest.approx(15.49), pytest.approx(12.99),
                                                               "entertainment")
    alerts = subs.summary(store, TODAY)["alerts"]
    assert [(a["kind"], a["name"], a["previous"], a["amount"]) for a in alerts] == [
        ("changed", "Netflix", 12.99, 15.49)]


def test_a_scan_does_not_propose_what_is_already_known_or_pending(store):
    add(store, amount=15.49)
    assert store.scan(FakeInbox([receipt()])) == []
    store.scan(FakeInbox([receipt(sender="billing@dropbox.com")]))
    assert store.scan(FakeInbox([receipt(sender="billing@dropbox.com")])) == []
    assert len(store.pending()) == 1


def test_an_amount_changed_alert_fades_after_a_month(store):
    add(store, amount=12.99)
    store.confirm(store.scan(FakeInbox([receipt()]))[0].id, today=date(2026, 9, 1))
    assert subs.summary(store, TODAY)["alerts"] == []


# ---------------------------------------------------------------------- panel requests

def test_handle_adds_lists_and_removes(store):
    subs.handle({"action": "add", "name": "Netflix", "amount": 12.99, "currency": "eur",
                 "next_charge": "2026-10-20"}, store)
    [sub] = store.subscriptions()
    assert sub.currency == "EUR" and sub.period == "monthly"
    subs.handle({"action": "remove", "id": sub.id}, store)
    assert store.subscriptions() == []


def test_handle_scan_without_gmail_says_so(store):
    with pytest.raises(ValueError, match="Gmail"):
        subs.handle({"action": "scan"}, store, inbox=None)


def test_handle_refuses_unknown_actions(store):
    with pytest.raises(ValueError):
        subs.handle({"action": "pay"}, store)


def test_the_summary_is_plain_json(store):
    add(store)
    json.dumps(subs.summary(store, TODAY))


def test_the_tool_answers_only_when_asked_and_reads_the_local_file(store, monkeypatch):
    from rafiq.tools import REGISTRY, load_all

    load_all({})
    monkeypatch.setattr(subs, "default_store", lambda: store)
    add(store, next_charge="2026-10-11")
    text = REGISTRY["subscriptions"].run({}, None)
    assert "Netflix" in text and "12.99" in text and REGISTRY["subscriptions"].action({}) is None


# ---------------------------------------------------------------------- the panel page

@pytest.fixture
def panel():
    import threading

    from test_brain_panel import FakeAssistant

    from rafiq.panel import serve

    assistant = FakeAssistant()
    assistant.cfg = {"google": {"enabled": False}}
    server = serve(assistant, "127.0.0.1", 0, "t" * 40)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def test_the_page_carries_its_token(panel):
    from test_brain_panel import request

    status, page = request(panel, "/subscriptions")
    assert status == 200 and "t" * 40 in page and "__TOKEN__" not in page


def test_the_panel_adds_and_lists_subscriptions(panel):
    from test_brain_panel import request

    body = {"action": "add", "name": "Netflix", "amount": 12.99, "currency": "EUR", "next_charge": "2030-01-05"}
    assert request(panel, "/api/subscriptions", body)[0] == 200
    status, text = request(panel, "/api/subscriptions?token=" + "t" * 40)
    assert status == 200 and json.loads(text)["subscriptions"][0]["name"] == "Netflix"
    assert [s.name for s in subs.default_store().subscriptions()] == ["Netflix"]


def test_the_panel_refuses_bad_or_untokened_requests(panel):
    from test_brain_panel import request

    assert request(panel, "/api/subscriptions?token=" + "x" * 40)[0] == 403
    assert request(panel, "/api/subscriptions", {"action": "add", "name": "x"}, token=None)[0] == 403
    status, text = request(panel, "/api/subscriptions", {"action": "add", "name": "x", "amount": -3})
    assert status == 400 and "amount" in json.loads(text)["error"]
    status, text = request(panel, "/api/subscriptions", {"action": "scan"})
    assert status == 400 and "Gmail" in json.loads(text)["error"]
    assert subs.default_store().subscriptions() == []
