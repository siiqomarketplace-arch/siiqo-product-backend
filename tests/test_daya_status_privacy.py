"""M5 / S5: the public Daya status endpoint (polled by order id) never gives a
stranger the delivery-release link, the buyer's contact details, or a paid
file before payment completes."""
import pytest

from app.extensions import db
from app.models.withdrawal import DayaPayment


@pytest.fixture
def daya_order(make_user, make_vendor, make_order):
    buyer = make_user()
    vendor, _ = make_vendor()
    order = make_order(vendor, buyer=buyer, total=5000)
    order.buyer_phone = "08000000000"
    dp = DayaPayment(order_id=order.id, buyer_id=buyer.id, payment_type="ngn_onramp",
                     amount_ngn=5000, status="COMPLETED")
    db.session.add(dp)
    db.session.commit()
    return buyer, order, dp


def _status(client, order, headers=None):
    return client.get(f"/api/payments/daya/status?order_id={order.id}", headers=headers or {})


def test_stranger_gets_no_release_link_or_contact(client, daya_order):
    _, order, _ = daya_order
    data = _status(client, order).get_json()
    assert data["status"] == "COMPLETED"
    assert data["confirmation_url"] is None
    assert data["buyer_phone"] is None and data["buyer_email"] is None
    assert "token=" not in str(data)


def test_signed_in_buyer_gets_release_link(client, daya_order, auth_headers):
    buyer, order, _ = daya_order
    data = _status(client, order, auth_headers(buyer)).get_json()
    assert "token=" in data["confirmation_url"]
    assert data["buyer_email"] == buyer.email


def test_other_signed_in_user_is_refused(client, daya_order, make_user, auth_headers):
    _, order, _ = daya_order
    assert _status(client, order, auth_headers(make_user())).status_code == 403
