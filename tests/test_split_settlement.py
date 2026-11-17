"""B1: a seller is treated as "already paid by the Paystack split" only when
the split was really applied to that payment (single seller). In a
multi-seller payment no split happens, so Siiqo transfers their share or the
money stays in their withdrawable balance. B3: a failed automatic transfer
tells the seller once. Gateways are mocked; nothing leaves the process."""
from datetime import datetime
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.audit import AdminAuditLog
from app.models.communication import Notification
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.finance import Ledger
from app.models.order import OrderItem
from app.models.withdrawal import VendorBankAccount
from app.routes import escrow as escrow_routes
from app.routes.escrow import (PAYOUT_FAILED_TITLE, _deliver_digital_products,
                               execute_order_escrow_release)
from app.routes.withdrawal import _get_ledger_balance
from app.services import split_settlement
from app.services.escrow import paystack_provider


@pytest.fixture(autouse=True)
def _no_email(monkeypatch):
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: True)


@pytest.fixture
def transfers(monkeypatch):
    """Records Paystack transfers; result set per test (default: fails)."""
    calls = []
    state = {"success": False}

    def _transfer(**kwargs):
        calls.append(kwargs)
        return {"success": state["success"], "error_message": "mocked"}

    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor", _transfer)
    monkeypatch.setattr(paystack_provider, "ensure_paystack_transfer_recipient",
                        lambda **k: {"success": False})
    return calls, state


def _seller(make_vendor, subaccount=True):
    vendor, store = make_vendor()
    db.session.add(VendorBankAccount(
        vendor_id=vendor.id, bank_name="Test Bank", bank_code="000", account_number="0000000000",
        account_name="Test Vendor", is_default=True, recipient_code=f"RCP_{vendor.id}",
        paystack_subaccount_code=f"ACCT_{vendor.id}" if subaccount else None))
    db.session.commit()
    return vendor, store


def _paid_digital(vendor, store, make_product, make_order, ref, split_settled=None, n=0):
    product = make_product(store, price=10000, product_type="digital", file_url="https://files.example/x.pdf")
    order = make_order(vendor, total=10000, payment_method="PAYSTACK")
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, price_at_purchase=10000, quantity=1))
    escrow = EscrowTransaction(order_id=order.id, transaction_number=f"{ref}-{n}" if n else ref,
                               status=EscrowStatus.IN_ESCROW, amount=10000, fee_amount=300,
                               payscrow_ref=ref, split_settled=split_settled)
    db.session.add(escrow)
    db.session.commit()
    return order, escrow


def _deliver(order, escrow):
    _deliver_digital_products(order, escrow)
    db.session.commit()


# ── New rows: the flag decides ──────────────────────────────────────────────
def test_single_seller_split_is_not_paid_twice(make_vendor, make_product, make_order, transfers):
    calls, _ = transfers
    vendor, store = _seller(make_vendor)
    order, escrow = _paid_digital(vendor, store, make_product, make_order, "ORD-ONE", split_settled=True)
    _deliver(order, escrow)
    assert calls == []
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def test_multi_seller_with_subaccount_is_not_marked_paid(make_vendor, make_product, make_order, transfers):
    calls, _ = transfers  # transfer fails
    a, sa = _seller(make_vendor)
    b, sb = _seller(make_vendor)
    oa, ea = _paid_digital(a, sa, make_product, make_order, "ORD-MULTI", split_settled=False, n=1)
    ob, eb = _paid_digital(b, sb, make_product, make_order, "ORD-MULTI", split_settled=False, n=2)
    _deliver(oa, ea)
    _deliver(ob, eb)
    assert len(calls) == 2  # a transfer was attempted for each seller
    assert _get_ledger_balance(a.id) == Decimal("9700")
    assert _get_ledger_balance(b.id) == Decimal("9700")
    assert not Ledger.query.filter(Ledger.reference_id.like("PAYOUT-%")).first()


def test_multi_seller_successful_transfer_debits_once(make_vendor, make_product, make_order, transfers):
    calls, state = transfers
    state["success"] = True
    a, sa = _seller(make_vendor)
    oa, ea = _paid_digital(a, sa, make_product, make_order, "ORD-M2", split_settled=False)
    _deliver(oa, ea)
    assert len(calls) == 1
    assert _get_ledger_balance(a.id) == Decimal("0")


def test_generic_release_follows_the_flag(make_vendor, make_order, transfers):
    calls, _ = transfers
    vendor, _ = _seller(make_vendor)
    order = make_order(vendor, total=10000, payment_method="PAYSTACK")
    order.status = "DELIVERED"
    escrow = EscrowTransaction(order_id=order.id, transaction_number="ORD-GEN", payscrow_ref="ORD-GEN",
                               status=EscrowStatus.DELIVERED, amount=10000, fee_amount=300,
                               split_settled=False)
    db.session.add(escrow)
    db.session.commit()
    assert execute_order_escrow_release(order, escrow, source="test") is True
    assert len(calls) == 1
    assert _get_ledger_balance(vendor.id) == Decimal("9700")


# ── Old rows (flag NULL) ───────────────────────────────────────────────────
def test_old_single_seller_row_keeps_old_rule(make_vendor, make_product, make_order, transfers):
    calls, _ = transfers
    vendor, store = _seller(make_vendor)
    order, escrow = _paid_digital(vendor, store, make_product, make_order, "ORD-OLD1")
    assert escrow.split_settled is None
    _deliver(order, escrow)
    assert calls == []
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def test_old_multi_seller_row_is_not_split(make_vendor, make_product, make_order, transfers):
    calls, _ = transfers
    a, sa = _seller(make_vendor)
    b, sb = _seller(make_vendor)
    oa, ea = _paid_digital(a, sa, make_product, make_order, "ORD-OLD2", n=1)
    ob, eb = _paid_digital(b, sb, make_product, make_order, "ORD-OLD2", n=2)
    assert split_settlement.transaction_vendor_ids(ea, oa) == {a.id, b.id}
    _deliver(oa, ea)
    assert len(calls) == 1
    assert _get_ledger_balance(a.id) == Decimal("9700")


# ── Flag is recorded at Paystack initialise time ────────────────────────────
@pytest.mark.parametrize("used_split", [True, False])
def test_initiate_records_split_flag(client, make_vendor, make_user, make_product, make_order,
                                     auth_headers, monkeypatch, used_split):
    vendor, store = _seller(make_vendor)
    buyer = make_user()
    product = make_product(store, price=10000, product_type="digital")
    order = make_order(vendor, buyer, total=10000, payment_method="PAYSTACK")
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, price_at_purchase=10000, quantity=1))
    db.session.commit()

    class FakeProvider:
        def initiate_transaction(self, orders, existing=None, return_url=None):
            return {"success": True, "payment_link": "https://pay.example/x", "transaction_number": "ORD-INIT",
                    "provider_transaction_id": "AC_1", "provider_reference": "ORD-INIT",
                    "amount": 10000, "used_split": used_split}

    monkeypatch.setattr(escrow_routes, "get_escrow_provider", lambda *a, **k: FakeProvider())
    res = client.post("/api/escrow/initiate", headers=auth_headers(buyer), json={"order_id": order.id})
    assert res.status_code == 200, res.get_json()
    assert EscrowTransaction.query.filter_by(order_id=order.id).first().split_settled is used_split


# ── B3: failed transfer tells the seller once; holds do not ─────────────────
def test_failed_transfer_notifies_seller_once(make_vendor, make_product, make_order, transfers):
    vendor, store = _seller(make_vendor, subaccount=False)
    order, escrow = _paid_digital(vendor, store, make_product, make_order, "ORD-FAIL", split_settled=False)
    _deliver(order, escrow)
    escrow_routes._paystack_payout_vendor(order, escrow)  # a retry fails again
    db.session.commit()
    notes = Notification.query.filter_by(user_id=vendor.id, title=PAYOUT_FAILED_TITLE).all()
    assert len(notes) == 1
    assert "₦9,700.00 for Order #%d" % order.id in notes[0].message
    assert "safe in your Siiqo balance" in notes[0].message


def test_dispute_hold_does_not_send_failure_notice(make_vendor, make_product, make_order, transfers, monkeypatch):
    vendor, store = _seller(make_vendor, subaccount=False)
    order, escrow = _paid_digital(vendor, store, make_product, make_order, "ORD-HOLD", split_settled=False)
    monkeypatch.setattr(escrow_routes, "_dispute_blocks_payout", lambda o, e: True)
    assert escrow_routes._paystack_payout_vendor(order, escrow) is False
    db.session.commit()
    assert not Notification.query.filter_by(user_id=vendor.id, title=PAYOUT_FAILED_TITLE).first()


# ── Past damage: admin list + restore ───────────────────────────────────────
@pytest.fixture
def superadmin(monkeypatch):
    monkeypatch.setenv("ADMIN_2FA_REQUIRED", "false")
    admin = AdminUser(name="Boss", email=f"boss-{datetime.now().timestamp()}@siiqo.test", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _old_damage(make_vendor, make_order, vendor_count):
    """Old rows as the previous code left them: credit + PAYOUT debit."""
    sellers, ref = [], f"ORD-DMG{vendor_count}"
    for i in range(vendor_count):
        vendor, _ = _seller(make_vendor)
        order = make_order(vendor, total=10000, payment_method="PAYSTACK")
        escrow = EscrowTransaction(order_id=order.id, transaction_number=f"{ref}-{i}", payscrow_ref=ref,
                                   status=EscrowStatus.RELEASED, amount=10000, fee_amount=300)
        db.session.add(escrow)
        escrow_routes._credit_vendor_ledger(vendor.id, 9700.0, escrow.transaction_number, f"Payout for Order #{order.id}")
        db.session.flush()
        escrow_routes._record_payout_debit(order, escrow, 9700.0)
        db.session.commit()
        sellers.append((vendor, order))
    return sellers


def test_split_mismatch_list_and_restore(client, make_vendor, make_order, superadmin):
    multi = _old_damage(make_vendor, make_order, 2)
    _old_damage(make_vendor, make_order, 1)  # single seller: a real split, not listed
    res = client.get("/api/admin/payouts/split-mismatch", headers=superadmin)
    assert res.status_code == 200
    body = res.get_json()
    assert sorted(r["order_id"] for r in body["orders"]) == sorted(o.id for _, o in multi)
    assert body["total_amount"] == "19400.00"
    row = body["orders"][0]
    assert row["vendor"]["store_name"] and row["transaction_ref"] == "ORD-DMG2"

    vendor, order = multi[0]
    assert _get_ledger_balance(vendor.id) == Decimal("0")
    url = f"/api/admin/payouts/split-mismatch/{order.id}/restore"
    assert client.post(url, headers=superadmin, json={"note": "checked Paystack"}).status_code == 200
    assert client.post(url, headers=superadmin, json={}).status_code == 200  # idempotent
    assert _get_ledger_balance(vendor.id) == Decimal("9700")
    assert Ledger.query.filter_by(vendor_id=vendor.id, reference_id=f"RESTORE-PAYOUT-{order.id}").count() == 1
    assert AdminAuditLog.query.filter_by(action="PAYOUT_SPLIT_MISMATCH_RESTORED").count() == 1
    note = Notification.query.filter_by(user_id=vendor.id, order_id=order.id).first()
    assert note.message == f"₦9,700.00 from Order #{order.id} is now in your Siiqo balance. You can withdraw it."
    body = client.get("/api/admin/payouts/split-mismatch", headers=superadmin).get_json()
    assert body["total_amount"] == "9700.00"


def test_split_mismatch_needs_superadmin(client, make_user, auth_headers):
    assert client.get("/api/admin/payouts/split-mismatch", headers=auth_headers(make_user())).status_code == 403
