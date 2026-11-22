"""Money and fraud fixes:
1. A seller's status edit never counts as payment (paid_at, agent rewards).
2. Daya (CRYPTO) orders sync their escrow on SHIPPED/DELIVERED, so Safe Pay
   auto-release works for them.
3. Daya confirms only a settled deposit that covers the server-set amount.
4. A card-paid event ticket is paid out on the card rail, never through Daya.
5. Automatic payout retries write the matching ledger DEBIT.
6. Paid files are only shown once the order is paid (or free).
7. Releases lock the escrow row; card transfer references are deterministic.
8. Indexes on hot order / payment / ledger lookups.
"""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import inspect, text

from app.extensions import db
from app.models.admin import AdminUser
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.finance import Ledger
from app.models.order import Order, OrderItem
from app.models.withdrawal import DayaPayment, VendorBankAccount
from app.routes import escrow as escrow_routes
from app.routes import payments as payments_routes


@pytest.fixture(autouse=True)
def _no_email(monkeypatch):
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: True)


def _escrow(order, status=EscrowStatus.IN_ESCROW, amount=10000, txn=None):
    e = EscrowTransaction(order_id=order.id, transaction_number=txn or f"ESC-T{order.id}",
                          status=status, amount=amount, fee_amount=300)
    db.session.add(e)
    db.session.commit()
    return e


def _reload(order):
    db.session.expire_all()
    return db.session.get(Order, order.id)


def _admin_headers(email="root@example.com"):
    admin = AdminUser(name="Root", email=email, role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _bank(vendor, recipient_code=None):
    db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Test Bank", bank_code="058",
                                     account_number="0123456789", account_name="Test Vendor",
                                     is_default=True, is_verified=True, recipient_code=recipient_code))
    db.session.commit()


def _set_status(client, auth_headers, vendor, order, status):
    return client.patch(f"/api/vendor/orders/{order.id}/status", headers=auth_headers(vendor),
                        json={"status": status})


# ── 1. seller-set status is never payment ─────────────────────────────────

@pytest.mark.parametrize("method", ["CRYPTO", "DAYA_BANK_TRANSFER", "ESCROW", "FLUTTERWAVE"])
def test_seller_cannot_mark_escrow_or_daya_order_paid_or_completed(client, auth_headers, make_vendor,
                                                                   make_order, method):
    vendor, _ = make_vendor()
    order = make_order(vendor, payment_method=method)
    _escrow(order, status=EscrowStatus.PENDING_PAYMENT)
    for status in ("PAID", "COMPLETED"):
        res = _set_status(client, auth_headers, vendor, order, status)
        assert res.status_code == 400, (status, res.get_json())
    order = _reload(order)
    assert order.status == "PENDING" and order.paid_at is None


def test_seller_cannot_mark_daya_order_paid_even_without_escrow(client, auth_headers, make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, payment_method="CRYPTO")
    assert _set_status(client, auth_headers, vendor, order, "PAID").status_code == 400
    assert _reload(order).paid_at is None


def test_seller_cannot_ship_an_unpaid_escrow_order(client, auth_headers, make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, payment_method="CRYPTO")
    escrow = _escrow(order, status=EscrowStatus.PENDING_PAYMENT)
    for status in ("SHIPPED", "DELIVERED"):
        res = _set_status(client, auth_headers, vendor, order, status)
        assert res.status_code == 409 and res.get_json()["code"] == "PAYMENT_NOT_CONFIRMED"
    order = _reload(order)
    assert order.paid_at is None and db.session.get(EscrowTransaction, escrow.id).status == EscrowStatus.PENDING_PAYMENT


def test_pod_status_edit_is_not_payment_but_pod_confirmation_is(client, auth_headers, make_vendor, make_order):
    from app.services.marketing import paid_orders_query
    vendor, _ = make_vendor()
    edited = make_order(vendor, payment_method="POD")
    assert _set_status(client, auth_headers, vendor, edited, "DELIVERED").status_code == 200
    assert _set_status(client, auth_headers, vendor, edited, "COMPLETED").status_code == 200
    edited = _reload(edited)
    assert edited.status == "COMPLETED" and edited.paid_at is None
    assert edited.id not in [o.id for o in paid_orders_query(vendor.id).all()]

    confirmed = make_order(vendor, payment_method="POD")
    res = client.post(f"/api/withdrawal/pod-payments/{confirmed.id}/confirm", headers=auth_headers(vendor), json={})
    assert res.status_code == 200
    confirmed = _reload(confirmed)
    assert confirmed.paid_at is not None
    assert confirmed.id in [o.id for o in paid_orders_query(vendor.id).all()]


def test_shipping_status_alone_never_sets_paid_at(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, payment_method="POD")
    order.status = "SHIPPED"
    db.session.commit()
    order.status = "DELIVERED"
    db.session.commit()
    order = _reload(order)
    assert order.paid_at is None and order.delivered_at is not None


def test_seller_edit_flag_is_cleared_after_the_request(client, auth_headers, make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, payment_method="POD")
    assert _set_status(client, auth_headers, vendor, order, "PROCESSING").status_code == 200
    assert not getattr(db.session.get(Order, order.id), "_seller_status_edit", False)


# ── 2. Daya orders sync the escrow, so auto-release fires ─────────────────

def test_daya_cart_order_ship_and_deliver_sync_escrow_and_auto_release(client, auth_headers, make_vendor,
                                                                        make_order, monkeypatch):
    from app.services.trust import release_deadline
    from app.tasks.escrow_tasks import auto_release_escrow
    vendor, _ = make_vendor()
    order = make_order(vendor, total=10000, payment_method="CRYPTO")
    order.status = "PAID"
    db.session.commit()
    escrow = _escrow(order, status=EscrowStatus.IN_ESCROW, txn=f"DYA-T{order.id}")

    assert _set_status(client, auth_headers, vendor, order, "SHIPPED").status_code == 200
    assert db.session.get(EscrowTransaction, escrow.id).status == EscrowStatus.SHIPPED
    assert _set_status(client, auth_headers, vendor, order, "DELIVERED").status_code == 200
    db.session.expire_all()
    escrow = db.session.get(EscrowTransaction, escrow.id)
    order = db.session.get(Order, order.id)
    assert escrow.status == EscrowStatus.DELIVERED
    delivered_at = order.delivered_at
    assert delivered_at is not None and release_deadline(escrow) is not None

    # A repeat DELIVERED edit doesn't restart the buyer's clock.
    assert _set_status(client, auth_headers, vendor, order, "DELIVERED").status_code == 200
    assert _reload(order).delivered_at == delivered_at

    # The seller still can't complete it themselves.
    assert _set_status(client, auth_headers, vendor, order, "COMPLETED").status_code == 400

    payouts = []
    monkeypatch.setattr(payments_routes, "_payout_vendor_via_daya", lambda o, e: payouts.append(o.id) or True)
    order = _reload(order)
    order.delivered_at = datetime.now(timezone.utc) - timedelta(hours=73)
    db.session.commit()
    assert auto_release_escrow() == 1
    db.session.expire_all()
    assert db.session.get(EscrowTransaction, escrow.id).status == EscrowStatus.RELEASED
    assert payouts == [order.id]


def test_logistics_delivered_escrow_starts_the_clock_once(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, payment_method="CRYPTO")
    escrow = _escrow(order, status=EscrowStatus.IN_ESCROW)
    escrow.status = EscrowStatus.DELIVERED
    db.session.commit()
    assert _reload(order).delivered_at is not None


# ── 3. Daya confirms only a settled, fully paid deposit ───────────────────

def _daya_order(make_vendor, make_order, payment_type="ngn_onramp", amount_ngn=10150, amount_crypto=None):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=10000, payment_method="CRYPTO")
    escrow = _escrow(order, status=EscrowStatus.PENDING_PAYMENT)
    dp = DayaPayment(order_id=order.id, payment_type=payment_type, daya_funding_account_id=f"fa_{order.id}",
                     amount_ngn=amount_ngn, amount_crypto=amount_crypto,
                     asset="USDT" if payment_type == "crypto_direct" else None, status="PENDING")
    db.session.add(dp)
    db.session.commit()
    return order, escrow, dp


def _deposit(dp, status="COMPLETED", amount="10150.00", currency="NGN", **extra):
    d = {"id": f"dep_{dp.id}", "funding_account_id": dp.daya_funding_account_id, "status": status}
    if amount is not None:
        d["amount"] = amount
    if currency is not None:
        d["currency"] = currency
    d.update(extra)
    return d


def test_processing_or_received_deposit_does_not_confirm(make_vendor, make_order):
    order, escrow, dp = _daya_order(make_vendor, make_order)
    assert payments_routes.apply_daya_deposit(dp, _deposit(dp, status="PROCESSING"), "deposit.processing") == "not_settled"
    assert payments_routes.apply_daya_deposit(dp, _deposit(dp, status="RECEIVED"), "deposit.received") == "not_settled"
    assert payments_routes.apply_daya_deposit(dp, _deposit(dp, status="PROCESSING")) == "not_settled"
    order = _reload(order)
    assert order.status == "PENDING" and order.paid_at is None
    assert db.session.get(EscrowTransaction, escrow.id).status == EscrowStatus.PENDING_PAYMENT
    assert db.session.get(DayaPayment, dp.id).status == "RECEIVED"


def test_completed_deposit_with_full_amount_confirms(make_vendor, make_order):
    order, escrow, dp = _daya_order(make_vendor, make_order)
    assert payments_routes.apply_daya_deposit(dp, _deposit(dp), "deposit.completed") == "confirmed"
    order = _reload(order)
    assert order.status == "PAID" and order.paid_at is not None
    assert db.session.get(EscrowTransaction, escrow.id).status == EscrowStatus.IN_ESCROW
    assert db.session.get(DayaPayment, dp.id).status == "COMPLETED"


def test_underpaid_deposit_is_flagged_not_confirmed(make_vendor, make_order):
    from app.models.telemetry import PlatformEvent
    order, escrow, dp = _daya_order(make_vendor, make_order)
    assert payments_routes.apply_daya_deposit(dp, _deposit(dp, amount="5000.00"), "deposit.completed") == "underpaid"
    order = _reload(order)
    assert order.status == "PENDING" and order.paid_at is None
    assert db.session.get(EscrowTransaction, escrow.id).status == EscrowStatus.PENDING_PAYMENT
    assert db.session.get(DayaPayment, dp.id).status == "UNDERPAID"
    flag = PlatformEvent.query.filter_by(event_name="daya_deposit_flagged", order_id=order.id).one()
    assert flag.properties["received_amount"] == "5000.00" and flag.properties["expected_amount"] == "10150.00"
    # A repeat of the same webhook doesn't record a second flag.
    payments_routes.apply_daya_deposit(db.session.get(DayaPayment, dp.id), _deposit(dp, amount="5000.00"),
                                       "deposit.completed")
    assert PlatformEvent.query.filter_by(event_name="daya_deposit_flagged", order_id=order.id).count() == 1


@pytest.mark.parametrize("amount,currency,expected", [
    (None, "NGN", "unverified"),     # no amount in the payload: fail closed
    ("10150.00", None, "unverified"),
    ("10150.00", "USDT", "currency_mismatch"),
])
def test_unverifiable_deposit_fails_closed(make_vendor, make_order, amount, currency, expected):
    order, escrow, dp = _daya_order(make_vendor, make_order)
    assert payments_routes.apply_daya_deposit(dp, _deposit(dp, amount=amount, currency=currency),
                                              "deposit.completed") == expected
    assert _reload(order).paid_at is None
    assert db.session.get(EscrowTransaction, escrow.id).status == EscrowStatus.PENDING_PAYMENT


def test_crypto_deposit_checked_in_the_asset(make_vendor, make_order):
    order, escrow, dp = _daya_order(make_vendor, make_order, payment_type="crypto_direct", amount_crypto="6.5")
    assert payments_routes.apply_daya_deposit(dp, _deposit(dp, amount="6.00", currency="USDT")) == "underpaid"
    order2, _, dp2 = _daya_order(make_vendor, make_order, payment_type="crypto_direct", amount_crypto="6.5")
    assert payments_routes.apply_daya_deposit(dp2, _deposit(dp2, amount="6.50", currency="USDT")) == "confirmed"
    assert _reload(order2).paid_at is not None


def _signed_webhook(client, monkeypatch, event, deposit):
    monkeypatch.setenv("DAYA_WEBHOOK_SECRET", "test-secret")
    body = json.dumps({"event": event, "data": deposit}).encode()
    sig = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    return client.post("/api/payments/daya/webhook", data=body, content_type="application/json",
                       headers={"X-Daya-Signature": sig})


def test_webhook_processing_does_not_confirm_but_completed_does(client, make_vendor, make_order, monkeypatch):
    order, escrow, dp = _daya_order(make_vendor, make_order)
    assert _signed_webhook(client, monkeypatch, "deposit.processing",
                           _deposit(dp, status="PROCESSING")).status_code == 200
    assert _reload(order).paid_at is None
    assert _signed_webhook(client, monkeypatch, "deposit.completed", _deposit(dp)).status_code == 200
    order = _reload(order)
    assert order.status == "PAID" and order.paid_at is not None


def test_webhook_underpaid_completed_is_not_confirmed(client, make_vendor, make_order, monkeypatch):
    order, escrow, dp = _daya_order(make_vendor, make_order)
    _signed_webhook(client, monkeypatch, "deposit.completed", _deposit(dp, amount="100.00"))
    assert _reload(order).paid_at is None
    assert db.session.get(DayaPayment, dp.id).status == "UNDERPAID"


# ── 4. card-paid event ticket is paid out on the card rail ───────────────

def _event_order(make_vendor, make_order):
    from app.models.event import Event, TicketPurchase, TicketType
    vendor, store = make_vendor()
    order = make_order(vendor, total=10000, payment_method="CRYPTO")  # as events.py creates it
    event = Event(storefront_id=store.id, vendor_id=vendor.id, title="Gig", slug=f"gig-{order.id}",
                  description="A gig", start_date=datetime(2027, 1, 1), end_date=datetime(2027, 1, 2),
                  event_type="concert")
    db.session.add(event)
    db.session.flush()
    tt = TicketType(event_id=event.id, name="Regular", price=10000)
    db.session.add(tt)
    db.session.flush()
    db.session.add(TicketPurchase(event_id=event.id, ticket_type_id=tt.id, order_id=order.id,
                                  ticket_code=f"TKT-{order.id}", buyer_name="B", buyer_email="b@example.com",
                                  price_paid=10000, status="PENDING"))
    db.session.commit()
    escrow = _escrow(order, status=EscrowStatus.PENDING_PAYMENT)
    return vendor, order, escrow


def test_paystack_paid_event_ticket_never_triggers_daya_payout(make_vendor, make_order, monkeypatch):
    vendor, order, escrow = _event_order(make_vendor, make_order)
    daya, paystack = [], []
    monkeypatch.setattr(payments_routes, "_payout_vendor_via_daya", lambda o, e: daya.append(o.id) or True)
    monkeypatch.setattr(escrow_routes, "_paystack_payout_vendor", lambda o, e: paystack.append(o.id) or True)
    assert escrow_routes._activate_paystack_escrow(escrow, "PSK-REF-1") is True
    db.session.commit()
    # A repeat confirmation (webhook + poll) changes nothing.
    assert escrow_routes._activate_paystack_escrow(escrow, "PSK-REF-1") is False
    order = _reload(order)
    assert daya == [] and paystack == [order.id]
    assert order.payment_method == "PAYSTACK" and order.status == "COMPLETED"
    debits = Ledger.query.filter_by(vendor_id=vendor.id, transaction_type="DEBIT").all()
    assert [d.reference_id for d in debits] == [f"PAYOUT-{escrow.transaction_number}"]


def test_record_card_rail_keeps_escrow_paylinks():
    o = Order(payment_method="ESCROW")
    escrow_routes.record_card_rail(o, "PAYSTACK")
    assert o.payment_method == "ESCROW"


# ── 5. payout retries write the matching DEBIT ───────────────────────────

def _released_daya_order(make_vendor, make_order, status=EscrowStatus.RELEASED):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=10000, payment_method="CRYPTO")
    order.status = "COMPLETED"
    db.session.commit()
    escrow = _escrow(order, status=status, txn=f"DYA-R{order.id}")
    escrow_routes._credit_vendor_ledger(vendor.id, 9700.0, escrow.transaction_number, "credit")
    db.session.commit()
    _bank(vendor)
    return vendor, order, escrow


def _balance(vendor_id):
    from app.routes.withdrawal import _get_ledger_balance
    return _get_ledger_balance(vendor_id)


def test_admin_retry_all_writes_debit_and_never_pays_twice(client, make_vendor, make_order, monkeypatch):
    vendor, order, escrow = _released_daya_order(make_vendor, make_order)
    calls = []
    monkeypatch.setattr(payments_routes, "_payout_vendor_via_daya", lambda o, e: calls.append(o.id) or True)
    h = _admin_headers()
    res = client.post("/api/admin/payout/retry-all-failed", headers=h, json={"dry_run": False})
    assert res.status_code == 200
    assert _balance(vendor.id) == Decimal("0")
    res = client.post("/api/admin/payout/retry-all-failed", headers=h, json={"dry_run": False})
    entry = next(r for r in res.get_json()["results"] if r["order_id"] == order.id)
    assert entry["status"] == "SKIPPED" and calls == [order.id]


def test_admin_retry_all_skips_unreleased_escrow(client, make_vendor, make_order, monkeypatch):
    vendor, order, escrow = _released_daya_order(make_vendor, make_order, status=EscrowStatus.IN_ESCROW)
    calls = []
    monkeypatch.setattr(payments_routes, "_payout_vendor_via_daya", lambda o, e: calls.append(o.id) or True)
    res = client.post("/api/admin/payout/retry-all-failed", headers=_admin_headers(), json={"dry_run": False})
    entry = next(r for r in res.get_json()["results"] if r["order_id"] == order.id)
    assert entry["status"] == "SKIPPED" and calls == []


def test_admin_retry_all_failed_payout_keeps_balance(client, make_vendor, make_order, monkeypatch):
    vendor, order, escrow = _released_daya_order(make_vendor, make_order)
    monkeypatch.setattr(payments_routes, "_payout_vendor_via_daya", lambda o, e: False)
    res = client.post("/api/admin/payout/retry-all-failed", headers=_admin_headers(), json={"dry_run": False})
    entry = next(r for r in res.get_json()["results"] if r["order_id"] == order.id)
    assert entry["status"] == "FAILED" and _balance(vendor.id) == Decimal("9700")


def test_admin_retry_single_order_writes_debit_then_refuses_repeat(client, make_vendor, make_order, monkeypatch):
    from app.services import daya_service
    vendor, order, escrow = _released_daya_order(make_vendor, make_order)
    monkeypatch.setattr(daya_service, "get_merchant_balance",
                        lambda: {"data": {"collection_balance_usd": 0, "withdrawal_balance_usd": 1000}})
    sent = []
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor", lambda **k: sent.append(k) or {"success": True})
    h = _admin_headers()
    assert client.post(f"/api/admin/payout/retry-order/{order.id}", headers=h, json={}).status_code == 200
    assert _balance(vendor.id) == Decimal("0")
    res = client.post(f"/api/admin/payout/retry-order/{order.id}", headers=h, json={})
    assert res.status_code == 409 and res.get_json()["code"] == "ALREADY_PAID_OUT"
    assert len(sent) == 1


# ── 6. paid files only after payment ─────────────────────────────────────

def test_cart_shows_file_only_for_free_products(client, auth_headers, make_user, make_vendor, make_product):
    buyer = make_user()
    vendor, store = make_vendor()
    paid = make_product(store, price=5000, product_type="digital", file_url="https://files.example/paid.pdf")
    free = make_product(store, price=0, product_type="digital", file_url="https://files.example/free.pdf",
                        is_free=True)
    for p in (paid, free):
        assert client.post("/api/cart/add", headers=auth_headers(buyer),
                           json={"product_id": p.id, "quantity": 1}).status_code in (200, 201)
    items = {i["product_id"]: i for i in client.get("/api/cart", headers=auth_headers(buyer)).get_json()["items"]}
    assert items[paid.id]["file_url"] is None
    assert items[free.id]["file_url"] == "https://files.example/free.pdf"


def _digital_buyer_order(make_user, make_vendor, make_product, make_order, escrow_status):
    buyer = make_user()
    vendor, store = make_vendor()
    product = make_product(store, price=5000, product_type="digital", file_url="https://files.example/paid.pdf")
    order = make_order(vendor, buyer=buyer, total=5000, payment_method="CRYPTO")
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, price_at_purchase=5000, quantity=1))
    db.session.commit()
    _escrow(order, status=escrow_status, amount=5000)
    return buyer, vendor, order


def test_buyer_order_hides_file_until_paid(client, auth_headers, make_user, make_vendor, make_product, make_order):
    buyer, vendor, order = _digital_buyer_order(make_user, make_vendor, make_product, make_order,
                                                EscrowStatus.PENDING_PAYMENT)
    res = client.get(f"/api/buyer-orders/{order.id}", headers=auth_headers(buyer))
    assert res.status_code == 200
    assert res.get_json()["order"]["items"][0]["file_url"] is None

    # A status that isn't payment (e.g. set by hand) still doesn't reveal it.
    order = _reload(order)
    order.status = "DELIVERED"
    db.session.commit()
    res = client.get(f"/api/buyer-orders/{order.id}", headers=auth_headers(buyer))
    assert res.get_json()["order"]["items"][0]["file_url"] is None
    assert client.get(f"/api/marketplace/orders/{order.id}/downloads").status_code == 400


def test_buyer_order_shows_file_once_paid(client, auth_headers, make_user, make_vendor, make_product, make_order):
    buyer, vendor, order = _digital_buyer_order(make_user, make_vendor, make_product, make_order,
                                                EscrowStatus.RELEASED)
    order = _reload(order)
    order.status = "COMPLETED"
    db.session.commit()
    res = client.get(f"/api/buyer-orders/{order.id}", headers=auth_headers(buyer))
    assert res.get_json()["order"]["items"][0]["file_url"] == "https://files.example/paid.pdf"


# ── 7. release locking and deterministic transfer references ─────────────

def _delivered_paystack_order(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=10000, payment_method="ESCROW")
    order.status = "DELIVERED"
    db.session.commit()
    escrow = _escrow(order, status=EscrowStatus.DELIVERED)
    return vendor, order, escrow


def test_release_twice_pays_once(make_vendor, make_order, monkeypatch):
    vendor, order, escrow = _delivered_paystack_order(make_vendor, make_order)
    calls = []
    monkeypatch.setattr(escrow_routes, "_paystack_payout_vendor", lambda o, e: calls.append(o.id) or True)
    assert escrow_routes.execute_order_escrow_release(order, escrow, source="test") is True
    assert escrow_routes.execute_order_escrow_release(order, escrow, source="test") is True
    assert calls == [order.id]
    assert Ledger.query.filter_by(vendor_id=vendor.id, transaction_type="DEBIT").count() == 1
    assert Ledger.query.filter_by(vendor_id=vendor.id, transaction_type="CREDIT").count() == 1


def test_release_rechecks_status_after_locking(make_vendor, make_order, monkeypatch):
    """A caller holding a stale escrow object (another release committed in the
    meantime) must see the committed RELEASED status and pay nothing."""
    vendor, order, escrow = _delivered_paystack_order(make_vendor, make_order)
    calls = []
    monkeypatch.setattr(escrow_routes, "_paystack_payout_vendor", lambda o, e: calls.append(o.id) or True)
    db.session.execute(text("UPDATE escrow_transactions SET status = 'RELEASED' WHERE id = :i"), {"i": escrow.id})
    assert escrow.status == EscrowStatus.DELIVERED  # stale in memory
    escrow_routes.execute_order_escrow_release(order, escrow, source="auto_release")
    assert calls == []
    assert Ledger.query.filter_by(vendor_id=vendor.id).count() == 0


def test_paystack_transfer_reference_is_deterministic(make_vendor, make_order, monkeypatch):
    from app.services.escrow import paystack_provider
    vendor, order, escrow = _delivered_paystack_order(make_vendor, make_order)
    _bank(vendor, recipient_code="RCP_test")
    refs = []
    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor",
                        lambda **k: refs.append(k["reference"]) or {"success": True})
    escrow_routes._paystack_payout_vendor(order, escrow)
    escrow_routes._paystack_payout_vendor(order, escrow)
    assert len(refs) == 2 and refs[0] == refs[1] and refs[0].startswith(f"PAYOUT-{order.id}-")


def test_flutterwave_transfer_reference_is_deterministic(make_vendor, make_order, monkeypatch):
    from app.services import flutterwave_service
    vendor, order, escrow = _delivered_paystack_order(make_vendor, make_order)
    _bank(vendor)
    refs = []
    monkeypatch.setattr(flutterwave_service, "transfer_to_vendor",
                        lambda **k: refs.append(k["reference"]) or {"success": True})
    escrow_routes._flutterwave_payout_vendor(order, escrow)
    escrow_routes._flutterwave_payout_vendor(order, escrow)
    assert len(refs) == 2 and refs[0] == refs[1] and refs[0].startswith(f"WD-FLW-{order.id}-")


def test_transfer_reference_differs_per_order_and_recipient(make_vendor, make_order):
    vendor, _ = make_vendor()
    a, b = make_order(vendor), make_order(vendor)
    ref = escrow_routes.payout_transfer_reference
    assert ref("PAYOUT", a, "RCP_1") != ref("PAYOUT", b, "RCP_1")
    assert ref("PAYOUT", a, "RCP_1") != ref("PAYOUT", a, "RCP_2")


# ── 8. indexes ────────────────────────────────────────────────────────────

def test_hot_lookup_indexes_exist():
    insp = inspect(db.engine)

    def names(table):
        return {i["name"] for i in insp.get_indexes(table)}

    assert {"ix_orders_vendor_id", "ix_orders_buyer_id", "ix_orders_status"} <= names("orders")
    assert {"ix_ledgers_vendor_id", "ix_ledgers_vendor_id_reference_id"} <= names("ledgers")
    assert "ix_order_items_order_id" in names("order_items")
    assert "ix_escrow_transactions_status" in names("escrow_transactions")
    assert "ix_daya_payments_daya_funding_account_id" in names("daya_payments")
    assert "ix_ticket_purchases_order_id" in names("ticket_purchases")


def test_boot_list_creates_the_same_indexes():
    import app as app_pkg
    src = open(app_pkg.__file__, encoding="utf-8").read()
    for name in ("ix_orders_vendor_id", "ix_orders_buyer_id", "ix_orders_status", "ix_ledgers_vendor_id",
                 "ix_ledgers_vendor_id_reference_id", "ix_order_items_order_id",
                 "ix_escrow_transactions_status", "ix_daya_payments_daya_funding_account_id",
                 "ix_ticket_purchases_order_id"):
        assert f"CREATE INDEX IF NOT EXISTS {name} " in src, name
