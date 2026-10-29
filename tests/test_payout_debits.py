"""M2: when a gateway pays the vendor automatically, the ledger credit is matched
by a debit, so the same money cannot be withdrawn a second time. When the payout
is not dispatched, the credit stays withdrawable."""
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.order import OrderItem
from app.models.withdrawal import VendorBankAccount
from app.routes import payments as payments_routes
from app.routes.escrow import _deliver_digital_products
from app.routes.withdrawal import _get_ledger_balance
from app.services import daya_service, flutterwave_service


@pytest.fixture(autouse=True)
def _no_email(monkeypatch):
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: True)


def _digital_order(make_vendor, make_product, make_order, payscrow_ref=None):
    vendor, store = make_vendor()
    product = make_product(store, price=10000, product_type="digital", file_url="https://files.example/x.pdf")
    order = make_order(vendor, total=10000, payment_method="FLUTTERWAVE")
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, price_at_purchase=10000, quantity=1))
    escrow = EscrowTransaction(order_id=order.id, transaction_number=f"FLW-PL-{order.id}-AAAA",
                               status=EscrowStatus.IN_ESCROW, amount=10000, fee_amount=300,
                               payscrow_ref=payscrow_ref)
    db.session.add(escrow)
    db.session.commit()
    return vendor, order, escrow


def _bank(vendor):
    db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Test Bank", bank_code="000",
                                     account_number="0000000000", account_name="Test Vendor", is_default=True))
    db.session.commit()


def test_split_payout_leaves_nothing_to_withdraw(make_vendor, make_product, make_order):
    vendor, order, escrow = _digital_order(make_vendor, make_product, make_order, payscrow_ref="SPLIT:RS_1")
    _deliver_digital_products(order, escrow)
    db.session.commit()
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def test_successful_transfer_leaves_nothing_to_withdraw(make_vendor, make_product, make_order, monkeypatch):
    vendor, order, escrow = _digital_order(make_vendor, make_product, make_order)
    _bank(vendor)
    monkeypatch.setattr(flutterwave_service, "transfer_to_vendor", lambda **k: {"success": True})
    _deliver_digital_products(order, escrow)
    db.session.commit()
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def test_failed_transfer_keeps_balance_withdrawable(make_vendor, make_product, make_order, monkeypatch):
    vendor, order, escrow = _digital_order(make_vendor, make_product, make_order)
    _bank(vendor)
    monkeypatch.setattr(flutterwave_service, "transfer_to_vendor", lambda **k: {"success": False})
    _deliver_digital_products(order, escrow)
    db.session.commit()
    assert _get_ledger_balance(vendor.id) == Decimal("9700")


def test_no_bank_account_keeps_balance_withdrawable(make_vendor, make_product, make_order):
    vendor, order, escrow = _digital_order(make_vendor, make_product, make_order)
    _deliver_digital_products(order, escrow)
    db.session.commit()
    assert _get_ledger_balance(vendor.id) == Decimal("9700")


def test_debit_is_recorded_once(make_vendor, make_product, make_order):
    from app.routes.escrow import _record_payout_debit
    vendor, order, escrow = _digital_order(make_vendor, make_product, make_order, payscrow_ref="SPLIT:RS_1")
    _deliver_digital_products(order, escrow)
    _record_payout_debit(order, escrow, 9700)
    db.session.commit()
    assert _get_ledger_balance(vendor.id) == Decimal("0")


# ── Daya payout reports whether it dispatched ──────────────────────────────

@pytest.fixture
def daya_stubs(monkeypatch):
    monkeypatch.setattr(daya_service, "get_rate", lambda **k: {"rate": 1500})
    monkeypatch.setattr(daya_service, "get_merchant_balance",
                        lambda: {"data": {"collection_balance_usd": 0, "withdrawal_balance_usd": 1000}})
    return monkeypatch


def _crypto_order(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=10000, payment_method="CRYPTO")
    escrow = EscrowTransaction(order_id=order.id, transaction_number=f"DAYA-{order.id}",
                               status=EscrowStatus.IN_ESCROW, amount=10000, fee_amount=300)
    db.session.add(escrow)
    db.session.commit()
    return vendor, order, escrow


def test_daya_payout_false_without_bank(make_vendor, make_order, daya_stubs):
    _, order, escrow = _crypto_order(make_vendor, make_order)
    assert payments_routes._payout_vendor_via_daya(order, escrow) is False


def test_daya_payout_true_on_transfer_success(make_vendor, make_order, daya_stubs):
    vendor, order, escrow = _crypto_order(make_vendor, make_order)
    _bank(vendor)
    daya_stubs.setattr(daya_service, "transfer_ngn_to_vendor", lambda **k: {"success": True})
    assert payments_routes._payout_vendor_via_daya(order, escrow) is True


def test_daya_payout_false_on_transfer_failure(make_vendor, make_order, daya_stubs):
    vendor, order, escrow = _crypto_order(make_vendor, make_order)
    _bank(vendor)
    daya_stubs.setattr(daya_service, "transfer_ngn_to_vendor", lambda **k: {"success": False})
    assert payments_routes._payout_vendor_via_daya(order, escrow) is False
