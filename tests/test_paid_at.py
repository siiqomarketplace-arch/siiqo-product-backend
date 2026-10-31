"""0B-5: every payment path leaves EscrowTransaction.paid_at and Order.paid_at
set (the monthly metrics count sales by them), set once and never overwritten."""
from datetime import datetime, timedelta, timezone

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.order import Order
from app.routes import payments as payments_routes

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_email(monkeypatch):
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: True)


def _naive(dt):
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


def _pending(make_vendor, make_order, payment_method="FLUTTERWAVE", ref=None):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=5000, payment_method=payment_method)
    escrow = EscrowTransaction(order_id=order.id, transaction_number=ref or f"REF-{order.id}",
                               status=EscrowStatus.PENDING_PAYMENT, amount=5000, fee_amount=150)
    db.session.add(escrow)
    db.session.commit()
    return order, escrow


def _reload(order, escrow):
    db.session.expire_all()
    return db.session.get(Order, order.id), db.session.get(EscrowTransaction, escrow.id)


# ── the flush-hook safety net (covers any path that forgets) ───────────────

def test_escrow_reaching_in_escrow_sets_both_paid_at(make_vendor, make_order):
    order, escrow = _pending(make_vendor, make_order)
    escrow.status = EscrowStatus.IN_ESCROW          # a path that sets no paid_at at all
    db.session.commit()

    order, escrow = _reload(order, escrow)
    assert escrow.paid_at is not None
    assert order.paid_at is not None
    assert _naive(order.paid_at) == _naive(escrow.paid_at)


def test_escrow_created_already_paid_gets_paid_at(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=5000)
    escrow = EscrowTransaction(order_id=order.id, transaction_number="NEW-PAID",
                               status=EscrowStatus.IN_ESCROW, amount=5000, fee_amount=150)
    db.session.add(escrow)
    db.session.commit()

    order, escrow = _reload(order, escrow)
    assert escrow.paid_at is not None and order.paid_at is not None


def test_later_status_changes_never_overwrite_paid_at(make_vendor, make_order):
    order, escrow = _pending(make_vendor, make_order)
    escrow.status = EscrowStatus.IN_ESCROW
    escrow.paid_at = T0
    order.paid_at = T0
    db.session.commit()

    for status in (EscrowStatus.SHIPPED, EscrowStatus.DELIVERED, EscrowStatus.RELEASED):
        escrow.status = status
        db.session.commit()

    order, escrow = _reload(order, escrow)
    assert _naive(escrow.paid_at) == _naive(T0)
    assert _naive(order.paid_at) == _naive(T0)


def test_unpaid_states_do_not_set_paid_at(make_vendor, make_order):
    order, escrow = _pending(make_vendor, make_order)
    escrow.status = EscrowStatus.CANCELLED
    db.session.commit()

    order, escrow = _reload(order, escrow)
    assert escrow.paid_at is None and order.paid_at is None


# ── admin "mark payment received" (/api/admin/escrow/verify) ───────────────

def _admin_headers(role="SUPERADMIN", email="ops@example.com"):
    admin = AdminUser(name="Ops", email=email, role=role)
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def test_admin_verify_sets_both_paid_at(client, make_vendor, make_order):
    order, escrow = _pending(make_vendor, make_order, payment_method="BANK_TRANSFER")
    res = client.post(f"/api/admin/escrow/verify/{order.id}", headers=_admin_headers())
    assert res.status_code == 200

    order, escrow = _reload(order, escrow)
    assert escrow.status == EscrowStatus.IN_ESCROW
    assert escrow.paid_at is not None
    assert order.paid_at is not None


def test_admin_verify_keeps_an_earlier_paid_at(client, make_vendor, make_order):
    order, escrow = _pending(make_vendor, make_order, payment_method="BANK_TRANSFER")
    escrow.paid_at = T0
    order.paid_at = T0
    db.session.commit()

    client.post(f"/api/admin/escrow/verify/{order.id}", headers=_admin_headers())

    order, escrow = _reload(order, escrow)
    assert _naive(escrow.paid_at) == _naive(T0)
    assert _naive(order.paid_at) == _naive(T0)


# ── Flutterwave confirmation (webhook and /status share the handler) ──────

def test_flutterwave_confirm_sets_then_keeps_paid_at(make_vendor, make_order):
    order, escrow = _pending(make_vendor, make_order, ref="FLW-ORD-1-AAAA")
    verification = {"success": True, "status": "successful", "tx_ref": escrow.transaction_number,
                    "amount": 5150.0, "currency": "NGN"}

    payments_routes._handle_flutterwave_payment_confirmed(escrow.transaction_number, "111", verification)
    order, escrow = _reload(order, escrow)
    first = _naive(escrow.paid_at)
    assert first is not None and order.paid_at is not None

    # A second webhook delivery / status poll for the same payment, later on.
    escrow.paid_at = first - timedelta(hours=1)   # make any overwrite visible
    db.session.commit()
    kept = _naive(escrow.paid_at)
    payments_routes._handle_flutterwave_payment_confirmed(escrow.transaction_number, "111", verification)

    order, escrow = _reload(order, escrow)
    assert _naive(escrow.paid_at) == kept


# ── money-path guards ──────────────────────────────────────────────────────

def test_admin_verify_requires_superadmin(client, make_vendor, make_order):
    order, escrow = _pending(make_vendor, make_order, payment_method="BANK_TRANSFER")
    res = client.post(f"/api/admin/escrow/verify/{order.id}",
                      headers=_admin_headers(role="ADMIN", email="support@example.com"))
    assert res.status_code == 403
    order, escrow = _reload(order, escrow)
    assert escrow.status == EscrowStatus.PENDING_PAYMENT


def test_admin_verify_refuses_non_pending_and_audits_success(client, make_vendor, make_order):
    from app.models.audit import AdminAuditLog
    order, escrow = _pending(make_vendor, make_order, payment_method="BANK_TRANSFER")
    headers = _admin_headers()
    assert client.post(f"/api/admin/escrow/verify/{order.id}", headers=headers).status_code == 200
    assert AdminAuditLog.query.filter_by(action="ADMIN_ESCROW_MARKED_PAID").count() == 1

    escrow.status = EscrowStatus.SHIPPED
    db.session.commit()
    res = client.post(f"/api/admin/escrow/verify/{order.id}", headers=headers)
    assert res.status_code == 409 and res.get_json()["code"] == "NOT_PENDING_PAYMENT"
    order, escrow = _reload(order, escrow)
    assert escrow.status == EscrowStatus.SHIPPED


def test_repeat_flutterwave_confirmation_changes_nothing(make_vendor, make_order, monkeypatch):
    calls = []
    import app.routes.escrow as escrow_routes
    monkeypatch.setattr(escrow_routes, "_deliver_event_tickets", lambda o, e: calls.append(o.id) or False)
    order, escrow = _pending(make_vendor, make_order, ref="FLW-ORD-2-BBBB")
    verification = {"success": True, "status": "successful", "tx_ref": escrow.transaction_number,
                    "amount": 5150.0, "currency": "NGN"}
    payments_routes._handle_flutterwave_payment_confirmed(escrow.transaction_number, "222", verification)
    order, escrow = _reload(order, escrow)
    escrow.status = EscrowStatus.SHIPPED
    order.status = "SHIPPED"
    db.session.commit()
    delivered_before = len(calls)
    assert delivered_before == 1  # the first confirmation did run delivery

    payments_routes._handle_flutterwave_payment_confirmed(escrow.transaction_number, "222", verification)
    order, escrow = _reload(order, escrow)
    assert escrow.status == EscrowStatus.SHIPPED and order.status == "SHIPPED"
    assert len(calls) == delivered_before  # delivery did not run again
