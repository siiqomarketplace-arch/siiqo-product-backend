"""Pay Link bank transfer converts at live rates only (no fixed cross rates, no guessed rate)."""
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.order import Order
from app.models.payment_link import PaymentLink
from app.services import daya_service, flutterwave_service


@pytest.fixture
def gateways(monkeypatch):
    state = {"daya_rate": 1500, "flw_ngn": {"GBP": 2000}, "funded": []}
    monkeypatch.setenv("FLW_SECRET_KEY", "FLWSECK_TEST-dummy")
    monkeypatch.setattr(daya_service, "get_or_create_customer", lambda **k: "cus_test")
    monkeypatch.setattr(daya_service, "get_rate", lambda **k: (
        {"rate": state["daya_rate"], "rate_id": "rate_test"} if state["daya_rate"] else {}))

    def fund(**k):
        state["funded"].append(k["amount_ngn"])
        return {"id": "fa_test", "amount": k["amount_ngn"],
                "instructions": [{"bank_name": "Test Bank", "account_number": "0000000000",
                                  "account_name": "Siiqo Test"}]}
    monkeypatch.setattr(daya_service, "create_ngn_funding_account", fund)

    def flw_rate(source_currency, destination_currency, amount):
        ngn = state["flw_ngn"].get(source_currency)
        if not ngn:
            return {"success": False}
        return {"success": True, "source": {"amount": amount / ngn}, "destination": {"amount": amount}}
    monkeypatch.setattr(flutterwave_service, "get_transfer_rate", flw_rate)
    return state


def _pay(client, vendor, currency, amount=100, product_type="digital"):
    link = PaymentLink(vendor_id=vendor.id, title="Consultation", amount=amount, currency=currency,
                       status="ACTIVE", slug=f"consult-{currency.lower()}-{product_type}",
                       product_type=product_type)
    db.session.add(link)
    db.session.commit()
    return link, client.post(f"/api/marketplace/payment-links/{link.id}/pay", json={
        "buyer_name": "Ada", "buyer_email": "ada@example.com", "buyer_phone": "+2348000000000",
        "payment_method": "bank_transfer"})


@pytest.mark.parametrize("currency, expected_ngn", [("NGN", 100), ("USD", 150000), ("GBP", 200000)])
def test_converts_at_live_rates(client, make_vendor, gateways, currency, expected_ngn):
    vendor, _ = make_vendor()
    _, res = _pay(client, vendor, currency)
    assert res.status_code == 200, res.get_json()
    assert gateways["funded"] == [expected_ngn]
    assert res.get_json()["amount_ngn"] == expected_ngn


def test_no_live_rate_refuses_instead_of_guessing(client, make_vendor, gateways):
    gateways["flw_ngn"] = {}
    vendor, _ = make_vendor()
    link, res = _pay(client, vendor, "EUR")
    assert res.status_code == 409
    body = res.get_json()
    assert body["code"] == "FX_RATE_UNAVAILABLE" and "card" in body["message"]
    assert gateways["funded"] == []
    assert Order.query.filter_by(payment_link_id=link.id).count() == 0


def test_no_daya_rate_refuses_instead_of_using_1500(client, make_vendor, gateways):
    gateways["daya_rate"] = None
    vendor, _ = make_vendor()
    _, res = _pay(client, vendor, "USD", product_type="physical")
    assert res.status_code == 409
    assert "try again" in res.get_json()["message"]
    assert gateways["funded"] == []
