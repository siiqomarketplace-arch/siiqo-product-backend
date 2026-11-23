"""Logistics partner flow: approval gate, apply form, admin review, riders,
server-priced checkout, delivery lifecycle, buyer code, earnings and
withdrawals. Every gateway call is mocked."""
from datetime import timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.communication import Notification
from app.models.escrow import EscrowStatus, EscrowTransaction, LogisticsAssignment
from app.models.order import Order
from app.models.partnerships import PartnerApplication, PartnerStaff
from app.models.user import User, UserRole
from app.models.withdrawal import PartnerBankAccount, PartnerWithdrawal, PODPayment

PASSWORD = "not-a-real-password"


@pytest.fixture
def emails(monkeypatch):
    sent = []
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: sent.append(k) or True)
    monkeypatch.setattr("app.routes.admin.send_siiqo_email", lambda *a, **k: sent.append(k) or True)
    monkeypatch.setattr("app.routes.cart.send_siiqo_email", lambda *a, **k: True, raising=False)
    return sent


@pytest.fixture
def make_partner(make_user):
    def _make(status="APPROVED", coverage=None, business="Fast Riders"):
        user = make_user(role=UserRole.PARTNER)
        user.is_verified = status == "APPROVED"
        db.session.add(PartnerApplication(
            user_id=user.id, business_name=business, service_type="LOGISTICS", status=status,
            pricing_settings={"coverage": coverage if coverage is not None
                              else [{"state": "Lagos", "lgas": [], "price": 2000}]}))
        db.session.commit()
        return user
    return _make


def _admin_headers(role="SUPERADMIN"):
    admin = AdminUser(name="Ops", email=f"{role.lower()}@example.com", role=role)
    admin.set_password(PASSWORD)
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _store(make_vendor, make_product, state="Lagos", city="Ikeja", enabled=None):
    vendor, store = make_vendor()
    store.city, store.state = city, state
    if enabled is not None:
        store.logistics_settings = enabled
    db.session.commit()
    return vendor, store, make_product(store, price=5000)


def _checkout(client, product, option, payment_method="CRYPTO", state="Lagos", city="Yaba", headers=None):
    body = {"items": [{"product_id": product.id, "quantity": 1}], "payment_method": payment_method,
            "email": "guest@example.com", "delivery_name": "Ada Buyer", "delivery_phone": "08031234567",
            "delivery_address": "1 Road", "delivery_city": city, "delivery_state": state,
            "logistics_selections": [{"vendorId": product.storefront.vendor_id,
                                      "selectedOption": {"id": option, "fee": 1}}]}
    return client.post("/api/cart/checkout", json=body, headers=headers or {})


def _pay(order):
    escrow = EscrowTransaction.query.filter_by(order_id=order.id).first()
    escrow.status = EscrowStatus.IN_ESCROW
    db.session.commit()
    return escrow


# ── 1. Only approved partners get in ──────────────────────────────────────

def test_pending_partner_cannot_log_in_or_use_routes(client, make_partner, auth_headers):
    partner = make_partner(status="PENDING")
    res = client.post("/api/auth/partner/login", json={"email": partner.email, "password": PASSWORD})
    assert res.status_code == 403
    assert res.get_json()["code"] == "PARTNER_PENDING"
    assert "being reviewed" in res.get_json()["message"]
    h = auth_headers(partner)
    for method, url in [("get", "/api/partners/dashboard/assignments"), ("get", "/api/logistics/staff"),
                        ("get", "/api/logistics/my-assignments"), ("get", "/api/withdrawal/partner/balance"),
                        ("post", "/api/withdrawal/partner/withdrawals"), ("get", "/api/partners/earnings"),
                        ("get", "/api/vendor/logistics/settings")]:
        r = getattr(client, method)(url, headers=h, json={} if method == "post" else None)
        assert r.status_code == 403, url
        assert r.get_json()["code"] == "PARTNER_PENDING", url
    me = client.get("/api/partners/me", headers=h).get_json()
    assert me["access"] is False and me["application_status"] == "PENDING"


def test_approved_partner_logs_in_and_suspended_is_blocked(client, make_partner):
    partner = make_partner()
    res = client.post("/api/auth/partner/login", json={"email": partner.email, "password": PASSWORD})
    assert res.status_code == 200 and res.get_json()["access_token"]
    assert res.get_json()["partner"]["business_name"] == "Fast Riders"
    PartnerApplication.query.filter_by(user_id=partner.id).one().status = "SUSPENDED"
    db.session.commit()
    res = client.post("/api/auth/partner/login", json={"email": partner.email, "password": PASSWORD})
    assert res.status_code == 403 and res.get_json()["code"] == "PARTNER_SUSPENDED"


# ── 2. Apply form keeps business, bank and coverage ───────────────────────

def test_apply_saves_business_bank_and_coverage(client, monkeypatch):
    from app.services import bank_resolve
    monkeypatch.setattr(bank_resolve, "resolve_account",
                        lambda code, num, **k: bank_resolve.BankResolveResult(
                            status="resolved", account_name="FAST RIDERS LTD", message="ok"))
    res = client.post("/api/partners/apply", json={
        "email": "fast@example.com", "password": "a-long-password", "fullname": "Ada Obi",
        "business_name": "Fast Riders Ltd", "phone": "08030000001", "partner_role": "LOGISTICS",
        "bank_code": "058", "account_number": "0123456789", "bank_name": "GTBank",
        "account_name": "Somebody Else",
        "coverage": [{"state": "Lagos", "lgas": ["Ikeja", "Yaba"], "price": 2500},
                     {"state": "Ogun", "price": "4000"}],
    })
    assert res.status_code == 201, res.get_json()
    row = PartnerApplication.query.one()
    assert row.business_name == "Fast Riders Ltd"
    assert (row.bank_code, row.account_number, row.bank_name) == ("058", "0123456789", "GTBank")
    assert row.account_name == "FAST RIDERS LTD"          # from the bank, not the form
    assert row.pricing_settings["coverage"] == [
        {"state": "Lagos", "lgas": ["Ikeja", "Yaba"], "price": 2500.0},
        {"state": "Ogun", "lgas": [], "price": 4000.0}]
    assert row.state_of_operation == "Lagos, Ogun" and row.status == "PENDING"


def test_apply_refuses_bad_bank_and_missing_coverage(client, monkeypatch):
    from app.services import bank_resolve
    monkeypatch.setattr(bank_resolve, "resolve_account",
                        lambda code, num, **k: bank_resolve.BankResolveResult(
                            status="rejected", account_name=None, message="no"))
    base = {"email": "x@example.com", "password": "a-long-password", "business_name": "X Ltd",
            "partner_role": "LOGISTICS"}
    res = client.post("/api/partners/apply", json={**base, "coverage": [{"state": "Lagos", "price": 1000}],
                                                    "bank_code": "058", "account_number": "0123456789"})
    assert res.status_code == 400 and res.get_json()["code"] == "BANK_NOT_FOUND"
    res = client.post("/api/partners/apply", json=base)
    assert res.status_code == 400 and res.get_json()["code"] == "INVALID_COVERAGE"
    res = client.post("/api/partners/apply", json={**base, "coverage": [{"state": "Lagos", "price": 0}]})
    assert res.status_code == 400
    assert User.query.filter_by(email="x@example.com").first() is None


# ── 3. Admin review emails the applicant and blocks access ────────────────

@pytest.mark.parametrize("status,subject_word", [("REJECTED", "application"), ("SUSPENDED", "suspended")])
def test_reject_and_suspend_notify_store_note_and_block(client, make_partner, emails, status, subject_word):
    partner = make_partner()
    app_row = PartnerApplication.query.filter_by(user_id=partner.id).one()
    res = client.patch(f"/api/admin/partners/{app_row.id}/status", headers=_admin_headers(),
                       json={"status": status, "note": "Documents <b>unclear</b>"})
    assert res.status_code == 200
    db.session.expire_all()
    app_row = db.session.get(PartnerApplication, app_row.id)
    assert app_row.status == status and app_row.review_note == "Documents <b>unclear</b>"
    assert db.session.get(User, partner.id).is_verified is False
    mail = [e for e in emails if e.get("to_email") == partner.email]
    assert len(mail) == 1 and subject_word in mail[0]["subject"].lower()
    assert "Documents &lt;b&gt;unclear&lt;/b&gt;" in mail[0]["notice_text"]
    res = client.post("/api/auth/partner/login", json={"email": partner.email, "password": PASSWORD})
    assert res.status_code == 403


def test_approve_emails_and_unlocks(client, make_partner, emails):
    partner = make_partner(status="PENDING")
    app_row = PartnerApplication.query.filter_by(user_id=partner.id).one()
    res = client.patch(f"/api/admin/partners/{app_row.id}/status", headers=_admin_headers(),
                       json={"status": "APPROVED"})
    assert res.status_code == 200
    assert any("approved" in (e.get("notice_text") or "") for e in emails)
    res = client.post("/api/auth/partner/login", json={"email": partner.email, "password": PASSWORD})
    assert res.status_code == 200


# ── 4. Riders: no shared password, linked by id ───────────────────────────

def test_rider_gets_one_time_password_and_is_linked_by_id(client, make_partner, auth_headers, emails,
                                                          make_vendor, make_product):
    partner = make_partner()
    h = auth_headers(partner)
    res = client.post("/api/logistics/staff", headers=h,
                      json={"name": "Musa Rider", "phone": "08030000009", "email": "musa@example.com",
                            "password": "SiiqoRiderTempPass123!"})
    assert res.status_code == 201, res.get_json()
    temp = res.get_json()["temporary_password"]
    assert temp and temp != "SiiqoRiderTempPass123!"
    rider = User.query.filter_by(email="musa@example.com").one()
    assert rider.role == UserRole.RIDER and not rider.check_password("SiiqoRiderTempPass123!")
    assert rider.check_password(temp)
    staff = PartnerStaff.query.one()
    assert staff.user_id == rider.id
    assert any(e.get("to_email") == "musa@example.com" and temp not in str(e) for e in emails)
    assert client.get("/api/logistics/staff", headers=h).get_json()["staff"][0].get("temporary_password") is None

    # Another user with the same phone never sees the rider's jobs.
    vendor, store, p = _store(make_vendor, make_product)
    order_id = _checkout(client, p, f"siiqo_partner_{partner.id}").get_json()["id"]
    _pay(db.session.get(Order, order_id))
    a = LogisticsAssignment.query.one()
    assert client.patch(f"/api/logistics/assignments/{a.id}/assign", headers=h,
                        json={"staff_id": staff.id}).status_code == 200
    assert db.session.get(LogisticsAssignment, a.id).rider_user_id == rider.id
    impostor = User(email="imp@example.com", role=UserRole.RIDER, phone="08030000009", first_name="I")
    impostor.set_password(PASSWORD)
    db.session.add(impostor)
    db.session.commit()
    assert client.get("/api/rider/dashboard", headers=auth_headers(impostor)).get_json()["orders"] == []
    assert len(client.get("/api/rider/dashboard", headers=auth_headers(rider)).get_json()["orders"]) == 1


# ── 5. Checkout: approved, covering, chosen partners only; server price ───

def test_checkout_partner_rules(client, make_partner, make_vendor, make_product):
    lagos = make_partner(coverage=[{"state": "Lagos", "lgas": ["Yaba"], "price": 1500},
                                   {"state": "Lagos", "lgas": [], "price": 3000}])
    pending = make_partner(status="PENDING")
    other = make_partner(business="Other")
    vendor, store, p = _store(make_vendor, make_product)

    res = _checkout(client, p, f"siiqo_partner_{lagos.id}", city="Yaba")   # LGA price wins
    assert res.status_code == 200 and float(db.session.get(Order, res.get_json()["id"]).logistics_fee) == 1650.0
    res = _checkout(client, p, f"siiqo_partner_{lagos.id}", city="Ikeja")  # whole-state price
    assert float(db.session.get(Order, res.get_json()["id"]).logistics_fee) == 3300.0
    assert _checkout(client, p, f"siiqo_partner_{lagos.id}", state="Kano").get_json()["code"] == \
        "DELIVERY_NOT_AVAILABLE"
    assert _checkout(client, p, f"siiqo_partner_{pending.id}").status_code == 400
    assert _checkout(client, p, "siiqo_partner_0").status_code == 400
    assert _checkout(client, p, "siiqo_dispatch").status_code == 400

    store.logistics_settings = ["self_pickup", f"siiqo_partner_{other.id}"]
    db.session.commit()
    assert _checkout(client, p, f"siiqo_partner_{lagos.id}").status_code == 400
    assert _checkout(client, p, f"siiqo_partner_{other.id}").status_code == 200
    a = LogisticsAssignment.query.filter_by(partner_id=other.id).one()
    assert float(a.delivery_fee) == 2000.0 and a.status == "PENDING" and a.provider_type == "siiqo_partner"
    assert LogisticsAssignment.query.filter_by(partner_id=0).count() == 0


def test_quote_returns_only_valid_options_with_server_prices(client, make_partner, make_vendor, make_product):
    partner = make_partner()
    make_partner(status="PENDING", business="Not yet")
    vendor, store, p = _store(make_vendor, make_product)
    store.delivery_settings = {"self_delivery": {"enabled": True, "fee_same_city": 1000, "fee_same_state": 1800,
                                                 "fee_other_states": None, "ships_within_days": 2},
                               "pickup": {"enabled": True, "area": "Ikeja"}}
    db.session.commit()
    opts = client.get(f"/api/logistics/quote?vendor_id={vendor.id}&state=Lagos&city=Yaba").get_json()["options"]
    by_id = {o["id"]: o for o in opts}
    assert by_id["vendor_self"]["fee"] == 1800
    assert by_id[f"siiqo_partner_{partner.id}"]["fee"] == 2200.0
    assert "provider_fee" not in by_id[f"siiqo_partner_{partner.id}"]
    assert by_id["self_pickup"]["fee"] == 0
    assert len(opts) == 3
    kano = client.get(f"/api/logistics/quote?vendor_id={vendor.id}&state=Kano").get_json()["options"]
    assert [o["id"] for o in kano] == ["self_pickup"]
    active = client.get("/api/logistics/active?state=Kano").get_json()["data"]
    assert active == []                                   # no "Siiqo Standard Delivery" id 0


# ── 6/7. Lifecycle, buyer code, guests ────────────────────────────────────

def test_paid_order_assigns_partner_then_pickup_and_delivery(client, make_partner, make_vendor, make_product,
                                                            auth_headers, emails):
    partner = make_partner()
    vendor, store, p = _store(make_vendor, make_product)
    order = db.session.get(Order, _checkout(client, p, f"siiqo_partner_{partner.id}").get_json()["id"])
    a = LogisticsAssignment.query.one()
    assert a.status == "PENDING"
    h = auth_headers(partner)
    res = client.patch(f"/api/logistics/assignments/{a.id}/status", headers=h, json={"status": "PICKED_UP"})
    assert res.status_code == 409 and res.get_json()["code"] == "NOT_PAID"

    escrow = _pay(order)
    db.session.expire_all()
    a = db.session.get(LogisticsAssignment, a.id)
    assert a.status == "ASSIGNED" and a.assigned_at is not None
    assert Notification.query.filter_by(user_id=partner.id, order_id=order.id).count() == 1
    assert any(e.get("to_email") == partner.email and "New delivery" in e["subject"] for e in emails)

    res = client.patch(f"/api/logistics/assignments/{a.id}/status", headers=h, json={"status": "PICKED_UP"})
    assert res.status_code == 200, res.get_json()
    db.session.expire_all()
    order = db.session.get(Order, order.id)
    assert order.status == "SHIPPED" and order.escrow.status == EscrowStatus.SHIPPED
    assert Notification.query.filter_by(user_id=vendor.id, title="Order picked up").count() == 1
    guest_mail = [e for e in emails if e.get("to_email") == "guest@example.com"]
    assert guest_mail and escrow.escrow_code in guest_mail[-1]["notice_text"]

    res = client.patch(f"/api/logistics/assignments/{a.id}/status", headers=h, json={"status": "IN_TRANSIT"})
    assert res.status_code == 200
    res = client.patch(f"/api/logistics/assignments/{a.id}/status", headers=h, json={"status": "DELIVERED"})
    assert res.status_code == 400 and res.get_json()["code"] == "CODE_REQUIRED"
    res = client.patch(f"/api/logistics/assignments/{a.id}/status", headers=h,
                       json={"status": "DELIVERED", "delivery_code": escrow.escrow_code,
                             "proof_photo_url": "/static/uploads/delivery_proof/abc.jpg"})
    assert res.status_code == 200, res.get_json()
    db.session.expire_all()
    order = db.session.get(Order, order.id)
    a = db.session.get(LogisticsAssignment, a.id)
    assert a.status == "DELIVERED" and a.proof_photo_url.endswith("abc.jpg")
    assert order.status == "DELIVERED" and order.escrow.status == EscrowStatus.DELIVERED
    assert order.delivered_at is not None                  # Safe Pay 72h clock started
    assert Notification.query.filter(Notification.user_id.is_(None)).count() == 0
    assert any(e.get("to_email") == "guest@example.com" and "delivered" in e.get("subject", "") for e in emails)


def test_wrong_codes_lock_after_five(client, make_partner, make_vendor, make_product, auth_headers, emails):
    partner = make_partner()
    vendor, store, p = _store(make_vendor, make_product)
    order = db.session.get(Order, _checkout(client, p, f"siiqo_partner_{partner.id}").get_json()["id"])
    escrow = _pay(order)
    a = LogisticsAssignment.query.one()
    h = auth_headers(partner)
    url = f"/api/logistics/assignments/{a.id}/status"
    client.patch(url, headers=h, json={"status": "PICKED_UP"})
    for i in range(4):
        r = client.patch(url, headers=h, json={"status": "DELIVERED", "delivery_code": "000000x"})
        assert r.status_code == 400 and r.get_json()["attempts_left"] == 4 - i
    r = client.patch(url, headers=h, json={"status": "DELIVERED", "delivery_code": "000000x"})
    assert r.status_code == 429 and r.get_json()["code"] == "CODE_LOCKED"
    r = client.patch(url, headers=h, json={"status": "DELIVERED", "delivery_code": escrow.escrow_code})
    assert r.status_code == 429                            # even the right code waits for the lock
    assert Notification.query.filter_by(user_id=vendor.id, title="Delivery code locked").count() == 1
    assert db.session.get(LogisticsAssignment, a.id).status == "PICKED_UP"


def test_unapproved_or_other_partner_cannot_move_job(client, make_partner, make_vendor, make_product,
                                                    auth_headers):
    partner = make_partner()
    stranger = make_partner(business="Stranger")
    vendor, store, p = _store(make_vendor, make_product)
    order = db.session.get(Order, _checkout(client, p, f"siiqo_partner_{partner.id}").get_json()["id"])
    _pay(order)
    a = LogisticsAssignment.query.one()
    url = f"/api/logistics/assignments/{a.id}/status"
    assert client.patch(url, headers=auth_headers(stranger), json={"status": "PICKED_UP"}).status_code == 404
    PartnerApplication.query.filter_by(user_id=partner.id).one().status = "SUSPENDED"
    db.session.commit()
    assert client.patch(url, headers=auth_headers(partner), json={"status": "PICKED_UP"}).status_code == 403


def test_refund_cancels_job(client, make_partner, make_vendor, make_product):
    partner = make_partner()
    vendor, store, p = _store(make_vendor, make_product)
    order = db.session.get(Order, _checkout(client, p, f"siiqo_partner_{partner.id}").get_json()["id"])
    escrow = _pay(order)
    escrow.status = EscrowStatus.REFUNDED
    db.session.commit()
    a = LogisticsAssignment.query.one()
    assert a.status == "CANCELLED" and a.cancelled_at is not None
    assert Notification.query.filter_by(user_id=partner.id, title="Delivery cancelled").count() == 1


def test_pod_order_assigned_at_checkout_and_needs_handover_code(client, make_partner, make_vendor,
                                                                make_product, auth_headers, emails,
                                                                monkeypatch):
    """Checkout no longer offers delivery partners with Pay on Delivery; a
    POD partner job made before that (legacy row) still needs the code."""
    monkeypatch.setattr("app.services.entitlements.has_feature", lambda vid, f: True)
    partner = make_partner()
    vendor, store, p = _store(make_vendor, make_product)
    res = _checkout(client, p, f"siiqo_partner_{partner.id}", payment_method="POD")
    assert res.status_code == 400 and res.get_json()["code"] == "POD_PARTNER_NOT_ALLOWED"
    res = _checkout(client, p, "self_pickup", payment_method="POD")
    assert res.status_code == 200, res.get_json()
    order = Order.query.one()
    order.logistics_provider_id, order.logistics_fee = f"siiqo_partner_{partner.id}", 2200
    db.session.add(LogisticsAssignment(order_id=order.id, partner_id=partner.id, status="ASSIGNED",
                                       delivery_fee=2000))
    db.session.commit()
    a = LogisticsAssignment.query.one()
    assert a.status == "ASSIGNED"
    h = auth_headers(partner)
    url = f"/api/logistics/assignments/{a.id}/status"
    r = client.patch(url, headers=h, json={"status": "DELIVERED"})
    assert r.status_code == 409 and r.get_json()["code"] == "NEEDS_PICKUP"
    assert client.patch(url, headers=h, json={"status": "PICKED_UP"}).status_code == 200
    text = [e for e in emails if e.get("to_email") == "guest@example.com"][-1]["notice_text"]
    import re
    code = re.search(r"<strong>(\d{6})</strong>", text).group(1)
    assert client.patch(url, headers=h, json={"status": "DELIVERED", "delivery_code": "999999"}).status_code == 400
    r = client.patch(url, headers=h, json={"status": "DELIVERED", "delivery_code": code})
    assert r.status_code == 200, r.get_json()
    assert db.session.get(Order, a.order_id).status == "DELIVERED"


def test_guest_paystack_style_order_delivery_never_crashes(client, make_partner, make_vendor, make_order,
                                                          auth_headers, emails):
    """Older rows: an assignment for a guest order with an escrow (no buyer id)."""
    partner = make_partner()
    vendor, store = make_vendor()
    order = make_order(vendor, None, total=5000, payment_method="PAYSTACK", is_guest=True)
    escrow = EscrowTransaction(order_id=order.id, status=EscrowStatus.IN_ESCROW, amount=5000, fee_amount=150,
                               currency="NGN", transaction_number=f"PSK-{order.id}")
    db.session.add(escrow)
    db.session.add(LogisticsAssignment(order_id=order.id, partner_id=partner.id, status="IN_TRANSIT",
                                       delivery_fee=2000))
    db.session.commit()
    a = LogisticsAssignment.query.one()
    r = client.patch(f"/api/logistics/assignments/{a.id}/deliver", headers=auth_headers(partner),
                     json={"status": "DELIVERED", "delivery_otp": escrow.escrow_code})
    assert r.status_code == 200, r.get_json()
    assert db.session.get(EscrowTransaction, escrow.id).status == EscrowStatus.DELIVERED


def test_invalid_proof_url_refused(client, make_partner, make_vendor, make_product, auth_headers):
    partner = make_partner()
    vendor, store, p = _store(make_vendor, make_product)
    order = db.session.get(Order, _checkout(client, p, f"siiqo_partner_{partner.id}").get_json()["id"])
    escrow = _pay(order)
    a = LogisticsAssignment.query.one()
    h = auth_headers(partner)
    url = f"/api/logistics/assignments/{a.id}/status"
    client.patch(url, headers=h, json={"status": "PICKED_UP"})
    r = client.patch(url, headers=h, json={"status": "DELIVERED", "delivery_code": escrow.escrow_code,
                                           "proof_photo_url": "https://evil.example.com/x.jpg"})
    assert r.status_code == 400 and r.get_json()["code"] == "INVALID_PROOF"
    up = client.post(f"/api/logistics/assignments/{a.id}/proof-upload", headers=h,
                     json={"filename": "p.jpg", "file_type": "image/jpeg"}).get_json()
    assert up["is_direct_s3"] is False and up["fallback_upload_url"].endswith("/proof-upload")


# ── 8. Earnings and withdrawals ───────────────────────────────────────────

def _delivered_job(client, make_partner, make_vendor, make_product, auth_headers, partner=None):
    partner = partner or make_partner()
    vendor, store, p = _store(make_vendor, make_product)
    order = db.session.get(Order, _checkout(client, p, f"siiqo_partner_{partner.id}").get_json()["id"])
    escrow = _pay(order)
    a = LogisticsAssignment.query.filter_by(order_id=order.id).one()
    h = auth_headers(partner)
    client.patch(f"/api/logistics/assignments/{a.id}/status", headers=h, json={"status": "PICKED_UP"})
    r = client.patch(f"/api/logistics/assignments/{a.id}/status", headers=h,
                     json={"status": "DELIVERED", "delivery_code": escrow.escrow_code})
    assert r.status_code == 200, r.get_json()
    return partner, db.session.get(EscrowTransaction, escrow.id)


def test_fee_earned_only_after_release(client, make_partner, make_vendor, make_product, auth_headers, emails):
    partner, escrow = _delivered_job(client, make_partner, make_vendor, make_product, auth_headers)
    h = auth_headers(partner)
    bal = client.get("/api/withdrawal/partner/balance", headers=h).get_json()
    assert Decimal(bal["available"]) == 0 and Decimal(bal["pending_earnings"]) == Decimal("2000")
    escrow.status = EscrowStatus.DISPUTED
    db.session.commit()
    assert Decimal(client.get("/api/withdrawal/partner/balance", headers=h).get_json()["available"]) == 0
    escrow.status = EscrowStatus.RELEASED
    db.session.commit()
    assert Decimal(client.get("/api/withdrawal/partner/balance", headers=h).get_json()["available"]) == 2000
    assert client.get("/api/partners/earnings", headers=h).get_json()["total_earned"] == 2000.0


def test_withdrawal_needs_bank_uses_fixed_reference_and_cannot_double_spend(
        client, make_partner, make_vendor, make_product, auth_headers, emails, monkeypatch):
    partner, escrow = _delivered_job(client, make_partner, make_vendor, make_product, auth_headers)
    escrow.status = EscrowStatus.RELEASED
    db.session.commit()
    h = auth_headers(partner)
    r = client.post("/api/withdrawal/partner/withdrawals", headers=h, json={})
    assert r.status_code == 400 and r.get_json()["code"] == "NO_PAYOUT_ACCOUNT"

    db.session.add(PartnerBankAccount(partner_id=partner.id, bank_name="GTBank", bank_code="058",
                                      account_number="0123456789", account_name="FAST RIDERS",
                                      recipient_code="RCP_test", is_verified=True, is_default=True))
    db.session.commit()
    calls = []
    import app.services.escrow.paystack_provider as psp
    monkeypatch.setattr(psp, "paystack_transfer_to_vendor",
                        lambda **k: calls.append(k) or {"success": True, "transfer_code": "TRF_1"})
    r = client.post("/api/withdrawal/partner/withdrawals", headers=h, json={})
    assert r.status_code == 200, r.get_json()
    w = PartnerWithdrawal.query.one()
    assert calls[0]["reference"] == w.transfer_reference == r.get_json()["reference"]
    assert w.transfer_reference.startswith(f"PWD-{partner.id}-{w.id}-")
    assert float(calls[0]["amount_ngn"]) == 2000.0
    r = client.post("/api/withdrawal/partner/withdrawals", headers=h, json={})
    assert r.status_code == 400 and len(calls) == 1


def test_legacy_pod_partner_job_never_creates_siiqo_paid_earnings(client, make_partner, make_vendor,
                                                                   make_product, auth_headers, emails,
                                                                   monkeypatch):
    """Pay on Delivery: the buyer paid the seller in cash, so Siiqo never held
    the delivery fee. Older POD partner jobs are not Siiqo-paid earnings,
    even once the seller confirms the cash (changed deliberately)."""
    monkeypatch.setattr("app.services.entitlements.has_feature", lambda vid, f: True)
    partner = make_partner()
    vendor, store, p = _store(make_vendor, make_product)
    assert _checkout(client, p, "self_pickup", payment_method="POD").status_code == 200
    order = Order.query.one()
    db.session.add(LogisticsAssignment(order_id=order.id, partner_id=partner.id, status="DELIVERED",
                                       delivery_fee=2000))
    db.session.commit()
    a = LogisticsAssignment.query.one()
    h = auth_headers(partner)
    bal = client.get("/api/withdrawal/partner/balance", headers=h).get_json()
    assert Decimal(bal["available"]) == 0 and Decimal(bal["pending_earnings"]) == 0
    PODPayment.query.filter_by(order_id=a.order_id).one().confirmed_by_vendor = True
    db.session.commit()
    assert Decimal(client.get("/api/withdrawal/partner/balance", headers=h).get_json()["available"]) == 0


def test_partner_edits_coverage(client, make_partner, auth_headers):
    partner = make_partner()
    h = auth_headers(partner)
    r = client.post("/api/vendor/logistics/settings", headers=h,
                    json={"coverage": [{"state": "FCT", "lgas": ["Wuse"], "price": 3000}]})
    assert r.status_code == 200, r.get_json()
    data = client.get("/api/vendor/logistics/settings", headers=h).get_json()["data"]
    assert data["coverage"] == [{"state": "FCT", "lgas": ["Wuse"], "price": 3000.0}]
    r = client.post("/api/vendor/logistics/settings", headers=h, json={"coverage": []})
    assert r.status_code == 400


def test_unpaid_job_hidden_from_partner_until_paid(client, make_partner, make_vendor, make_product,
                                                  auth_headers, emails):
    partner = make_partner()
    vendor, store, p = _store(make_vendor, make_product)
    order = db.session.get(Order, _checkout(client, p, f"siiqo_partner_{partner.id}").get_json()["id"])
    h = auth_headers(partner)
    assert client.get("/api/partners/dashboard/assignments", headers=h).get_json()["data"] == []
    assert client.get("/api/logistics/my-assignments", headers=h).get_json()["assignments"] == []
    _pay(order)
    rows = client.get("/api/partners/dashboard/assignments", headers=h).get_json()["data"]
    assert len(rows) == 1 and rows[0]["status"] == "ASSIGNED" and rows[0]["buyer_name"] == "Ada Buyer"
