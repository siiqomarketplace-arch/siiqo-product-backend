"""M7: the admin 'fix crypto order' tool is superadmin-only, works only on
PENDING orders and can never credit or pay a vendor twice."""
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.order import Order
from app.models.withdrawal import VendorBankAccount
from app.routes import payments as payments_routes
from app.routes.withdrawal import _get_ledger_balance


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: True)
    monkeypatch.setattr("app.routes.admin.send_siiqo_email", lambda *a, **k: True)


def _admin(role):
    admin = AdminUser(name="Ops", email=f"{role.lower()}@example.com", role=role)
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _stuck_order(make_vendor, make_order, payment_method="CRYPTO", status="PENDING"):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=10000, payment_method=payment_method)
    order.status = status
    escrow = EscrowTransaction(order_id=order.id, transaction_number=f"T-{order.id}",
                               status=EscrowStatus.PENDING_PAYMENT, amount=10000, fee_amount=300)
    db.session.add(escrow)
    db.session.commit()
    return vendor, order


def _url(order):
    return f"/api/admin/escrow/fix-crypto-order/{order.id}"


def test_regular_admin_is_refused(client, make_vendor, make_order):
    _, order = _stuck_order(make_vendor, make_order)
    res = client.post(_url(order), headers=_admin("ADMIN"))
    assert res.status_code == 403


def test_fixes_pending_order_once(client, make_vendor, make_order):
    vendor, order = _stuck_order(make_vendor, make_order)
    headers = _admin("SUPERADMIN")

    first = client.post(_url(order), headers=headers)
    assert first.status_code == 200
    assert _get_ledger_balance(vendor.id) == Decimal("9700")

    second = client.post(_url(order), headers=headers)
    assert second.status_code == 409
    assert _get_ledger_balance(vendor.id) == Decimal("9700")


def test_completed_order_is_refused(client, make_vendor, make_order):
    vendor, order = _stuck_order(make_vendor, make_order, status="COMPLETED")
    res = client.post(_url(order), headers=_admin("SUPERADMIN"))
    assert res.status_code == 409
    assert db.session.get(Order, order.id).status == "COMPLETED"
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def test_non_crypto_order_is_never_paid_from_daya(client, make_vendor, make_order, monkeypatch):
    calls = []
    monkeypatch.setattr(payments_routes, "_payout_vendor_via_daya", lambda *a: calls.append(a) or True)
    vendor, order = _stuck_order(make_vendor, make_order, payment_method="PAYSTACK")
    res = client.post(_url(order), headers=_admin("SUPERADMIN"))
    assert res.status_code == 200
    assert calls == []
    assert _get_ledger_balance(vendor.id) == Decimal("9700")


def test_crypto_payout_is_debited(client, make_vendor, make_order, monkeypatch):
    from app.services import daya_service
    monkeypatch.setattr(daya_service, "get_rate", lambda **k: {"rate": 1500})
    monkeypatch.setattr(daya_service, "get_merchant_balance",
                        lambda: {"data": {"collection_balance_usd": 0, "withdrawal_balance_usd": 1000}})
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor", lambda **k: {"success": True})
    vendor, order = _stuck_order(make_vendor, make_order)
    db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Test Bank", bank_code="000",
                                     account_number="0000000000", account_name="Test Vendor", is_default=True))
    db.session.commit()

    res = client.post(_url(order), headers=_admin("SUPERADMIN"))
    assert res.status_code == 200
    assert _get_ledger_balance(vendor.id) == Decimal("0")
