"""Phase 4: mandatory verification flag (D11, VERIFICATION_REQUIRED_FOR).

Each mode, the grace period, payouts held then released after verification
(never retroactive, nothing lost), the `none` default unchanged, and the
superadmin preview count."""
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.communication import Notification
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.finance import Ledger
from app.models.user import Storefront
from app.models.verification import VerificationStatus
from app.models.verification_requirement import VerificationHold
from app.models.withdrawal import VendorBankAccount
from app.routes import escrow as escrow_routes
from app.routes import payments as payments_routes
from app.routes.withdrawal import _get_ledger_balance
from app.services import verification_requirement as vr
from app.services.escrow import paystack_provider


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import app.utils.email as email
    import app.routes.vendor as vendor_routes
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: True)
    monkeypatch.setattr(vendor_routes, "send_siiqo_email", lambda **k: None)
    for key in ("VERIFICATION_REQUIRED_FOR", "VERIFICATION_REQUIRED_GRACE_DAYS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VERIFICATION_V2_ENABLED", "true")


@pytest.fixture
def transfers(monkeypatch):
    calls = []
    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor",
                        lambda **k: calls.append(k) or {"success": True})
    monkeypatch.setattr(paystack_provider, "create_paystack_subaccount",
                        lambda *a, **k: {"success": False})
    return calls


def _mode(monkeypatch, value, grace=None):
    monkeypatch.setenv("VERIFICATION_REQUIRED_FOR", value)
    if grace is not None:
        monkeypatch.setenv("VERIFICATION_REQUIRED_GRACE_DAYS", str(grace))


def _activate(days_ago=0):
    return vr.record_activation(now=datetime.now(timezone.utc) - timedelta(days=days_ago))


def _verify(user_id):
    now = datetime.now(timezone.utc)
    db.session.add(VerificationStatus(user_id=user_id, identity_verified_at=now, valid_until=now + timedelta(days=365),
                                      subscription_active=True, source_provider="youverify"))
    db.session.commit()


def _existing_seller(make_vendor, days_old=60):
    vendor, store = make_vendor()
    store.created_at = datetime.now(timezone.utc) - timedelta(days=days_old)
    store.is_published = True
    db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Test Bank", bank_code="058",
                                     account_number="0123456789", account_name="Test Vendor", is_default=True,
                                     recipient_code=f"RCP_{secrets.token_hex(4)}"))
    db.session.commit()
    return vendor, store


def _paid_order(vendor, make_order, amount=100_000, fee=3_000, paid_days_ago=0):
    order = make_order(vendor, total=amount, payment_method="PAYSTACK")
    order.status = "PAID"
    order.paid_at = datetime.now(timezone.utc) - timedelta(days=paid_days_ago)
    escrow = EscrowTransaction(order_id=order.id, transaction_number=f"ESC-{order.id}-{secrets.token_hex(2)}",
                               status=EscrowStatus.IN_ESCROW, amount=amount, fee_amount=fee, currency="NGN")
    db.session.add(escrow)
    db.session.commit()
    return order, escrow


def _release(order, escrow):
    assert escrow_routes.execute_order_escrow_release(order, escrow, source="test") is True


def _superadmin_headers():
    admin = AdminUser(name="Ops", email=f"ops-{secrets.token_hex(3)}@example.com", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


ONBOARDING_ANSWERS = {
    "business_name": "Ada Fabrics", "what_you_sell": "Ankara dresses and tops made to measure.",
    "who_buys": "Women who want outfits for weddings and church", "city": "Enugu", "state": "Enugu",
    "delivery": "Pickup in Enugu or delivery by courier", "price_range": "15k - 40k",
    "contact_phone": "08031234567", "product_type": "physical",
}
ONBOARDING_BANK = {"bank_code": "058", "account_number": "0123456789", "account_name": "ADA OKAFOR",
                   "bank_name": "GTBank"}


def _verified_user(make_user):
    u = make_user()
    u.is_verified = True
    db.session.commit()
    return u


# ── parsing ────────────────────────────────────────────────────────────────

def test_parse_mode():
    assert vr.parse_mode(None) == ("none", None) and vr.parse_mode("") == ("none", None)
    assert vr.parse_mode("NONE") == ("none", None)
    assert vr.parse_mode("new_stores") == ("new_stores", None)
    assert vr.parse_mode(" all_sellers ") == ("all_sellers", None)
    assert vr.parse_mode("payouts_over:50000") == ("payouts_over", Decimal("50000"))
    assert vr.parse_mode("payouts_over:₦250,000") == ("payouts_over", Decimal("250000"))
    # Anything not understood is today's behaviour.
    for bad in ("payouts_over:", "payouts_over:-5", "everyone", "payouts_over:abc"):
        assert vr.parse_mode(bad) == ("none", None)
    assert vr.step_title("payouts_over", Decimal("50000")) == "Verify to receive payouts above ₦50,000"
    assert vr.step_title("all_sellers", None) == "Verify to keep selling"


# ── none (default): identical to before ────────────────────────────────────

def test_none_changes_nothing(client, make_vendor, make_order, make_user, auth_headers, transfers):
    vendor, store = _existing_seller(make_vendor, days_old=0)
    order, escrow = _paid_order(vendor, make_order)
    _release(order, escrow)
    db.session.commit()
    assert [c["amount_ngn"] for c in transfers] == [97_000.0]
    assert _get_ledger_balance(vendor.id) == Decimal("0")
    assert VerificationHold.query.count() == 0
    assert not [r for r in Ledger.query.all() if (r.reference_id or "").startswith("VERIFY-")]

    # New stores publish at once; the publish toggle works.
    seller = _verified_user(make_user)
    res = client.post("/api/vendor/onboard", headers=auth_headers(seller), json={"business_name": "Plain Store"})
    assert res.status_code == 201 and res.get_json()["status"] == "success"
    assert "verification_required" not in res.get_json()
    sf = Storefront.query.filter_by(vendor_id=seller.id).one()
    assert sf.is_published is True
    client.patch("/api/vendor/update-settings", headers=auth_headers(seller), json={"is_published": False})
    assert client.patch("/api/vendor/update-settings", headers=auth_headers(seller),
                        json={"is_published": True}).status_code == 200
    db.session.expire_all()
    assert db.session.get(Storefront, sf.id).is_published is True

    status = client.get("/api/vendor/verification-requirement", headers=auth_headers(vendor)).get_json()
    assert status["mode"] == "none" and status["required"] is False and status["step"] is None
    assert vr.run_verification_requirement_sweep() == {"sellers": 0, "released": 0}
    assert vr.activation_at() is None


def test_none_guard_does_not_touch_the_database(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no query expected")
    monkeypatch.setattr(vr, "payout_needs_verification", boom)
    assert vr.hold_payout(object(), object(), 1_000_000) is False


# ── payouts_over ───────────────────────────────────────────────────────────

def test_payouts_over_holds_large_payouts_until_verified(client, make_vendor, make_order, auth_headers,
                                                         transfers, monkeypatch):
    _mode(monkeypatch, "payouts_over:50000", grace=14)
    _activate(days_ago=30)                                # grace already over
    vendor, store = _existing_seller(make_vendor, days_old=90)
    small, small_escrow = _paid_order(vendor, make_order, amount=30_000, fee=900)
    _release(small, small_escrow)
    assert [c["amount_ngn"] for c in transfers] == [29_100.0]

    big, big_escrow = _paid_order(vendor, make_order, amount=100_000, fee=3_000)
    _release(big, big_escrow)
    assert len(transfers) == 1                             # nothing sent
    hold = VerificationHold.query.one()
    assert (hold.kind, hold.order_id, hold.status, float(hold.amount_ngn)) == ("payout", big.id, "held", 97_000.0)
    assert _get_ledger_balance(vendor.id) == Decimal("0")  # held money can't be withdrawn twice
    assert Notification.query.filter_by(user_id=vendor.id, title="Verify to receive payouts above ₦50,000").count() == 1

    status = client.get("/api/vendor/verification-requirement", headers=auth_headers(vendor)).get_json()
    assert status["required"] is True and status["step"]["title"] == "Verify to receive payouts above ₦50,000"
    assert status["held_payouts"] == {"count": 1, "amount_ngn": 97_000.0} and status["payout_limit_ngn"] == 50_000.0

    # Calling the payout again while held does not hold twice.
    assert escrow_routes._paystack_payout_vendor(big, big_escrow) is False
    assert VerificationHold.query.count() == 1 and len(transfers) == 1

    # Verify → released and sent; nothing lost.
    assert vr.release_for(vendor.id) == {"payouts": 0, "dispatched": 0, "stores": 0}   # not verified yet
    _verify(vendor.id)
    assert vr.run_verification_requirement_sweep()["released"] == 1
    assert [c["amount_ngn"] for c in transfers] == [29_100.0, 97_000.0]
    hold = VerificationHold.query.one()
    assert hold.status == "released" and hold.release_result == "dispatched"
    assert _get_ledger_balance(vendor.id) == Decimal("0")
    refs = sorted(r.reference_id for r in Ledger.query.filter_by(vendor_id=vendor.id).all())
    assert any(r.startswith("VERIFY-HOLD-") for r in refs) and any(r.startswith("VERIFY-RELEASE-") for r in refs)
    assert client.get("/api/vendor/verification-requirement",
                      headers=auth_headers(vendor)).get_json()["required"] is False


def test_release_that_fails_to_send_leaves_money_withdrawable(make_vendor, make_order, transfers, monkeypatch):
    _mode(monkeypatch, "payouts_over:1000", grace=0)
    _activate(days_ago=1)
    vendor, _ = _existing_seller(make_vendor)
    order, escrow = _paid_order(vendor, make_order)
    _release(order, escrow)
    assert VerificationHold.query.one().status == "held"
    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor", lambda **k: {"success": False})
    _verify(vendor.id)
    assert vr.release_for(vendor.id) == {"payouts": 1, "dispatched": 0, "stores": 0}
    assert VerificationHold.query.one().release_result == "balance"
    assert _get_ledger_balance(vendor.id) == Decimal("97000")


def test_every_payout_function_respects_the_hold(make_vendor, make_order, monkeypatch):
    _mode(monkeypatch, "all_sellers", grace=0)
    _activate(days_ago=1)

    def gateway(**k):
        raise AssertionError("no money may move while held")
    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor", gateway)
    from app.services import daya_service, flutterwave_service
    monkeypatch.setattr(flutterwave_service, "transfer_to_vendor", gateway)
    monkeypatch.setattr(daya_service, "transfer_ngn_to_vendor", gateway)
    monkeypatch.setattr(daya_service, "get_merchant_balance", gateway)
    vendor, _ = _existing_seller(make_vendor)
    for fn in (escrow_routes._paystack_payout_vendor, escrow_routes._flutterwave_payout_vendor,
               payments_routes._payout_vendor_via_daya):
        order, escrow = _paid_order(vendor, make_order)
        assert fn(order, escrow) is False
    assert VerificationHold.query.filter_by(status="held").count() == 3


# ── grace and never retroactive ────────────────────────────────────────────

def test_all_sellers_grace_for_existing_sellers(client, make_vendor, make_order, auth_headers, transfers, monkeypatch):
    _mode(monkeypatch, "all_sellers", grace=14)
    vendor, store = _existing_seller(make_vendor)

    # Before the sweep has started the clock: not affected.
    order, escrow = _paid_order(vendor, make_order)
    _release(order, escrow)
    assert len(transfers) == 1 and VerificationHold.query.count() == 0

    _activate(days_ago=0)
    status = client.get("/api/vendor/verification-requirement", headers=auth_headers(vendor)).get_json()
    assert status["required"] is True and status["step"]["title"] == "Verify to keep selling"
    assert status["grace_ends_at"] is not None
    order, escrow = _paid_order(vendor, make_order)
    _release(order, escrow)
    assert len(transfers) == 2 and VerificationHold.query.count() == 0     # still in grace

    # Grace over: a sale paid before the end still pays out (never retroactive) ...
    early, early_escrow = _paid_order(vendor, make_order, paid_days_ago=20)
    _activate_shift = vr._setting()
    import json
    _activate_shift.value = json.dumps({"mode": "all_sellers",
                                        "at": (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()})
    db.session.commit()
    _release(early, early_escrow)
    assert len(transfers) == 3 and VerificationHold.query.count() == 0
    # ... a sale paid after it waits.
    late, late_escrow = _paid_order(vendor, make_order)
    _release(late, late_escrow)
    assert len(transfers) == 3 and VerificationHold.query.filter_by(order_id=late.id, status="held").count() == 1
    status = client.get("/api/vendor/verification-requirement", headers=auth_headers(vendor)).get_json()
    assert status["grace_ends_at"] is None and status["held_payouts"]["count"] == 1

    # An existing live store stays live; turning it off and on again needs verification.
    assert db.session.get(Storefront, store.id).is_published is True
    client.patch("/api/vendor/update-settings", headers=auth_headers(vendor), json={"is_published": False})
    res = client.patch("/api/vendor/update-settings", headers=auth_headers(vendor), json={"is_published": True})
    assert res.status_code == 403 and res.get_json()["code"] == "VERIFICATION_REQUIRED"

    _verify(vendor.id)
    assert vr.release_for(vendor.id)["dispatched"] == 1 and len(transfers) == 4
    assert client.patch("/api/vendor/update-settings", headers=auth_headers(vendor),
                        json={"is_published": True}).status_code == 200


def test_grace_resets_when_the_mode_changes(monkeypatch):
    _mode(monkeypatch, "all_sellers")
    first = _activate(days_ago=30)
    assert vr.activation_at() == first
    _mode(monkeypatch, "payouts_over:100000")
    assert vr.activation_at() is None
    assert vr.record_activation() is not None and vr.activation_at() is not None
    _mode(monkeypatch, "none")
    vr.record_activation()
    assert vr._setting() is None


# ── new_stores ─────────────────────────────────────────────────────────────

def test_new_stores_start_unpublished_and_publish_after_verification(client, make_user, make_vendor, make_order,
                                                                     auth_headers, transfers, monkeypatch):
    _mode(monkeypatch, "new_stores", grace=0)
    _activate(days_ago=100)
    # An existing seller is never affected in this mode.
    old_vendor, _ = _existing_seller(make_vendor, days_old=200)
    order, escrow = _paid_order(old_vendor, make_order)
    _release(order, escrow)
    assert len(transfers) == 1 and VerificationHold.query.count() == 0

    seller = _verified_user(make_user)
    res = client.post("/api/vendor/onboard", headers=auth_headers(seller), json={"business_name": "Fresh Shop"})
    assert res.status_code == 201
    body = res.get_json()
    assert body["status"] == "verification_required"
    assert body["verification_required"]["title"] == "Verify to keep selling"
    sf = Storefront.query.filter_by(vendor_id=seller.id).one()
    assert sf.is_published is False
    assert VerificationHold.query.filter_by(vendor_id=seller.id, kind="store", status="held").count() == 1
    res = client.patch("/api/vendor/update-settings", headers=auth_headers(seller), json={"is_published": True})
    assert res.status_code == 403 and res.get_json()["title"] == "Verify to keep selling"

    # A sale through their Pay Link still waits for verification.
    db.session.add(VendorBankAccount(vendor_id=seller.id, bank_name="B", bank_code="058",
                                     account_number="0123456780", account_name="S", is_default=True,
                                     recipient_code="RCP_new"))
    db.session.commit()
    order, escrow = _paid_order(seller, make_order)
    _release(order, escrow)
    assert len(transfers) == 1
    status = client.get("/api/vendor/verification-requirement", headers=auth_headers(seller)).get_json()
    assert status["required"] and status["store_held"] and status["held_payouts"]["count"] == 1

    _verify(seller.id)
    assert vr.release_for(seller.id) == {"payouts": 1, "dispatched": 1, "stores": 1}
    db.session.expire_all()
    assert db.session.get(Storefront, sf.id).is_published is True and len(transfers) == 2


def test_new_stores_through_onboarding_publish(client, make_user, auth_headers, monkeypatch, transfers):
    for key in ("AI_ENABLED", "AI_ENABLED_ACTIONS", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    _mode(monkeypatch, "all_sellers")
    seller = _verified_user(make_user)
    h = auth_headers(seller)
    res = client.post("/api/onboarding/sessions", headers=h, json={"answers": ONBOARDING_ANSWERS})
    assert res.status_code in (200, 201), res.get_json()
    sid = res.get_json()["session"]["id"]
    assert client.post(f"/api/onboarding/sessions/{sid}/build", headers=h).status_code == 202
    res = client.post(f"/api/onboarding/sessions/{sid}/publish", headers=auth_headers(seller), json=ONBOARDING_BANK)
    assert res.status_code == 201, res.get_json()
    assert res.get_json()["verification_required"]["title"] == "Verify to keep selling"
    sf = Storefront.query.filter_by(vendor_id=seller.id).one()
    assert sf.is_published is False
    # A repeat publish answers the same stored result.
    again = client.post(f"/api/onboarding/sessions/{sid}/publish", headers=auth_headers(seller), json=ONBOARDING_BANK)
    assert again.status_code == 200 and again.get_json()["verification_required"]["code"] == "VERIFICATION_REQUIRED"


def test_verified_new_seller_publishes_at_once(client, make_user, auth_headers, monkeypatch):
    _mode(monkeypatch, "new_stores")
    seller = _verified_user(make_user)
    _verify(seller.id)
    res = client.post("/api/vendor/onboard", headers=auth_headers(seller), json={"business_name": "Known Shop"})
    assert res.get_json()["status"] == "success"
    assert Storefront.query.filter_by(vendor_id=seller.id).one().is_published is True


def test_not_enforced_while_verification_is_closed(client, make_user, make_vendor, make_order, auth_headers,
                                                   transfers, monkeypatch):
    monkeypatch.delenv("VERIFICATION_V2_ENABLED", raising=False)
    _mode(monkeypatch, "all_sellers", grace=0)
    _activate(days_ago=5)
    vendor, _ = _existing_seller(make_vendor)
    order, escrow = _paid_order(vendor, make_order)
    _release(order, escrow)
    assert len(transfers) == 1 and VerificationHold.query.count() == 0
    seller = _verified_user(make_user)
    assert client.post("/api/vendor/onboard", headers=auth_headers(seller),
                       json={"business_name": "Closed Shop"}).get_json()["status"] == "success"


# ── superadmin preview ─────────────────────────────────────────────────────

def test_admin_preview_counts_before_switching(client, make_vendor, make_order, auth_headers, transfers):
    verified, _ = _existing_seller(make_vendor)
    _verify(verified.id)
    big_seller, _ = _existing_seller(make_vendor)
    small_seller, _ = _existing_seller(make_vendor)
    for vendor, amount in ((verified, 200_000), (big_seller, 100_000), (small_seller, 20_000)):
        order, escrow = _paid_order(vendor, make_order, amount=amount, fee=amount * 0.03)
        _release(order, escrow)

    url = "/api/admin/verification-requirement"
    assert client.get(url, headers=auth_headers(verified)).status_code == 403
    headers = _superadmin_headers()
    now = client.get(url, headers=headers).get_json()
    assert now["current_mode"] == "none" and now["mode"] == "none" and now["affected_sellers"] == 0
    assert now["sellers_with_store"] == 3 and now["unverified_sellers"] == 2 and now["verification_open"] is True

    everyone = client.get(f"{url}?mode=all_sellers", headers=headers).get_json()
    assert everyone["affected_sellers"] == 2 and everyone["grace_days"] == 14
    over = client.get(f"{url}?mode=payouts_over:50000", headers=headers).get_json()
    assert over["affected_sellers"] == 1 and over["payout_limit_ngn"] == 50_000.0
    assert client.get(f"{url}?mode=new_stores", headers=headers).get_json()["affected_sellers"] == 0
    assert client.get(f"{url}?mode=everyone", headers=headers).status_code == 400
    # The preview never switches anything on.
    assert vr.current_mode() == ("none", None) and VerificationHold.query.count() == 0


def test_switching_back_to_none_releases_everything_held(client, make_user, make_vendor, make_order, auth_headers,
                                                         transfers, monkeypatch):
    _mode(monkeypatch, "all_sellers", grace=0)
    _activate(days_ago=2)
    vendor, _ = _existing_seller(make_vendor)
    order, escrow = _paid_order(vendor, make_order)
    _release(order, escrow)
    seller = _verified_user(make_user)
    client.post("/api/vendor/onboard", headers=auth_headers(seller), json={"business_name": "Waiting Shop"})
    assert VerificationHold.query.filter_by(status="held").count() == 2 and len(transfers) == 0

    _mode(monkeypatch, "none")
    assert vr.run_verification_requirement_sweep() == {"sellers": 2, "released": 2}
    assert [c["amount_ngn"] for c in transfers] == [97_000.0]
    assert Storefront.query.filter_by(vendor_id=seller.id).one().is_published is True
    assert _get_ledger_balance(vendor.id) == Decimal("0") and vr.activation_at() is None
