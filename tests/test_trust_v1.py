"""Trust Score v1 (app/services/trust_v1.py): components, anti-gaming rules,
minimum history, founder rules (money and decoration earn nothing), the
daily shadow job and the TRUST_SCORE_V1_LIVE switch."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser, SubscriptionPlan, VendorSubscription
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.foundations import SellerIdentifier
from app.models.order import Order
from app.models.telemetry import TrustEvidence
from app.models.trust import TrustScoreShadow, VendorTrustProfile
from app.models.user import UserRole
from app.services import trust_v1
from app.services.foundations import payout_account_hash
from app.services.trust_v1 import score_orders

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


# ── pure scoring helpers ────────────────────────────────────────────────────

def fact(i, buyer=None, days=10, amount=10_000, outcome="good", **kw):
    return {"order_id": i, "buyer_key": buyer or f"b{i}", "linked": False, "buyer_new": False,
            "time": NOW - timedelta(days=days), "amount_ngn": amount, "outcome": outcome,
            "seller_fault": False, "refunded": False, "dispute_opened": False,
            "delivered_on_time": None, "evidence_ids": [i], **kw}


def good_sales(n=6, start=0, days=10):
    return [fact(start + i, days=days + i) for i in range(n)]


def score(facts):
    return score_orders(facts, now=NOW)


# ── minimum history ─────────────────────────────────────────────────────────

def test_minimum_history_five_sales_three_buyers():
    four = score(good_sales(4))
    assert four["score"] is None and four["tier"] == "NEW" and four["label"] == "New seller"
    assert four["reasons"] == ["not_enough_history"]

    two_buyers = score([fact(i, buyer="a" if i % 2 else "b", days=10 + 3 * i) for i in range(6)])
    assert two_buyers["score"] is None and two_buyers["minimum_history"]["distinct_buyers"] == 2

    enough = score(good_sales(5))
    assert enough["score"] is not None and enough["tier"] != "NEW"
    assert enough["minimum_history"] == {"met": True, "qualifying_sales": 5, "distinct_buyers": 5,
                                         "needs_sales": 5, "needs_buyers": 3}


def test_pending_orders_do_not_count():
    r = score(good_sales(4) + [fact(99, outcome="pending")])
    assert r["score"] is None and r["counts"]["pending_orders"] == 1


# ── components ──────────────────────────────────────────────────────────────

def test_outcomes_refund_and_seller_fault_dispute():
    base = score(good_sales(6))
    refund = score(good_sales(6) + [fact(50, outcome="bad", refunded=True)])
    fault = score(good_sales(6) + [fact(50, outcome="bad", refunded=True, seller_fault=True,
                                        dispute_opened=True)])
    assert fault["components"]["outcomes"] < refund["components"]["outcomes"] < base["components"]["outcomes"]
    assert refund["counts"]["refunds"] == 1 and fault["counts"]["seller_fault_disputes"] == 1
    assert fault["indicators"]["seller_fault_disputes_per_100"] == pytest.approx(100 / 7, abs=0.01)


def test_seller_fault_dispute_weighs_five_refunds():
    assert trust_v1.RULES["seller_fault_dispute_bad_units"] == 5.0
    one_fault = score(good_sales(6) + [fact(50, outcome="bad", seller_fault=True, days=10)])
    five_refunds = score(good_sales(6) + [fact(50 + i, outcome="bad", refunded=True, days=10) for i in range(5)])
    assert one_fault["components"]["outcomes"] == pytest.approx(five_refunds["components"]["outcomes"])


def _hundred(faults, refunds=0, fault_days=5):
    """100 recent orders from 100 buyers, delivered on time; `faults` seller-fault disputes."""
    good = [fact(i, days=1 + 1.8 * i, delivered_on_time=True) for i in range(100 - faults - refunds)]
    bad = [fact(500 + j, outcome="bad", seller_fault=True, days=fault_days + 25 * j, delivered_on_time=True)
           for j in range(faults)]
    ref = [fact(700 + j, outcome="bad", refunded=True, days=5 + 25 * j, delivered_on_time=True)
           for j in range(refunds)]
    return score(good + bad + ref)


def test_dispute_rate_below_two_per_100_is_not_capped():
    r = _hundred(1)
    assert r["dispute_cap"] == {"cap": None, "applied": False, "rate_per_100": 1.0}
    assert r["tier"] == "GOLD" and r["score"] == int(round(r["raw_score"]))


def test_dispute_rate_two_per_100_caps_at_silver():
    r = _hundred(2)
    assert r["raw_score"] >= 75  # GOLD on the score alone
    assert r["dispute_cap"] == {"cap": "SILVER", "applied": True, "rate_per_100": 2.0}
    assert r["tier"] == "SILVER" and r["score"] == 74
    assert r["indicators"]["seller_fault_disputes_per_100_6m_weighted"] == 2.0


def test_dispute_rate_five_and_seven_per_100_cap_at_bronze():
    for n in (5, 7):
        r = _hundred(n)
        assert r["dispute_cap"]["cap"] == "BRONZE" and r["dispute_cap"]["applied"]
        assert r["tier"] == "BRONZE" and r["score"] == 54
    # Even without the cap, 7 per 100 is no longer GOLD on the score alone.
    assert _hundred(7)["raw_score"] < 75


def test_dispute_cap_ignores_refunds_and_disputes_older_than_six_months():
    assert _hundred(0, refunds=7)["dispute_cap"]["cap"] is None
    old = _hundred(3, fault_days=200)  # all three over 6 months ago
    assert old["dispute_cap"]["cap"] is None and old["counts"]["seller_fault_disputes"] == 3


def test_dispute_cap_never_raises_a_lower_tier():
    low = score([fact(i, days=300 + i, delivered_on_time=False) for i in range(5)]
                + [fact(50, outcome="bad", seller_fault=True, days=100)])
    assert low["dispute_cap"]["cap"] == "BRONZE" and low["tier"] == "BRONZE"
    assert low["score"] == min(54, int(round(low["raw_score"])))


def test_dispute_not_seller_fault_costs_nothing():
    base = score(good_sales(6))
    cleared = score(good_sales(6) + [fact(50, outcome="good", dispute_opened=True)])
    neutral = score(good_sales(6) + [fact(51, outcome="neutral", dispute_opened=True, refunded=True)])
    assert cleared["components"]["outcomes"] >= base["components"]["outcomes"]
    assert cleared["counts"]["disputes_not_seller_fault"] == 1
    assert neutral["components"]["outcomes"] == base["components"]["outcomes"]


def test_on_time_delivery():
    on_time = score([fact(i, days=10 + i, delivered_on_time=True) for i in range(6)])
    late = score([fact(i, days=10 + i, delivered_on_time=False) for i in range(6)])
    unknown = score(good_sales(6))
    assert late["components"]["on_time"] < unknown["components"]["on_time"] < on_time["components"]["on_time"]
    assert on_time["indicators"]["on_time_rate"] == 1.0 and late["indicators"]["on_time_rate"] == 0.0
    assert on_time["score"] > late["score"]


def test_repeat_buyers_need_a_later_day():
    others = [fact(i, days=40 + i) for i in range(4)]
    later = score(others + [fact(10, buyer="r", days=30), fact(11, buyer="r", days=20)])
    same_day = score(others + [fact(10, buyer="r", days=30, ), fact(11, buyer="r", days=30)])
    assert later["indicators"]["repeat_buyers"] == 1 and same_day["indicators"]["repeat_buyers"] == 0
    assert later["components"]["repeat_buyers"] > same_day["components"]["repeat_buyers"] == 0


def test_tenure_and_consistency():
    young = score(good_sales(6, days=5))
    # One sale in each of the last six months.
    steady = score([fact(i, days=5 + 30 * i) for i in range(6)])
    # Same number of sales, all in one month, first one a year ago.
    bunched = score([fact(i, days=365 - i) for i in range(6)])
    assert young["components"]["tenure"] < steady["components"]["tenure"] < bunched["components"]["tenure"]
    assert steady["components"]["consistency"] == 1.0
    assert bunched["components"]["consistency"] < steady["components"]["consistency"]
    assert young["components"]["consistency"] == 1.0  # new seller: judged on the months they have sold


def test_weights_are_versioned_and_sum_to_one():
    assert trust_v1.VERSION == "v1.0"
    assert sum(trust_v1.WEIGHTS.values()) == pytest.approx(1.0)
    r = score(good_sales(6))
    assert r["version"] == "v1.0" and r["weights"] == trust_v1.WEIGHTS
    assert sorted(r["evidence_ids"]) == [0, 1, 2, 3, 4, 5]


# ── anti-gaming ─────────────────────────────────────────────────────────────

def test_self_dealing_orders_count_for_nothing():
    base = score(good_sales(4))
    linked = score(good_sales(4) + [fact(90 + i, linked=True, buyer=f"x{i}") for i in range(10)])
    assert linked["score"] is None  # still below the minimum history
    assert linked["anti_gaming"]["self_dealing_orders"] == 10
    assert linked["components"] == base["components"]
    # A linked refund cannot be used to sink a seller either.
    ok = score(good_sales(6))
    sabotage = score(good_sales(6) + [fact(80, outcome="bad", refunded=True, linked=True)])
    assert sabotage["components"] == ok["components"]


def test_many_tiny_orders():
    tiny = score([fact(i, amount=500, days=10 + 3 * i) for i in range(20)])
    assert tiny["score"] is None and tiny["anti_gaming"]["tiny_orders"] == 20
    assert tiny["minimum_history"]["qualifying_sales"] == 0
    # Tiny orders barely move the outcome share either.
    with_refund = good_sales(6) + [fact(70, outcome="bad", refunded=True)]
    padded = with_refund + [fact(100 + i, amount=500, days=10 + i) for i in range(20)]
    real = with_refund + [fact(100 + i, amount=10_000, days=10 + i) for i in range(20)]
    assert score(padded)["components"]["outcomes"] < score(real)["components"]["outcomes"]


def test_burst_from_new_buyer_accounts():
    burst = score([fact(i, buyer_new=True, days=10, ) for i in range(6)])
    assert burst["anti_gaming"]["burst_orders"] == 6 and burst["score"] is None
    spread = score([fact(i, buyer_new=True, days=10 + 7 * i) for i in range(6)])
    assert spread["anti_gaming"]["burst_orders"] == 0 and spread["score"] is not None
    # Four new buyers in a window is not a burst; established buyers never are.
    assert score([fact(i, buyer_new=True, days=10) for i in range(4)])["anti_gaming"]["burst_orders"] == 0
    assert score([fact(i, days=10) for i in range(8)])["anti_gaming"]["burst_orders"] == 0


def test_diminishing_returns_per_buyer():
    one_buyer = [fact(i, buyer="same", days=10 + 2 * i) for i in range(10)]
    r = score(one_buyer)
    assert r["score"] is None  # one buyer can never unlock a score
    assert r["anti_gaming"]["diminished_orders"] == 9
    refund = [fact(70, outcome="bad", refunded=True)]
    many = [fact(i, days=10 + 2 * i) for i in range(10)]
    assert score(one_buyer + refund)["components"]["outcomes"] < score(many + refund)["components"]["outcomes"]


def test_recency_decay():
    recent_refund = score(good_sales(6) + [fact(70, outcome="bad", refunded=True, days=1)])
    old_refund = score(good_sales(6) + [fact(70, outcome="bad", refunded=True, days=730)])
    assert old_refund["components"]["outcomes"] > recent_refund["components"]["outcomes"]


# ── database: evidence → facts ─────────────────────────────────────────────

def _old_buyer(make_user, **fields):
    u = make_user(role=UserRole.BUYER)
    u.created_at = datetime.utcnow() - timedelta(days=400)
    for k, v in fields.items():
        setattr(u, k, v)
    db.session.commit()
    return u


def _sale(vendor, buyer=None, days_ago=10, amount=10_000, outcome="released", delivered_days=2,
          buyer_email=None, seller_fault=False, status="ACTIVE", **order_fields):
    paid = datetime.now(timezone.utc) - timedelta(days=days_ago)
    order = Order(vendor_id=vendor.id, buyer_id=buyer.id if buyer else None,
                  buyer_email=buyer_email or (buyer.email if buyer else f"g-{uuid.uuid4().hex[:6]}@example.com"),
                  buyer_name="Buyer", total_amount=amount, status="COMPLETED", payment_method="ESCROW",
                  created_at=paid.replace(tzinfo=None) - timedelta(hours=1), paid_at=paid,
                  delivered_at=paid + timedelta(days=delivered_days) if delivered_days is not None else None,
                  **order_fields)
    db.session.add(order)
    db.session.flush()

    def ev(kind, at, **props):
        db.session.add(TrustEvidence(business_id=vendor.id, evidence_type=kind, provenance="siiqo_transaction_verified",
                                     source="escrow", source_id=str(order.id), status=status, confidence=1,
                                     properties=props, created_at=at, verified_at=at))
    ev("order_paid", paid, value=float(amount), currency="NGN")
    if outcome == "released":
        ev("escrow_released", paid + timedelta(days=3), value=float(amount), currency="NGN")
    elif outcome == "refunded":
        ev("order_refunded", paid + timedelta(days=3))
        if seller_fault:
            ev("dispute_opened", paid + timedelta(days=2))
            ev("dispute_lost", paid + timedelta(days=3))
    db.session.commit()
    return order


def _history(vendor, make_user, n=6):
    return [_sale(vendor, _old_buyer(make_user), days_ago=10 + 20 * i) for i in range(n)]


def test_evidence_from_the_real_escrow_hooks(make_vendor, make_order, make_user):
    vendor, _ = make_vendor()
    buyer = _old_buyer(make_user)
    order = make_order(vendor, buyer=buyer, total=10_000)
    escrow = EscrowTransaction(order_id=order.id, transaction_number=f"T-{order.id}",
                               status=EscrowStatus.PENDING_PAYMENT, amount=10_000, fee_amount=300,
                               fee_percent=3, currency="NGN")
    db.session.add(escrow)
    db.session.commit()
    escrow.status = EscrowStatus.IN_ESCROW
    db.session.commit()
    facts = trust_v1.gather_orders(vendor.id)
    assert len(facts) == 1 and facts[0]["outcome"] == "pending" and facts[0]["amount_ngn"] == 10_000
    escrow.status = EscrowStatus.RELEASED
    db.session.commit()
    facts = trust_v1.gather_orders(vendor.id)
    assert facts[0]["outcome"] == "good" and facts[0]["buyer_key"] == f"e:{buyer.email}"
    assert not facts[0]["linked"] and not facts[0]["buyer_new"]


def test_db_refund_dispute_on_time_and_revoked(make_vendor, make_user):
    vendor, _ = make_vendor()
    _history(vendor, make_user)
    _sale(vendor, _old_buyer(make_user), outcome="refunded", seller_fault=True)
    _sale(vendor, _old_buyer(make_user), delivered_days=12)
    _sale(vendor, _old_buyer(make_user), outcome="refunded", status="REVOKED")  # appealed away
    r = trust_v1.compute(vendor.id)
    assert r["counts"]["seller_fault_disputes"] == 1 and r["counts"].get("refunds", 0) == 0
    assert r["counts"]["delivered_late"] == 1 and r["counts"]["delivered_on_time"] == 7
    assert r["counts"]["completed_orders"] == 7 and r["score"] is not None


def test_self_dealing_via_identifier_graph(make_vendor, make_user):
    vendor, _ = make_vendor()
    vendor.phone = "08031234567"
    db.session.commit()
    _history(vendor, make_user, n=4)

    # 1. Buyer account with the seller's phone in another format.
    _sale(vendor, _old_buyer(make_user, phone="+234 803 123 4567"))
    # 2. Guest checkout with the seller's own email.
    _sale(vendor, None, buyer_email=vendor.email.upper())
    # 3. Another seller account that shares the seller's payout account.
    other, _ = make_vendor()
    other.created_at = datetime.utcnow() - timedelta(days=400)
    acct = payout_account_hash("058", "0123456789")
    db.session.add_all([
        SellerIdentifier(vendor_id=vendor.id, kind="payout_account_hash", value=acct, source="payout"),
        SellerIdentifier(vendor_id=other.id, kind="payout_account_hash", value=acct, source="payout"),
    ])
    db.session.commit()
    _sale(vendor, other)
    # 4. Delivery phone that is in the seller's identifier graph (WhatsApp number).
    db.session.add(SellerIdentifier(vendor_id=vendor.id, kind="phone", value="2348099990000", source="store"))
    db.session.commit()
    _sale(vendor, _old_buyer(make_user), delivery_phone="08099990000")
    # 5. The seller buying from themself.
    _sale(vendor, vendor)

    r = trust_v1.compute(vendor.id)
    assert r["anti_gaming"]["self_dealing_orders"] == 5
    assert r["minimum_history"]["qualifying_sales"] == 4 and r["score"] is None


def test_guest_burst_and_tiny_orders_from_db(make_vendor, make_user):
    vendor, _ = make_vendor()
    for i in range(6):  # six first-time guests within two days
        _sale(vendor, None, days_ago=5 + (i % 2))
    r = trust_v1.compute(vendor.id)
    assert r["anti_gaming"]["burst_orders"] == 6 and r["score"] is None

    vendor2, _ = make_vendor()
    for i in range(8):
        _sale(vendor2, _old_buyer(make_user), amount=300, days_ago=10 + 5 * i)
    r2 = trust_v1.compute(vendor2.id)
    assert r2["anti_gaming"]["tiny_orders"] == 8 and r2["score"] is None


def test_new_registered_buyer_accounts_burst(make_vendor, make_user):
    vendor, _ = make_vendor()
    for _ in range(5):
        u = make_user(role=UserRole.BUYER)  # created just now
        _sale(vendor, u, days_ago=0)
    assert trust_v1.compute(vendor.id)["anti_gaming"]["burst_orders"] == 5


def test_reviewer_dispute_decision_from_dispute_cases(make_vendor, make_user):
    """dispute_resolved rows point at the case (source_id) and name the order in properties."""
    vendor, _ = make_vendor()
    _history(vendor, make_user)
    blamed = _sale(vendor, _old_buyer(make_user), outcome="refunded")
    cleared = _sale(vendor, _old_buyer(make_user), outcome="refunded", seller_fault=True)  # escrow guessed "lost"
    now = datetime.now(timezone.utc)
    for case_id, order, fault in ((901, blamed, True), (902, cleared, False)):
        db.session.add(TrustEvidence(business_id=vendor.id, evidence_type="dispute_resolved",
                                     provenance="siiqo_transaction_verified", source="dispute_cases",
                                     source_id=str(case_id), status="ACTIVE", confidence=1,
                                     properties={"order_id": order.id, "seller_fault": fault, "outcome": "refund"},
                                     created_at=now, verified_at=now))
    db.session.commit()
    facts = {f["order_id"]: f for f in trust_v1.gather_orders(vendor.id)}
    assert 901 not in facts and 902 not in facts
    assert facts[blamed.id]["seller_fault"] is True and facts[blamed.id]["outcome"] == "bad"
    assert facts[cleared.id]["seller_fault"] is False and facts[cleared.id]["outcome"] == "neutral"
    r = trust_v1.compute(vendor.id)
    assert r["counts"]["seller_fault_disputes"] == 1 and r["counts"]["neutral_orders"] == 1


# ── founder rules ──────────────────────────────────────────────────────────

def test_payment_plan_verification_and_logo_do_not_move_the_score(make_vendor, make_user):
    vendor, store = make_vendor()
    _history(vendor, make_user)
    before = trust_v1.compute(vendor.id, now=NOW)
    assert before["score"] is not None

    plan = SubscriptionPlan(name="PRO", price_ngn=5000)
    db.session.add(plan)
    db.session.flush()
    db.session.add(VendorSubscription(vendor_id=vendor.id, plan_id=plan.id, status="ACTIVE",
                                      end_date=datetime.now(timezone.utc) + timedelta(days=30)))
    store.is_pro_verified = True
    store.pro_verified_expires_at = datetime.now(timezone.utc) + timedelta(days=365)
    store.verification_status = "VERIFIED"
    store.store_logo = "https://cdn.example/logo.png"
    store.banner_url = "https://cdn.example/banner.png"
    store.cac_reg = "RC1234567"
    vendor.nin = "12345678901"
    vendor.is_verified = True
    db.session.add(VendorTrustProfile(vendor_id=vendor.id, total_trust_score=990, trust_tier="PLATINUM"))
    db.session.commit()

    assert trust_v1.compute(vendor.id, now=NOW) == before


# ── shadow mode ─────────────────────────────────────────────────────────────

def test_shadow_job_is_idempotent_and_keeps_old_score_public(make_vendor, make_user, monkeypatch):
    monkeypatch.delenv("TRUST_SCORE_V1_LIVE", raising=False)
    vendor, _ = make_vendor()
    newbie, _ = make_vendor()
    _history(vendor, make_user)

    first = trust_v1.run_shadow(now=NOW)
    assert first["stored"] == 2 and first["failed"] == 0
    rows = TrustScoreShadow.query.order_by(TrustScoreShadow.vendor_id).all()
    assert len(rows) == 2
    scored = TrustScoreShadow.query.filter_by(vendor_id=vendor.id).one()
    assert scored.score is not None and scored.version == "v1.0"
    assert scored.components["components"] and scored.components["evidence_ids"]
    assert (scored.old_score, scored.old_tier) == (500, "SILVER")  # what buyers saw
    new_row = TrustScoreShadow.query.filter_by(vendor_id=newbie.id).one()
    assert new_row.score is None and new_row.tier == "NEW"

    # Same day again, with one more refund: the row is updated, not duplicated.
    _sale(vendor, _old_buyer(make_user), outcome="refunded", days_ago=1)
    trust_v1.run_shadow(now=NOW + timedelta(hours=3))
    assert TrustScoreShadow.query.count() == 2
    updated = TrustScoreShadow.query.filter_by(vendor_id=vendor.id).one()
    assert updated.components["counts"]["refunds"] == 1

    # Next day: a new row per seller.
    trust_v1.run_shadow(now=NOW + timedelta(days=1))
    assert TrustScoreShadow.query.count() == 4

    # Public score untouched while the flag is off.
    db.session.expire_all()
    from app.models.user import User
    assert db.session.get(User, vendor.id).trust_score_or_default == 500
    assert db.session.get(User, vendor.id).trust_tier_or_default == "SILVER"


def test_scheduled_task_wrapper(make_vendor):
    from app.tasks.trust_v1_shadow import run_trust_v1_shadow
    make_vendor()
    assert run_trust_v1_shadow()["stored"] == 1
    assert TrustScoreShadow.query.count() == 1


# ── go-live switch ─────────────────────────────────────────────────────────

def test_flag_switches_passport_and_store_score(client, make_vendor, make_user, monkeypatch):
    monkeypatch.delenv("TRUST_SCORE_V1_LIVE", raising=False)
    vendor, store = make_vendor()
    newbie, new_store = make_vendor()
    _history(vendor, make_user)
    db.session.add(VendorTrustProfile(vendor_id=vendor.id, total_trust_score=742, trust_tier="GOLD"))
    db.session.commit()
    trust_v1.run_shadow()
    v1_row = trust_v1.latest_row(vendor.id)

    off = client.get(f"/api/passport/{store.store_slug}").get_json()["passport"]
    assert (off["score"], off["tier"], off["score_max"]) == (742, "GOLD", 1000)
    assert client.get(f"/api/marketplace/vendor/{newbie.id}/trust-breakdown").get_json()["total_score"] == 500

    monkeypatch.setenv("TRUST_SCORE_V1_LIVE", "true")
    on = client.get(f"/api/passport/{store.store_slug}").get_json()["passport"]
    assert (on["score"], on["tier"], on["score_max"]) == (v1_row.score, v1_row.tier, 100)
    new_on = client.get(f"/api/passport/{new_store.store_slug}").get_json()["passport"]
    assert new_on["score"] is None and new_on["tier"] == "NEW"

    db.session.expire_all()
    from app.models.user import Storefront, User
    assert db.session.get(User, vendor.id).trust_score_or_default == v1_row.score
    assert db.session.get(Storefront, store.id).to_public_dict()["trust_score"] == v1_row.score
    # The SILVER-500 default is gone for sellers without history.
    assert db.session.get(User, newbie.id).trust_score_or_default is None
    assert db.session.get(User, newbie.id).trust_tier_or_default == "NEW"
    breakdown = client.get(f"/api/marketplace/vendor/{vendor.id}/trust-breakdown").get_json()
    assert breakdown["total_score"] == v1_row.score
    assert {p["max"] for p in breakdown["pillars"]} == {100} and len(breakdown["pillars"]) == 5

    monkeypatch.setenv("TRUST_SCORE_V1_LIVE", "false")
    back = client.get(f"/api/passport/{store.store_slug}").get_json()["passport"]
    assert back["score"] == 742


# ── admin comparison ───────────────────────────────────────────────────────

def _admin_headers(role="SUPERADMIN"):
    admin = AdminUser(name="Ops", email=f"ops-{uuid.uuid4().hex[:6]}@example.com", role=role)
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def test_admin_comparison(client, make_vendor, make_user, auth_headers):
    vendor, store = make_vendor()
    newbie, _ = make_vendor()
    _history(vendor, make_user)
    db.session.add(VendorTrustProfile(vendor_id=vendor.id, total_trust_score=300, trust_tier="SILVER"))
    db.session.commit()

    assert client.get("/api/admin/trust/v1-comparison", headers=auth_headers(vendor)).status_code == 403
    assert client.get("/api/admin/trust/v1-comparison", headers=_admin_headers("ADMIN")).status_code == 403

    headers = _admin_headers()
    empty = client.get("/api/admin/trust/v1-comparison", headers=headers).get_json()
    assert empty["computed_on"] is None and empty["sellers"] == [] and empty["live"] is False

    assert client.post("/api/admin/trust/v1-run", headers=headers).get_json()["stored"] == 2
    data = client.get("/api/admin/trust/v1-comparison", headers=headers).get_json()
    row = trust_v1.latest_row(vendor.id)
    assert data["version"] == "v1.0" and data["total"] == 2
    top = data["biggest_differences"][0]
    assert top["vendor_id"] == vendor.id and top["store_slug"] == store.store_slug
    assert top["old_score"] == 300 and top["v1_score"] == row.score
    assert top["difference"] == pytest.approx(row.score - 30.0)
    assert data["sellers"][-1]["vendor_id"] == newbie.id and data["sellers"][-1]["v1_tier"] == "NEW"
    dist = data["distribution"]
    assert dist["sellers"] == 2 and dist["new_sellers"] == 1
    assert sum(b["count"] for b in dist["v1_histogram"]) == 1
    assert dist["old_tiers"] == {"SILVER": 2}

    detail = client.get(f"/api/admin/trust/v1/{vendor.id}", headers=headers).get_json()
    assert detail["components"]["minimum_history"]["met"] is True
    assert client.get("/api/admin/trust/v1/999999", headers=headers).status_code == 404


def test_every_score_carries_its_scale(client, make_vendor, make_user, auth_headers, monkeypatch):
    monkeypatch.delenv("TRUST_SCORE_V1_LIVE", raising=False)
    vendor, store = make_vendor()
    newbie, _ = make_vendor()
    _history(vendor, make_user)
    trust_v1.run_shadow()
    from app.models.user import Storefront, User

    def readers():
        db.session.expire_all()
        u = db.session.get(User, vendor.id)
        sf = db.session.get(Storefront, store.id)
        return (u.to_public_dict(), sf.to_public_dict(),
                client.get(f"/api/marketplace/vendor/{vendor.id}/trust-breakdown").get_json(),
                client.get("/api/vendor/trust-profile", headers=auth_headers(vendor)).get_json(),
                client.get("/api/vendor/trust-profile", headers=auth_headers(newbie)).get_json())

    user_d, store_d, breakdown, own, own_new = readers()
    assert user_d["trust_score_max"] == store_d["trust_score_max"] == breakdown["score_max"] == 1000
    assert own["score_max"] == 1000 and "completion_score" in own

    monkeypatch.setenv("TRUST_SCORE_V1_LIVE", "1")
    user_d, store_d, breakdown, own, own_new = readers()
    row = trust_v1.latest_row(vendor.id)
    assert user_d["trust_score_max"] == store_d["trust_score_max"] == breakdown["score_max"] == 100
    assert own["score_max"] == 100 and own["total_trust_score"] == row.score and own["trust_tier"] == row.tier
    assert [p["key"] for p in own["pillars"]] == list(trust_v1.WEIGHTS)
    assert own["dispute_cap"]["cap"] is None
    assert own_new["total_trust_score"] is None and own_new["trust_tier"] == "NEW"
    assert own_new["label"] == "New seller" and own_new["pillars"] == []
