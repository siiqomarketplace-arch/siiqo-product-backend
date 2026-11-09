"""
Guest buyers can review from a private link (G1); unpaid orders get one
reminder and a predictable cancellation (G2); sellers are reminded to ship
paid orders (G3).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.communication import Notification
from app.models.community import Review
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.order import OrderItem
from app.models.withdrawal import DayaPayment, PODPayment
from app.routes import escrow as escrow_routes
from app.routes.escrow import generate_order_token, generate_review_token
from app.tasks import escrow_tasks

NOW = datetime.now(timezone.utc)


@pytest.fixture
def emails(monkeypatch):
    sent = []
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda **k: sent.append(k) or True)
    monkeypatch.delenv("PENDING_PAYMENT_CANCEL_HOURS", raising=False)
    return sent


@pytest.fixture
def telegrams(monkeypatch):
    sent = []
    import app.utils.telegram as tg
    monkeypatch.setattr(tg, "send_telegram_message", lambda chat_id, text, **k: sent.append((chat_id, text)) or True)
    return sent


@pytest.fixture
def order_with(make_vendor, make_product, make_order):
    """An order with one item and an escrow row in the given state."""
    def _make(order_status="PENDING", escrow_status=EscrowStatus.PENDING_PAYMENT, buyer=None,
              payment_method="CRYPTO", created_hours_ago=0.0, product_type="physical",
              buyer_name="Ada Obi", buyer_email="ada@example.com", escrow=True, logistics_fee=0,
              stock=5, quantity=2):
        vendor, store = make_vendor()
        product = make_product(store, price=5000, stock=stock, product_type=product_type,
                               images=["https://cdn.example.com/p.jpg"])
        order = make_order(vendor, buyer, total="10000.00", payment_method=payment_method,
                           logistics_fee=logistics_fee)
        order.status = order_status
        order.buyer_name = buyer_name
        order.buyer_email = buyer_email
        db.session.add(OrderItem(order_id=order.id, product_id=product.id, price_at_purchase=5000,
                                 quantity=quantity))
        row = None
        if escrow:
            row = EscrowTransaction(order_id=order.id, status=escrow_status, amount=Decimal("10000.00"),
                                    fee_amount=Decimal("300.00"), currency="NGN",
                                    transaction_number=f"ESC-T-{order.id}",
                                    created_at=NOW - timedelta(hours=created_hours_ago))
            db.session.add(row)
        db.session.commit()
        return order, row, vendor, store, product
    return _make


# ── G1: reviews without an account ───────────────────────────────────────────
def _get_review(client, order_id, token):
    return client.get(f"/api/escrow/review-by-token?order_id={order_id}&token={token}")


def _post_review(client, order_id, token, **body):
    return client.post("/api/escrow/review-by-token",
                       json={"order_id": order_id, "token": token, **body})


def test_review_token_differs_from_order_token():
    assert generate_review_token(5) != generate_order_token(5)


def test_review_page_shows_order_for_valid_link(client, order_with):
    order, *_ , store, product = order_with(order_status="DELIVERED", escrow_status=EscrowStatus.DELIVERED)
    res = _get_review(client, order.id, generate_review_token(order.id))
    assert res.status_code == 200
    body = res.get_json()
    assert body == {
        "order_id": order.id, "store_name": store.store_name,
        "items": [{"name": product.name, "image": "https://cdn.example.com/p.jpg"}],
        "can_review": True, "reason": None, "buyer_first_name": "Ada",
    }


def test_review_page_rejects_bad_or_order_token(client, order_with):
    order, *_ = order_with(order_status="DELIVERED")
    assert _get_review(client, order.id, "nope").status_code == 404
    # The confirm-delivery token is not a review token.
    assert _get_review(client, order.id, generate_order_token(order.id)).status_code == 404
    assert _get_review(client, 99999, generate_review_token(99999)).status_code == 404


def test_review_page_says_not_delivered_yet(client, order_with):
    order, *_ = order_with(order_status="PAID", escrow_status=EscrowStatus.IN_ESCROW)
    body = _get_review(client, order.id, generate_review_token(order.id)).get_json()
    assert body["can_review"] is False and body["reason"] == "NOT_DELIVERED_YET"


def test_guest_can_review_once(client, emails, order_with):
    order, _, vendor, _, product = order_with(order_status="COMPLETED", escrow_status=EscrowStatus.RELEASED)
    token = generate_review_token(order.id)
    res = _post_review(client, order.id, token, vendor_rating=5, product_rating=4, review="Fast and neat.")
    assert res.status_code == 201 and res.get_json()["status"] == "success"
    review = Review.query.one()
    assert review.buyer_id is None and review.reviewer_name == "Ada"
    assert review.product_id == product.id and review.vendor_id == vendor.id
    assert review.vendor_rating == 5 and review.product_rating == 4
    assert Notification.query.filter_by(user_id=vendor.id, type="REVIEW").count() == 1

    again = _post_review(client, order.id, token, vendor_rating=1)
    assert again.status_code == 409 and again.get_json()["code"] == "ALREADY_REVIEWED"
    page = _get_review(client, order.id, token).get_json()
    assert page["can_review"] is False and page["reason"] == "ALREADY_REVIEWED"


@pytest.mark.parametrize("body", [{"vendor_rating": 0}, {"vendor_rating": 6}, {"vendor_rating": "x"},
                                  {}, {"vendor_rating": 4, "product_rating": 9}])
def test_review_rejects_bad_rating(client, order_with, body):
    order, *_ = order_with(order_status="DELIVERED")
    res = _post_review(client, order.id, generate_review_token(order.id), **body)
    assert res.status_code == 400 and res.get_json()["code"] == "BAD_RATING"
    assert Review.query.count() == 0


def test_review_rejects_undelivered_and_bad_token(client, order_with):
    order, *_ = order_with(order_status="PAID", escrow_status=EscrowStatus.IN_ESCROW)
    res = _post_review(client, order.id, generate_review_token(order.id), vendor_rating=5)
    assert res.status_code == 400 and res.get_json()["code"] == "NOT_DELIVERED_YET"
    assert _post_review(client, order.id, "bad", vendor_rating=5).status_code == 404


def test_review_text_limit(client, order_with):
    order, *_ = order_with(order_status="DELIVERED")
    res = _post_review(client, order.id, generate_review_token(order.id), vendor_rating=5, review="a" * 1001)
    assert res.status_code == 400 and Review.query.count() == 0


def test_signed_in_review_blocked_after_guest_link_review(client, auth_headers, make_user, order_with):
    buyer = make_user()
    order, *_ = order_with(order_status="COMPLETED", escrow_status=EscrowStatus.RELEASED, buyer=buyer)
    assert _post_review(client, order.id, generate_review_token(order.id), vendor_rating=4).status_code == 201
    assert Review.query.one().buyer_id == buyer.id
    res = client.post("/api/reviews", headers=auth_headers(buyer), json={"order_id": order.id, "vendor_rating": 5})
    assert res.status_code == 409
    assert Review.query.count() == 1


def test_public_reviews_show_first_name_or_verified_buyer(client, make_user, order_with):
    buyer = make_user()
    buyer.first_name, buyer.last_name = "Chinedu", "Eze"
    o1, *_, vendor_store, _ = order_with(order_status="COMPLETED", buyer=buyer)
    vendor_id = o1.vendor_id
    o2, *_ = order_with(order_status="COMPLETED", buyer_name="")
    o2.vendor_id = vendor_id
    o3, *_ = order_with(order_status="COMPLETED")
    o3.vendor_id = vendor_id
    db.session.add_all([
        Review(order_id=o1.id, buyer_id=buyer.id, vendor_id=vendor_id, vendor_rating=5),
        Review(order_id=o2.id, buyer_id=None, vendor_id=vendor_id, vendor_rating=4),
        Review(order_id=o3.id, buyer_id=None, vendor_id=vendor_id, vendor_rating=3, reviewer_name="Ada"),
    ])
    db.session.commit()
    body = client.get(f"/api/marketplace/reviews/{vendor_id}").get_json()
    names = sorted(r["buyer_name"] for r in body["reviews"])
    assert names == ["Ada", "Chinedu", "Verified buyer"]
    assert all(r["verified_purchase"] is True for r in body["reviews"])
    raw = str(body)
    assert "Eze" not in raw and "@" not in raw


def test_completed_email_has_review_link(emails, monkeypatch, order_with):
    monkeypatch.setattr(escrow_routes, "_paystack_payout_vendor", lambda order, escrow: True)
    order, escrow, *_ = order_with(order_status="DELIVERED", escrow_status=EscrowStatus.DELIVERED,
                                    payment_method="PAYSTACK")
    assert escrow_routes.execute_order_escrow_release(order, escrow, source="test") is True
    mine = [e for e in emails if e["to_email"] == "ada@example.com"]
    assert len(mine) == 1
    assert f"https://siiqo.com/review/{order.id}?token={generate_review_token(order.id)}" in mine[0]["notice_text"]
    assert "order dashboard" not in mine[0]["notice_text"]


def test_delivered_email_has_review_link_reminder_does_not(emails, order_with):
    order, escrow, *_ = order_with(order_status="DELIVERED", escrow_status=EscrowStatus.DELIVERED)
    escrow_tasks.email_buyer_confirm_by(order, NOW + timedelta(hours=72))
    escrow_tasks.email_buyer_confirm_by(order, NOW + timedelta(hours=12), reminder=True)
    assert f"/review/{order.id}?token=" in emails[0]["message"]
    assert "/review/" not in emails[1]["message"]


def test_order_details_review_url_only_with_proof(client, order_with):
    order, *_ = order_with(order_status="COMPLETED", escrow_status=EscrowStatus.RELEASED)
    with_token = client.get(f"/api/escrow/order-details-by-token?order_id={order.id}"
                            f"&token={generate_order_token(order.id)}").get_json()
    assert with_token["review_url"] == escrow_routes.review_url(order.id)
    assert with_token["order"]["review_url"] == escrow_routes.review_url(order.id)
    shared = client.get(f"/api/escrow/order-details-by-token?order_id={order.id}").get_json()
    assert shared["review_url"] is None


# ── G2: unpaid orders ────────────────────────────────────────────────────────
def test_order_details_has_resume_fields(client, emails, order_with):
    order, escrow, *_ = order_with(created_hours_ago=2, logistics_fee=1500)
    body = client.get(f"/api/escrow/order-details-by-token?order_id={order.id}"
                      f"&token={generate_order_token(order.id)}").get_json()
    assert body["payment_method"] == "CRYPTO"
    assert body["escrow_status"] == "PENDING_PAYMENT"
    assert body["total_payable"] == 11500.0
    assert body["is_guest_order"] is True
    cancel_at = datetime.fromisoformat(body["cancel_at"])
    expected = NOW - timedelta(hours=2) + timedelta(hours=24)
    assert abs((cancel_at.replace(tzinfo=timezone.utc) - expected).total_seconds()) < 5
    assert body["order"]["cancel_at"] == body["cancel_at"]
    assert body["review_url"] is None


def test_cancel_hours_setting_is_clamped(monkeypatch):
    for value, hours in [("6", 6), ("0", 1), ("500", 72), ("abc", 24)]:
        monkeypatch.setenv("PENDING_PAYMENT_CANCEL_HOURS", value)
        assert escrow_tasks.pending_payment_cancel_hours() == hours


def test_unpaid_reminder_sent_once_in_window(emails, order_with):
    too_new, *_ = order_with(created_hours_ago=0.5)
    due, *_ = order_with(created_hours_ago=2)
    too_old, *_ = order_with(created_hours_ago=21)
    assert escrow_tasks.send_unpaid_reminders() == 1
    assert escrow_tasks.send_unpaid_reminders() == 0
    assert len(emails) == 1
    mail = emails[0]
    assert mail["subject"] == f"Your order #{due.id} is waiting"
    assert f"https://siiqo.com/order-confirm/{due.id}?token={generate_order_token(due.id)}" in mail["message"]
    assert "24 hours" in mail["message"] and "(Lagos time)" in mail["message"]
    assert "₦10,000.00" in mail["message"] and "Test product x2" in mail["message"]
    db.session.expire_all()
    assert due.payment_reminded_at is not None
    assert too_new.payment_reminded_at is None and too_old.payment_reminded_at is None


def test_unpaid_reminder_in_app_for_signed_in_buyer(emails, make_user, order_with):
    buyer = make_user()
    order, *_ = order_with(created_hours_ago=1, buyer=buyer, buyer_email=buyer.email)
    escrow_tasks.send_unpaid_reminders()
    assert Notification.query.filter_by(user_id=buyer.id, order_id=order.id).count() == 1
    assert [e["to_email"] for e in emails] == [buyer.email]


def test_unpaid_reminder_skips_pod_free_and_guest_placeholders(emails, order_with):
    order_with(created_hours_ago=2, payment_method="POD")
    order_with(created_hours_ago=2, payment_method="FREE")
    order_with(created_hours_ago=2, buyer_email="guest-12@siiqo.com")
    assert escrow_tasks.send_unpaid_reminders() == 0
    assert emails == []


def test_pending_payments_cancelled_after_window_and_buyer_emailed(emails, make_user, order_with):
    buyer = make_user()
    fresh, *_ = order_with(created_hours_ago=23)
    stale, stale_escrow, _, _, product = order_with(created_hours_ago=25, buyer=buyer, buyer_email=buyer.email)
    assert escrow_tasks.check_pending_payments() == 1
    db.session.expire_all()
    assert stale.status == "CANCELLED" and stale_escrow.status == EscrowStatus.CANCELLED
    assert fresh.status == "PENDING"
    assert product.stock_quantity == 7          # 5 + the 2 put back
    note = Notification.query.filter_by(user_id=buyer.id, order_id=stale.id).one()
    assert "within 24 hours" in note.message and "1 hour" not in note.message
    assert len(emails) == 1
    assert "within 24 hours" in emails[0]["message"] and "Nothing was charged" in emails[0]["message"]
    assert f"https://siiqo.com/products/{product.id}" in emails[0]["message"]
    # Runs again: nothing new, no second email.
    assert escrow_tasks.check_pending_payments() == 0
    assert len(emails) == 1


def test_cancel_window_follows_setting(emails, monkeypatch, order_with):
    monkeypatch.setenv("PENDING_PAYMENT_CANCEL_HOURS", "6")
    order, *_ = order_with(created_hours_ago=7)
    assert escrow_tasks.check_pending_payments() == 1
    assert "within 6 hours" in emails[0]["message"]


@pytest.mark.parametrize("status, expires_in_hours, cancelled", [
    ("RECEIVED", -5, False),          # money arrived, being confirmed
    ("REQUIRES_REVIEW", -5, False),
    ("PENDING", 1, False),            # account still open
    ("PENDING", -1, True),            # rate expired: nothing on its way
    ("EXPIRED", -1, True),
    ("FAILED", 1, True),
])
def test_daya_payment_in_flight_is_not_cancelled(emails, order_with, status, expires_in_hours, cancelled):
    order, *_ = order_with(created_hours_ago=30)
    db.session.add(DayaPayment(order_id=order.id, payment_type="ngn_onramp", amount_ngn=10000, status=status,
                               rate_expires_at=NOW + timedelta(hours=expires_in_hours)))
    db.session.commit()
    assert escrow_tasks.check_pending_payments() == (1 if cancelled else 0)


def test_pod_order_never_cancelled(emails, order_with):
    order, *_ = order_with(created_hours_ago=30, payment_method="POD", order_status="PENDING_DELIVERY",
                           escrow=False)
    db.session.add(PODPayment(order_id=order.id, vendor_id=order.vendor_id, amount=10000, currency="NGN"))
    db.session.commit()
    assert escrow_tasks.check_pending_payments() == 0
    db.session.expire_all()
    assert order.status == "PENDING_DELIVERY"


def test_guest_can_start_daya_payment_for_guest_order(client, monkeypatch, order_with):
    """Resume payment: a guest order needs no login (member orders keep their owner check)."""
    from app.routes import payments
    order, *_ = order_with(created_hours_ago=2)
    monkeypatch.setenv("DAYA_API_KEY", "test-not-real")
    monkeypatch.setattr(payments.daya_service, "get_or_create_customer", lambda *a, **k: "cus_1")
    monkeypatch.setattr(payments.daya_service, "get_rate",
                        lambda **k: {"rate_id": "r1", "rate": 1500, "expires_at": "2099-01-01T00:00:00Z"})
    monkeypatch.setattr(payments.daya_service, "create_ngn_funding_account",
                        lambda **k: {"id": "fa_1", "amount": 10000,
                                     "instructions": [{"bank_name": "B", "account_number": "1",
                                                       "account_name": "N"}]})
    res = client.post("/api/payments/daya/initiate", json={"order_id": order.id, "type": "ngn_onramp"})
    assert res.status_code == 200, res.get_json()


# ── G3: remind sellers to ship ──────────────────────────────────────────────
def _paid(order_with, hours_ago, **kw):
    order, escrow, vendor, store, product = order_with(order_status="PAID", escrow_status=EscrowStatus.IN_ESCROW,
                                                       payment_method="DAYA_BANK_TRANSFER", **kw)
    order.paid_at = NOW - timedelta(hours=hours_ago)
    db.session.commit()
    return order, escrow, vendor


def test_ship_reminders_at_24_and_48_hours(emails, telegrams, order_with):
    order, escrow, vendor = _paid(order_with, 25)
    vendor.telegram_id = "12345"
    db.session.commit()
    assert escrow_tasks.send_ship_reminders() == 1
    assert escrow_tasks.send_ship_reminders() == 0            # #2 waits for 48 h
    first = [e for e in emails if e["to_email"] == vendor.email]
    assert len(first) == 1
    assert f"Order #{order.id} was paid 25 hours ago and hasn't been marked shipped." in first[0]["message"]
    assert "cancel it so the buyer is refunded" not in first[0]["message"]
    assert len(telegrams) == 1 and telegrams[0][0] == "12345"
    assert Notification.query.filter_by(user_id=vendor.id, order_id=order.id).count() == 1

    order.paid_at = NOW - timedelta(hours=49)
    db.session.commit()
    assert escrow_tasks.send_ship_reminders() == 1
    second = [e for e in emails if e["to_email"] == vendor.email][-1]
    assert "If you can't fulfil it, cancel it so the buyer is refunded." in second["message"]
    db.session.expire_all()
    assert order.ship_reminders_sent == 2
    order.paid_at = NOW - timedelta(hours=100)
    db.session.commit()
    assert escrow_tasks.send_ship_reminders() == 0            # never more than two


def test_ship_reminder_skips_new_shipped_digital_and_pickup(emails, telegrams, order_with):
    _paid(order_with, 10)                                      # too early
    shipped, _, _ = _paid(order_with, 30)
    shipped.status = "SHIPPED"
    _paid(order_with, 30, product_type="digital")
    pickup, _, _ = _paid(order_with, 30)
    pickup.logistics_provider_id = "self_pickup"
    db.session.commit()
    assert escrow_tasks.send_ship_reminders() == 0
    assert emails == [] and telegrams == []
