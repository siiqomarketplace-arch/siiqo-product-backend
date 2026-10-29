"""M9: one 3% platform fee on every path (decision D1)."""
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.escrow import EscrowTransaction
from app.services.fees import PLATFORM_FEE_PERCENT, platform_fee


@pytest.mark.parametrize("subtotal,expected", [
    (10000, Decimal("300.00")),
    (2500, Decimal("75.00")),
    ("1234.56", Decimal("37.04")),
    (0, Decimal("0.00")),
    (None, Decimal("0.00")),
])
def test_platform_fee_is_three_percent(subtotal, expected):
    assert PLATFORM_FEE_PERCENT == Decimal("3.00")
    assert platform_fee(subtotal) == expected


def test_escrow_model_default_is_three_percent(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor)
    escrow = EscrowTransaction(order_id=order.id, transaction_number="T-1", status="PENDING_PAYMENT",
                               amount=1000)
    db.session.add(escrow)
    db.session.commit()
    assert float(escrow.fee_percent) == 3.0


def _paystack_split_fee(monkeypatch, store, order):
    """Run PaystackProvider.initiate_transaction with a fake HTTP call and return
    the transaction_charge (kobo) it sends to Paystack."""
    from app.services.escrow import paystack_provider as pp

    sent = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"status": True, "data": {"authorization_url": "https://paystack.test/x",
                                              "reference": "R", "access_code": "A"}}

    def _fake_post(url, json=None, headers=None, timeout=None):
        sent.update(json or {})
        return _Resp()

    monkeypatch.setattr(pp.requests, "post", _fake_post)
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_test_dummy")
    store.paystack_subaccount_code = "ACCT_test"
    db.session.commit()
    pp.PaystackProvider().initiate_transaction([order], existing_txn_number="PL-1-TEST")
    return sent.get("transaction_charge")


@pytest.mark.parametrize("pro_verified", [False, True])
def test_paystack_split_is_three_percent_for_every_seller(monkeypatch, make_vendor, make_order, pro_verified):
    vendor, store = make_vendor()
    store.is_pro_verified = pro_verified
    order = make_order(vendor, total=10000)

    charge = _paystack_split_fee(monkeypatch, store, order)

    assert charge == 30000  # ₦300 in kobo, whether or not the seller is Pro-verified
