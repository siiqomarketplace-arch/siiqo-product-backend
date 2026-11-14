"""Becoming an agent (docs/AGENT_ACCOUNTS_PLAN.md): one Siiqo account per person,
teams and read-only team leads, logistics companies with rider codes, the
pre-filled form, one agent per phone and bank account, the admin approval
checklist, the payout name warning, EMAIL_EXISTS on sign-up and partner
logins on the agent portal."""
import uuid

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.audit import AdminAuditLog
from app.models.marketing import AgentEarning, AgentProfile, AgentTeam, MarketingCode, SellerAcquisition
from app.models.partnerships import PartnerApplication
from app.models.user import User, UserRole
from app.models.withdrawal import VendorBankAccount
from app.services import marketing as svc


def _admin(role="SUPERADMIN"):
    admin = AdminUser(name="Ops", email=f"ops-{uuid.uuid4().hex[:6]}@example.com", role=role)
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _apply(client, headers, **over):
    body = {"full_name": "Amaka Obi", "phone": "08031234567", "city": "Abuja", "agent_type": "field",
            "team": "Wuse", "bank_name": "Test Bank", "bank_code": "058", "account_number": "0123456789",
            "account_name": "Amaka Obi", "agree_conduct": True, **over}
    return client.post("/api/agents/apply", json=body, headers=headers)


@pytest.fixture
def approved(client, make_user, auth_headers):
    """Apply and approve: returns (user, profile, headers)."""
    def _make(admin_headers=None, user=None, **over):
        user = user or make_user()
        headers = auth_headers(user)
        res = _apply(client, headers, **over)
        assert res.status_code == 201, res.get_json()
        profile = AgentProfile.query.filter_by(user_id=user.id).first()
        res = client.post(f"/api/admin/agents/{profile.id}/approve", headers=admin_headers or _admin())
        assert res.status_code == 200, res.get_json()
        db.session.expire_all()
        return user, AgentProfile.query.filter_by(user_id=user.id).first(), headers
    return _make


def _team(client, headers, **over):
    body = {"name": "Wuse Market", "kind": "market", "state": "FCT", "city": "Abuja", **over}
    res = client.post("/api/admin/marketing/teams", json=body, headers=headers)
    assert res.status_code == 201, res.get_json()
    return res.get_json()["team"]


# ── B1: teams ────────────────────────────────────────────────────────────────

def test_teams_crud_is_admin_only_and_audited(client, make_user, auth_headers):
    admin = _admin()
    team = _team(client, admin)
    assert team["state"] == "FCT" and team["active"] is True and team["lead"] is None
    _team(client, admin, name="UniLag", kind="campus", state="lagos", city="Akoka")

    bad = client.post("/api/admin/marketing/teams", json={"name": "X", "kind": "market", "state": "Atlantis"},
                      headers=admin)
    assert bad.status_code == 400 and bad.get_json()["field"] == "state"
    bad = client.post("/api/admin/marketing/teams", json={"name": "X", "kind": "boss", "state": "FCT"},
                      headers=admin)
    assert bad.get_json()["field"] == "kind"

    res = client.patch(f"/api/admin/marketing/teams/{team['id']}", json={"active": False, "city": "Wuse 2"},
                       headers=admin)
    assert res.status_code == 200 and res.get_json()["team"]["active"] is False
    assert len(client.get("/api/admin/marketing/teams", headers=admin).get_json()["teams"]) == 2
    assert AdminAuditLog.query.filter(AdminAuditLog.action.like("AGENT_TEAM_%")).count() == 3

    # Normal users: no admin team routes; the form only lists active teams.
    user_headers = auth_headers(make_user())
    assert client.get("/api/admin/marketing/teams", headers=user_headers).status_code == 403
    assert client.post("/api/admin/marketing/teams", json={"name": "Y", "kind": "area", "state": "FCT"},
                       headers=user_headers).status_code == 403
    listed = client.get("/api/agents/teams", headers=user_headers).get_json()["teams"]
    assert [t["name"] for t in listed] == ["UniLag"]
    assert client.get("/api/agents/teams?state=Lagos", headers=user_headers).get_json()["teams"][0]["city"] == "Akoka"
    assert client.get("/api/agents/teams?state=FCT", headers=user_headers).get_json()["teams"] == []
    assert client.get("/api/agents/teams").status_code == 401


def test_apply_with_team_state_and_somewhere_else(client, make_user, auth_headers):
    admin = _admin()
    team = _team(client, admin)
    user = make_user()
    res = _apply(client, auth_headers(user), team_id=team["id"], city="", state="", team=None)
    assert res.status_code == 201, res.get_json()
    p = AgentProfile.query.filter_by(user_id=user.id).first()
    assert (p.team_id, p.team, p.state, p.city) == (team["id"], "Wuse Market", "FCT", "Abuja")

    other = make_user()
    res = _apply(client, auth_headers(other), phone="08055555555", account_number="5555555555",
                 state="Lagos", city="Yaba", team="Tejuosho")
    assert res.status_code == 201
    p = AgentProfile.query.filter_by(user_id=other.id).first()
    assert (p.team_id, p.team, p.state) == (None, "Tejuosho", "Lagos")
    assert _apply(client, auth_headers(make_user()), phone="08066666666", account_number="6666666666",
                  team_id=99999).get_json()["field"] == "team_id"


# ── B1/B2: team leads ────────────────────────────────────────────────────────

def test_team_lead_sees_team_read_only_and_cannot_approve(client, make_user, auth_headers, approved):
    admin = _admin()
    team = _team(client, admin)
    lead_user, lead, lead_headers = approved(admin, team_id=team["id"], full_name="Bola Musa")
    _, member, _ = approved(admin, team_id=team["id"], full_name="Chidi Eze", phone="08070000001",
                            account_number="1111111111")
    pending_user = make_user()
    assert _apply(client, auth_headers(pending_user), team_id=team["id"], full_name="Dayo Ola",
                  phone="08070000002", account_number="2222222222").status_code == 201
    pending = AgentProfile.query.filter_by(user_id=pending_user.id).first()

    # Not a lead yet.
    res = client.get("/api/agents/team", headers=lead_headers)
    assert res.status_code == 403 and res.get_json()["code"] == "NOT_A_TEAM_LEAD"

    # Only an approved agent can be made lead.
    assert client.post(f"/api/admin/agents/{pending.id}/make-lead", json={"team_id": team["id"]},
                       headers=admin).status_code == 409
    res = client.post(f"/api/admin/agents/{lead.id}/make-lead", json={"team_id": team["id"]}, headers=admin)
    assert res.status_code == 200 and res.get_json()["team"]["lead"]["user_id"] == lead_user.id
    assert client.get("/api/agents/me", headers=lead_headers).get_json()["profile"]["is_team_lead"] is True

    view = client.get("/api/agents/team", headers=lead_headers).get_json()
    assert view["team"]["name"] == "Wuse Market"
    names = {m["name"]: m["status"] for m in view["members"]}
    assert names == {"Bola M.": "APPROVED", "Chidi E.": "APPROVED", "Dayo O.": "PENDING"}
    assert view["totals"]["members"] == 3 and view["totals"]["approved"] == 2
    assert set(view["members"][0]) >= {"name", "status", "setups", "first_sales", "rewarded_sales"}

    # A lead's token is a normal user token: admin approval routes refuse it.
    res = client.post(f"/api/admin/agents/{pending.id}/approve", headers=lead_headers)
    assert res.status_code == 403
    db.session.expire_all()
    assert db.session.get(AgentProfile, pending.id).status == "PENDING"

    # Other members are not leads.
    member_headers = auth_headers(db.session.get(User, member.user_id))
    assert client.get("/api/agents/team", headers=member_headers).status_code == 403

    res = client.post(f"/api/admin/agents/{lead.id}/remove-lead", json={}, headers=admin)
    assert res.status_code == 200 and res.get_json()["removed"] == 1
    assert client.get("/api/agents/team", headers=lead_headers).status_code == 403


# ── B3: logistics companies and rider codes ─────────────────────────────────

def test_company_rider_codes_credit_the_company_with_per_rider_results(
        client, make_user, make_vendor, make_order, auth_headers, approved):
    admin = _admin()
    company_user, company, company_headers = approved(
        admin, agent_type="logistics_company", full_name="Tunde Bello", company_name="Swift Riders Ltd",
        cac_number="rc-123456", account_name="SWIFT RIDERS LIMITED")
    assert company.company_name == "Swift Riders Ltd" and company.cac_number == "RC123456"

    res = client.post("/api/agents/riders", json={"name": "Musa Rider", "phone": "08012340000"},
                      headers=company_headers)
    assert res.status_code == 201, res.get_json()
    rider = res.get_json()["rider"]
    assert rider["name"] == "Musa Rider" and rider["active"] is True and rider["link"].endswith(rider["code"])
    assert not set(rider["code"]) & set("0O1I")
    second = client.post("/api/agents/riders", json={"name": "Ade Rider"}, headers=company_headers).get_json()["rider"]
    code = db.session.get(MarketingCode, rider["code_id"])
    assert (code.kind, code.owner_user_id, code.destination) == ("agent", company_user.id, "/sell")

    # A seller signs up through the rider's code: credited to the company, the code recorded.
    client.get(f"/api/public/r/{rider['code']}?sid=s1")
    seller, store = make_vendor()
    acq = svc.credit_signup(seller, {"ref_code": rider["code"]})
    db.session.commit()
    assert acq.agent_user_id == company_user.id and acq.code_id == rider["code_id"]

    buyer = make_user()
    order = make_order(seller, buyer=buyer)
    from datetime import datetime, timezone
    order.paid_at = datetime.now(timezone.utc)
    order.status = "PAID"
    db.session.commit()
    db.session.expire_all()
    earnings = AgentEarning.query.filter_by(seller_user_id=seller.id).all()
    assert len(earnings) == 1 and earnings[0].agent_user_id == company_user.id

    rows = {r["code_id"]: r for r in client.get("/api/agents/riders", headers=company_headers).get_json()["riders"]}
    assert set(rows) == {rider["code_id"], second["code_id"]}  # the company's own code is not a rider
    assert rows[rider["code_id"]]["scans"] == 1
    assert rows[rider["code_id"]]["signups"] == 1
    assert rows[rider["code_id"]]["first_sales"] == 1
    assert rows[rider["code_id"]]["rewarded_sales"] == 1
    assert rows[second["code_id"]]["signups"] == 0
    me = client.get("/api/agents/me", headers=company_headers).get_json()
    assert me["totals"]["signups"] == 1 and me["totals"]["scans"] == 1

    # Pause and rename a rider code; a paused code no longer resolves.
    res = client.patch(f"/api/agents/riders/{rider['code_id']}", json={"active": False, "name": "Musa B."},
                       headers=company_headers)
    assert res.status_code == 200 and res.get_json()["rider"]["active"] is False
    assert client.get(f"/api/public/r/{rider['code']}").status_code == 410
    # The company's own code can't be edited through the riders route.
    assert client.patch(f"/api/agents/riders/{company.code_id}", json={"active": False},
                        headers=company_headers).status_code == 404


def test_rider_routes_are_for_approved_companies_only(client, make_user, auth_headers, approved, monkeypatch):
    admin = _admin()
    _, _, agent_headers = approved(admin)
    for method, path in (("get", "/api/agents/riders"), ("post", "/api/agents/riders"),
                         ("patch", "/api/agents/riders/1")):
        res = getattr(client, method)(path, json={"name": "X"}, headers=agent_headers)
        assert res.status_code == 403 and res.get_json()["code"] == "NOT_A_COMPANY"

    pending_user = make_user()
    pending_headers = auth_headers(pending_user)
    _apply(client, pending_headers, agent_type="logistics_company", company_name="Go Ltd",
           phone="08090000000", account_number="9090909090")
    assert client.get("/api/agents/riders", headers=pending_headers).status_code == 403
    assert _apply(client, auth_headers(make_user()), agent_type="logistics_company", phone="08090000001",
                  account_number="9090909091").get_json()["field"] == "company_name"

    _, _, company_headers = approved(admin, agent_type="logistics_company", company_name="Fast Co",
                                     phone="08090000002", account_number="9090909092")
    monkeypatch.setattr(svc, "MAX_RIDER_CODES", 1)
    assert client.post("/api/agents/riders", json={"name": "One"}, headers=company_headers).status_code == 201
    res = client.post("/api/agents/riders", json={"name": "Two"}, headers=company_headers)
    assert res.status_code == 409 and res.get_json()["code"] == "RIDER_LIMIT"


# ── B4: pre-fill, use_payout, one agent per phone and bank ───────────────────

def test_prefill_masks_numbers_and_use_payout_copies_server_side(client, make_vendor, auth_headers):
    seller, store = make_vendor()
    seller.first_name, seller.last_name, seller.phone = "Ngozi", "Okafor", "08011112222"
    store.city, store.state = "Ikeja", "Lagos"
    db.session.add(VendorBankAccount(vendor_id=seller.id, bank_name="GTBank", bank_code="058",
                                     account_number="0987651234", account_name="OKAFOR NGOZI", is_default=True))
    db.session.add(PartnerApplication(user_id=seller.id, business_name="Ngozi Rides", service_type="LOGISTICS",
                                      status="APPROVED", bank_code="044", account_number="1122334455",
                                      account_name="NGOZI OKAFOR"))
    db.session.commit()
    headers = auth_headers(seller)

    res = client.get("/api/agents/prefill", headers=headers)
    assert res.status_code == 200
    body = res.get_json()
    assert body["full_name"] == "Ngozi Okafor" and body["phone"] == "08011112222"
    assert body["state"] == "Lagos" and body["city"] == "Ikeja" and body["telegram_linked"] is False
    assert body["store_payout"] == {"bank_name": "GTBank", "bank_code": "058",
                                    "account_number_masked": "••••1234",
                                    "account_name": "OKAFOR NGOZI"}
    assert body["partner_payout"]["account_number_masked"].endswith("4455")
    text = res.get_data(as_text=True)
    assert "0987651234" not in text and "1122334455" not in text

    res = _apply(client, headers, full_name="Ngozi Okafor", phone="08011112222", use_payout="store",
                 bank_name=None, bank_code=None, account_number=None, account_name=None)
    assert res.status_code == 201, res.get_json()
    p = AgentProfile.query.filter_by(user_id=seller.id).first()
    assert (p.bank_name, p.bank_code, p.account_number, p.account_name) == (
        "GTBank", "058", "0987651234", "OKAFOR NGOZI")


def test_use_partner_payout_and_missing_source(client, make_user, auth_headers):
    partner = make_user(role=UserRole.PARTNER)
    db.session.add(PartnerApplication(user_id=partner.id, business_name="Kemi Logistics", service_type="LOGISTICS",
                                      status="APPROVED", bank_code="044", account_number="1122334455",
                                      account_name="KEMI ADE"))
    db.session.commit()
    headers = auth_headers(partner)
    res = _apply(client, headers, use_payout="store", account_number=None)
    assert res.status_code == 400 and res.get_json()["code"] == "NO_PAYOUT_ACCOUNT"
    res = _apply(client, headers, full_name="Kemi Ade", use_payout="partner", bank_name="Access Bank",
                 account_number=None, account_name=None)
    assert res.status_code == 201, res.get_json()
    p = AgentProfile.query.filter_by(user_id=partner.id).first()
    assert (p.bank_name, p.bank_code, p.account_number, p.account_name) == (
        "Access Bank", "044", "1122334455", "KEMI ADE")


def test_one_agent_per_phone_and_bank_account(client, make_user, auth_headers):
    assert _apply(client, auth_headers(make_user())).status_code == 201
    res = _apply(client, auth_headers(make_user()), phone="+234 803 123 4567", account_number="5555555555")
    assert res.status_code == 409 and res.get_json()["code"] == "PHONE_IN_USE"
    res = _apply(client, auth_headers(make_user()), phone="08099999999")
    assert res.status_code == 409 and res.get_json()["code"] == "BANK_IN_USE"
    assert _apply(client, auth_headers(make_user()), phone="08099999999",
                  account_number="5555555555").status_code == 201


# ── B5/B6: approval checklist and payout warning ─────────────────────────────

def test_names_match():
    assert svc.names_match("OBI AMAKA CHIOMA", "Amaka Obi") == "yes"
    assert svc.names_match("Amaka", "Amaka Obi") == "yes"
    assert svc.names_match("SWIFT RIDERS LIMITED", "Tunde Bello", "Swift Riders Ltd") == "yes"
    assert svc.names_match("JOHN DOE", "Amaka Obi") == "no"
    assert svc.names_match("", "Amaka Obi") == "unknown"
    assert svc.names_match("Amaka Obi", None, "") == "unknown"


def test_admin_approval_checks(client, make_user, auth_headers):
    admin = _admin()
    good_user = make_user()
    good_user.is_verified = True
    db.session.commit()
    assert _apply(client, auth_headers(good_user), account_name="OBI AMAKA").status_code == 201
    bad_user = make_user()
    bad_user.telegram_id = "777"
    db.session.commit()
    assert _apply(client, auth_headers(bad_user), full_name="Bola Musa", phone="08050000000",
                  account_number="3333333333", account_name="JOHN DOE").status_code == 201
    good = AgentProfile.query.filter_by(user_id=good_user.id).first()
    bad = AgentProfile.query.filter_by(user_id=bad_user.id).first()

    checks = client.get(f"/api/admin/agents/{good.id}", headers=admin).get_json()["agent"]["checks"]
    assert checks == {"email_verified": True, "telegram_linked": False, "phone_present": True,
                      "bank_name_matches": "yes", "duplicate_phone_agents": [], "duplicate_bank_agents": []}
    checks = client.get(f"/api/admin/agents/{bad.id}", headers=admin).get_json()["agent"]["checks"]
    assert checks["telegram_linked"] is True and checks["bank_name_matches"] == "no"

    # A duplicate that slipped in (an old row) shows on both.
    bad.phone = "0803 123 4567"
    db.session.commit()
    checks = client.get(f"/api/admin/agents/{good.id}", headers=admin).get_json()["agent"]["checks"]
    assert checks["duplicate_phone_agents"] == [bad.id]

    listed = {a["id"]: a for a in client.get("/api/admin/agents", headers=admin).get_json()["agents"]}
    assert listed[good.id]["checks_ok"] is False   # the duplicate phone
    bad.phone = "08050000000"
    db.session.commit()
    listed = {a["id"]: a for a in client.get("/api/admin/agents", headers=admin).get_json()["agents"]}
    assert listed[good.id]["checks_ok"] is True and listed[bad.id]["checks_ok"] is False


def test_mark_paid_warns_on_bank_name_mismatch(client, make_user, make_vendor, auth_headers, approved):
    admin = _admin()
    agent_user, _, _ = approved(admin, account_name="JOHN DOE")
    seller, _ = make_vendor()
    earning = AgentEarning(agent_user_id=agent_user.id, seller_user_id=seller.id, kind="bonus", amount_ngn=200,
                           status="PAYABLE")
    db.session.add(earning)
    db.session.commit()
    rows = client.get("/api/admin/marketing/earnings", headers=admin).get_json()["earnings"]
    assert rows[0]["agent"]["bank_name_matches"] == "no"
    res = client.post("/api/admin/marketing/earnings/mark-paid", json={"ids": [earning.id], "reference": "TRF1"},
                      headers=admin)
    assert res.status_code == 200
    body = res.get_json()
    assert body["paid"] == 1 and body["warnings"][0]["code"] == "BANK_NAME_MISMATCH"


# ── B7: sign-up with an email that already exists ────────────────────────────

def test_signup_with_existing_email_says_log_in(client, make_user, monkeypatch):
    import app.routes.auth as auth_routes
    monkeypatch.setattr(auth_routes, "send_siiqo_email", lambda **kw: None, raising=False)
    user = make_user(email="taken@example.com")
    user.is_verified = True
    db.session.commit()
    res = client.post("/api/auth/register", json={"email": "Taken@Example.com", "password": "a-long-password",
                                                  "first_name": "T"})
    assert res.status_code == 409
    assert res.get_json() == {"code": "EMAIL_EXISTS",
                              "message": "You already have a Siiqo account with this email. Log in to continue."}


# ── B8: a partner login works on the agent portal ────────────────────────────

def test_partner_login_token_works_on_agent_routes(client, make_user):
    partner = make_user(role=UserRole.PARTNER, email="rider@example.com")
    res = client.post("/api/auth/partner/login", json={"email": "rider@example.com",
                                                       "password": "not-a-real-password"})
    assert res.status_code == 200, res.get_json()
    headers = {"Authorization": f"Bearer {res.get_json()['access_token']}"}
    res = client.get("/api/agents/me", headers=headers)
    assert res.status_code == 404 and res.get_json()["code"] == "NOT_AN_AGENT"
    assert client.get("/api/agents/prefill", headers=headers).get_json()["email"] == "rider@example.com"
    assert _apply(client, headers, agent_type="rider").status_code == 201
    assert client.get("/api/agents/me", headers=headers).get_json()["profile"]["status"] == "PENDING"
    assert AgentProfile.query.filter_by(user_id=partner.id).count() == 1
