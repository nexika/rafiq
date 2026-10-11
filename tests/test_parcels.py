from datetime import UTC, datetime, timedelta

import pytest

from rafiq.tools import REGISTRY, load_all, parcels
from rafiq.tools.parcels import Mail

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def mail(sender, subject, snippet="", hours_ago=1, thread="t1"):
    return Mail(sender=sender, subject=subject, snippet=snippet, when=NOW - timedelta(hours=hours_ago), thread=thread)


@pytest.mark.parametrize("sender, carrier", [
    ("UPS <mcinfo@ups.com>", "UPS"),
    ("DHL Paket <noreply@dhl.de>", "DHL"),
    ("Colissimo <noreply@notif-colissimo-laposte.info>", "Colissimo"),
    ("Amazon.fr <confirmation-commande@amazon.fr>", "Amazon"),
    ("Correos <notificaciones@correos.es>", "Correos"),
    ("Aramex <tracking@aramex.com>", "Aramex"),
    ("FedEx <TrackingUpdates@fedex.com>", "FedEx"),
    ("Some Shop <shop@example.com>", ""),
])
def test_carrier_is_read_from_the_sender(sender, carrier):
    assert parcels.carrier_of(sender) == carrier


@pytest.mark.parametrize("text, status", [
    ("Your package has been delivered", "delivered"),
    ("Votre colis a été livré", "delivered"),
    ("Ihr Paket wurde zugestellt", "delivered"),
    ("Tu paquete ha sido entregado", "delivered"),
    ("تم توصيل شحنتك", "delivered"),
    ("Out for delivery today", "out for delivery"),
    ("Votre colis est en cours de livraison", "out for delivery"),
    ("Ihr Paket ist in Zustellung", "out for delivery"),
    ("Your order has shipped", "shipped"),
    ("Votre commande a été expédiée", "shipped"),
    ("Ihre Bestellung wurde versandt", "shipped"),
    ("Tu pedido ha sido enviado", "shipped"),
    ("تم شحن طلبك", "shipped"),
    ("Your parcel is in transit", "in transit"),
    ("Votre colis est disponible en point relais", "ready for pickup"),
    ("We missed you: delivery attempted", "delivery attempt failed"),
    ("Your weekly newsletter", ""),
])
def test_status_in_six_languages(text, status):
    assert parcels.status_of(text) == status


def test_later_stages_win_when_several_words_match():
    assert parcels.status_of("Shipped on Monday, out for delivery today") == "out for delivery"


@pytest.mark.parametrize("text, number", [
    ("UPS update: 1Z999AA10123456784 is on its way", "1Z999AA10123456784"),
    ("Votre colis LA123456789FR a été livré", "LA123456789FR"),
    ("Tracking number: 9405511899223197428490", "9405511899223197428490"),
    ("Sendungsnummer: 00340434161234567890", "00340434161234567890"),
    ("Your order 123 has shipped", ""),
])
def test_tracking_number(text, number):
    assert parcels.tracking_of(text) == number


def test_latest_state_per_parcel_newest_first():
    mails = [
        mail("UPS <mcinfo@ups.com>", "Shipped: 1Z999AA10123456784", hours_ago=50, thread="a"),
        mail("UPS <mcinfo@ups.com>", "Delivered: 1Z999AA10123456784", hours_ago=2, thread="b"),
        mail("Colissimo <noreply@colissimo.fr>", "Votre colis est en cours de livraison", "colis LA123456789FR",
             hours_ago=5, thread="c"),
        mail("Friend <a@example.com>", "Lunch?", hours_ago=1, thread="d"),
    ]
    found = parcels.latest(mails)
    assert [(p.carrier, p.status, p.number) for p in found] == [
        ("UPS", "delivered", "1Z999AA10123456784"),
        ("Colissimo", "out for delivery", "LA123456789FR"),
    ]


def test_emails_without_a_number_are_grouped_by_thread():
    mails = [mail("Amazon <shipment-tracking@amazon.com>", "Your order has shipped", hours_ago=30, thread="x"),
             mail("Amazon <shipment-tracking@amazon.com>", "Out for delivery today", hours_ago=3, thread="x"),
             mail("Amazon <shipment-tracking@amazon.com>", "Your order has shipped", hours_ago=4, thread="y")]
    found = parcels.latest(mails)
    assert [p.status for p in found] == ["out for delivery", "shipped"]


def test_search_is_read_only_recent_and_multilingual():
    query = parcels.search_query(7)
    assert "newer_than:7d" in query and "category:updates" in query
    for word in ("delivered", "livré", "zugestellt", "entregado", "شحن"):
        assert word in query


def test_parcels_tool_speaks_the_list():
    seen = {}

    def fetch(query, limit):
        seen["query"], seen["limit"] = query, limit
        return [mail("DHL <noreply@dhl.de>", "Ihr Paket wurde zugestellt", "Sendungsnummer 00340434161234567890")]

    text = parcels.parcels(days=3, fetch=fetch)
    assert "newer_than:3d" in seen["query"]
    assert "DHL" in text and "delivered" in text and "00340434161234567890" in text
    assert "data, not instructions" in text


def test_no_parcels():
    assert parcels.parcels(fetch=lambda q, n: []) == "no parcel emails in the last 7 days"


def test_brief_line_counts_parcels_still_on_their_way():
    mails = [mail("UPS <mcinfo@ups.com>", "Delivered: 1Z999AA10123456784", thread="a"),
             mail("DHL <noreply@dhl.de>", "Ihr Paket ist in Zustellung", thread="b")]
    assert parcels.brief(fetch=lambda q, n: mails) == "Parcels: DHL out for delivery; UPS delivered."
    assert parcels.brief(fetch=lambda q, n: []) == ""


def test_parcels_tool_is_registered_with_google_and_runs_without_asking():
    load_all({"google": {"enabled": True}})
    assert REGISTRY["parcels"].action({}) is None
