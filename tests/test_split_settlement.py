"""Never assume a split (Order 639). A seller counts as "paid by split" only
when the gateway's verified payment proves it; otherwise Siiqo transfers the
share at release (or it stays in the withdrawable balance). Never both.
Past releases recorded as paid without proof are listed for an admin, who can
restore them to the seller's balance or record the manual payout already sent.
Every gateway call is mocked; nothing leaves the process."""
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import event

from app.extensions import db
from app.models.admin import AdminUser
from app.models.audit import AdminAuditLog
from app.models.communication import Notification
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.finance import Ledger
from app.models.order import OrderItem
from app.models.payouts import ManualPayout, PayoutTransfer
from app.models.withdrawal import VendorBankAccount
from app.routes import escrow as escrow_routes
from app.routes.escrow import (PAYOUT_FAILED_TITLE, _deliver_digital_products,
                               execute_order_escrow_release)
from app.routes.withdrawal import _get_ledger_balance
from app.services import split_settlement
from app.services.escrow import paystack_provider
from app.services.payout_reconciliation import payout_state


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
        return {"success": state["success"], "transfer_code": "TRF_1", "status": "pending",
                "error_message": None if state["success"] else "mocked"}

    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor", _transfer)
    monkeypatch.setattr(paystack_provider, "ensure_paystack_transfer_recipient",
                        lambda **k: {"success": False})
    return calls, state


@pytest.fixture
def verify(monkeypatch):
    """Paystack GET /transaction/verify, mocked. state['raw'] is the verified
    transaction (None = Paystack unreachable)."""
    state = {"raw": None, "calls": 0}

    def _verify(self, ref):
        state["calls"] += 1
        if state["raw"] is None:
            return {"success": False, "error_message": "unreachable"}
        return {"success": True, "status": "success", "raw": state["raw"]}

    monkeypatch.setattr(paystack_provider.PaystackProvider, "verify_transaction", _verify)
    return state


def _seller(make_vendor, subaccount=True):
    vendor, store = make_vendor()
    db.session.add(VendorBankAccount(is_verified=True,
        vendor_id=vendor.id, bank_name="Test Bank", bank_code="000", account_number="0000000000",
        account_name="Test Vendor", is_default=True, recipient_code=f"RCP_{vendor.id}",
        paystack_subaccount_code=f"ACCT_{vendor.id}" if subaccount else None))
    db.session.commit()
    return vendor, store


def _paid_digital(vendor, store, make_product, make_order, ref, split_settled=None, n=0, **escrow_extra):
    product = make_product(store, price=10000, product_type="digital", file_url="https://files.example/x.pdf")
    order = make_order(vendor, total=10000, payment_method="PAYSTACK")
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, price_at_purchase=10000, quantity=1))
    escrow = EscrowTransaction(order_id=order.id, transaction_number=f"{ref}-{n}" if n else ref,
                               status=EscrowStatus.IN_ESCROW, amount=10000, fee_amount=300,
                               payscrow_ref=ref, split_settled=split_settled, **escrow_extra)
    db.session.add(escrow)
    db.session.commit()
    return order, escrow


def _deliver(order, escrow):
    _deliver_digital_products(order, escrow)
    db.session.commit()


def _split_txn(code, kobo):
    return {"status": "success", "reference": "R", "subaccount": {"subaccount_code": code},
            "fees_split": {"paystack": 25000, "integration": 30000, "subaccount": kobo}, "split": {}}


NO_SPLIT_TXN = {"status": "success", "reference": "R", "subaccount": {}, "fees_split": None, "split": {}}


# ── 1. proof decides ─────────────────────────────────────────────────────────
def test_unproven_split_is_paid_by_transfer(make_vendor, make_product, make_order, transfers, verify):
    """A split was requested (old flag) but Paystack's verified transaction
    shows none: Siiqo sends the transfer."""
    calls, state = transfers
    state["success"] = True
    verify["raw"] = NO_SPLIT_TXN
    vendor, store = _seller(make_vendor)
    order, escrow = _paid_digital(vendor, store, make_product, make_order, "ORD-UNP", split_settled=True)
    _deliver(order, escrow)
    assert len(calls) == 1 and calls[0]["amount_ngn"] == 9700.0
    assert escrow.split_settled is False and escrow.split_proof is None
    assert _get_ledger_balance(vendor.id) == Decimal("0")
    assert PayoutTransfer.query.filter_by(order_id=order.id, gateway="paystack", status="pending").count() == 1


def test_proven_split_sends_no_transfer(make_vendor, make_product, make_order, transfers, verify):
    calls, _ = transfers
    vendor, store = _seller(make_vendor)
    verify["raw"] = _split_txn(f"ACCT_{vendor.id}", 970000)
    order, escrow = _paid_digital(vendor, store, make_product, make_order, "ORD-PRV",
                                  split_requested=f"ACCT_{vendor.id}")
    _deliver(order, escrow)
    assert calls == []
    assert escrow.split_settled is True and "970000 kobo" in escrow.split_proof
    assert _get_ledger_balance(vendor.id) == Decimal("0")
    assert payout_state(order, escrow)["label"] == "Paid by split (proven)"


def test_split_to_someone_else_or_short_is_not_proof(make_vendor, make_product, make_order, transfers, verify):
    calls, state = transfers
    state["success"] = True
    vendor, store = _seller(make_vendor)
    verify["raw"] = _split_txn("ACCT_OTHER", 970000)
    order, escrow = _paid_digital(vendor, store, make_product, make_order, "ORD-OTH",
                                  split_requested=f"ACCT_{vendor.id}")
    _deliver(order, escrow)
    assert len(calls) == 1
    verify["raw"] = _split_txn(f"ACCT_{vendor.id}", 500000)   # less than the seller's 9,700
    o2, e2 = _paid_digital(vendor, store, make_product, make_order, "ORD-SHORT",
                           split_requested=f"ACCT_{vendor.id}")
    _deliver(o2, e2)
    assert len(calls) == 2 and e2.split_settled is False


def test_never_both_split_and_transfer(make_vendor, make_order, transfers, verify):
    """Proven split + repeated releases: no transfer ever. Transfer path +
    repeated release: one transfer, one debit."""
    calls, state = transfers
    state["success"] = True
    vendor, _ = _seller(make_vendor)
    for proven, ref in ((True, "ORD-NB1"), (False, "ORD-NB2")):
        order = make_order(vendor, total=10000, payment_method="PAYSTACK")
        order.status = "DELIVERED"
        escrow = EscrowTransaction(order_id=order.id, transaction_number=ref, payscrow_ref=ref,
                                   status=EscrowStatus.DELIVERED, amount=10000, fee_amount=300,
                                   split_settled=proven, split_proof="paystack verify: test" if proven else None)
        db.session.add(escrow)
        db.session.commit()
        assert execute_order_escrow_release(order, escrow, source="test") is True
        assert execute_order_escrow_release(order, escrow, source="test") is True
    assert len(calls) == 1
    assert Ledger.query.filter(Ledger.reference_id.like("PAYOUT-ORD-NB%")).count() == 2
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def test_split_flag_without_proof_and_paystack_unreachable_keeps_balance(
        make_vendor, make_product, make_order, transfers, verify):
    """Can't ask Paystack: never risk paying twice. No transfer, no debit;
    the money stays withdrawable and an ERROR is logged."""
    calls, _ = transfers
    vendor, store = _seller(make_vendor)
    order, escrow = _paid_digital(vendor, store, make_product, make_order, "ORD-UNK", split_settled=True)
    _deliver(order, escrow)
    assert calls == [] and verify["calls"] == 1
    assert _get_ledger_balance(vendor.id) == Decimal("9700")


def test_multi_seller_payment_without_split_request_is_transferred(make_vendor, make_product, make_order,
                                                                   transfers, verify):
    calls, _ = transfers  # transfer fails -> balance stays
    a, sa = _seller(make_vendor)
    b, sb = _seller(make_vendor)
    oa, ea = _paid_digital(a, sa, make_product, make_order, "ORD-MULTI", split_settled=False, n=1)
    ob, eb = _paid_digital(b, sb, make_product, make_order, "ORD-MULTI", split_settled=False, n=2)
    _deliver(oa, ea)
    _deliver(ob, eb)
    assert len(calls) == 2 and verify["calls"] == 0
    assert _get_ledger_balance(a.id) == Decimal("9700")
    assert not Ledger.query.filter(Ledger.reference_id.like("PAYOUT-%")).first()


def test_old_multi_seller_row_is_not_split(make_vendor, make_product, make_order, transfers, verify):
    calls, _ = transfers
    a, sa = _seller(make_vendor)
    b, sb = _seller(make_vendor)
    oa, ea = _paid_digital(a, sa, make_product, make_order, "ORD-OLD2", n=1)
    _paid_digital(b, sb, make_product, make_order, "ORD-OLD2", n=2)
    assert split_settlement.transaction_vendor_ids(ea, oa) == {a.id, b.id}
    _deliver(oa, ea)
    assert len(calls) == 1 and verify["calls"] == 0


def test_paystack_webhook_records_proof_at_confirmation(client, make_vendor, make_product, make_order,
                                                        transfers, verify, monkeypatch):
    """charge.success (signed) carrying fees_split proves the split when the
    payment is confirmed: no transfer, no extra Paystack call."""
    import hashlib
    import hmac
    import json
    from app.routes import bridge
    key = "sk_test_not_a_real_key"
    monkeypatch.setattr(bridge, "PAYSTACK_SECRET_KEY", key)
    calls, _ = transfers
    vendor, store = _seller(make_vendor)
    order, escrow = _paid_digital(vendor, store, make_product, make_order, "ORD-WHP",
                                  split_requested=f"ACCT_{vendor.id}")
    escrow.status, escrow.payment_ref, order.status = EscrowStatus.PENDING_PAYMENT, "ORD-WHP", "PENDING"
    db.session.commit()
    data = dict(_split_txn(f"ACCT_{vendor.id}", 970000), reference="ORD-WHP", amount=1015000,
                customer={"email": "b@example.com"},
                metadata={"order_ids": [str(order.id)], "source": "marketplace_checkout"})
    body = json.dumps({"event": "charge.success", "data": data}).encode()
    sig = hmac.new(key.encode(), body, hashlib.sha512).hexdigest()
    res = client.post("/api/payments/webhook", data=body,
                      headers={"Content-Type": "application/json", "x-paystack-signature": sig})
    assert res.status_code == 200
    db.session.expire_all()
    e = db.session.get(EscrowTransaction, escrow.id)
    assert e.status == EscrowStatus.RELEASED and e.split_settled is True and e.split_proof
    assert calls == [] and verify["calls"] == 0


@pytest.mark.parametrize("used_split", [True, False])
def test_initiate_records_only_the_request(client, make_vendor, make_user, make_product, make_order,
                                           auth_headers, monkeypatch, used_split):
    vendor, store = _seller(make_vendor)
    buyer = make_user()
    product = make_product(store, price=10000, product_type="digital")
    order = make_order(vendor, buyer, total=10000, payment_method="PAYSTACK")
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, price_at_purchase=10000, quantity=1))
    db.session.commit()

    class FakeProvider:
        def initiate_transaction(self, orders, existing=None, return_url=None, **kwargs):
            return {"success": True, "payment_link": "https://pay.example/x", "transaction_number": "ORD-INIT",
                    "provider_transaction_id": "AC_1", "provider_reference": "ORD-INIT",
                    "amount": 10000, "used_split": used_split,
                    "split_subaccounts": {vendor.id: f"ACCT_{vendor.id}"} if used_split else {}}

    monkeypatch.setattr(escrow_routes, "get_escrow_provider", lambda *a, **k: FakeProvider())
    res = client.post("/api/escrow/initiate", headers=auth_headers(buyer), json={"order_id": order.id})
    assert res.status_code == 200, res.get_json()
    e = EscrowTransaction.query.filter_by(order_id=order.id).first()
    assert e.split_settled is None
    assert e.split_requested == (f"ACCT_{vendor.id}" if used_split else None)


# ── failed transfer tells the seller once; holds do not ──────────────────────
def test_failed_transfer_notifies_seller_once(make_vendor, make_product, make_order, transfers, verify):
    vendor, store = _seller(make_vendor, subaccount=False)
    order, escrow = _paid_digital(vendor, store, make_product, make_order, "ORD-FAIL", split_settled=False)
    _deliver(order, escrow)
    escrow_routes._paystack_payout_vendor(order, escrow)  # a retry fails again
    db.session.commit()
    notes = Notification.query.filter_by(user_id=vendor.id, title=PAYOUT_FAILED_TITLE).all()
    assert len(notes) == 1
    assert "₦9,700.00 for Order #%d" % order.id in notes[0].message


def test_dispute_hold_does_not_send_failure_notice(make_vendor, make_product, make_order, transfers, monkeypatch):
    vendor, store = _seller(make_vendor, subaccount=False)
    order, escrow = _paid_digital(vendor, store, make_product, make_order, "ORD-HOLD", split_settled=False)
    monkeypatch.setattr(escrow_routes, "_dispute_blocks_payout", lambda o, e: True)
    assert escrow_routes._paystack_payout_vendor(order, escrow) is False
    db.session.commit()
    assert not Notification.query.filter_by(user_id=vendor.id, title=PAYOUT_FAILED_TITLE).first()


# ── 2. past damage: admin list, restore, manual payout ───────────────────────
@pytest.fixture
def superadmin(monkeypatch):
    monkeypatch.setenv("ADMIN_2FA_REQUIRED", "false")
    admin = AdminUser(name="Boss", email=f"boss-{datetime.now().timestamp()}@siiqo.test", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _released_with_debit(make_vendor, make_order, *, ref, gateway="paystack", payscrow_ref=None,
                         subaccount=True, amount=10000, split_settled=None):
    """A release as the OLD code left it: credit + PAYOUT debit, no transfer record."""
    vendor, _ = _seller(make_vendor, subaccount=subaccount)
    method = "FLUTTERWAVE" if gateway == "flutterwave" else "PAYSTACK"
    order = make_order(vendor, total=amount, payment_method=method)
    order.status = "COMPLETED"
    fee = round(amount * 0.03, 2)
    escrow = EscrowTransaction(order_id=order.id, transaction_number=ref, payscrow_ref=payscrow_ref or ref,
                               status=EscrowStatus.RELEASED, amount=amount, fee_amount=fee,
                               split_settled=split_settled,
                               released_at=datetime.now(timezone.utc) - timedelta(days=2))
    db.session.add(escrow)
    db.session.flush()
    net = float(amount - fee)
    escrow_routes._credit_vendor_ledger(vendor.id, net, escrow.transaction_number, f"Payout for Order #{order.id}")
    db.session.flush()
    escrow_routes._record_payout_debit(order, escrow, net)
    db.session.commit()
    return vendor, order, escrow


def _order_639(make_vendor, make_order):
    """Single seller, ₦2,500 digital, Flutterwave bank transfer, "SPLIT:" marker,
    released with a payout debit and no transfer."""
    return _released_with_debit(make_vendor, make_order, ref="FLW-ORD-639-ABCDEF12", gateway="flutterwave",
                                payscrow_ref="SPLIT:RS_TEST:FLW-ORD-639-ABCDEF12", subaccount=False,
                                amount=2500)


def test_reconciliation_lists_the_639_case(client, make_vendor, make_order, superadmin):
    vendor, order, escrow = _order_639(make_vendor, make_order)
    _released_with_debit(make_vendor, make_order, ref="ORD-LEGACYTRF", subaccount=False,
                         split_settled=False)  # a legacy transfer: not a split case
    body = client.get("/api/admin/payouts/split-mismatch", headers=superadmin).get_json()
    assert [r["order_id"] for r in body["orders"]] == [order.id]
    row = body["orders"][0]
    assert row["gateway"] == "flutterwave" and row["kind"] == "split_unproven"
    assert row["amount"] == "2425.00" and body["total_amount"] == "2425.00"
    st = payout_state(order, escrow)
    assert st["state"] == "needs_look" and "no proof" in st["label"]
    # Untracked legacy transfers show only on request.
    body = client.get("/api/admin/payouts/split-mismatch?include_untracked=1", headers=superadmin).get_json()
    assert {r["kind"] for r in body["orders"]} == {"split_unproven", "transfer_untracked"}


def test_single_and_multi_seller_paystack_cases_listed(client, make_vendor, make_order, superadmin):
    _, single, _ = _released_with_debit(make_vendor, make_order, ref="ORD-PS1")             # old rule: subaccount
    _, flagged, _ = _released_with_debit(make_vendor, make_order, ref="ORD-PS2", split_settled=True)
    _, proven, e = _released_with_debit(make_vendor, make_order, ref="ORD-PS3", split_settled=True)
    e.split_proof = "paystack verify: proven"
    db.session.commit()
    ids = {r["order_id"] for r in client.get("/api/admin/payouts/split-mismatch",
                                             headers=superadmin).get_json()["orders"]}
    assert ids == {single.id, flagged.id}


def test_record_manual_payout_for_639_is_idempotent_and_audited(client, make_vendor, make_order, superadmin):
    vendor, order, escrow = _order_639(make_vendor, make_order)
    url = f"/api/admin/payouts/split-mismatch/{order.id}/manual-payout"
    payload = {"amount": "2425", "gateway": "flutterwave", "reference": "TRF-088878864676",
               "paid_on": "2026-11-26", "note": "Paid by hand from the Flutterwave dashboard"}
    balance_before = _get_ledger_balance(vendor.id)
    res = client.post(url, headers=superadmin, json=payload)
    assert res.status_code == 200 and res.get_json()["message"].startswith("Manual payout recorded")
    res = client.post(url, headers=superadmin, json=payload)
    assert res.status_code == 200 and res.get_json()["message"] == "Already recorded. Nothing changed."
    assert ManualPayout.query.filter_by(order_id=order.id).count() == 1
    assert AdminAuditLog.query.filter_by(action="PAYOUT_MANUAL_RECORDED").count() == 1
    assert _get_ledger_balance(vendor.id) == balance_before == Decimal("0")
    st = payout_state(order, escrow)
    assert st["state"] == "manual" and "TRF-088878864676" in st["label"]
    assert client.get("/api/admin/payouts/split-mismatch", headers=superadmin).get_json()["orders"] == []
    # A different reference for the same order is refused; restore is no longer offered.
    bad = client.post(url, headers=superadmin, json=dict(payload, reference="TRF-OTHER"))
    assert bad.status_code == 400
    assert client.post(f"/api/admin/payouts/split-mismatch/{order.id}/restore", headers=superadmin,
                       json={}).status_code == 404


def test_manual_payout_validation(client, make_vendor, make_order, superadmin):
    _, order, _ = _order_639(make_vendor, make_order)
    url = f"/api/admin/payouts/split-mismatch/{order.id}/manual-payout"
    for bad in ({"amount": "x", "gateway": "flutterwave", "reference": "R", "paid_on": "2026-11-26"},
                {"amount": "1", "gateway": "cash", "reference": "R", "paid_on": "2026-11-26"},
                {"amount": "1", "gateway": "flutterwave", "reference": "", "paid_on": "2026-11-26"},
                {"amount": "1", "gateway": "flutterwave", "reference": "R", "paid_on": "26/11/2026"}):
        assert client.post(url, headers=superadmin, json=bad).status_code == 400


def test_restore_is_idempotent_then_manual_payout_refused(client, make_vendor, make_order, superadmin):
    vendor, order, _ = _order_639(make_vendor, make_order)
    url = f"/api/admin/payouts/split-mismatch/{order.id}/restore"
    assert client.post(url, headers=superadmin, json={"note": "checked Flutterwave"}).status_code == 200
    assert client.post(url, headers=superadmin, json={}).get_json()["message"] == "Already restored. Nothing changed."
    assert _get_ledger_balance(vendor.id) == Decimal("2425")
    assert Ledger.query.filter_by(vendor_id=vendor.id, reference_id=f"RESTORE-PAYOUT-{order.id}").count() == 1
    assert AdminAuditLog.query.filter_by(action="PAYOUT_SPLIT_MISMATCH_RESTORED").count() == 1
    res = client.post(f"/api/admin/payouts/split-mismatch/{order.id}/manual-payout", headers=superadmin,
                      json={"amount": "2425", "gateway": "flutterwave", "reference": "TRF-1", "paid_on": "2026-11-26"})
    assert res.status_code == 400 and "paid twice" in res.get_json()["message"]
    assert payout_state(order, db.session.get(EscrowTransaction, order.escrow.id))["state"] == "held"


def test_check_with_paystack_records_proof(client, make_vendor, make_order, superadmin, verify):
    vendor, order, escrow = _released_with_debit(make_vendor, make_order, ref="ORD-CHK", split_settled=True)
    verify["raw"] = _split_txn(f"ACCT_{vendor.id}", 970000)
    res = client.post(f"/api/admin/payouts/split-mismatch/{order.id}/check", headers=superadmin)
    assert res.status_code == 200 and res.get_json()["split_proven"] is True
    assert client.get("/api/admin/payouts/split-mismatch", headers=superadmin).get_json()["orders"] == []


def test_list_needs_superadmin(client, make_user, auth_headers):
    assert client.get("/api/admin/payouts/split-mismatch", headers=auth_headers(make_user())).status_code == 403


def test_list_is_paginated_and_bounded(client, make_vendor, make_order, superadmin):
    for i in range(30):
        _released_with_debit(make_vendor, make_order, ref=f"FLW-ORD-{i}-X", gateway="flutterwave",
                             payscrow_ref=f"SPLIT:RS_{i}:FLW-ORD-{i}-X", subaccount=False)
    queries = []

    def _count(*a, **k):
        queries.append(1)

    engine = db.engine
    event.listen(engine, "before_cursor_execute", _count)
    try:
        start = time.perf_counter()
        body = client.get("/api/admin/payouts/split-mismatch?per_page=10&page=2", headers=superadmin).get_json()
        elapsed = time.perf_counter() - start
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert body["total"] == 30 and body["pages"] == 3 and body["page"] == 2 and len(body["orders"]) == 10
    assert len(queries) <= 15, len(queries)    # fixed, not one-per-row
    assert elapsed < 5
