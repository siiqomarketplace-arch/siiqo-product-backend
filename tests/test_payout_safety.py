"""Payouts can't be lost: Flutterwave never splits (Siiqo transfers), every
payout transfer is followed to its final status (webhooks + scheduled check)
and a failed/reversed one goes back to the balance exactly once, a daily check
flags any released order not paid by proven split / transfer / balance /
manual payout, the Flutterwave subaccount default gives Siiqo only 3%, the
buyer pays the gateway fee once, and order views say what really happened.
Every gateway call is mocked; nothing leaves the process."""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
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
from app.models.payouts import ManualPayout, PayoutTransfer
from app.models.withdrawal import PartnerBankAccount, PartnerWithdrawal, VendorBankAccount, Withdrawal
from app.routes import bridge
from app.routes import escrow as escrow_routes
from app.routes.withdrawal import _get_ledger_balance
from app.services import daya_service, flutterwave_service, payout_reconciliation, payout_tracking
from app.services.escrow import paystack_provider

PSK = "sk_test_not_a_real_key"


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: True)
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", PSK)
    monkeypatch.setenv("FLW_SECRET_KEY", "flw_test_not_a_real_key")
    monkeypatch.setattr(bridge, "PAYSTACK_SECRET_KEY", PSK)
    monkeypatch.delenv("FLW_FEE_ADDED_BY", raising=False)


@pytest.fixture
def superadmin(monkeypatch):
    monkeypatch.setenv("ADMIN_2FA_REQUIRED", "false")
    admin = AdminUser(name="Boss", email=f"boss-{datetime.now().timestamp()}@siiqo.test", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _seller(make_vendor, flw_sub=True):
    vendor, store = make_vendor()
    db.session.add(VendorBankAccount(is_verified=True, vendor_id=vendor.id, bank_name="Access Bank",
                                     bank_code="044", account_number="0123456789", account_name="Test Vendor",
                                     is_default=True, recipient_code=f"RCP_{vendor.id}",
                                     flw_subaccount_id=f"RS_{vendor.id}" if flw_sub else None))
    db.session.commit()
    return vendor, store


def _paystack_signed(client, event):
    body = json.dumps(event).encode()
    sig = hmac.new(PSK.encode(), body, hashlib.sha512).hexdigest()
    return client.post("/api/payments/webhook", data=body,
                       headers={"Content-Type": "application/json", "x-paystack-signature": sig})


# ── 1. Flutterwave: a seller with a subaccount, paid by bank transfer ────────
def test_flutterwave_bank_transfer_with_subaccount_is_paid_by_transfer(
        client, make_vendor, make_user, make_product, make_order, auth_headers, monkeypatch):
    vendor, store = _seller(make_vendor, flw_sub=True)
    buyer = make_user()
    product = make_product(store, price=2500, product_type="digital", file_url="https://files.example/x.pdf")
    order = make_order(vendor, buyer, total=2500, payment_method="FLUTTERWAVE")
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, price_at_purchase=2500, quantity=1))
    db.session.commit()

    payloads = []
    monkeypatch.setattr(flutterwave_service.requests, "post", lambda url, json=None, headers=None, timeout=None:
                        payloads.append(json) or _Resp({"status": "success", "data": {"link": "https://pay/x"}}))
    res = client.post("/api/payments/flutterwave/initiate", headers=auth_headers(buyer),
                      json={"orderId": str(order.id), "buyerEmail": "buyer@example.com"})
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert "subaccounts" not in payloads[0]                 # no split asked for
    assert payloads[0]["amount"] == 2500.0                 # fee added once, by Flutterwave
    assert body["flwFee"] == 0 and body["feeAddedBy"] == "flutterwave"
    ref = body["transactionNumber"]
    escrow = EscrowTransaction.query.filter_by(order_id=order.id).one()
    assert escrow.payscrow_ref == f"DIRECT:{ref}"

    sent = []
    monkeypatch.setattr(flutterwave_service, "transfer_to_vendor",
                        lambda **k: sent.append(k) or {"success": True, "transfer_id": "555", "status": "NEW"})
    monkeypatch.setattr(flutterwave_service, "verify_webhook_signature", lambda *a: True)
    # Bank transfer: Flutterwave's verify response carries no split information.
    monkeypatch.setattr(flutterwave_service, "verify_transaction", lambda tid: {
        "success": True, "status": "successful", "tx_ref": ref, "amount": 2550.71, "currency": "NGN",
        "order_id": str(order.id)})
    res = client.post("/api/payments/flutterwave/webhook", headers={"verif-hash": "x"},
                      json={"event": "charge.completed", "data": {"id": 99, "tx_ref": ref, "status": "successful",
                                                                 "payment_type": "bank_transfer"}})
    assert res.status_code == 200
    db.session.expire_all()
    escrow = db.session.get(EscrowTransaction, escrow.id)
    assert escrow.status == EscrowStatus.RELEASED and escrow.split_settled is False
    assert len(sent) == 1 and sent[0]["amount_ngn"] == 2425.0
    tr = PayoutTransfer.query.filter_by(order_id=order.id).one()
    assert tr.gateway == "flutterwave" and tr.gateway_transfer_id == "555" and tr.status == "pending"
    assert _get_ledger_balance(vendor.id) == Decimal("0")


# ── 3. transfer outcomes ─────────────────────────────────────────────────────
def _released_by_transfer(make_vendor, make_order, gateway="paystack", amount=10000):
    vendor, _ = _seller(make_vendor)
    order = make_order(vendor, total=amount, payment_method="PAYSTACK" if gateway == "paystack" else "FLUTTERWAVE")
    order.status = "COMPLETED"
    escrow = EscrowTransaction(order_id=order.id, transaction_number=f"ORD-T{order.id}", status=EscrowStatus.RELEASED,
                               amount=amount, fee_amount=amount * 0.03, split_settled=False,
                               released_at=datetime.now(timezone.utc) - timedelta(hours=30))
    db.session.add(escrow)
    db.session.flush()
    net = float(amount * 0.97)
    escrow_routes._credit_vendor_ledger(vendor.id, net, escrow.transaction_number, "credit")
    db.session.flush()
    escrow_routes._track_order_transfer(order, escrow, gateway, f"PAYOUT-{order.id}-ABC", net,
                                        transfer_id="777", status="pending")
    escrow_routes._record_payout_debit(order, escrow, net)
    db.session.commit()
    return vendor, order, escrow


def _bounce_notes(vendor, order):
    return Notification.query.filter_by(user_id=vendor.id, order_id=order.id,
                                        title=payout_tracking.ORDER_FAILED_TITLE).count()


@pytest.mark.parametrize("event_name", ["transfer.failed", "transfer.reversed"])
def test_paystack_transfer_failed_webhook_restores_once(client, make_vendor, make_order, event_name):
    vendor, order, _ = _released_by_transfer(make_vendor, make_order)
    assert _get_ledger_balance(vendor.id) == Decimal("0")
    event = {"event": event_name, "data": {"reference": f"PAYOUT-{order.id}-ABC", "transfer_code": "TRF_x",
                                           "status": event_name.split(".")[1], "reason": "Account closed"}}
    assert _paystack_signed(client, event).status_code == 200
    assert _paystack_signed(client, event).status_code == 200      # repeat: nothing more
    assert _get_ledger_balance(vendor.id) == Decimal("9700")
    assert Ledger.query.filter(Ledger.reference_id.like("%-RETURNED")).count() == 1
    assert _bounce_notes(vendor, order) == 1
    note = Notification.query.filter_by(user_id=vendor.id, title=payout_tracking.ORDER_FAILED_TITLE).first()
    assert note.message.startswith(f"Your payout for Order #{order.id} didn't go through; it's back in your Siiqo balance")
    st = payout_reconciliation.payout_state(order, order.escrow)
    assert st["state"] == "held"


def test_paystack_transfer_webhook_needs_signature(client, make_vendor, make_order):
    vendor, order, _ = _released_by_transfer(make_vendor, make_order)
    res = client.post("/api/payments/webhook", json={"event": "transfer.failed",
                                                     "data": {"reference": f"PAYOUT-{order.id}-ABC"}},
                      headers={"x-paystack-signature": "forged"})
    assert res.status_code == 400
    assert _get_ledger_balance(vendor.id) == Decimal("0")


def test_paystack_success_then_reversed(client, make_vendor, make_order):
    vendor, order, _ = _released_by_transfer(make_vendor, make_order)
    ref = f"PAYOUT-{order.id}-ABC"
    _paystack_signed(client, {"event": "transfer.success", "data": {"reference": ref, "status": "success"}})
    assert PayoutTransfer.query.filter_by(reference=ref).one().status == "success"
    assert payout_reconciliation.payout_state(order, order.escrow)["label"].startswith("Paid by transfer (ref")
    _paystack_signed(client, {"event": "transfer.reversed", "data": {"reference": ref, "status": "reversed"}})
    assert _get_ledger_balance(vendor.id) == Decimal("9700")


def test_flutterwave_failed_webhook_is_confirmed_with_the_api(client, make_vendor, make_order, monkeypatch):
    vendor, order, _ = _released_by_transfer(make_vendor, make_order, gateway="flutterwave")
    monkeypatch.setattr(flutterwave_service, "verify_webhook_signature", lambda *a: True)
    event = {"event": "transfer.completed", "event.type": "Transfer",
             "data": {"id": 777, "reference": f"PAYOUT-{order.id}-ABC", "status": "FAILED"}}
    # Forged failure: Flutterwave's API says it succeeded -> nothing given back.
    monkeypatch.setattr(flutterwave_service, "get_transfer_status",
                        lambda tid: {"success": True, "status": "SUCCESSFUL", "data": {}})
    assert client.post("/api/payments/flutterwave/webhook", json=event, headers={"verif-hash": "x"}).status_code == 200
    assert _get_ledger_balance(vendor.id) == Decimal("0")
    monkeypatch.setattr(flutterwave_service, "get_transfer_status",
                        lambda tid: {"success": True, "status": "FAILED", "data": {}})
    for _ in range(2):
        assert client.post("/api/payments/flutterwave/webhook", json=event,
                           headers={"verif-hash": "x"}).status_code == 200
    assert _get_ledger_balance(vendor.id) == Decimal("9700")
    assert _bounce_notes(vendor, order) == 1


def test_flutterwave_transfer_webhook_rejected_without_hash(client, make_vendor, make_order, monkeypatch):
    monkeypatch.delenv("FLW_WEBHOOK_SECRET", raising=False)
    res = client.post("/api/payments/flutterwave/webhook", json={"event": "transfer.completed", "data": {}},
                      headers={"verif-hash": "x"})
    assert res.status_code == 401


def test_daya_transfer_failed_webhook(client, make_vendor, make_order, monkeypatch):
    vendor, order, _ = _released_by_transfer(make_vendor, make_order, gateway="daya")
    monkeypatch.setenv("DAYA_WEBHOOK_SECRET", "whsec_test")
    body = json.dumps({"event": "transfer.failed", "data": {"id": "777", "reference": f"PAYOUT-{order.id}-ABC",
                                                            "status": "FAILED"}}).encode()
    bad = client.post("/api/payments/daya/webhook", data=body,
                      headers={"Content-Type": "application/json", "X-Daya-Signature": "forged"})
    assert bad.status_code == 401
    sig = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
    for _ in range(2):
        assert client.post("/api/payments/daya/webhook", data=body,
                           headers={"Content-Type": "application/json", "X-Daya-Signature": sig}).status_code == 200
    assert _get_ledger_balance(vendor.id) == Decimal("9700")


def test_scheduled_check_restores_once_and_never_resends(make_vendor, make_order, monkeypatch):
    vendor, order, _ = _released_by_transfer(make_vendor, make_order)
    tr = PayoutTransfer.query.filter_by(order_id=order.id).one()
    tr.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db.session.commit()
    resent = []
    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor", lambda **k: resent.append(k))
    monkeypatch.setattr(paystack_provider, "paystack_verify_transfer",
                        lambda ref: {"success": True, "status": "failed", "reason": "Invalid account"})
    assert payout_tracking.run_transfer_check()["failed"] == 1
    assert payout_tracking.run_transfer_check()["checked"] == 0     # no longer pending
    assert _get_ledger_balance(vendor.id) == Decimal("9700")
    assert _bounce_notes(vendor, order) == 1 and resent == []
    tr = PayoutTransfer.query.filter_by(order_id=order.id).one()
    assert tr.status == "failed" and tr.failure_reason == "Invalid account"


def test_scheduled_check_skips_young_and_records_success(make_vendor, make_order, monkeypatch):
    _, order, _ = _released_by_transfer(make_vendor, make_order)
    monkeypatch.setattr(paystack_provider, "paystack_verify_transfer",
                        lambda ref: {"success": True, "status": "success"})
    assert payout_tracking.run_transfer_check()["checked"] == 0           # < 2h old
    tr = PayoutTransfer.query.filter_by(order_id=order.id).one()
    tr.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db.session.commit()
    assert payout_tracking.run_transfer_check()["success"] == 1


def test_seller_withdrawal_failed_restores_once(client, make_vendor, monkeypatch):
    vendor, _ = _seller(make_vendor)
    escrow_routes._credit_vendor_ledger(vendor.id, 5000.0, "C1", "credit")
    db.session.commit()
    monkeypatch.setattr(flutterwave_service, "transfer_to_vendor",
                        lambda **k: {"success": True, "transfer_id": "888", "status": "NEW"})
    from flask_jwt_extended import create_access_token as tok
    res = client.post("/api/withdrawal/withdrawals", json={"amount": "5000"},
                      headers={"Authorization": f"Bearer {tok(identity=str(vendor.id))}"})
    assert res.status_code == 200, res.get_json()
    w = Withdrawal.query.filter_by(vendor_id=vendor.id).one()
    tr = PayoutTransfer.query.filter_by(kind="withdrawal", withdrawal_id=w.id).one()
    assert tr.gateway == "flutterwave" and tr.gateway_transfer_id == "888"
    assert _get_ledger_balance(vendor.id) == Decimal("0")
    for _ in range(2):
        payout_tracking.apply_outcome("flutterwave", reference=w.transfer_reference,
                                      raw_status="FAILED")
    db.session.expire_all()
    assert db.session.get(Withdrawal, w.id).status == "FAILED"
    assert _get_ledger_balance(vendor.id) == Decimal("5000")


def test_partner_withdrawal_failed_gives_earnings_back(make_user):
    partner = make_user()
    acc = PartnerBankAccount(partner_id=partner.id, bank_name="Bank", bank_code="000",
                             account_number="0123456789", account_name="Rider Co")
    db.session.add(acc)
    db.session.flush()
    w = PartnerWithdrawal(partner_id=partner.id, bank_account_id=acc.id, amount=Decimal("3000"),
                          net_amount=Decimal("3000"), fee_amount=Decimal("0"), status="PENDING",
                          transfer_reference="PWD-1", channel="daya", transfer_code="d-1")
    db.session.add(w)
    db.session.flush()
    payout_tracking.record_transfer(kind="partner_withdrawal", user_id=partner.id, gateway="daya",
                                    reference="PWD-1", amount=3000, gateway_transfer_id="d-1",
                                    partner_withdrawal_id=w.id)
    db.session.commit()
    for _ in range(2):
        payout_tracking.apply_outcome("daya", transfer_id="d-1", raw_status="FAILED")
    assert db.session.get(PartnerWithdrawal, w.id).status == "FAILED"
    assert Notification.query.filter_by(user_id=partner.id, title=payout_tracking.WITHDRAWAL_FAILED_TITLE).count() == 1


# ── 4. daily reconciliation ──────────────────────────────────────────────────
def test_daily_job_flags_gaps_only(make_vendor, make_order, caplog):
    # gap: recorded as paid by split, no proof (the 639 case)
    v1, _ = _seller(make_vendor)
    gap = make_order(v1, total=2500, payment_method="FLUTTERWAVE")
    e1 = EscrowTransaction(order_id=gap.id, transaction_number="FLW-ORD-GAP", payscrow_ref="SPLIT:RS_1:FLW-ORD-GAP",
                           status=EscrowStatus.RELEASED, amount=2500, fee_amount=75,
                           released_at=datetime.now(timezone.utc) - timedelta(hours=30))
    db.session.add(e1)
    db.session.flush()
    escrow_routes._credit_vendor_ledger(v1.id, 2425.0, e1.transaction_number, "credit")
    db.session.flush()
    escrow_routes._record_payout_debit(gap, e1, 2425.0)
    db.session.commit()
    # fine: successful transfer / held in balance / manual payout
    _, ok_tr, _ = _released_by_transfer(make_vendor, make_order)
    PayoutTransfer.query.filter_by(order_id=ok_tr.id).one().status = "success"
    v3, _ = _seller(make_vendor)
    held = make_order(v3, total=1000, payment_method="PAYSTACK")
    e3 = EscrowTransaction(order_id=held.id, transaction_number="ORD-HELD", status=EscrowStatus.RELEASED,
                           amount=1000, fee_amount=30, released_at=datetime.now(timezone.utc) - timedelta(hours=30))
    db.session.add(e3)
    escrow_routes._credit_vendor_ledger(v3.id, 970.0, "ORD-HELD", "credit")
    _, manual, _ = _released_by_transfer(make_vendor, make_order)
    db.session.add(ManualPayout(order_id=manual.id, vendor_id=manual.vendor_id, amount=Decimal("1"),
                                gateway="bank", reference="X", paid_on=datetime(2026, 11, 26).date()))
    db.session.commit()

    with caplog.at_level(logging.ERROR, logger="app.services.payout_reconciliation"):
        res = payout_reconciliation.run_daily_reconciliation()
    assert res["order_ids"] == [gap.id]
    assert any(f"Order #{gap.id}" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)
    from app.services.activation import payout_failures
    kinds = {(i["kind"], i["order_id"]) for i in payout_failures()}
    assert ("payout_gap", gap.id) in kinds


def test_pending_transfer_after_24h_needs_a_look(make_vendor, make_order):
    _, order, _ = _released_by_transfer(make_vendor, make_order)
    tr = PayoutTransfer.query.filter_by(order_id=order.id).one()
    tr.created_at = datetime.now(timezone.utc) - timedelta(hours=26)
    db.session.commit()
    assert payout_reconciliation.run_daily_reconciliation()["order_ids"] == [order.id]
    assert any(i["kind"] == "payout_transfer" for i in payout_tracking.needs_a_look())


# ── 5. Flutterwave subaccount default + repair ───────────────────────────────
def test_create_subaccount_gives_siiqo_three_percent(monkeypatch):
    sent = []
    monkeypatch.setattr(flutterwave_service.requests, "post", lambda url, json=None, headers=None, timeout=None:
                        sent.append(json) or _Resp({"status": "success", "data": {"subaccount_id": "RS_1"}}))
    assert flutterwave_service.create_subaccount("Shop", "044", "0123456789", "080")["success"]
    assert sent[0]["split_type"] == "percentage" and sent[0]["split_value"] == 0.03


def test_fix_subaccount_splits_action(client, superadmin, monkeypatch):
    listing = {"status": "success", "meta": {"page_info": {"current_page": 1, "total_pages": 1}},
               "data": [{"id": 11, "subaccount_id": "RS_A", "split_type": "percentage", "split_value": 0.97},
                        {"id": 12, "subaccount_id": "RS_B", "split_type": "percentage", "split_value": 0.03}]}
    puts = []
    monkeypatch.setattr(flutterwave_service.requests, "get", lambda url, params=None, headers=None, timeout=None:
                        _Resp(listing))
    monkeypatch.setattr(flutterwave_service.requests, "put", lambda url, json=None, headers=None, timeout=None:
                        puts.append((url, json)) or _Resp({"status": "success"}))
    res = client.post("/api/admin/payouts/flutterwave/fix-subaccount-splits", headers=superadmin)
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["fixed"] == ["RS_A"] and res.get_json()["skipped"] == 1
    assert puts == [("https://api.flutterwave.com/v3/subaccounts/11", {"split_type": "percentage", "split_value": 0.03})]
    assert AdminAuditLog.query.filter_by(action="FLW_SUBACCOUNT_SPLITS_FIXED").count() == 1


def test_fix_subaccount_splits_needs_superadmin(client, make_user, auth_headers):
    assert client.post("/api/admin/payouts/flutterwave/fix-subaccount-splits",
                       headers=auth_headers(make_user())).status_code == 403


# ── 6. gateway fee charged once ──────────────────────────────────────────────
def test_flutterwave_fee_charged_once(monkeypatch):
    # Default: the Flutterwave dashboard adds its fee; Siiqo adds nothing.
    d = flutterwave_service.calculate_buyer_total(2500)
    assert d["buyer_total"] == 2500 and d["flw_fee"] == 0 and d["fee_added_by"] == "flutterwave"
    # Dashboard switched to merchant-pays: Siiqo adds its estimate instead.
    monkeypatch.setenv("FLW_FEE_ADDED_BY", "siiqo")
    d = flutterwave_service.calculate_buyer_total(2500)
    assert d["buyer_total"] == 2535.5 and d["flw_fee"] == 35.5 and d["fee_added_by"] == "siiqo"


def test_paystack_single_split_gives_subaccount_exactly_the_seller_share(make_vendor, make_user, make_product,
                                                                        make_order, monkeypatch):
    vendor, store = make_vendor()
    store.paystack_subaccount_code = "ACCT_S"
    order = make_order(vendor, make_user(), total=10000, payment_method="PAYSTACK")
    db.session.commit()
    sent = []
    monkeypatch.setattr(paystack_provider.requests, "post", lambda url, json=None, headers=None, timeout=None:
                        sent.append(json) or _Resp({"status": True, "data": {"authorization_url": "u",
                                                                              "access_code": "a"}}))
    res = paystack_provider.PaystackProvider().initiate_transaction([order], "ORD-FEE")
    p = sent[0]
    # Buyer pays listed + Paystack fee once; bearer=account so the main account
    # bears Paystack's fee out of transaction_charge; subaccount gets 9,700.
    assert p["bearer"] == "account" and p["amount"] == int(round(res["amount"] * 100))
    assert p["amount"] - p["transaction_charge"] == 970000
    assert res["split_subaccounts"] == {vendor.id: "ACCT_S"}


# ── 7. order views say what happened ─────────────────────────────────────────
def test_admin_and_seller_views_show_payout_status(client, make_vendor, make_order, superadmin, auth_headers):
    vendor, order, _ = _released_by_transfer(make_vendor, make_order)
    body = client.get(f"/api/admin/orders/{order.id}", headers=superadmin).get_json()
    assert body["order"]["payout"]["label"] == f"Paid by transfer (ref PAYOUT-{order.id}-ABC, status pending)"
    res = client.get("/api/vendor-orders/orders", headers=auth_headers(vendor))
    assert res.status_code == 200
    rows = res.get_json()
    rows = rows.get("orders") or rows.get("data") or rows if isinstance(rows, dict) else rows
    row = next(r for r in rows if r["id"] == order.id)
    assert row["payout_status"] == {"state": "transfer_pending",
                                    "label": f"On its way to your bank (ref PAYOUT-{order.id}-ABC)."}
