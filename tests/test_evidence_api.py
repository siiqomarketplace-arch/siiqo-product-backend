"""Phase 4: Evidence API (POST /v1/evidence, app/services/partner_evidence.py).

Signature and replay window, idempotent event ids, seller consent (accept,
revoke, dispute), the per-partner cap and lower weight, self-dealing keys,
superadmin throttle/revoke (audit-logged), test keys storing nothing, and no
effect on the old score."""
import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.audit import AdminAuditLog
from app.models.foundations import SellerIdentifier
from app.models.order import Order
from app.models.platform import (OwnershipClaim, PartnerEvidenceReceipt, PartnerSellerLink)
from app.models.telemetry import TrustEvidence
from app.models.trust import VendorTrustProfile
from app.models.user import UserRole
from app.services import partner_evidence as evidence
from app.services import platform_webhooks as webhooks
from app.services import trust_v1

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


# ── helpers ────────────────────────────────────────────────────────────────

def _bearer(raw):
    return {"Authorization": f"Bearer {raw}"}


def _key(client, owner, auth_headers, mode="live"):
    res = client.post("/api/passport/keys", json={"name": f"Platform {mode}", "mode": mode},
                      headers=auth_headers(owner))
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()


def _partner(client, make_user, auth_headers, mode="live", owner=None):
    """(client_id, api_key, evidence_secret) for a platform."""
    owner = owner or make_user()
    key = _key(client, owner, auth_headers, mode)
    res = client.post("/v1/evidence/secret", headers=_bearer(key["api_key"]))
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    assert body["secret"].startswith("evsec_") and body["secret_prefix"] == body["secret"][:12]
    return key["id"], key["api_key"], body["secret"]


def _event(slug, etype="order.completed", event_id=None, order_ref=None, occurred_at=None, **extra):
    body = {"id": event_id or f"evt_{uuid.uuid4().hex[:12]}", "type": etype, "seller": {"slug": slug},
            "order_ref": order_ref or f"ord-{uuid.uuid4().hex[:8]}",
            "occurred_at": (occurred_at or datetime.now(timezone.utc) - timedelta(days=3)).isoformat()}
    if etype == "order.delivered":
        body.setdefault("on_time", True)
    if etype == "dispute.resolved":
        body.setdefault("fault", "seller")
    body.update(extra)
    return body


def _post(client, api_key, secret, body, timestamp=None, signature=None, raw=None):
    raw = raw if raw is not None else json.dumps(body, separators=(",", ":"))
    headers = {**_bearer(api_key), "Content-Type": "application/json"}
    if signature is not False:
        headers["Siiqo-Signature"] = signature or webhooks.signature_header(secret, raw, timestamp)
    return client.post("/v1/evidence", data=raw, headers=headers)


def _old_buyer(make_user):
    u = make_user(role=UserRole.BUYER)
    u.created_at = datetime.utcnow() - timedelta(days=400)
    db.session.commit()
    return u


def _sale(vendor, buyer, days_ago=10, amount=10_000, outcome="released"):
    paid = datetime.now(timezone.utc) - timedelta(days=days_ago)
    order = Order(vendor_id=vendor.id, buyer_id=buyer.id, buyer_email=buyer.email, buyer_name="Buyer",
                  total_amount=amount, status="COMPLETED", payment_method="ESCROW",
                  created_at=paid.replace(tzinfo=None) - timedelta(hours=1), paid_at=paid,
                  delivered_at=paid + timedelta(days=2))
    db.session.add(order)
    db.session.flush()

    def ev(kind, at):
        db.session.add(TrustEvidence(business_id=vendor.id, evidence_type=kind, provenance="siiqo_transaction_verified",
                                     source="escrow", source_id=str(order.id), status="ACTIVE", confidence=1,
                                     properties={"value": float(amount), "currency": "NGN"}, created_at=at,
                                     verified_at=at))
    ev("order_paid", paid)
    ev("escrow_released" if outcome == "released" else "order_refunded", paid + timedelta(days=3))
    db.session.commit()
    return order


def _history(vendor, make_user, n=6, refunds=0):
    for i in range(n):
        _sale(vendor, _old_buyer(make_user), days_ago=10 + 20 * i)
    for i in range(refunds):
        _sale(vendor, _old_buyer(make_user), days_ago=15 + 20 * i, outcome="refunded")


def _report_good(client, api_key, secret, slug, n, prefix="r"):
    for i in range(n):
        res = _post(client, api_key, secret, _event(slug, order_ref=f"{prefix}{i}", buyer_ref=f"buyer-{prefix}{i}",
                                                    amount=20_000, currency="NGN"))
        assert res.status_code == 201, res.get_data(as_text=True)


def _superadmin_headers():
    admin = AdminUser(name="Ops", email=f"ops-{secrets.token_hex(3)}@example.com", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return admin, {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _pfact(i, client_id=1, outcome="good", days=10, amount=10_000, factor=1.0, buyer=None, **kw):
    return {"client_id": client_id, "order_ref": f"o{i}", "buyer_key": buyer or f"{client_id}:b{i}",
            "time": NOW - timedelta(days=days), "amount_ngn": amount, "outcome": outcome,
            "seller_fault": False, "delivered_on_time": None, "factor": factor, **kw}


def _fact(i, outcome="good", days=10, buyer=None):
    return {"order_id": i, "buyer_key": buyer or f"b{i}", "linked": False, "buyer_new": False,
            "time": NOW - timedelta(days=days), "amount_ngn": 10_000, "outcome": outcome,
            "seller_fault": False, "refunded": outcome == "bad", "dispute_opened": False,
            "delivered_on_time": None, "evidence_ids": [i]}


# ── signature, replay window, idempotency ──────────────────────────────────

def test_signature_is_required_and_checked(client, make_user, make_vendor, auth_headers):
    _, store = make_vendor()
    owner = make_user()
    key = _key(client, owner, auth_headers)
    body = _event(store.store_slug)
    res = _post(client, key["api_key"], "x", body, signature=False)
    assert res.status_code == 409 and res.get_json()["code"] == "NO_SIGNING_SECRET"

    secret = client.post("/v1/evidence/secret", headers=_bearer(key["api_key"])).get_json()["secret"]
    assert client.get("/v1/evidence/secret", headers=_bearer(key["api_key"])).get_json()["secret"]["secret_prefix"] \
        == secret[:12]
    assert _post(client, key["api_key"], secret, body, signature=False).get_json()["code"] == "BAD_SIGNATURE"
    assert _post(client, key["api_key"], "evsec_wrong", body).status_code == 401
    raw = json.dumps(body)
    tampered = _post(client, key["api_key"], secret, body, raw=raw.replace("order.completed", "order.refunded"),
                     signature=webhooks.signature_header(secret, raw))
    assert tampered.status_code == 401 and tampered.get_json()["code"] == "BAD_SIGNATURE"
    assert TrustEvidence.query.count() == 0
    assert _post(client, key["api_key"], secret, body).status_code == 201

    # A new secret replaces the old one at once.
    new_secret = client.post("/v1/evidence/secret", headers=_bearer(key["api_key"])).get_json()["secret"]
    assert _post(client, key["api_key"], secret, _event(store.store_slug)).status_code == 401
    assert _post(client, key["api_key"], new_secret, _event(store.store_slug)).status_code == 201
    assert client.post("/v1/evidence", data="{}").status_code == 401   # no key


def test_replay_window(client, make_user, make_vendor, auth_headers):
    _, store = make_vendor()
    _, api_key, secret = _partner(client, make_user, auth_headers)
    old = _post(client, api_key, secret, _event(store.store_slug), timestamp=time.time() - 301)
    assert old.status_code == 401 and old.get_json()["code"] == "SIGNATURE_EXPIRED"
    future = _post(client, api_key, secret, _event(store.store_slug), timestamp=time.time() + 400)
    assert future.get_json()["code"] == "SIGNATURE_EXPIRED"
    assert _post(client, api_key, secret, _event(store.store_slug), timestamp=time.time() - 200).status_code == 201


def test_same_event_id_is_idempotent(client, make_user, make_vendor, auth_headers):
    vendor, store = make_vendor()
    _, api_key, secret = _partner(client, make_user, auth_headers)
    body = _event(store.store_slug, event_id="evt-1", amount=5000)
    first = _post(client, api_key, secret, body)
    assert first.status_code == 201
    stored = first.get_json()
    assert stored["status"] == "stored" and stored["seller"] == {"slug": store.store_slug}
    assert stored["seller_link"] == "pending"
    again = _post(client, api_key, secret, body)        # a resend, freshly signed
    assert again.status_code == 200 and again.get_json()["status"] == "duplicate"
    assert again.get_json()["evidence_id"] == stored["evidence_id"]
    assert TrustEvidence.query.count() == 1 and PartnerEvidenceReceipt.query.count() == 1
    conflict = _post(client, api_key, secret, {**body, "type": "order.refunded"})
    assert conflict.status_code == 409 and conflict.get_json()["code"] == "IDEMPOTENCY_CONFLICT"

    row = TrustEvidence.query.one()
    assert row.source.startswith("partner:") and row.source == f"partner:{PartnerEvidenceReceipt.query.one().client_id}"
    assert row.provenance == "partner_reported" and row.evidence_type == "partner_order_completed"
    assert row.business_id == vendor.id and float(row.confidence) == 0.5
    link = PartnerSellerLink.query.one()
    assert link.vendor_id == vendor.id and link.status == "pending" and link.report_count == 1


def test_body_validation_and_seller_identifiers(client, make_user, make_vendor, auth_headers):
    vendor, store = make_vendor()
    _, api_key, secret = _partner(client, make_user, auth_headers)
    bad = _post(client, api_key, secret, {"id": "x", "type": "order.lost", "seller": {}, "order_ref": ""})
    assert bad.status_code == 400 and set(bad.get_json()["errors"]) >= {"type", "seller", "order_ref", "occurred_at"}
    no_fault = _post(client, api_key, secret, _event(store.store_slug, "dispute.resolved", fault="maybe"))
    assert "fault" in no_fault.get_json()["errors"]
    personal = _post(client, api_key, secret, _event(store.store_slug, buyer_ref="ada@example.com"))
    assert "buyer_ref" in personal.get_json()["errors"]
    future = _post(client, api_key, secret, _event(store.store_slug,
                                                    occurred_at=datetime.now(timezone.utc) + timedelta(days=1)))
    assert "occurred_at" in future.get_json()["errors"]

    # Unknown seller; a domain counts only once the seller has verified it.
    assert _post(client, api_key, secret, _event("no-such-store")).get_json()["code"] == "SELLER_NOT_FOUND"
    by_domain = {**_event(store.store_slug), "seller": {"domain": "https://www.adashop.ng/"}}
    db.session.add(OwnershipClaim(vendor_id=vendor.id, kind="domain", network="", value="adashop.ng",
                                  token="t", status="pending"))
    db.session.commit()
    assert _post(client, api_key, secret, by_domain).status_code == 404
    OwnershipClaim.query.update({"status": "verified"})
    db.session.commit()
    ok = _post(client, api_key, secret, by_domain)
    assert ok.status_code == 201 and ok.get_json()["seller"]["slug"] == store.store_slug


def test_test_key_validates_and_stores_nothing(client, make_user, make_vendor, auth_headers):
    _, store = make_vendor()
    _, api_key, secret = _partner(client, make_user, auth_headers, mode="test")
    res = _post(client, api_key, secret, _event("test-trusted-store", "dispute.resolved"))
    assert res.status_code == 200
    assert res.get_json() == {"status": "validated", "stored": False, "test": True,
                              "id": res.get_json()["id"], "type": "dispute.resolved",
                              "seller": {"slug": "test-trusted-store"}}
    # Same checks as live: signature, body, seller.
    assert _post(client, api_key, "evsec_wrong", _event("test-new-seller")).status_code == 401
    assert _post(client, api_key, secret, _event("test-new-seller", "order.lost")).status_code == 400
    assert _post(client, api_key, secret, _event(store.store_slug)).get_json()["code"] == "SELLER_NOT_FOUND"
    assert TrustEvidence.query.count() == 0
    assert PartnerEvidenceReceipt.query.count() == 0 and PartnerSellerLink.query.count() == 0


# ── consent ────────────────────────────────────────────────────────────────

def test_seller_consent_required_then_revoked(client, make_user, make_vendor, auth_headers):
    vendor, store = make_vendor()
    _history(vendor, make_user, n=6, refunds=2)
    client_id, api_key, secret = _partner(client, make_user, auth_headers)
    _report_good(client, api_key, secret, store.store_slug, 12)

    first_party = trust_v1.score_orders(trust_v1.gather_orders(vendor.id))
    pending = trust_v1.compute(vendor.id)
    assert pending["partner"]["applied"] is False and pending["partner"]["excluded"] == {"not_linked": 12}
    assert pending["score"] == first_party["score"]

    listed = client.get("/api/vendor/trust-partners", headers=auth_headers(vendor)).get_json()["platforms"]
    assert listed[0]["client_id"] == client_id and listed[0]["status"] == "pending" and listed[0]["reports"] == 12
    assert listed[0]["counts"] is False and listed[0]["platform"] == "Platform live"
    # Another seller can't accept for this one.
    other, _ = make_vendor()
    assert client.post(f"/api/vendor/trust-partners/{client_id}/accept",
                       headers=auth_headers(other)).status_code == 404

    res = client.post(f"/api/vendor/trust-partners/{client_id}/accept", headers=auth_headers(vendor))
    assert res.status_code == 200 and res.get_json()["platform"]["counts"] is True
    accepted = trust_v1.compute(vendor.id)
    assert accepted["partner"]["applied"] is True and accepted["score"] > first_party["score"]

    res = client.post(f"/api/vendor/trust-partners/{client_id}/revoke", json={"disputed": True},
                      headers=auth_headers(vendor))
    assert res.get_json()["platform"]["status"] == "revoked" and res.get_json()["platform"]["disputed"] is True
    revoked = trust_v1.compute(vendor.id)
    assert revoked["partner"]["applied"] is False and revoked["score"] == first_party["score"]
    # Reports after a revoke are still received but don't count.
    _report_good(client, api_key, secret, store.store_slug, 1, prefix="late")
    assert trust_v1.compute(vendor.id)["partner"]["excluded"] == {"not_linked": 13}


def test_partner_evidence_never_scores_a_new_seller(client, make_user, make_vendor, auth_headers):
    vendor, store = make_vendor()
    client_id, api_key, secret = _partner(client, make_user, auth_headers)
    _report_good(client, api_key, secret, store.store_slug, 30)
    evidence.set_link(vendor.id, client_id, "accept")
    r = trust_v1.compute(vendor.id)
    assert r["score"] is None and r["tier"] == "NEW" and r["partner"]["applied"] is False


# ── weight and cap ─────────────────────────────────────────────────────────

def test_partner_order_weighs_less_than_a_siiqo_order():
    p = trust_v1.score_partner([_pfact(1)], now=NOW)
    first = trust_v1.score_orders([_fact(1)], now=NOW)
    assert trust_v1.PARTNER_RULES["order_weight"] == 0.5
    one_siiqo_unit = 0.5 ** (10 / trust_v1.RULES["half_life_days"]) * (1 + __import__("math").log1p(1.0))
    assert p["units"] == pytest.approx(0.5 * one_siiqo_unit, abs=1e-3)
    assert first["counts"]["completed_orders"] == 1


def test_per_partner_cap_and_max_weight():
    P = trust_v1.PARTNER_RULES
    one = trust_v1.score_partner([_pfact(i, days=5) for i in range(200)], now=NOW)
    assert one["per_partner"]["1"]["capped"] is True
    assert one["units"] == pytest.approx(P["per_partner_cap_units"])
    assert one["weight"] == pytest.approx(P["max_weight"] * P["per_partner_cap_units"] / P["full_weight_units"])
    assert one["weight"] <= 0.05
    two = trust_v1.score_partner([_pfact(i, client_id=c, days=5) for c in (1, 2) for i in range(200)], now=NOW)
    assert two["weight"] == pytest.approx(P["max_weight"]) and two["units"] == pytest.approx(20.0)
    three = trust_v1.score_partner([_pfact(i, client_id=c, days=5) for c in (1, 2, 3) for i in range(200)], now=NOW)
    assert three["weight"] == pytest.approx(P["max_weight"])


def test_partner_moves_the_score_by_at_most_the_max_weight():
    first = [_fact(i, days=10 + 3 * i) for i in range(6)] + [_fact(50 + i, outcome="bad", days=20) for i in range(4)]
    perfect = [_pfact(i, client_id=c, days=5) for c in (1, 2, 3) for i in range(100)]
    base = trust_v1.score_orders(first, now=NOW)
    lifted = trust_v1.score_orders(first, now=NOW, partner_facts=perfect)
    assert lifted["partner"]["applied"] is True
    assert 0 < lifted["score"] - base["score"] <= 100 * trust_v1.PARTNER_RULES["max_weight"]
    assert lifted["raw_score"] == base["raw_score"]          # first-party part unchanged
    # Bad partner reports lower it, within the same bound.
    awful = [_pfact(i, client_id=c, days=5, outcome="bad", seller_fault=True) for c in (1, 2) for i in range(100)]
    good_seller = [_fact(i, days=10 + 3 * i) for i in range(10)]
    g = trust_v1.score_orders(good_seller, now=NOW)
    sunk = trust_v1.score_orders(good_seller, now=NOW, partner_facts=awful)
    assert 0 < g["score"] - sunk["score"] <= 100 * trust_v1.PARTNER_RULES["max_weight"]


def test_no_partner_evidence_changes_nothing():
    facts = [_fact(i, days=10 + 3 * i) for i in range(8)]
    a = trust_v1.score_orders(facts, now=NOW)
    b = trust_v1.score_orders(facts, now=NOW, partner_facts=[])
    assert {k: v for k, v in a.items()} == b
    assert a["partner"]["applied"] is False and a["partner"]["blended_score"] == a["raw_score"]
    assert sum(trust_v1.WEIGHTS.values()) == pytest.approx(1.0)      # partner is outside WEIGHTS


def test_dispute_cap_still_applies_after_partner_evidence():
    facts = [_fact(i, days=1 + i) for i in range(95)] + \
            [dict(_fact(500 + j, outcome="bad", days=5 + 20 * j), seller_fault=True) for j in range(5)]
    perfect = [_pfact(i, client_id=c, days=5) for c in (1, 2) for i in range(100)]
    r = trust_v1.score_orders(facts, now=NOW, partner_facts=perfect)
    assert r["dispute_cap"]["cap"] == "BRONZE" and r["tier"] == "BRONZE" and r["score"] <= 54


# ── self-dealing ───────────────────────────────────────────────────────────

def test_a_key_the_seller_owns_never_counts(client, make_user, make_vendor, auth_headers):
    vendor, store = make_vendor()
    _history(vendor, make_user)
    client_id, api_key, secret = _partner(client, make_user, auth_headers, owner=vendor)
    _report_good(client, api_key, secret, store.store_slug, 10)
    evidence.set_link(vendor.id, client_id, "accept")
    r = trust_v1.compute(vendor.id)
    assert r["partner"]["applied"] is False and r["partner"]["excluded"] == {"self_dealing": 10}
    listed = client.get("/api/vendor/trust-partners", headers=auth_headers(vendor)).get_json()["platforms"]
    assert listed[0]["own_key"] is True and listed[0]["counts"] is False


def test_a_key_owner_linked_through_the_identifier_graph_never_counts(client, make_user, make_vendor, auth_headers):
    vendor, store = make_vendor()
    _history(vendor, make_user)
    friend = make_user()
    db.session.add_all([SellerIdentifier(vendor_id=vendor.id, kind="phone", value="+2348011112222", source="t"),
                        SellerIdentifier(vendor_id=friend.id, kind="phone", value="+2348011112222", source="t")])
    db.session.commit()
    client_id, api_key, secret = _partner(client, make_user, auth_headers, owner=friend)
    _report_good(client, api_key, secret, store.store_slug, 4)
    evidence.set_link(vendor.id, client_id, "accept")
    assert trust_v1.compute(vendor.id)["partner"]["excluded"] == {"self_dealing": 4}

    # An unrelated platform's key counts.
    other_id, other_key, other_secret = _partner(client, make_user, auth_headers)
    _report_good(client, other_key, other_secret, store.store_slug, 4, prefix="o")
    evidence.set_link(vendor.id, other_id, "accept")
    assert trust_v1.compute(vendor.id)["partner"]["applied"] is True


# ── superadmin ─────────────────────────────────────────────────────────────

def test_superadmin_throttle_revoke_restore_are_audited(client, make_user, make_vendor, auth_headers):
    vendor, store = make_vendor()
    _history(vendor, make_user, refunds=2)
    client_id, api_key, secret = _partner(client, make_user, auth_headers)
    _report_good(client, api_key, secret, store.store_slug, 6)
    client.post(f"/api/vendor/trust-partners/{client_id}/revoke", json={"disputed": True}, headers=auth_headers(vendor))
    client.post(f"/api/vendor/trust-partners/{client_id}/accept", headers=auth_headers(vendor))
    normal = trust_v1.compute(vendor.id)["partner"]

    url = f"/api/admin/evidence-partners/{client_id}"
    assert client.post(f"{url}/revoke", json={"reason": "x"}, headers=auth_headers(vendor)).status_code == 403
    admin, headers = _superadmin_headers()
    listed = client.get("/api/admin/evidence-partners", headers=headers).get_json()["partners"]
    assert listed[0]["client_id"] == client_id and listed[0]["reports"] == 6
    assert listed[0]["disputed_by_sellers"] == 1 and listed[0]["accepted"] == 1 and listed[0]["status"] == "active"
    assert client.post(f"{url}/throttle", json={}, headers=headers).get_json()["code"] == "REASON_REQUIRED"
    assert client.post(f"{url}/throttle", json={"reason": "x", "factor": 2}, headers=headers).status_code == 400

    res = client.post(f"{url}/throttle", json={"reason": "Conflicts with Siiqo orders"}, headers=headers)
    assert res.status_code == 200 and res.get_json()["partner"]["status"] == "throttled"
    assert res.get_json()["partner"]["factor"] == evidence.DEFAULT_THROTTLE_FACTOR
    throttled = trust_v1.compute(vendor.id)["partner"]
    assert throttled["applied"] and throttled["units"] == pytest.approx(normal["units"] * 0.25, rel=1e-3)

    res = client.post(f"{url}/revoke", json={"reason": "Seller disputes"}, headers=headers)
    assert res.get_json()["partner"]["status"] == "revoked"
    assert trust_v1.compute(vendor.id)["partner"]["excluded"] == {"partner_revoked": 6}
    refused = _post(client, api_key, secret, _event(store.store_slug))
    assert refused.status_code == 403 and refused.get_json()["code"] == "PARTNER_REVOKED"
    assert client.get("/api/vendor/trust-partners", headers=auth_headers(vendor)).get_json()["platforms"][0][
        "suspended_by_siiqo"] is True

    client.post(f"{url}/restore", json={}, headers=headers)
    assert trust_v1.compute(vendor.id)["partner"]["applied"] is True
    actions = [l.action for l in AdminAuditLog.query.order_by(AdminAuditLog.id).all()]
    assert actions == ["EVIDENCE_PARTNER_THROTTLED", "EVIDENCE_PARTNER_REVOKED", "EVIDENCE_PARTNER_RESTORED"]
    log = AdminAuditLog.query.filter_by(action="EVIDENCE_PARTNER_REVOKED").one()
    assert log.admin_id == admin.id and log.resource_id == str(client_id)
    assert log.details["reason"] == "Seller disputes" and log.details["before"]["status"] == "throttled"


def test_conflict_with_first_party_data_is_flagged(client, make_user, make_vendor, auth_headers):
    vendor, store = make_vendor()
    _history(vendor, make_user, n=6)                        # first-party: all good
    client_id, api_key, secret = _partner(client, make_user, auth_headers)
    for i in range(6):
        assert _post(client, api_key, secret, _event(store.store_slug, "dispute.resolved", order_ref=f"d{i}",
                                                     fault="seller")).status_code == 201
    evidence.set_link(vendor.id, client_id, "accept")
    _, headers = _superadmin_headers()
    row = client.get("/api/admin/evidence-partners", headers=headers).get_json()["partners"][0]
    assert row["conflicts"] == 1


# ── the old score and the flag ─────────────────────────────────────────────

def test_old_score_and_public_passport_are_unaffected(client, make_user, make_vendor, auth_headers, monkeypatch):
    from app.services.trust import recalculate_vendor_trust
    monkeypatch.delenv("TRUST_SCORE_V1_LIVE", raising=False)
    vendor, store = make_vendor()
    _history(vendor, make_user)
    recalculate_vendor_trust(vendor.id)
    before = VendorTrustProfile.query.filter_by(vendor_id=vendor.id).one()
    old = (before.total_trust_score, before.trust_tier)
    passport_before = client.get(f"/api/passport/{store.store_slug}").get_json()["passport"]

    client_id, api_key, secret = _partner(client, make_user, auth_headers)
    _report_good(client, api_key, secret, store.store_slug, 10)
    evidence.set_link(vendor.id, client_id, "accept")
    recalculate_vendor_trust(vendor.id)
    after = VendorTrustProfile.query.filter_by(vendor_id=vendor.id).one()
    assert (after.total_trust_score, after.trust_tier) == old
    passport_after = client.get(f"/api/passport/{store.store_slug}").get_json()["passport"]
    assert (passport_after["score"], passport_after["tier"], passport_after["score_max"]) == \
        (passport_before["score"], passport_before["tier"], passport_before["score_max"])
    # First-party v1 inputs never include partner rows.
    assert all(f["order_id"] for f in trust_v1.gather_orders(vendor.id))
    assert len(trust_v1.gather_orders(vendor.id)) == 6
