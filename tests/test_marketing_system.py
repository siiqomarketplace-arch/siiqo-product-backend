"""Marketing system (docs/MARKETING_SYSTEM_PLAN.md): short codes and scans,
crediting new sellers, milestones, per-sale agent earnings with the hold, fraud flags,
the agent portal, the admin board and the seller growth kit."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from flask_jwt_extended import create_access_token

import app.routes.auth as auth_routes
from app.extensions import db
from app.models.admin import AdminUser
from app.models.audit import AdminAuditLog
from app.models.communication import Notification
from app.models.marketing import (AgentEarning, AgentProfile, MarketingCampaign, MarketingCode,
                                  MarketingScan, SellerAcquisition)
from app.models.payment_link import PaymentLink
from app.models.telemetry import PlatformEvent
from app.models.user import User
from app.models.withdrawal import VendorBankAccount
from app.services import marketing as svc


# ── helpers ──────────────────────────────────────────────────────────────────

def _admin(role="SUPERADMIN"):
    admin = AdminUser(name="Ops", email=f"ops-{uuid.uuid4().hex[:6]}@example.com", role=role)
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _code(kind="flyer", destination="/sell", code=None, **kw):
    row = svc.create_code(kind, destination, code=code, **kw)
    db.session.commit()
    return row


def _apply(client, headers, **over):
    body = {"full_name": "Amaka Obi", "phone": "08031234567", "city": "Abuja", "agent_type": "field",
            "team": "Wuse", "bank_name": "Test Bank", "bank_code": "058", "account_number": "0123456789",
            "account_name": "Amaka Obi", "agree_conduct": True, **over}
    return client.post("/api/agents/apply", json=body, headers=headers)


@pytest.fixture
def agent(client, make_user, auth_headers):
    """An approved agent: (user, profile, code)."""
    def _make(full_name="Amaka Obi", account_number="0123456789", phone="08031234567", team="Wuse"):
        user = make_user()
        assert _apply(client, auth_headers(user), full_name=full_name, account_number=account_number,
                      phone=phone, team=team).status_code == 201
        profile = AgentProfile.query.filter_by(user_id=user.id).first()
        res = client.post(f"/api/admin/agents/{profile.id}/approve", headers=_admin())
        assert res.status_code == 200, res.get_json()
        db.session.expire_all()
        profile = AgentProfile.query.filter_by(user_id=user.id).first()
        return user, profile, profile.code
    return _make


def _set_up(vendor, store, make_product, products=5, account_number="9876543210", phone=None):
    store.is_published = True
    store.city = "Abuja"
    if phone:
        store.phone = phone
    db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Test Bank", bank_code="058",
                                     account_number=account_number, account_name="Seller"))
    db.session.commit()
    for _ in range(products):
        make_product(store)


def _pay(order):
    order.status = "PAID"
    db.session.commit()
    return order


def _acq(user):
    db.session.expire_all()
    return SellerAcquisition.query.filter_by(seller_user_id=user.id).first()


# ── 1. resolve, scans, destinations ─────────────────────────────────────────

def test_resolve_counts_scans_once_per_session_and_refuses_inactive(client):
    campaign = MarketingCampaign(name="Wuse flyers", slug="wuse-flyers", channel="flyer")
    db.session.add(campaign)
    db.session.commit()
    code = _code("flyer", "/sell", code="fly-wuse1", campaign_id=campaign.id)
    assert code.code == "FLY-WUSE1"

    res = client.get("/api/public/r/fly-wuse1?sid=visitor-1")
    assert res.status_code == 200
    body = res.get_json()
    assert body == {"code": "FLY-WUSE1", "kind": "flyer", "label": None, "destination": "/sell",
                    "campaign": "wuse-flyers", "ref": "FLY-WUSE1",
                    "utm": {"source": "flyer", "medium": "qr", "campaign": "wuse-flyers"}}
    client.get("/api/public/r/FLY-WUSE1?sid=visitor-1")      # same visitor, within 30 min
    client.get("/api/public/r/FLY-WUSE1?sid=visitor-2")
    client.get("/api/public/r/FLY-WUSE1")                    # no session id: always counted
    db.session.expire_all()
    assert db.session.get(MarketingCode, code.id).scan_count == 3
    assert MarketingScan.query.filter_by(code_id=code.id).count() == 3
    assert MarketingScan.query.filter_by(campaign_id=campaign.id).count() == 3
    assert PlatformEvent.query.filter_by(event_name="funnel.code_scanned").count() == 3

    plain = _code("location", "/sell", code="WUSE")
    assert client.get("/api/public/r/wuse").get_json()["utm"]["campaign"] == "wuse"

    assert client.get("/api/public/r/NOPE99").get_json()["code"] == "CODE_NOT_FOUND"
    assert client.get("/api/public/r/NOPE99").status_code == 404

    plain.active = False
    db.session.commit()
    res = client.get("/api/public/r/WUSE")
    assert res.status_code == 410 and res.get_json() == {
        "code": "CODE_INACTIVE", "message": "This code is no longer in use.", "destination": "/"}

    campaign.status = "PAUSED"
    db.session.commit()
    assert client.get("/api/public/r/FLY-WUSE1").status_code == 410
    db.session.expire_all()
    assert db.session.get(MarketingCode, code.id).scan_count == 3   # inactive scans are not counted


def test_destination_must_be_a_siiqo_path(client, make_user, auth_headers):
    for bad in ("https://evil.com", "//evil.com", "/\\evil.com", "evil.com", "", "javascript:alert(1)"):
        with pytest.raises(svc.MarketingError) as err:
            svc.validate_destination(bad)
        assert err.value.code == "INVALID_DESTINATION"
    assert svc.validate_destination("/id/my-shop?x=1") == "/id/my-shop?x=1"

    headers = _admin()
    for bad in ("https://evil.com", "//evil.com"):
        res = client.post("/api/admin/marketing/codes", json={"kind": "flyer", "destination": bad}, headers=headers)
        assert res.status_code == 400 and res.get_json()["field"] == "destination"

    res = client.post("/api/admin/marketing/codes", json={"kind": "flyer", "destination": "/sell"}, headers=headers)
    assert res.status_code == 201
    made = res.get_json()["code"]
    assert len(made["code"]) == 6 and not set(made["code"]) & set("0O1I")
    assert made["link"] == f"https://siiqo.com/r/{made['code']}"

    res = client.post("/api/admin/marketing/codes", json={"kind": "flyer", "destination": "/sell", "code": "radio"},
                      headers=headers)
    assert res.status_code == 201
    res = client.post("/api/admin/marketing/codes", json={"kind": "media", "destination": "/check", "code": "RADIO"},
                      headers=headers)
    assert res.status_code == 409 and res.get_json()["code"] == "CODE_TAKEN"
    assert client.post("/api/admin/marketing/codes", json={"kind": "flyer", "destination": "/sell", "code": "a!"},
                       headers=headers).status_code == 400

    code_id = made["id"]
    res = client.patch(f"/api/admin/marketing/codes/{code_id}", json={"destination": "//evil.com"}, headers=headers)
    assert res.status_code == 400
    res = client.patch(f"/api/admin/marketing/codes/{code_id}", json={"label": "Batch 1", "active": False},
                       headers=headers)
    assert res.status_code == 200 and res.get_json()["code"]["active"] is False
    listed = client.get("/api/admin/marketing/codes?q=batch", headers=headers).get_json()
    assert listed["total"] == 1 and listed["codes"][0]["label"] == "Batch 1"
    assert AdminAuditLog.query.filter(AdminAuditLog.action.like("MARKETING_CODE_%")).count() == 3

    # A normal user token can't use admin routes.
    user_headers = auth_headers(make_user())
    for method, path in (("get", "/api/admin/marketing/codes"), ("get", "/api/admin/marketing/board"),
                         ("get", "/api/admin/agents"), ("get", "/api/admin/marketing/earnings"),
                         ("get", "/api/admin/marketing/flags"), ("get", "/api/admin/marketing/campaigns")):
        assert getattr(client, method)(path, headers=user_headers).status_code == 403
    assert client.post("/api/admin/marketing/codes", json={"kind": "flyer", "destination": "/sell"},
                       headers=user_headers).status_code == 403


# ── 2. crediting a new seller ───────────────────────────────────────────────

def test_signup_with_ref_code_credits_once_first_touch_wins(client, monkeypatch, make_user, agent):
    from app.middleware import bot_protection
    bot_protection.registration_attempts.clear()
    monkeypatch.setattr(auth_routes, "send_siiqo_email", lambda *a, **k: None)
    agent_user, profile, agent_code = agent()
    other = _code("flyer", "/sell", code="FLY-TWO")

    res = client.post("/api/auth/register", json={"email": "seller@example.com", "password": "a-long-password-1",
                                                  "first_name": "Ngozi", "ref_code": agent_code.code.lower()})
    assert res.status_code == 201, res.get_json()
    seller = User.query.filter_by(email="seller@example.com").first()
    acq = _acq(seller)
    assert acq.code_id == agent_code.id and acq.agent_user_id == agent_user.id and acq.flags == []

    # First touch wins: a later code never overwrites.
    assert svc.credit_signup(seller, {"attribution": {"ref": "fly-two"}}) is None
    db.session.commit()
    assert SellerAcquisition.query.filter_by(seller_user_id=seller.id).count() == 1
    assert _acq(seller).code_id == agent_code.id

    # attribution.ref works too; a flyer has no agent.
    buyer = make_user()
    assert svc.credit_signup(buyer, {"attribution": {"ref": "fly-two", "source": "flyer"}}) is not None
    db.session.commit()
    assert _acq(buyer).code_id == other.id and _acq(buyer).agent_user_id is None

    # Too old for the attribution window: no credit.
    late = make_user()
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    assert svc.credit_signup(late, {"ref_code": "FLY-TWO", "ref_at": old}) is None

    # No self-credit: the agent can't credit themselves.
    assert svc.credit_signup(agent_user, {"ref_code": agent_code.code}) is None
    db.session.commit()
    assert _acq(agent_user) is None

    # An inactive code credits nobody.
    other.active = False
    db.session.commit()
    assert svc.credit_signup(make_user(), {"ref_code": "FLY-TWO"}) is None


def test_store_setup_paths_accept_ref_code(make_user, make_vendor, agent):
    _, _, agent_code = agent()
    vendor, _ = make_vendor()
    from app.services.marketing import credit_signup
    assert credit_signup(vendor, {"attribution": '{"ref": "%s"}' % agent_code.code}) is not None  # form JSON string
    db.session.commit()
    assert _acq(vendor).code_id == agent_code.id


# ── 3. milestones and earnings ──────────────────────────────────────────────

def _credited(agent, make_vendor):
    agent_user, _, agent_code = agent()
    vendor, store = make_vendor()
    svc.credit_signup(vendor, {"ref_code": agent_code.code})
    db.session.commit()
    return agent_user, vendor, store


def _sales():
    db.session.expire_all()
    return AgentEarning.query.filter_by(kind="sale").order_by(AgentEarning.id).all()


def _live():
    return [e for e in _sales() if e.status != "REVERSED"]


def test_no_reward_for_signup_or_setup_then_200_for_each_of_5_sales(make_user, make_vendor, make_product,
                                                                     make_order, agent):
    agent_user, vendor, store = _credited(agent, make_vendor)
    assert AgentEarning.query.count() == 0                                # signup: nothing

    _set_up(vendor, store, make_product, products=4)
    assert _acq(vendor).setup_at is None
    make_product(store)                                                   # 5th product: set up
    acq = _acq(vendor)
    assert acq.setup_at is not None and acq.first_paid_order_at is None
    assert AgentEarning.query.count() == 0                                # set-up: nothing

    db.session.add(PaymentLink(vendor_id=vendor.id, title="Ankara", slug=f"pl-{uuid.uuid4().hex[:6]}"))
    db.session.commit()
    assert _acq(vendor).pay_link_at is not None and AgentEarning.query.count() == 0

    # The seller buying from themselves never earns, and is flagged.
    _pay(make_order(vendor, buyer=vendor))
    acq = _acq(vendor)
    assert acq.first_paid_order_at is None and "self_purchase" in acq.flags
    # Nor does a guest order using the agent's phone number, or a free order.
    _pay(make_order(vendor, buyer=None, buyer_phone="+234 803 123 4567"))
    _pay(make_order(vendor, buyer=make_user(), payment_method="FREE"))
    assert _acq(vendor).first_paid_order_at is None and AgentEarning.query.count() == 0

    orders = [_pay(make_order(vendor, buyer=make_user())) for _ in range(6)]
    acq = _acq(vendor)
    assert acq.first_paid_order_id == orders[0].id and acq.first_paid_order_at is not None
    sales = _sales()
    assert [e.sale_number for e in sales] == [1, 2, 3, 4, 5]               # nothing for the 6th
    assert [e.order_id for e in sales] == [o.id for o in orders[:5]]
    assert all(e.status == "HELD" and float(e.amount_ngn) == 200 and e.agent_user_id == agent_user.id
               for e in sales)
    assert svc._aware(sales[0].payable_at) - svc._aware(sales[0].earned_at) == timedelta(days=7)

    # Idempotent: more orders and recomputes add nothing and move nothing.
    stamped = (acq.setup_at, acq.pay_link_at, acq.first_paid_order_at)
    _pay(make_order(vendor, buyer=make_user()))
    svc.recompute_for(vendor.id)
    db.session.commit()
    acq = _acq(vendor)
    assert (acq.setup_at, acq.pay_link_at, acq.first_paid_order_at) == stamped
    assert AgentEarning.query.count() == 5

    # HELD -> PAYABLE only after the hold.
    svc.run_marketing_sweep()
    assert {e.status for e in _sales()} == {"HELD"}
    result = svc.run_marketing_sweep(now=datetime.now(timezone.utc) + timedelta(days=8))
    assert result["payable"] == 5
    assert {e.status for e in _sales()} == {"PAYABLE"}


def test_each_rewarded_sale_needs_a_different_buyer(monkeypatch, make_user, make_vendor, make_order, agent):
    _, vendor, _ = _credited(agent, make_vendor)
    buyer = make_user()
    _pay(make_order(vendor, buyer=buyer))
    _pay(make_order(vendor, buyer=buyer))                                 # same buyer again
    assert len(_sales()) == 1
    # Guests are told apart by email.
    for email in ("ada@example.com", "ADA@example.com "):
        guest_order = make_order(vendor, buyer=None)
        guest_order.buyer_email = email
        _pay(guest_order)
    assert len(_sales()) == 2

    monkeypatch.setenv("AGENT_SALES_DISTINCT_BUYERS", "false")
    svc.recompute_for(vendor.id)
    db.session.commit()
    assert [e.sale_number for e in _sales()] == [1, 2, 3, 4]               # repeat orders now count


def test_refund_before_payout_reverses_and_a_later_sale_fills_the_slot(make_user, make_vendor, make_order, agent):
    _, vendor, _ = _credited(agent, make_vendor)
    orders = [_pay(make_order(vendor, buyer=make_user())) for _ in range(5)]
    assert len(_live()) == 5

    orders[1].status = "REFUNDED"
    orders[3].status = "DISPUTED"
    db.session.commit()
    sales = {e.order_id: e for e in _sales()}
    assert sales[orders[1].id].status == "REVERSED" and sales[orders[1].id].reversed_reason == "Order refunded"
    assert sales[orders[3].id].status == "REVERSED" and sales[orders[3].id].reversed_reason == "Order disputed"
    assert len(_live()) == 3

    # Later sales from new buyers take the free slots, never more than 5 standing.
    later = [_pay(make_order(vendor, buyer=make_user())) for _ in range(3)]
    live = _live()
    assert len(live) == 5 and sorted(e.sale_number for e in live) == [1, 2, 3, 4, 5]
    assert {e.order_id for e in live} == {orders[0].id, orders[2].id, orders[4].id, later[0].id, later[1].id}
    assert len(_sales()) == 7                                             # reversed rows stay as history

    # A cancelled order found by the hourly job is reversed too.
    later[0].status = "CANCELLED"
    db.session.commit()
    svc.run_marketing_sweep(now=datetime.now(timezone.utc) + timedelta(days=8))
    reversed_row = next(e for e in _sales() if e.order_id == later[0].id)
    assert reversed_row.status == "REVERSED" and reversed_row.reversed_reason == "Order cancelled"
    live = _live()
    assert len(live) == 5 and later[2].id in {e.order_id for e in live}


def test_paid_reward_then_refund_is_flagged_not_reversed(make_user, make_vendor, make_order, agent):
    _, vendor, _ = _credited(agent, make_vendor)
    order = _pay(make_order(vendor, buyer=make_user()))
    earning = _sales()[0]
    earning.status = "PAID"
    earning.paid_reference = "TRF-9"
    db.session.commit()

    order.status = "REFUNDED"
    db.session.commit()
    svc.run_marketing_sweep(now=datetime.now(timezone.utc) + timedelta(days=8))
    assert _sales()[0].status == "PAID"
    assert "paid_then_refunded" in _acq(vendor).flags


def test_paused_agent_accrues_nothing_new(client, make_user, make_vendor, make_order, agent):
    _, vendor, _ = _credited(agent, make_vendor)
    _pay(make_order(vendor, buyer=make_user()))
    profile = AgentProfile.query.filter_by(user_id=_acq(vendor).agent_user_id).first()
    assert client.post(f"/api/admin/agents/{profile.id}/pause", headers=_admin()).status_code == 200
    _pay(make_order(vendor, buyer=make_user()))
    assert len(_sales()) == 1


def test_duplicate_bank_or_phone_stops_the_reward(client, make_user, make_vendor, make_product, make_order, agent):
    _, _, agent_code = agent()
    first, first_store = make_vendor()
    svc.credit_signup(first, {"ref_code": agent_code.code})
    db.session.commit()
    _set_up(first, first_store, make_product, account_number="1112223334", phone="08051112222")
    _pay(make_order(first, buyer=make_user()))
    assert AgentEarning.query.count() == 1

    again, again_store = make_vendor()                        # same bank account
    svc.credit_signup(again, {"ref_code": agent_code.code})
    db.session.commit()
    _set_up(again, again_store, make_product, account_number="1112223334")
    _pay(make_order(again, buyer=make_user()))
    acq = _acq(again)
    assert acq.setup_at is not None and "duplicate_bank" in acq.flags
    assert AgentEarning.query.count() == 1

    third, third_store = make_vendor()                        # same phone
    svc.credit_signup(third, {"ref_code": agent_code.code})
    db.session.commit()
    _set_up(third, third_store, make_product, account_number="5556667778", phone="+2348051112222")
    _pay(make_order(third, buyer=make_user()))
    assert "duplicate_phone" in _acq(third).flags
    assert AgentEarning.query.count() == 1

    shared, shared_store = make_vendor()                      # the agent's own bank account
    svc.credit_signup(shared, {"ref_code": agent_code.code})
    db.session.commit()
    _set_up(shared, shared_store, make_product, account_number="0123456789")
    _pay(make_order(shared, buyer=make_user()))
    assert "shared_bank_with_agent" in _acq(shared).flags
    assert AgentEarning.query.count() == 1

    res_flags = {f["acquisition_id"] for f in
                 client.get("/api/admin/marketing/flags", headers=_admin()).get_json()["flags"]}
    assert res_flags == {_acq(again).id, _acq(third).id, _acq(shared).id}


# ── 4. agent portal and agent admin ─────────────────────────────────────────

def test_agent_apply_approve_pause_and_privacy(client, make_user, make_vendor, make_product, make_order,
                                               auth_headers, agent):
    user = make_user()
    headers = auth_headers(user)
    assert client.get("/api/agents/me", headers=headers).get_json()["code"] == "NOT_AN_AGENT"
    bad = _apply(client, headers, account_number="123")
    assert bad.status_code == 400 and bad.get_json()["field"] == "account_number"
    assert _apply(client, headers, agree_conduct=False).get_json()["field"] == "agree_conduct"
    assert _apply(client, headers, agent_type="boss").get_json()["field"] == "agent_type"
    res = _apply(client, headers, full_name="Tolu Ade")
    assert res.status_code == 201 and res.get_json() == {"status": "PENDING"}
    again = _apply(client, headers)
    assert again.status_code == 409 and again.get_json()["code"] == "ALREADY_APPLIED"
    me = client.get("/api/agents/me", headers=headers).get_json()
    assert me["profile"]["status"] == "PENDING" and me["profile"]["link"] is None
    assert me["rates"] == {"per_sale": 200, "rewarded_sales": 5, "max_per_seller": 1000, "hold_days": 7,
                           "min_products": 5}

    profile = AgentProfile.query.filter_by(user_id=user.id).first()
    assert client.post(f"/api/admin/agents/{profile.id}/approve", headers=auth_headers(make_user())).status_code == 403
    admin = _admin(role="ADMIN")
    res = client.post(f"/api/admin/agents/{profile.id}/approve", headers=admin)
    assert res.status_code == 200
    code = res.get_json()["agent"]["code"]
    assert code.startswith("TOLU")
    me = client.get("/api/agents/me", headers=headers).get_json()
    assert me["profile"]["status"] == "APPROVED" and me["profile"]["link"] == f"https://siiqo.com/r/{code}"
    assert Notification.query.filter_by(user_id=user.id).count() == 1
    assert db.session.get(MarketingCode, profile.code_id).destination == "/sell"

    # Their seller, scans and stats.
    client.get(f"/api/public/r/{code}")
    vendor, store = make_vendor()
    svc.credit_signup(vendor, {"ref_code": code})
    db.session.commit()
    _set_up(vendor, store, make_product)
    _pay(make_order(vendor, buyer=make_user()))
    me = client.get("/api/agents/me", headers=headers).get_json()
    assert me["today"] == {"scans": 1, "signups": 1, "setups": 1, "first_sales": 1, "rewarded_sales": 1} \
        == me["totals"]
    assert me["earnings"] == {"held": 200.0, "payable": 0.0, "paid": 0.0}
    sellers = client.get("/api/agents/me/sellers", headers=headers).get_json()
    assert sellers["total"] == 1 and sellers["pages"] == 1
    row = sellers["sellers"][0]
    assert row["seller_id"] == vendor.id and row["status"] == "first_sale"
    assert (row["rewarded_sales"], row["rewarded_sales_max"], row["earned"]) == (1, 5, 200.0)
    assert row["steps"] == {"signed_up": True, "products": {"have": 5, "need": 5}, "place": True, "bank": True,
                            "pay_link": False, "first_sale": True}
    _pay(make_order(vendor, buyer=make_user()))
    row = client.get("/api/agents/me/sellers", headers=headers).get_json()["sellers"][0]
    assert row["status"] == "active" and row["rewarded_sales"] == 2 and row["earned"] == 400.0
    earnings = client.get("/api/agents/me/earnings", headers=headers).get_json()
    assert earnings["total"] == 2 and earnings["totals"]["held"] == 400.0
    assert set(earnings["earnings"][0]) == {"id", "kind", "sale_number", "order_id", "amount", "status",
                                            "store_name", "earned_at", "payable_at", "paid_at", "reference"}
    assert [(e["kind"], e["sale_number"]) for e in earnings["earnings"]] == [("sale", 2), ("sale", 1)]

    # Another agent sees only their own sellers.
    other_user, _, _ = agent(full_name="Bola Musa", account_number="2223334445", phone="08070000000", team="Garki")
    other_headers = auth_headers(other_user)
    assert client.get("/api/agents/me/sellers", headers=other_headers).get_json()["total"] == 0
    board = client.get("/api/agents/leaderboard?period=week", headers=other_headers).get_json()
    assert board["period"] == "week"
    assert board["agents"] == [{"rank": 1, "name": "Tolu A.", "team": "Wuse", "setups": 1, "first_sales": 1,
                                "is_me": False}]
    assert board["teams"] == [{"team": "Wuse", "setups": 1, "first_sales": 1}]

    # Paused: new sellers earn nothing and are not credited to the agent.
    assert client.post(f"/api/admin/agents/{profile.id}/pause", headers=admin).status_code == 200
    assert client.post(f"/api/admin/agents/{profile.id}/pause", headers=admin).status_code == 409
    later, _ = make_vendor()
    svc.credit_signup(later, {"ref_code": code})
    db.session.commit()
    assert _acq(later).agent_user_id is None
    assert client.post(f"/api/admin/agents/{profile.id}/resume", headers=admin).status_code == 200
    assert Notification.query.filter_by(user_id=user.id).count() == 3
    assert AdminAuditLog.query.filter(AdminAuditLog.action.like("AGENT_%")).count() >= 3

    listed = client.get("/api/admin/agents?q=tolu", headers=admin).get_json()
    assert listed["total"] == 1 and listed["agents"][0]["first_sales"] == 1
    assert listed["agents"][0]["rewarded_sales"] == 2
    detail = client.get(f"/api/admin/agents/{profile.id}", headers=admin).get_json()
    assert detail["agent"]["account_number"] == "0123456789" and len(detail["sellers"]) == 1


def test_mark_paid_and_reverse_need_a_superadmin(client, make_user, make_vendor, agent):
    agent_user, _, agent_code = agent()
    vendor, _ = make_vendor()
    svc.credit_signup(vendor, {"ref_code": agent_code.code})
    now = datetime.now(timezone.utc)
    rows = [AgentEarning(agent_user_id=agent_user.id, seller_user_id=vendor.id, kind="sale", amount_ngn=a,
                         status=s, sale_number=n, earned_at=now, payable_at=now)
            for n, a, s in ((1, 200, "PAYABLE"), (2, 200, "HELD"))]
    db.session.add_all(rows)
    db.session.commit()
    ids = [r.id for r in rows]

    plain_admin = _admin(role="ADMIN")
    assert client.post("/api/admin/marketing/earnings/mark-paid", json={"ids": ids, "reference": "TRF-1"},
                       headers=plain_admin).status_code == 403
    listed = client.get(f"/api/admin/marketing/earnings?agent_user_id={agent_user.id}", headers=plain_admin).get_json()
    assert listed["total"] == 2 and listed["totals"] == {"held": 200.0, "payable": 200.0, "paid": 0.0}
    assert {e["sale_number"] for e in listed["earnings"]} == {1, 2}
    assert listed["earnings"][0]["agent"]["account_number"] == "0123456789"

    boss = _admin()
    res = client.post("/api/admin/marketing/earnings/mark-paid", json={"ids": ids, "reference": "TRF-1"}, headers=boss)
    assert res.get_json() == {"paid": 1, "amount": 200.0, "warnings": []}   # only the PAYABLE row
    assert client.post(f"/api/admin/marketing/earnings/{ids[1]}/reverse", json={"reason": "x"},
                       headers=plain_admin).status_code == 403
    res = client.post(f"/api/admin/marketing/earnings/{ids[1]}/reverse", json={"reason": "Fake store"}, headers=boss)
    assert res.status_code == 200 and res.get_json()["earning"]["status"] == "REVERSED"
    assert client.post(f"/api/admin/marketing/earnings/{ids[0]}/reverse", json={"reason": "late"},
                       headers=boss).status_code == 409                 # already paid


# ── 5. board ─────────────────────────────────────────────────────────────────

def test_board_numbers_for_a_small_scenario(client, make_user, make_vendor, make_order, agent):
    headers = _admin()
    res = client.post("/api/admin/marketing/campaigns", json={"name": "Wuse Market", "channel": "agent",
                                                              "location": "Wuse", "budget_ngn": 50000},
                      headers=headers)
    assert res.status_code == 201
    campaign = res.get_json()["campaign"]
    assert campaign["slug"] == "wuse-market" and campaign["status"] == "ACTIVE"
    assert client.patch(f"/api/admin/marketing/campaigns/{campaign['id']}", json={"spend_ngn": 6000},
                        headers=headers).get_json()["campaign"]["spend_ngn"] == 6000.0
    assert client.get("/api/admin/marketing/campaigns?status=ACTIVE", headers=headers).get_json()["total"] == 1

    agent_user, profile, agent_code = agent()
    agent_code.campaign_id = campaign["id"]
    db.session.commit()
    for sid in ("a", "b", "c"):
        client.get(f"/api/public/r/{agent_code.code}?sid={sid}")

    sellers = [make_vendor()[0] for _ in range(2)]
    for s in sellers:
        svc.credit_signup(s, {"ref_code": agent_code.code})
    db.session.commit()
    _pay(make_order(sellers[0], buyer=make_user(), total=10000))       # first sale for seller 1

    # A buyer order that came through the code, and one through the campaign tag.
    _pay(make_order(sellers[1], buyer=make_user(), total=5000, ref_code=agent_code.code))
    _pay(make_order(sellers[1], buyer=make_user(), total=2500, campaign="wuse-market"))
    _pay(make_order(sellers[1], buyer=make_user(), total=999))          # untracked

    board = client.get("/api/admin/marketing/board", headers=headers).get_json()
    totals = board["totals"]
    assert totals["scans"] == 3 and totals["signups"] == 2
    # Seller 2's tracked orders are first sales too (separate buyers).
    assert totals["first_sales"] == 2 and totals["orders"] == 2 and totals["gmv_ngn"] == 7500.0
    assert totals["spend_ngn"] == 6000.0 and totals["cost_per_active_seller"] == 3000.0
    row = next(c for c in board["campaigns"] if c["id"] == campaign["id"])
    assert (row["scans"], row["signups"], row["first_sales"], row["orders"], row["gmv_ngn"]) == (3, 2, 2, 2, 7500.0)
    assert row["cost_per_active_seller"] == 3000.0
    code_row = next(c for c in board["codes"] if c["code"] == agent_code.code)
    assert (code_row["orders"], code_row["gmv_ngn"], code_row["campaign"]) == (1, 5000.0, "Wuse Market")
    agent_row = next(a for a in board["agents"] if a["agent_user_id"] == agent_user.id)
    assert agent_row["signups"] == 2 and agent_row["first_sales"] == 2
    assert agent_row["rewarded_sales"] == 4 and agent_row["earned_ngn"] == 800.0     # 4 sales x 200

    old = client.get("/api/admin/marketing/board?from=2020-01-01&to=2020-01-31", headers=headers).get_json()
    assert old["range"] == {"from": "2020-01-01", "to": "2020-01-31"} and old["totals"]["signups"] == 0
    # Spend is all-time, so its cost per active seller does not depend on the dates picked.
    assert old["totals"]["cost_per_active_seller"] == 3000.0 and old["totals"]["spend_basis"] == "all_time"


# ── 6. seller growth kit ────────────────────────────────────────────────────

def test_growth_kit_creates_codes_once(client, make_user, make_vendor, make_order, auth_headers):
    vendor, store = make_vendor()
    headers = auth_headers(vendor)
    first = client.get("/api/vendor/growth-kit", headers=headers).get_json()
    second = client.get("/api/vendor/growth-kit", headers=headers).get_json()
    assert first["sticker"] == second["sticker"] and first["invite"] == second["invite"]
    assert MarketingCode.query.filter_by(owner_user_id=vendor.id).count() == 2
    sticker = MarketingCode.query.filter_by(owner_user_id=vendor.id, kind="seller_sticker").one()
    invite = MarketingCode.query.filter_by(owner_user_id=vendor.id, kind="seller_invite").one()
    assert sticker.destination == f"/id/{store.store_slug}" and invite.destination == "/sell"
    assert first["sticker"]["link"] == f"https://siiqo.com/r/{sticker.code}"
    assert first["passport_url"] == f"https://siiqo.com/id/{store.store_slug}"

    client.get(f"/api/public/r/{sticker.code}")
    client.get(f"/api/public/r/{invite.code}")
    invited, _ = make_vendor()
    acq = svc.credit_signup(invited, {"ref_code": invite.code})
    assert acq is not None and acq.agent_user_id is None               # invites earn nothing yet
    db.session.commit()
    assert svc.credit_signup(make_user(), {"ref_code": sticker.code}) is None   # stickers credit nobody
    _pay(make_order(invited, buyer=make_user()))
    _pay(make_order(vendor, buyer=make_user(), source="pay_link"))

    results = client.get("/api/vendor/growth-kit", headers=headers).get_json()["results"]
    assert results == {"sticker_scans": 1, "invite_scans": 1, "invite_signups": 1, "invite_first_sales": 1,
                       "orders_by_source": [{"source": "pay_link", "orders": 1}]}
    assert AgentEarning.query.count() == 0

    buyer_headers = auth_headers(make_user())
    assert client.get("/api/vendor/growth-kit", headers=buyer_headers).get_json()["code"] == "NO_STORE"
