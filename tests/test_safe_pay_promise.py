"""
Safe Pay keeps the promise buyers are shown (docs/HCD_AUDIT_2026-11.md §4):
one release window for every seller, counted from when the order was first
marked delivered; guest buyers can report a problem with their private link;
anyone can report a seller; dispute and deadline emails go out.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models.dispute import DisputeCase
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.seller_report import SellerReport
from app.models.trust import VendorTrustProfile
from app.services import trust

NOW = datetime.now(timezone.utc)


@pytest.fixture
def emails(monkeypatch):
    sent = []
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda **k: sent.append(k) or True)
    monkeypatch.delenv("AUTO_RELEASE_HOURS", raising=False)
    return sent


@pytest.fixture
def delivered(make_vendor, make_order):
    """A guest Pay Link order, marked delivered, money held: no buyer account."""
    def _make(delivered_hours_ago=1, buyer_email="ada@example.com"):
        vendor, store = make_vendor()
        order = make_order(vendor, None, total="10000.00", payment_method="PAYSTACK")
        order.buyer_email = buyer_email
        order.status = "DELIVERED"
        db.session.commit()
        order.delivered_at = NOW - timedelta(hours=delivered_hours_ago)
        escrow = EscrowTransaction(order_id=order.id, status=EscrowStatus.DELIVERED, amount=Decimal("10000.00"),
                                   fee_amount=Decimal("300.00"), currency="NGN", paid_at=NOW - timedelta(days=2),
                                   transaction_number=f"PSK-{order.id}-T")
        db.session.add(escrow)
        db.session.commit()
        return SimpleNamespace(vendor=vendor, store=store, order=order, escrow=escrow)
    return _make


# ── One window for every seller ─────────────────────────────────────────────
@pytest.mark.parametrize("tier", ["PLATINUM", "GOLD", "SILVER", "BRONZE", None])
def test_every_seller_gets_the_same_72_hours(emails, make_vendor, tier):
    vendor, _ = make_vendor()
    if tier:
        db.session.add(VendorTrustProfile(vendor_id=vendor.id, trust_tier=tier))
        db.session.commit()
    assert trust.get_vendor_auto_release_hours(vendor.id) == 72


@pytest.mark.parametrize("value, hours", [("48", 48), ("1", 24), ("999", 168), ("abc", 72)])
def test_window_setting_is_clamped(monkeypatch, value, hours):
    monkeypatch.setenv("AUTO_RELEASE_HOURS", value)
    assert trust.auto_release_hours() == hours


def test_deadline_counts_from_first_delivery_not_last_edit(emails, delivered):
    p = delivered(delivered_hours_ago=10)
    p.escrow.updated_at = NOW            # an unrelated later edit
    db.session.commit()
    deadline = trust.release_deadline(p.escrow)
    assert abs((deadline - (p.order.delivered_at.replace(tzinfo=timezone.utc) + timedelta(hours=72))).total_seconds()) < 2


def test_auto_release_waits_for_the_deadline(emails, delivered, monkeypatch):
    from app.tasks import escrow_tasks
    released = []
    monkeypatch.setattr("app.routes.escrow.execute_order_escrow_release",
                        lambda order, escrow, **k: released.append(order.id) or True)
    early = delivered(delivered_hours_ago=71)
    late = delivered(delivered_hours_ago=73)
    escrow_tasks.auto_release_escrow()
    assert released == [late.order.id] and early.order.id not in released


def test_reminder_emails_guest_buyer_once_with_the_deadline(emails, delivered):
    from app.tasks import escrow_tasks
    p = delivered(delivered_hours_ago=60)        # 12h left
    escrow_tasks.send_delivery_reminders()
    escrow_tasks.send_delivery_reminders()
    mine = [e for e in emails if e["to_email"] == "ada@example.com"]
    assert len(mine) == 1
    assert "(Lagos time)" in mine[0]["message"] and "Never share it with the seller" in mine[0]["message"]
    assert f"/order-confirm/{p.order.id}?token=" in mine[0]["cta_url"]


def test_no_email_to_guest_placeholder(emails, delivered):
    from app.tasks import escrow_tasks
    delivered(delivered_hours_ago=60, buyer_email="guest-77@siiqo.com")
    escrow_tasks.send_delivery_reminders()
    assert not [e for e in emails if e["to_email"].startswith("guest-")]


# ── Guest "Report a problem" ────────────────────────────────────────────────
def test_guest_can_report_a_problem_with_private_link(client, emails, delivered):
    from app.routes.escrow import generate_order_token
    p = delivered()
    res = client.post("/api/escrow/disputes/by-token", json={
        "order_id": p.order.id, "token": generate_order_token(p.order.id), "reason": "Not delivered"})
    assert res.status_code == 201, res.get_json()
    case = DisputeCase.query.one()
    assert case.raised_by == "buyer" and case.buyer_id is None
    db.session.expire_all()
    assert db.session.get(EscrowTransaction, p.escrow.id).status == EscrowStatus.DISPUTED
    # Both sides are emailed: the seller can't miss it.
    to = {e["to_email"] for e in emails}
    assert p.vendor.email in to and "ada@example.com" in to


def test_guest_report_needs_proof(client, emails, delivered):
    p = delivered()
    res = client.post("/api/escrow/disputes/by-token", json={"order_id": p.order.id, "token": "wrong",
                                                            "reason": "Not delivered"})
    assert res.status_code == 403 and res.get_json()["code"] == "BUYER_CODE_REQUIRED"
    assert DisputeCase.query.count() == 0


def test_confirm_page_shows_the_deadline(client, emails, delivered):
    from app.routes.escrow import generate_order_token
    p = delivered(delivered_hours_ago=1)
    res = client.get(f"/api/escrow/order-details-by-token?order_id={p.order.id}&token={generate_order_token(p.order.id)}")
    order = res.get_json()["order"]
    assert order["confirm_by"] and order["confirm_by_text"].endswith("(Lagos time)")


# ── Report this seller ──────────────────────────────────────────────────────
def test_anyone_can_report_a_seller(client, emails, make_vendor):
    _, store = make_vendor()
    res = client.post("/api/check/report-seller", json={"store_slug": store.store_slug,
                                                        "reason": "never_delivered", "details": "Paid, nothing came"})
    assert res.status_code == 201 and res.get_json()["reference"].startswith("RPT-")
    assert SellerReport.query.one().vendor_id == store.vendor_id
    assert emails and "report" in emails[0]["subject"].lower()


@pytest.mark.parametrize("body, status", [
    ({"store_slug": "no-such-store", "reason": "other"}, 404),
    ({"reason": "other"}, 404),
])
def test_report_validation(client, emails, body, status):
    assert client.post("/api/check/report-seller", json=body).status_code == status


def test_report_needs_a_reason(client, emails, make_vendor):
    _, store = make_vendor()
    res = client.post("/api/check/report-seller", json={"store_slug": store.store_slug, "reason": "bad"})
    assert res.status_code == 400


# ── The missing email template exists ───────────────────────────────────────
def test_base_notification_template_renders(app):
    from flask import render_template
    with app.test_request_context():
        html = render_template("emails/base_notification.html", first_name="Ada", title="Hello",
                               message="Your order is ready.", cta_text="Open", cta_url="https://siiqo.com",
                               logo_url="https://siiqo.com/images/Siiqo.png")
    assert "Hi Ada" in html and "Your order is ready." in html and "https://siiqo.com" in html
