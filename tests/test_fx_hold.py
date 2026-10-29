"""M3: an order paid in another currency never has its foreign face value sent
(or withdrawn) as naira; it is held for support. Naira orders pay as before."""
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.order import OrderItem
from app.models.withdrawal import VendorBankAccount
from app.routes import payments as payments_routes
from app.routes.escrow import _deliver_digital_products, _paystack_payout_vendor
from app.routes.withdrawal import _get_ledger_balance
from app.services import daya_service, flutterwave_service


@pytest.fixture(autouse=True)
def _no_email(monkeypatch):
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: True)


@pytest.fixture
def transfers(monkeypatch):
    calls = []

    def record(**kwargs):
        calls.append(kwargs)
        return {"success": True}
    monkeypatch.setattr(flutterwave_service, "transfer_to_vendor", record)
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor", record)
    monkeypatch.setattr(daya_service, "get_rate", lambda **k: {"rate": 1500})
    monkeypatch.setattr(daya_service, "get_merchant_balance",
                        lambda: {"data": {"collection_balance_usd": 0, "withdrawal_balance_usd": 1000}})
    return calls


def _order(make_vendor, make_product, make_order, currency, payment_method="FLUTTERWAVE"):
    vendor, store = make_vendor()
    db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Test Bank", bank_code="000",
                                     account_number="0000000000", account_name="Test Vendor", is_default=True))
    product = make_product(store, price=100, product_type="digital", file_url="https://files.example/x.pdf")
    order = make_order(vendor, total=100, payment_method=payment_method)
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, price_at_purchase=100, quantity=1))
    escrow = EscrowTransaction(order_id=order.id, transaction_number=f"FLW-PL-{order.id}-AAAA",
                               status=EscrowStatus.IN_ESCROW, amount=100, fee_amount=3, currency=currency)
    db.session.add(escrow)
    db.session.commit()
    return vendor, order, escrow


def test_usd_order_is_held_not_sent_as_naira(make_vendor, make_product, make_order, transfers):
    vendor, order, escrow = _order(make_vendor, make_product, make_order, "USD")
    _deliver_digital_products(order, escrow)
    db.session.commit()
    assert transfers == []
    # credited for the record, held so it can't be withdrawn at face value
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def test_ngn_order_still_pays_out(make_vendor, make_product, make_order, transfers):
    vendor, order, escrow = _order(make_vendor, make_product, make_order, "NGN")
    _deliver_digital_products(order, escrow)
    db.session.commit()
    assert len(transfers) == 1 and transfers[0]["amount_ngn"] == 97.0


def test_daya_payout_holds_foreign_currency(make_vendor, make_product, make_order, transfers):
    _, order, escrow = _order(make_vendor, make_product, make_order, "GBP", payment_method="CRYPTO")
    assert payments_routes._payout_vendor_via_daya(order, escrow) is False
    assert transfers == []


def test_paystack_payout_holds_foreign_currency(make_vendor, make_product, make_order, monkeypatch):
    from app.services.escrow import paystack_provider
    calls = []
    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor", lambda **k: calls.append(k) or {"success": True})
    vendor, order, escrow = _order(make_vendor, make_product, make_order, "EUR", payment_method="PAYSTACK")
    VendorBankAccount.query.filter_by(vendor_id=vendor.id).update({"recipient_code": "RCP_test"})
    db.session.commit()
    assert _paystack_payout_vendor(order, escrow) is False
    assert calls == []
