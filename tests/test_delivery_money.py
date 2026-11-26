"""Delivery money: where the buyer's delivery fee goes.

* the seller's own delivery fee is paid with the order, whole (no 3%), on
  every rail and in the ledger, once;
* orders released before the fix are never paid it retroactively by a retry;
  the admin backfill credits it once (superadmin, audited);
* partner withdrawals go by Daya first, Paystack only after a definite Daya
  refusal, never both;
* Pay on Delivery can't use a delivery partner, and POD jobs never earn;
* refund rule: not delivered -> item + delivery back; delivered -> item only,
  delivery to the partner / seller;
* weekly automatic partner payouts: off by default, on pays once a week;
* the admin Delivery money report.
Every gateway call is mocked."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.audit import AdminAuditLog
from app.models.communication import Notification
from app.models.dispute import DisputeCase
from app.models.escrow import EscrowStatus, EscrowTransaction, LogisticsAssignment
from app.models.finance import Ledger
from app.models.order import Order
from app.models.partnerships import PartnerApplication
from app.models.user import UserRole
from app.models.withdrawal import (PartnerBankAccount, PartnerPayoutRun, PartnerWithdrawal,
                                   VendorBankAccount)
from app.routes.escrow import _vendor_net_amount, execute_order_escrow_release
from app.routes.withdrawal import _get_ledger_balance
from app.services import daya_service, delivery_money, flutterwave_service, partner_payouts
from app.services.logistics_flow import partner_earnings

NOW = datetime.now(timezone.utc)
PASSWORD = "not-a-real-password"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: True)
    monkeypatch.setattr("app.routes.cart.send_siiqo_email", lambda *a, **k: True, raising=False)
    monkeypatch.setattr("app.routes.admin.send_siiqo_email", lambda *a, **k: True, raising=False)
    monkeypatch.delenv("PARTNER_AUTO_PAYOUT_ENABLED", raising=False)
    monkeypatch.delenv("DAYA_API_KEY", raising=False)
    monkeypatch.setattr(daya_service, "get_rate", lambda **k: {"rate": 1500})
    monkeypatch.setattr(daya_service, "get_merchant_balance",
                        lambda: {"data": {"collection_balance_usd": 0, "withdrawal_balance_usd": 1000}})
    monkeypatch.setattr(daya_service, "transfer_collection_to_withdrawal", lambda **k: {})


def _admin(role="SUPERADMIN"):
    admin = AdminUser(name="Ops", email=f"{role.lower()}-{NOW.timestamp()}@siiqo.test", role=role)
    admin.set_password(PASSWORD)
    db.session.add(admin)
    db.session.commit()
    return admin, {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _bank(vendor, recipient="RCP_vendor"):
    db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="GTBank", bank_code="058",
                                     account_number="0123456789", account_name="ADA OKAFOR",
                                     recipient_code=recipient, is_verified=True, is_default=True))
    db.session.commit()


@pytest.fixture
def own_delivery(make_vendor, make_order):
    """A delivered order: items 10,000 (Siiqo 3% = 300) + the seller's own delivery 1,500."""
    def _make(payment_method="CRYPTO", txn=None, fee="1500", provider="vendor_self",
              escrow_status=EscrowStatus.DELIVERED, order_status="DELIVERED"):
        vendor, store = make_vendor()
        order = make_order(vendor, total=10000, logistics_fee=Decimal(fee), payment_method=payment_method,
                           logistics_provider_id=provider)
        order.status = order_status
        escrow = EscrowTransaction(order_id=order.id, status=escrow_status, amount=Decimal("10000"),
                                   fee_amount=Decimal("300"), currency="NGN", paid_at=NOW - timedelta(days=4),
                                   transaction_number=txn or f"DAYA-{order.id}", split_settled=False)
        db.session.add(escrow)
        order.paid_at = NOW - timedelta(days=4)
        db.session.commit()
        return SimpleNamespace(vendor=vendor, store=store, order=order, escrow=escrow)
    return _make


def _credits(vendor_id):
    return {r.reference_id: r.amount for r in
            Ledger.query.filter_by(vendor_id=vendor_id, transaction_type="CREDIT").all()}


# ── 1. Seller's own delivery fee is paid with the order ────────────────────

def test_breakdown_has_no_fee_on_delivery(own_delivery):
    p = own_delivery()
    b = delivery_money.payout_breakdown(p.order, p.escrow)
    assert (b["item"], b["siiqo_fee"], b["delivery"], b["total"]) == ("10000.00", "300.00", "1500.00", "11200.00")
    assert b["text"] == "Item ₦10,000.00 − Siiqo 3% ₦300.00 + your delivery ₦1,500.00 = ₦11,200.00"
    assert _vendor_net_amount(p.order, p.escrow) == 11200.0


def test_daya_release_pays_item_net_plus_delivery_once(own_delivery, monkeypatch):
    p = own_delivery()
    _bank(p.vendor)
    sent = []
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor", lambda **k: sent.append(k) or {"success": True})
    assert execute_order_escrow_release(p.order, p.escrow, source="test") is True
    assert [round(s["amount_ngn"], 2) for s in sent] == [11200.0]
    c = _credits(p.vendor.id)
    assert c[p.escrow.transaction_number] == Decimal("9700.00")
    assert c[f"delivery_fee:{p.order.id}"] == Decimal("1500.00")
    assert _get_ledger_balance(p.vendor.id) == 0            # PAYOUT debit 11,200 matches
    note = Notification.query.filter_by(user_id=p.vendor.id, title="Payment Released").one()
    assert "+ your delivery ₦1,500.00 = ₦11,200.00" in note.message
    # Idempotent: a second release pays and credits nothing more.
    assert execute_order_escrow_release(p.order, db.session.get(EscrowTransaction, p.escrow.id)) is True
    assert len(sent) == 1
    assert Ledger.query.filter_by(vendor_id=p.vendor.id, transaction_type="CREDIT").count() == 2
    assert _get_ledger_balance(p.vendor.id) == 0


def test_paystack_release_sends_delivery_too(own_delivery, monkeypatch):
    p = own_delivery(payment_method="PAYSTACK", txn=None)
    p.escrow.transaction_number = f"PSK-{p.order.id}"
    db.session.commit()
    _bank(p.vendor)
    sent = []
    import app.services.escrow.paystack_provider as psp
    monkeypatch.setattr(psp, "paystack_transfer_to_vendor",
                        lambda **k: sent.append(k) or {"success": True, "transfer_code": "TRF_1"})
    assert execute_order_escrow_release(p.order, p.escrow) is True
    assert [s["amount_ngn"] for s in sent] == [11200.0]
    assert _get_ledger_balance(p.vendor.id) == 0


def test_flutterwave_release_sends_delivery_too(own_delivery, monkeypatch):
    p = own_delivery(payment_method="FLUTTERWAVE")
    p.escrow.transaction_number = f"FLW-{p.order.id}"
    db.session.commit()
    _bank(p.vendor)
    sent = []
    monkeypatch.setattr(flutterwave_service, "transfer_to_vendor", lambda **k: sent.append(k) or {"success": True})
    assert execute_order_escrow_release(p.order, p.escrow) is True
    assert [s["amount_ngn"] for s in sent] == [11200.0]
    assert _get_ledger_balance(p.vendor.id) == 0


def test_failed_transfer_leaves_item_and_delivery_withdrawable(own_delivery, monkeypatch):
    p = own_delivery()
    _bank(p.vendor)
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor", lambda **k: {"success": False})
    execute_order_escrow_release(p.order, p.escrow)
    assert _get_ledger_balance(p.vendor.id) == Decimal("11200.00")


def test_split_settled_paystack_order_is_not_paid_twice(own_delivery, monkeypatch):
    """A Paystack single-seller split pays the subaccount items + delivery − 3%
    at checkout: no transfer, and the PAYOUT debit matches the two credits."""
    p = own_delivery(payment_method="PAYSTACK")
    p.escrow.transaction_number = f"PSK-{p.order.id}"
    p.escrow.split_settled = True
    p.escrow.split_proof = "paystack verify: 1120000 kobo to ACCT_x"  # proven at confirmation
    db.session.commit()
    import app.services.escrow.paystack_provider as psp
    monkeypatch.setattr(psp, "paystack_transfer_to_vendor", lambda **k: pytest.fail("no transfer on a split"))
    execute_order_escrow_release(p.order, p.escrow)
    assert _get_ledger_balance(p.vendor.id) == 0


def test_multi_split_share_includes_own_delivery_only(own_delivery):
    from app.services.payment_refs import seller_net_by_vendor
    own = own_delivery()
    partner = own_delivery(provider="siiqo_partner_9", fee="2200")
    nets = seller_net_by_vendor([own.order, partner.order])
    assert nets[own.vendor.id] == Decimal("11200.00")
    assert nets[partner.vendor.id] == Decimal("9700.00")


def test_partner_delivery_and_pod_are_not_paid_to_the_seller(own_delivery):
    partner = own_delivery(provider="siiqo_partner_9", fee="2200")
    assert _vendor_net_amount(partner.order, partner.escrow) == 9700.0
    pod = own_delivery(payment_method="POD")
    assert delivery_money.seller_delivery_fee(pod.order) == 0


def test_old_release_not_paid_retroactively_then_admin_credit_once(client, own_delivery, monkeypatch):
    p = own_delivery(escrow_status=EscrowStatus.RELEASED, order_status="COMPLETED")
    db.session.add(Ledger(vendor_id=p.vendor.id, transaction_type="CREDIT", amount=9700,
                          reference_id=p.escrow.transaction_number, description="old release"))
    db.session.commit()
    # A retried payout for this old order sends the item share only.
    assert _vendor_net_amount(p.order, p.escrow) == 9700.0

    _, plain = _admin("ADMIN")
    sa, h = _admin()
    rows = client.get("/api/admin/delivery-money/seller-fees-unpaid", headers=plain).get_json()
    assert [r["order_id"] for r in rows["orders"]] == [p.order.id] and rows["total_amount"] == "1500.00"
    url = f"/api/admin/delivery-money/seller-fees-unpaid/{p.order.id}/credit"
    assert client.post(url, headers=plain, json={}).status_code == 403
    res = client.post(url, headers=h, json={"note": "backfill"})
    assert res.status_code == 200, res.get_json()
    assert _credits(p.vendor.id)[f"delivery_fee:{p.order.id}"] == Decimal("1500.00")
    assert AdminAuditLog.query.filter_by(action="SELLER_DELIVERY_FEE_CREDITED").count() == 1
    again = client.post(url, headers=h, json={})
    assert again.status_code == 200 and "Already credited" in again.get_json()["message"]
    assert Ledger.query.filter_by(reference_id=f"delivery_fee:{p.order.id}").count() == 1
    assert client.get("/api/admin/delivery-money/seller-fees-unpaid", headers=plain).get_json()["count"] == 0
    assert _get_ledger_balance(p.vendor.id) == Decimal("11200.00")
    # Once credited, a payout retry includes it (credit and payout match).
    assert _vendor_net_amount(p.order, db.session.get(EscrowTransaction, p.escrow.id)) == 11200.0


def test_vendor_orders_show_breakdown(client, own_delivery, auth_headers):
    p = own_delivery()
    rows = client.get("/api/vendor/orders", headers=auth_headers(p.vendor)).get_json()
    row = next(r for r in rows if r["id"] == p.order.id)
    assert row["delivery_by"] == "seller"
    assert row["payout_breakdown"]["text"].endswith("+ your delivery ₦1,500.00 = ₦11,200.00")


# ── 2. Partner withdrawals: Daya first, Paystack only on a definite refusal ──

@pytest.fixture
def earned_partner(make_user, make_vendor, make_order):
    def _make(fee=2000):
        partner = make_user(role=UserRole.PARTNER)
        partner.is_verified = True
        db.session.add(PartnerApplication(user_id=partner.id, business_name="Fast Riders", service_type="LOGISTICS",
                                          status="APPROVED", pricing_settings={"coverage": []}))
        vendor, _ = make_vendor()
        order = make_order(vendor, total=5000, logistics_fee=Decimal(fee) * Decimal("1.1"),
                           payment_method="CRYPTO", logistics_provider_id=f"siiqo_partner_{partner.id}")
        order.paid_at = NOW - timedelta(days=2)
        db.session.add(EscrowTransaction(order_id=order.id, status=EscrowStatus.RELEASED, amount=5000,
                                         fee_amount=150, currency="NGN", transaction_number=f"DAYA-{order.id}"))
        db.session.add(LogisticsAssignment(order_id=order.id, partner_id=partner.id, status="DELIVERED",
                                           delivery_fee=fee))
        db.session.add(PartnerBankAccount(partner_id=partner.id, bank_name="GTBank", bank_code="058",
                                          account_number="0123456789", account_name="FAST RIDERS LTD",
                                          recipient_code="RCP_partner", is_verified=True, is_default=True))
        db.session.commit()
        return partner, order
    return _make


def test_partner_withdrawal_goes_by_daya_and_never_paystack(client, earned_partner, auth_headers, monkeypatch):
    partner, _ = earned_partner()
    monkeypatch.setenv("DAYA_API_KEY", "test-not-real")
    daya, psk = [], []
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor",
                        lambda **k: daya.append(k) or {"success": True, "transfer_id": "DT_1", "outcome": "sent"})
    import app.services.escrow.paystack_provider as psp
    monkeypatch.setattr(psp, "paystack_transfer_to_vendor", lambda **k: psk.append(k) or {"success": True})
    res = client.post("/api/withdrawal/partner/withdrawals", headers=auth_headers(partner), json={})
    assert res.status_code == 200, res.get_json()
    w = PartnerWithdrawal.query.one()
    assert (w.status, w.channel, w.transfer_code, w.source) == ("PENDING", "daya", "DT_1", "manual")
    assert daya[0]["reference"] == w.transfer_reference and daya[0]["amount_ngn"] == 2000.0
    assert daya[0]["account_name"] == "FAST RIDERS LTD" and psk == []
    assert partner_earnings(partner.id)["available"] == 0


def test_daya_refusal_falls_back_to_paystack_once(client, earned_partner, auth_headers, monkeypatch):
    partner, _ = earned_partner()
    monkeypatch.setenv("DAYA_API_KEY", "test-not-real")
    psk = []
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor",
                        lambda **k: {"success": False, "outcome": "refused", "error_message": "Daya error (400)"})
    import app.services.escrow.paystack_provider as psp
    monkeypatch.setattr(psp, "paystack_transfer_to_vendor",
                        lambda **k: psk.append(k) or {"success": True, "transfer_code": "TRF_9"})
    res = client.post("/api/withdrawal/partner/withdrawals", headers=auth_headers(partner), json={})
    assert res.status_code == 200, res.get_json()
    w = PartnerWithdrawal.query.one()
    assert (w.status, w.channel) == ("PENDING", "paystack") and len(psk) == 1
    assert psk[0]["reference"] == w.transfer_reference


def test_daya_unknown_outcome_stays_reserved_and_skips_paystack(client, earned_partner, auth_headers, monkeypatch):
    partner, _ = earned_partner()
    monkeypatch.setenv("DAYA_API_KEY", "test-not-real")
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor",
                        lambda **k: {"success": False, "outcome": "unknown", "error_message": "timed out"})
    import app.services.escrow.paystack_provider as psp
    monkeypatch.setattr(psp, "paystack_transfer_to_vendor", lambda **k: pytest.fail("never both rails"))
    h = auth_headers(partner)
    res = client.post("/api/withdrawal/partner/withdrawals", headers=h, json={})
    assert res.status_code == 202
    assert PartnerWithdrawal.query.one().status == "PROCESSING"
    again = client.post("/api/withdrawal/partner/withdrawals", headers=h, json={})
    assert again.status_code == 400 and PartnerWithdrawal.query.count() == 1


def test_paystack_used_when_daya_not_configured(client, earned_partner, auth_headers, monkeypatch):
    partner, _ = earned_partner()
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor", lambda **k: pytest.fail("Daya not configured"))
    import app.services.escrow.paystack_provider as psp
    monkeypatch.setattr(psp, "paystack_transfer_to_vendor",
                        lambda **k: {"success": True, "transfer_code": "TRF_2"})
    res = client.post("/api/withdrawal/partner/withdrawals", headers=auth_headers(partner), json={})
    assert res.status_code == 200 and PartnerWithdrawal.query.one().channel == "paystack"


def test_partner_unconfirmed_name_holds_withdrawal(client, earned_partner, auth_headers, monkeypatch):
    partner, _ = earned_partner()
    acc = PartnerBankAccount.query.filter_by(partner_id=partner.id).one()
    acc.account_name = "GTBank"           # a bank's name, never a holder's
    db.session.commit()
    monkeypatch.setenv("DAYA_API_KEY", "test-not-real")
    monkeypatch.setattr("app.services.payout_account_name.resolve_holder_name",
                        lambda *a, **k: {"status": "unavailable", "account_name": None, "source": None})
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor", lambda **k: pytest.fail("no money to a bad name"))
    res = client.post("/api/withdrawal/partner/withdrawals", headers=auth_headers(partner), json={})
    assert res.status_code == 400 and res.get_json()["code"] == "ACCOUNT_NAME_NOT_CONFIRMED"
    assert PartnerWithdrawal.query.count() == 0


@pytest.mark.parametrize("status, outcome", [(400, "refused"), (422, "refused"), (409, "unknown"),
                                             (429, "unknown"), (500, "unknown"), (None, "unknown")])
def test_daya_transfer_outcome_classification(monkeypatch, status, outcome):
    import requests as _rq
    monkeypatch.setenv("DAYA_API_KEY", "test-not-real")

    def fake(method, url, **kwargs):
        if status is None:
            raise _rq.exceptions.Timeout()
        return SimpleNamespace(ok=False, status_code=status, text="no", json=lambda: {"message": "no"})
    monkeypatch.setattr(daya_service.requests, "request", fake)
    res = daya_service.transfer_ngn_to_vendor(amount_ngn=100, bank_code="058", account_number="0123456789",
                                              reference="PWD-1", order_id=0)
    assert res["success"] is False and res["outcome"] == outcome


# ── 3. Pay on Delivery can't use a delivery partner ────────────────────────

def test_pod_with_partner_refused_and_quote_hides_partners(client, earned_partner, make_vendor, make_product,
                                                           monkeypatch):
    monkeypatch.setattr("app.services.entitlements.has_feature", lambda vid, f: True)
    partner, _ = earned_partner()
    app_row = PartnerApplication.query.filter_by(user_id=partner.id).one()
    app_row.pricing_settings = {"coverage": [{"state": "Lagos", "lgas": [], "price": 2000}]}
    vendor, store = make_vendor()
    store.city, store.state = "Ikeja", "Lagos"
    product = make_product(store, price=5000)
    db.session.commit()
    body = {"items": [{"product_id": product.id, "quantity": 1}], "payment_method": "POD",
            "email": "guest@example.com", "delivery_name": "Ada", "delivery_phone": "08031234567",
            "delivery_address": "1 Road", "delivery_city": "Yaba", "delivery_state": "Lagos",
            "logistics_selections": [{"vendorId": vendor.id,
                                      "selectedOption": {"id": f"siiqo_partner_{partner.id}", "fee": 1}}]}
    res = client.post("/api/cart/checkout", json=body)
    assert res.status_code == 400
    assert res.get_json() == {"code": "POD_PARTNER_NOT_ALLOWED",
                              "message": "Delivery partners need Safe Pay. Choose Safe Pay or the seller's own delivery."}
    q = f"/api/logistics/quote?vendor_id={vendor.id}&state=Lagos&city=Yaba"
    assert any(o["id"].startswith("siiqo_partner_") for o in client.get(q).get_json()["options"])
    pod = client.get(q + "&payment_method=POD").get_json()
    assert not any(o["id"].startswith("siiqo_partner_") for o in pod["options"]) and pod["pod_note"]


# ── 4. Refund rule ─────────────────────────────────────────────────────────

def _partner_job(earned_partner, status):
    partner, order = earned_partner()
    escrow = order.escrow
    escrow.status = EscrowStatus.IN_ESCROW if status != "DELIVERED" else EscrowStatus.DELIVERED
    order.status = "PAID" if status != "DELIVERED" else "DELIVERED"
    LogisticsAssignment.query.filter_by(order_id=order.id).one().status = status
    db.session.commit()
    return partner, order, escrow


def test_refund_before_delivery_returns_item_and_delivery(client, earned_partner):
    partner, order, escrow = _partner_job(earned_partner, "ASSIGNED")
    _, h = _admin()
    res = client.post(f"/api/admin/escrow/refund/{order.id}", headers=h)
    assert res.status_code == 200, res.get_json()
    e = db.session.get(EscrowTransaction, escrow.id)
    assert (e.buyer_refund_amount, e.delivery_refund_ngn, e.delivery_fee_to) == (Decimal("7200.00"),
                                                                                 Decimal("2200.00"), "buyer")
    assert LogisticsAssignment.query.filter_by(order_id=order.id).one().status == "CANCELLED"
    assert partner_earnings(partner.id)["earned"] == 0


def test_refund_after_partner_delivered_pays_partner(client, earned_partner):
    partner, order, escrow = _partner_job(earned_partner, "DELIVERED")
    assert partner_earnings(partner.id)["earned"] == 0      # not final yet
    _, h = _admin()
    res = client.post(f"/api/admin/escrow/refund/{order.id}", headers=h)
    assert res.status_code == 200 and res.get_json()["buyer_refund_total"] == "5000.00"
    e = db.session.get(EscrowTransaction, escrow.id)
    assert (e.buyer_refund_amount, e.delivery_refund_ngn, e.delivery_fee_to) == (Decimal("5000.00"),
                                                                                 Decimal("0.00"), "partner")
    # Changed deliberately: a job delivered before the refund is earned.
    money = partner_earnings(partner.id)
    assert money["earned"] == Decimal("2000") and money["available"] == Decimal("2000")


def test_refund_after_own_delivery_credits_seller_delivery_once(client, own_delivery):
    p = own_delivery()
    _, h = _admin()
    assert client.post(f"/api/admin/escrow/refund/{p.order.id}", headers=h).status_code == 200
    e = db.session.get(EscrowTransaction, p.escrow.id)
    assert e.buyer_refund_amount == Decimal("10000.00") and e.delivery_fee_to == "seller"
    assert _credits(p.vendor.id) == {f"delivery_fee:{p.order.id}": Decimal("1500.00")}


def test_dispute_refund_not_delivered_includes_delivery(client, own_delivery, auth_headers, make_user):
    from app.services import disputes as dsvc
    p = own_delivery(escrow_status=EscrowStatus.IN_ESCROW, order_status="PAID")
    buyer = make_user()
    p.order.buyer_id = buyer.id
    db.session.commit()
    r = client.post("/api/escrow/disputes", headers=auth_headers(buyer),
                    json={"order_id": p.order.id, "reason": "Never arrived", "details": "Nothing came."})
    assert r.status_code in (200, 201), r.get_json()
    case = db.session.get(DisputeCase, r.get_json()["dispute"]["id"])
    dsvc.move_to_review(case, "admin")
    sa, h = _admin()
    res = client.post(f"/api/admin/disputes/{case.id}/decide", headers=h,
                      json={"outcome": "refund_full", "seller_fault": True})
    assert res.status_code == 200, res.get_json()
    case = db.session.get(DisputeCase, case.id)
    assert case.refund_amount == Decimal("11500.00")
    assert "delivery fee" in case.refund_instructions
    assert db.session.get(EscrowTransaction, p.escrow.id).delivery_fee_to == "buyer"
    assert Ledger.query.filter_by(reference_id=f"delivery_fee:{p.order.id}").count() == 0
    view = client.get(f"/api/admin/disputes/{case.id}", headers=h).get_json()
    assert "refund rule" in str(view).lower()


# ── 5. Weekly automatic partner payouts ────────────────────────────────────

def test_weekly_payout_off_by_default(earned_partner, monkeypatch):
    earned_partner()
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor", lambda **k: pytest.fail("switched off"))
    import app.services.escrow.paystack_provider as psp
    monkeypatch.setattr(psp, "paystack_transfer_to_vendor", lambda **k: pytest.fail("switched off"))
    assert partner_payouts.run_weekly_partner_payouts() == {"enabled": False, "ran": False}
    assert PartnerWithdrawal.query.count() == 0 and PartnerPayoutRun.query.count() == 0


def test_weekly_payout_on_pays_once_per_week(client, earned_partner, monkeypatch):
    partner, _ = earned_partner()
    monkeypatch.setenv("PARTNER_AUTO_PAYOUT_ENABLED", "true")
    monkeypatch.setenv("DAYA_API_KEY", "test-not-real")
    sent = []
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor",
                        lambda **k: sent.append(k) or {"success": True, "transfer_id": "DT_7"})
    friday = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)
    out = partner_payouts.run_weekly_partner_payouts(now=friday)
    assert out["ran"] and out["paid"] == 1 and Decimal(out["paid_total"]) == 2000
    w = PartnerWithdrawal.query.one()
    assert (w.source, w.run_key, w.channel, w.status) == ("auto", "2026-W48", "daya", "PENDING")
    assert Notification.query.filter_by(user_id=partner.id, title="Weekly payout sent").count() == 1
    again = partner_payouts.run_weekly_partner_payouts(now=friday + timedelta(hours=1))
    assert again["ran"] is False and again["reason"] == "already_ran" and len(sent) == 1
    run = PartnerPayoutRun.query.one()
    assert run.status == "DONE" and run.paid_count == 1 and run.results[0]["status"] == "sent"
    _, h = _admin("ADMIN")
    report = client.get("/api/admin/delivery-money", headers=h).get_json()
    assert report["auto_payouts"]["enabled"] is True and report["auto_payouts"]["runs"][0]["run_key"] == "2026-W48"


def test_weekly_payout_skips_below_minimum(earned_partner, monkeypatch):
    earned_partner(fee=100)
    monkeypatch.setenv("PARTNER_AUTO_PAYOUT_ENABLED", "1")
    out = partner_payouts.run_weekly_partner_payouts(now=datetime(2026, 11, 27, 8, tzinfo=timezone.utc))
    assert out["paid"] == 0 and out["skipped"] == 1 and PartnerWithdrawal.query.count() == 0


# ── 6. Delivery money report ───────────────────────────────────────────────

def test_delivery_money_report_numbers(client, earned_partner, own_delivery, make_vendor, make_order):
    partner, order = earned_partner()                       # buyer paid 2,200; partner earns 2,000
    held = own_delivery(escrow_status=EscrowStatus.IN_ESCROW, order_status="PAID")   # seller's 1,500 held
    old = own_delivery(escrow_status=EscrowStatus.RELEASED, order_status="COMPLETED")
    old.order.logistics_fee = Decimal("800")                # released before the fix, not credited
    vendor, _ = make_vendor()
    pod = make_order(vendor, total=3000, logistics_fee=1000, payment_method="POD",
                     logistics_provider_id=f"siiqo_partner_{partner.id}")
    pod.paid_at = NOW
    db.session.add(LogisticsAssignment(order_id=pod.id, partner_id=partner.id, status="DELIVERED",
                                       delivery_fee=900))
    db.session.add(PartnerWithdrawal(partner_id=partner.id, amount=500, fee_amount=0, net_amount=500,
                                     status="PENDING", processed_at=NOW,
                                     bank_account_id=PartnerBankAccount.query.first().id))
    db.session.commit()
    _, h = _admin("ADMIN")
    r = client.get("/api/admin/delivery-money", headers=h).get_json()
    t = r["totals"]
    assert t["collected_from_buyers"] == "4500.00"          # 2,200 + 1,500 + 800 (POD excluded)
    assert t["collected_for_partners"] == "2200.00" and t["collected_for_sellers"] == "2300.00"
    assert t["siiqo_delivery_margin"] == "200.00"
    assert t["owed_to_sellers_held"] == "1500.00" and t["owed_to_sellers_unpaid_released"] == "800.00"
    assert t["paid_to_partners"] == "500.00" and t["owed_to_partners"] == "1500.00"
    row = next(x for x in r["partners"] if x["partner_id"] == partner.id)
    assert Decimal(row["earned"]) == 2000 and Decimal(row["available"]) == 1500 and row["pod_jobs_legacy"] == 1
    assert r["pod_partner_jobs_legacy"]["count"] == 1 and r["unpaid_seller_delivery_count"] == 1
    assert "3%" in r["notes"]["no_fee_on_delivery"]
    # Admins only.
    assert client.get("/api/admin/delivery-money", headers={}).status_code == 401


def test_checkout_charges_3_percent_on_items_only(client, make_vendor, make_product):
    vendor, store = make_vendor()
    store.city, store.state = "Ikeja", "Lagos"
    store.delivery_settings = {"self_delivery": {"enabled": True, "fee_same_city": 1500, "fee_same_state": 2000},
                               "pickup": {"enabled": True, "area": "Ikeja"}}
    product = make_product(store, price=5000)
    db.session.commit()
    body = {"items": [{"product_id": product.id, "quantity": 1}], "payment_method": "CRYPTO",
            "email": "guest@example.com", "delivery_name": "Ada", "delivery_phone": "08031234567",
            "delivery_address": "1 Road", "delivery_city": "Ikeja", "delivery_state": "Lagos",
            "logistics_selections": [{"vendorId": vendor.id, "selectedOption": {"id": "vendor_self", "fee": 1}}]}
    res = client.post("/api/cart/checkout", json=body)
    assert res.status_code == 200, res.get_json()
    order = Order.query.one()
    escrow = EscrowTransaction.query.filter_by(order_id=order.id).one()
    assert order.logistics_fee == Decimal("1500.00") and escrow.amount == Decimal("5000.00")
    assert escrow.fee_amount == Decimal("150.00")            # 3% of the item total, nothing on delivery
    assert delivery_money.payout_breakdown(order, escrow)["total"] == "6350.00"
    detail = client.get(f"/api/escrow/order-details-by-token?order_id={order.id}").get_json()
    assert detail.get("delivery_fee") == 1500.0 and detail.get("delivery_by") == "seller"
