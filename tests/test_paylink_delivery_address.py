"""Physical Pay Links record the buyer's delivery address on the order."""
import pytest

from app.extensions import db
from app.models.order import Order
from app.models.payment_link import PaymentLink
from app.services import daya_service


@pytest.fixture
def daya(monkeypatch):
    monkeypatch.setattr(daya_service, "get_or_create_customer", lambda **k: "cus_test")
    monkeypatch.setattr(daya_service, "get_rate",
                        lambda **k: {"rate": 1500, "rate_id": "rate_test", "expires_at": None})
    fa = {"id": "fa_test", "amount": 5000,
          "instructions": [{"bank_name": "Test Bank", "account_number": "0000000000",
                            "account_name": "Siiqo Test"}]}
    monkeypatch.setattr(daya_service, "create_ngn_funding_account", lambda **k: fa)


def _link(vendor, product_type):
    link = PaymentLink(vendor_id=vendor.id, title="Ankara fabric", amount=5000, currency="NGN",
                       status="ACTIVE", slug=f"ankara-{product_type}", product_type=product_type)
    db.session.add(link)
    db.session.commit()
    return link


@pytest.mark.parametrize("product_type, expect_address", [("physical", True), ("digital", False)])
def test_delivery_address_saved_only_for_physical(client, make_vendor, daya, product_type, expect_address):
    vendor, _ = make_vendor()
    link = _link(vendor, product_type)
    res = client.post(f"/api/marketplace/payment-links/{link.id}/pay", json={
        "buyer_name": "Ada Buyer", "buyer_email": "ada@example.com", "buyer_phone": "+2348000000000",
        "payment_method": "bank_transfer",
        "delivery_address": "12 Allen Avenue", "delivery_city": "Ikeja", "delivery_state": "Lagos",
    })
    assert res.status_code in (200, 201), res.get_json()
    order = Order.query.filter_by(payment_link_id=link.id).one()
    if expect_address:
        assert (order.delivery_address, order.delivery_city, order.delivery_state) == \
            ("12 Allen Avenue", "Ikeja", "Lagos")
    else:
        assert order.delivery_address is None and order.delivery_city is None
