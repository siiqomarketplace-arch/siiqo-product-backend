"""R3: Daya checkout uses the server-side amount (items + delivery) and rejects tampering."""
import pytest

from app.models.withdrawal import DayaPayment
from app.services import daya_service


@pytest.fixture
def fake_daya(monkeypatch):
    """Stand-in for the Daya API. Records the amount Siiqo asked Daya to collect."""
    monkeypatch.setenv("DAYA_API_KEY", "test-key")
    calls = {}

    monkeypatch.setattr(daya_service, "get_or_create_customer",
                        lambda email, first, last: "cust_test")
    monkeypatch.setattr(daya_service, "get_rate",
                        lambda asset="USDT", side="BUY": {
                            "rate_id": "rate_test", "rate": 1500.0,
                            "expires_at": "2099-01-01T00:00:00Z"})

    def _create_ngn(customer_id, amount_ngn, rate_id, idempotency_key, developer_fee_pct="0"):
        calls["amount_ngn"] = amount_ngn
        return {
            "id": "fa_test",
            "amount": amount_ngn,
            "instructions": [{"bank_name": "Test Bank", "account_number": "0000000000",
                              "account_name": "Siiqo Test"}],
        }

    monkeypatch.setattr(daya_service, "create_ngn_funding_account", _create_ngn)
    return calls


def _initiate(client, order_ids, amount=None, headers=None, **extra):
    body = {"orderId": ",".join(str(i) for i in order_ids), "type": "ngn_onramp", **extra}
    if amount is not None:
        body["amountNgn"] = amount
    return client.post("/api/payments/daya/initiate", json=body, headers=headers or {})


def test_correct_total_including_delivery_succeeds(client, make_vendor, make_order, fake_daya):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=10000, logistics_fee=500)

    res = _initiate(client, [order.id], amount=10500)

    assert res.status_code == 200, res.get_json()
    assert fake_daya["amount_ngn"] == 10500
    assert DayaPayment.query.filter_by(order_id=order.id).count() == 1


def test_missing_client_amount_uses_server_total(client, make_vendor, make_order, fake_daya):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=7000, logistics_fee=1000)

    res = _initiate(client, [order.id])

    assert res.status_code == 200, res.get_json()
    assert fake_daya["amount_ngn"] == 8000


@pytest.mark.parametrize("sent", [100, 10000, 20000])
def test_wrong_amount_is_rejected(client, make_vendor, make_order, fake_daya, sent):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=10000, logistics_fee=500)

    res = _initiate(client, [order.id], amount=sent)

    assert res.status_code == 400
    assert "mismatch" in res.get_json()["message"].lower()
    assert "amount_ngn" not in fake_daya


def test_multiple_orders_are_summed(client, make_vendor, make_order, fake_daya):
    vendor_a, _ = make_vendor()
    vendor_b, _ = make_vendor()
    first = make_order(vendor_a, total=4000, logistics_fee=500)
    second = make_order(vendor_b, total=6000, logistics_fee=0)

    res = _initiate(client, [first.id, second.id], amount=10500)

    assert res.status_code == 200, res.get_json()
    assert fake_daya["amount_ngn"] == 10500


def test_not_configured_returns_503(client, make_vendor, make_order, monkeypatch):
    monkeypatch.delenv("DAYA_API_KEY", raising=False)
    vendor, _ = make_vendor()
    order = make_order(vendor)

    res = _initiate(client, [order.id], amount=10000)

    assert res.status_code == 503


def test_invalid_payment_type_is_rejected(client, make_vendor, make_order, fake_daya):
    vendor, _ = make_vendor()
    order = make_order(vendor)

    res = _initiate(client, [order.id], amount=10000, type="card")

    assert res.status_code == 400


def test_other_buyers_order_is_refused(client, make_user, make_vendor, make_order, fake_daya, auth_headers):
    owner = make_user()
    stranger = make_user()
    vendor, _ = make_vendor()
    order = make_order(vendor, buyer=owner, total=5000)

    res = _initiate(client, [order.id], amount=5000, headers=auth_headers(stranger))

    assert res.status_code == 403


def test_missing_order_id_is_rejected(client, fake_daya):
    res = client.post("/api/payments/daya/initiate", json={"amountNgn": 1000})

    assert res.status_code == 400
