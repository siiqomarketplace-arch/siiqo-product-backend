"""
Disputes and refunds (Phase 2 item 2-5).

Raise -> payout frozen on every path; evidence from both sides; deadline ->
review; reviewer summary job (AI off / mocked / critic rejects); superadmin
decision; deterministic, idempotent outcomes with exact Decimal splits; the
outcome recorded as trust evidence; one open dispute per order.
No network: AI uses a fake client, payouts have no bank account to send to.
"""
import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.admin import AdminUser
from app.models.audit import AdminAuditLog
from app.models.dispute import DisputeCase, DisputeEvidence, DisputeStatus
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.finance import Ledger
from app.models.telemetry import TrustEvidence
from app.models.user import UserRole
from app.services import disputes as svc

NOW = datetime.now(timezone.utc)


# ── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _no_email_no_ai(monkeypatch):
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda **k: True)
    for key in ("AI_ENABLED", "AI_ENABLED_ACTIONS", "ANTHROPIC_API_KEY", "DISPUTE_SELLER_RESPONSE_DAYS",
                "DISPUTE_BUYER_REPLY_DAYS"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def paid(make_vendor, make_user, make_order):
    """A Paystack order, delivered, money held by Siiqo: 10,000.00 with a 300.00 fee."""
    def _make(amount="10000.00", fee="300.00", status=EscrowStatus.DELIVERED, payment_method="PAYSTACK"):
        vendor, store = make_vendor()
        buyer = make_user()
        order = make_order(vendor, buyer, total=amount, payment_method=payment_method)
        order.status = "DELIVERED"
        escrow = EscrowTransaction(order_id=order.id, status=status, amount=Decimal(amount),
                                   fee_amount=Decimal(fee), currency="NGN", paid_at=NOW - timedelta(days=5),
                                   transaction_number=f"PSK-{order.id}-TEST")
        db.session.add(escrow)
        db.session.commit()
        return SimpleNamespace(vendor=vendor, store=store, buyer=buyer, order=order, escrow=escrow)
    return _make


@pytest.fixture
def make_admin():
    def _make(role="SUPERADMIN"):
        admin = AdminUser(name="Reviewer", email=f"{role.lower()}-{datetime.now().timestamp()}@siiqo.test", role=role)
        admin.set_password("not-a-real-password")
        db.session.add(admin)
        db.session.commit()
        return admin
    return _make


def admin_headers(admin):
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def raise_dispute(client, auth_headers, p, reason="Item arrived damaged", details="The screen is cracked."):
    return client.post("/api/escrow/disputes", headers=auth_headers(p.buyer),
                       json={"order_id": p.order.id, "reason": reason, "details": details})


def to_review(case):
    svc.move_to_review(db.session.get(DisputeCase, case.id), "admin")
    return db.session.get(DisputeCase, case.id)


def credits(vendor_id):
    return Ledger.query.filter_by(vendor_id=vendor_id, transaction_type="CREDIT").all()


# ── Raise and freeze ────────────────────────────────────────────────────────
def test_raise_freezes_payout_on_every_path(client, paid, auth_headers, make_admin, monkeypatch):
    from app.routes import escrow as escrow_routes
    from app.routes import payments as payment_routes
    from app.tasks.escrow_tasks import auto_release_escrow

    p = paid()
    res = raise_dispute(client, auth_headers, p)
    assert res.status_code == 201, res.get_json()
    body = res.get_json()["dispute"]
    assert body["status"] == DisputeStatus.AWAITING_SELLER and body["your_party"] == "buyer"
    db.session.expire_all()
    escrow = EscrowTransaction.query.filter_by(order_id=p.order.id).first()
    assert escrow.status == EscrowStatus.DISPUTED and p.order.status == "DISPUTED"
    assert escrow.dispute_id == body["reference"]

    # 1. The unified release pipeline refuses.
    assert escrow_routes.execute_order_escrow_release(p.order, escrow, source="test") is False
    # 2. Buyer "confirm received".
    assert client.post("/api/escrow/release", headers=auth_headers(p.buyer),
                       json={"order_id": p.order.id}).status_code == 400
    # 3. Token / code confirmation.
    token = escrow_routes.generate_order_token(p.order.id)
    assert client.post("/api/escrow/confirm-delivery-by-token",
                       json={"order_id": p.order.id, "token": token}).status_code == 400
    # 4. Hourly auto-release, even long after delivery.
    escrow.updated_at = NOW - timedelta(days=30)
    db.session.commit()
    auto_release_escrow()
    # 5. Admin tools.
    sa = make_admin()
    h = admin_headers(sa)
    for method, url, payload in [
        ("post", f"/api/admin/escrow/release/{p.order.id}", None),
        ("patch", f"/api/admin/orders/{p.order.id}/status", {"status": "COMPLETED"}),
        ("post", f"/api/admin/escrow/refund/{p.order.id}", None),
        ("post", f"/api/admin/payout/retry-order/{p.order.id}", {}),
    ]:
        res = getattr(client, method)(url, headers=h, json=payload)
        assert res.status_code == 409 and res.get_json()["code"] == "DISPUTE_OPEN", (url, res.get_json())
    # 6. Seller can't change the order status (could re-open a payout path).
    res = client.patch(f"/api/vendor/orders/{p.order.id}/status", headers=auth_headers(p.vendor),
                       json={"status": "PAID"})
    assert res.status_code == 409
    # 7. Payment-confirmation replays and delivery releases.
    payment_routes._handle_crypto_payment_confirmed(p.order.id, None)
    # 8. Low-level transfers refuse even if a caller had already moved the status.
    escrow.status = EscrowStatus.RELEASED   # simulate a caller that set RELEASED first (not committed)
    assert escrow_routes._paystack_payout_vendor(p.order, escrow) is False
    assert escrow_routes._flutterwave_payout_vendor(p.order, escrow) is False
    assert payment_routes._payout_vendor_via_daya(p.order, escrow) is False
    db.session.rollback()

    db.session.expire_all()
    escrow = EscrowTransaction.query.filter_by(order_id=p.order.id).first()
    assert escrow.status == EscrowStatus.DISPUTED
    assert credits(p.vendor.id) == []


def test_digital_delivery_and_rider_delivery_respect_dispute(client, paid, auth_headers, make_product,
                                                            make_user):
    from app.models.escrow import LogisticsAssignment
    from app.models.order import OrderItem
    from app.routes.escrow import _deliver_digital_products

    p = paid(status=EscrowStatus.IN_ESCROW)
    product = make_product(p.store, product_type="digital", file_url="https://example.org/f.pdf")
    db.session.add(OrderItem(order_id=p.order.id, product_id=product.id, quantity=1, price_at_purchase=10000))
    rider = make_user(role=UserRole.RIDER)
    rider.phone = "08030000000"
    db.session.add(LogisticsAssignment(order_id=p.order.id, partner_id=rider.id, rider_phone=rider.phone))
    db.session.commit()
    assert raise_dispute(client, auth_headers, p).status_code == 201

    escrow = EscrowTransaction.query.filter_by(order_id=p.order.id).first()
    assert _deliver_digital_products(p.order, escrow) is True     # handled: nothing released
    assert escrow.status == EscrowStatus.DISPUTED
    db.session.commit()

    res = client.post(f"/api/rider/orders/{p.order.id}/deliver", headers=auth_headers(rider), json={})
    assert res.status_code == 200, res.get_json()
    db.session.expire_all()
    assert EscrowTransaction.query.filter_by(order_id=p.order.id).first().status == EscrowStatus.DISPUTED
    assert credits(p.vendor.id) == []


def test_one_open_dispute_per_order(client, paid, auth_headers):
    p = paid()
    first = raise_dispute(client, auth_headers, p)
    assert first.status_code == 201
    second = raise_dispute(client, auth_headers, p)
    assert second.status_code == 409 and second.get_json()["code"] == "already_open"
    # The old endpoint goes through the same service.
    legacy = client.post("/api/escrow/dispute", headers=auth_headers(p.buyer),
                         json={"order_id": p.order.id, "reason": "again"})
    assert legacy.status_code == 400
    assert DisputeCase.query.filter_by(order_id=p.order.id).count() == 1
    # And the database refuses a second active case.
    db.session.add(DisputeCase(reference="DSP-DUPLICAT", order_id=p.order.id, vendor_id=p.vendor.id,
                               open_key=str(p.order.id), status=DisputeStatus.AWAITING_SELLER))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_only_parties_and_only_paid_orders(client, paid, auth_headers, make_user):
    p = paid(status=EscrowStatus.PENDING_PAYMENT)
    res = raise_dispute(client, auth_headers, p)
    assert res.status_code == 400 and res.get_json()["code"] == "not_disputable"
    q = paid()
    stranger = make_user()
    assert client.post("/api/escrow/disputes", headers=auth_headers(stranger),
                       json={"order_id": q.order.id, "reason": "x"}).status_code == 404
    # Sellers respond; they don't open cases on the new endpoint.
    assert client.post("/api/escrow/disputes", headers=auth_headers(q.vendor),
                       json={"order_id": q.order.id, "reason": "x"}).status_code == 403


def test_legacy_endpoint_opens_a_case_and_seller_may_raise(client, paid, auth_headers):
    p = paid()
    res = client.post("/api/escrow/dispute", headers=auth_headers(p.vendor),
                      json={"order_id": p.order.id, "reason": "Buyer unreachable"})
    assert res.status_code == 200 and res.get_json()["disputeId"].startswith("DSP-")
    case = DisputeCase.query.filter_by(order_id=p.order.id).one()
    assert case.raised_by == "seller" and case.status == DisputeStatus.AWAITING_BUYER


# ── Evidence ────────────────────────────────────────────────────────────────
def test_evidence_from_both_sides_then_review(client, paid, auth_headers, make_user, tmp_path, monkeypatch):
    monkeypatch.setattr(client.application, "root_path", str(tmp_path))
    p = paid()
    case_id = raise_dispute(client, auth_headers, p).get_json()["dispute"]["id"]
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64

    # Buyer adds a photo with a caption (multipart, shared upload helper).
    res = client.post(f"/api/escrow/disputes/{case_id}/evidence", headers=auth_headers(p.buyer),
                      data={"text": "Photo of the cracked screen", "photo": (io.BytesIO(png), "crack.png")},
                      content_type="multipart/form-data")
    assert res.status_code == 201, res.get_json()
    # Seller answers with text and a photo of the packed item.
    res = client.post(f"/api/escrow/disputes/{case_id}/evidence", headers=auth_headers(p.vendor),
                      data={"text": "It was packed in bubble wrap.", "photo": (io.BytesIO(png), "pack.png")},
                      content_type="multipart/form-data")
    assert res.status_code == 201
    # Wrong file type is refused by the upload helper.
    res = client.post(f"/api/escrow/disputes/{case_id}/evidence", headers=auth_headers(p.vendor),
                      data={"photo": (io.BytesIO(b"MZ"), "x.exe")}, content_type="multipart/form-data")
    assert res.status_code == 400
    # A stranger sees nothing.
    stranger = make_user()
    assert client.post(f"/api/escrow/disputes/{case_id}/evidence", headers=auth_headers(stranger),
                       json={"text": "hi"}).status_code == 404
    # Turn order: the buyer can't submit while the seller is answering.
    assert client.post(f"/api/escrow/disputes/{case_id}/submit", headers=auth_headers(p.buyer)).status_code == 409
    view = client.post(f"/api/escrow/disputes/{case_id}/submit", headers=auth_headers(p.vendor)).get_json()["dispute"]
    assert view["status"] == DisputeStatus.AWAITING_BUYER
    view = client.post(f"/api/escrow/disputes/{case_id}/submit", headers=auth_headers(p.buyer)).get_json()["dispute"]
    assert view["status"] == DisputeStatus.UNDER_REVIEW

    case = db.session.get(DisputeCase, case_id)
    parties = [(e.party, e.kind) for e in case.evidence]
    assert ("buyer", "text") in parties and ("buyer", "photo") in parties
    assert ("seller", "text") in parties and ("seller", "photo") in parties
    assert ("system", "timeline") in parties                 # order facts, citable
    assert all(e.created_at for e in case.evidence)           # timestamps
    photo = next(e for e in case.evidence if e.kind == "photo")
    assert photo.file_url.startswith("/static/uploads/dispute_evidence/")
    # Parties never see the reviewer summary.
    assert "summary" not in view
    # Evidence is closed once the case is under review.
    assert client.post(f"/api/escrow/disputes/{case_id}/evidence", headers=auth_headers(p.buyer),
                       json={"text": "one more"}).status_code == 409
    # Both see the same case from their order.
    seller_view = client.get(f"/api/escrow/disputes/order/{p.order.id}", headers=auth_headers(p.vendor)).get_json()
    assert seller_view["dispute"]["your_party"] == "seller"
    mine = client.get("/api/escrow/disputes/mine", headers=auth_headers(p.vendor)).get_json()["disputes"]
    assert [d["id"] for d in mine] == [case_id]


# ── Deadlines ───────────────────────────────────────────────────────────────
def test_seller_deadline_moves_case_to_review(client, paid, auth_headers, monkeypatch):
    monkeypatch.setenv("DISPUTE_SELLER_RESPONSE_DAYS", "3")
    p = paid()
    case_id = raise_dispute(client, auth_headers, p).get_json()["dispute"]["id"]
    case = db.session.get(DisputeCase, case_id)
    deadline = svc._aware(case.deadline_at)
    assert timedelta(days=2, hours=23) < deadline - NOW < timedelta(days=3, hours=1)

    assert svc.sweep_deadlines(NOW + timedelta(days=2)) == 0
    assert db.session.get(DisputeCase, case_id).status == DisputeStatus.AWAITING_SELLER
    assert svc.sweep_deadlines(NOW + timedelta(days=3, hours=1)) == 1
    case = db.session.get(DisputeCase, case_id)
    assert case.status == DisputeStatus.UNDER_REVIEW and case.review_reason == "seller_deadline"
    assert case.summary_status == "unavailable"           # AI off: no summary, raw evidence only
    assert svc.sweep_deadlines(NOW + timedelta(days=10)) == 0   # idempotent


def test_hourly_task_runs_the_deadline_sweep(client, paid, auth_headers):
    from app.tasks.escrow_tasks import auto_release_escrow
    p = paid()
    case_id = raise_dispute(client, auth_headers, p).get_json()["dispute"]["id"]
    case = db.session.get(DisputeCase, case_id)
    case.deadline_at = NOW - timedelta(minutes=1)
    db.session.commit()
    auto_release_escrow()
    assert db.session.get(DisputeCase, case_id).status == DisputeStatus.UNDER_REVIEW


# ── Summary job ─────────────────────────────────────────────────────────────
def _resp(obj):
    usage = SimpleNamespace(input_tokens=10, output_tokens=10, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, iterations=None)
    return SimpleNamespace(id="msg_test", model="claude-opus-5-5", stop_reason="end_turn", stop_details=None,
                           content=[SimpleNamespace(type="text", text=json.dumps(obj))], usage=usage)


class FakeClient:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = 0
        endpoint = SimpleNamespace(create=self._create)
        self.messages = endpoint
        self.beta = SimpleNamespace(messages=endpoint)

    def _create(self, **kwargs):
        self.calls += 1
        self.last_request = kwargs
        return _resp(self.payloads[min(self.calls, len(self.payloads)) - 1])


def _good_summary(case):
    ids = {e.party: e.cite_id for e in case.evidence}
    return {
        "timeline": [{"text": "The order was paid and marked delivered.", "cites": [ids["system"]]}],
        "buyer": [{"text": "The buyer says the screen arrived cracked.", "cites": [ids["buyer"]]}],
        "seller": [],
        "disagreements": [{"text": "The condition of the item on arrival is disputed.", "cites": [ids["buyer"]]}],
    }


def test_summary_ai_off_leaves_raw_evidence(client, paid, auth_headers):
    p = paid()
    case = to_review(db.session.get(DisputeCase, raise_dispute(client, auth_headers, p).get_json()["dispute"]["id"]))
    assert case.summary_status == "unavailable" and case.summary is None
    from app.models.jobs import Job
    assert Job.query.filter_by(type="dispute_summary").count() == 1


def test_summary_with_mocked_ai_is_cited_and_stored(client, paid, auth_headers, monkeypatch):
    monkeypatch.setenv("AI_ENABLED", "1")
    p = paid()
    case = db.session.get(DisputeCase, raise_dispute(client, auth_headers, p).get_json()["dispute"]["id"])
    fake = FakeClient(_good_summary(case))
    monkeypatch.setattr(svc, "get_ai_client", lambda: fake)
    case = to_review(case)
    assert case.summary_status == "ready", case.summary_error
    assert case.summary["buyer"][0]["cites"] and case.summary["prompt_version"] == "v1"
    assert fake.calls == 1
    sent = json.loads(fake.last_request["messages"][0]["content"].split("<inputs>")[1].split("</inputs>")[0])
    assert {e["id"] for e in sent["evidence"]} == {e.cite_id for e in case.evidence}
    # Admin detail shows it; the parties never do.
    sa_view = svc.admin_view(case)
    assert sa_view["summary"]["buyer"]
    assert "summary" not in svc.party_view(case, p.buyer.id)


@pytest.mark.parametrize("bad", [
    {"text": "The seller should refund the buyer.", "cites": None},          # recommendation words
    {"text": "The buyer is the winner here.", "cites": None},
    {"text": "The buyer says the screen arrived cracked.", "cites": []},     # no citation
    {"text": "The buyer says the screen arrived cracked.", "cites": ["E99999"]},  # unknown evidence
])
def test_summary_critic_rejects_and_no_summary_is_kept(client, paid, auth_headers, monkeypatch, bad):
    monkeypatch.setenv("AI_ENABLED", "1")
    p = paid()
    case = db.session.get(DisputeCase, raise_dispute(client, auth_headers, p).get_json()["dispute"]["id"])
    summary = _good_summary(case)
    if bad["cites"] is None:
        bad = {**bad, "cites": summary["buyer"][0]["cites"]}
    summary["buyer"] = [bad]
    fake = FakeClient(summary)
    monkeypatch.setattr(svc, "get_ai_client", lambda: fake)
    case = to_review(case)
    assert case.summary_status == "failed" and case.summary is None
    assert "critic rejected" in case.summary_error
    assert fake.calls == 2            # one retry, then give up


def test_critic_rules():
    known = {"E1", "E2"}
    ok = {"timeline": [{"text": "Paid on 1 May.", "cites": ["E1"]}],
          "buyer": [{"text": "The buyer says the phone is faulty.", "cites": ["E2"]}],
          "seller": [], "disagreements": []}
    assert svc.critique(ok, known) == []
    for word in ("refund", "released", "recommend", "should", "favour", "at fault", "deserves", "winner"):
        bad = {**ok, "buyer": [{"text": f"The buyer {word} it.", "cites": ["E2"]}]}
        assert svc.critique(bad, known), word
    two = {**ok, "buyer": [{"text": "The buyer says it broke. The seller disagrees.", "cites": ["E2"]}]}
    assert any("more than one sentence" in x for x in svc.critique(two, known))
    assert svc.critique({"timeline": [], "buyer": [], "seller": [], "disagreements": []}, known)


# ── Decision ────────────────────────────────────────────────────────────────
def _decide(client, admin, case_id, **body):
    return client.post(f"/api/admin/disputes/{case_id}/decide", headers=admin_headers(admin), json=body)


def test_decision_requires_superadmin_with_2fa(client, paid, auth_headers, make_admin, monkeypatch):
    p = paid()
    case = to_review(db.session.get(DisputeCase, raise_dispute(client, auth_headers, p).get_json()["dispute"]["id"]))
    body = {"outcome": "release_to_seller", "seller_fault": False}
    # A buyer token, a plain admin: refused.
    assert client.post(f"/api/admin/disputes/{case.id}/decide", headers=auth_headers(p.buyer), json=body).status_code == 403
    assert _decide(client, make_admin("ADMIN"), case.id, **body).status_code == 403
    # A superadmin without 2FA, when 2FA is enforced: refused.
    monkeypatch.setenv("ADMIN_2FA_REQUIRED", "true")
    sa = make_admin()
    res = _decide(client, sa, case.id, **body)
    assert res.status_code == 403 and res.get_json()["code"] == "2FA_REQUIRED"
    assert db.session.get(DisputeCase, case.id).status == DisputeStatus.UNDER_REVIEW
    # seller_fault is required.
    monkeypatch.setenv("ADMIN_2FA_REQUIRED", "false")
    assert _decide(client, sa, case.id, outcome="release_to_seller").status_code == 400
    # A plain admin can read the case.
    assert client.get(f"/api/admin/disputes/{case.id}", headers=admin_headers(make_admin("ADMIN"))).status_code == 200


def test_cannot_decide_before_review(client, paid, auth_headers, make_admin):
    p = paid()
    case_id = raise_dispute(client, auth_headers, p).get_json()["dispute"]["id"]
    res = _decide(client, make_admin(), case_id, outcome="refund_full", seller_fault=True)
    assert res.status_code == 409 and res.get_json()["code"] == "not_under_review"


def test_release_is_idempotent_and_audited(client, paid, auth_headers, make_admin):
    from app.routes.escrow import execute_order_escrow_release
    p = paid()
    case = to_review(db.session.get(DisputeCase, raise_dispute(client, auth_headers, p).get_json()["dispute"]["id"]))
    sa = make_admin()
    res = _decide(client, sa, case.id, outcome="release_to_seller", seller_fault=False, note="Courier photo shows intact box.")
    assert res.status_code == 200, res.get_json()
    d = res.get_json()["dispute"]
    assert d["status"] == "resolved" and d["decision"]["seller_payout_status"] == "released"
    db.session.expire_all()
    escrow = EscrowTransaction.query.filter_by(order_id=p.order.id).first()
    assert escrow.status == EscrowStatus.RELEASED
    assert [c.amount for c in credits(p.vendor.id)] == [Decimal("9700.00")]

    again = _decide(client, sa, case.id, outcome="release_to_seller", seller_fault=False)
    assert again.status_code == 200 and again.get_json()["already_decided"] is True
    other = _decide(client, sa, case.id, outcome="refund_full", seller_fault=True)
    assert other.status_code == 409
    assert execute_order_escrow_release(p.order, escrow, source="again") is True   # already released: no-op
    assert len(credits(p.vendor.id)) == 1

    logs = AdminAuditLog.query.filter_by(action="DISPUTE_DECIDED", resource_id=str(case.id)).all()
    assert len(logs) == 1 and logs[0].details["outcome"] == "release_to_seller"
    ev = TrustEvidence.query.filter_by(evidence_type="dispute_resolved", source_id=str(case.id)).all()
    assert len(ev) == 1 and ev[0].properties["seller_fault"] is False and ev[0].business_id == p.vendor.id


@pytest.mark.parametrize("seller_fault", [True, False])
def test_full_refund_is_manual_and_never_faked(client, paid, auth_headers, make_admin, seller_fault):
    from app.routes.escrow import execute_order_escrow_release
    p = paid()
    case = to_review(db.session.get(DisputeCase, raise_dispute(client, auth_headers, p).get_json()["dispute"]["id"]))
    sa = make_admin()
    res = _decide(client, sa, case.id, outcome="refund_full", seller_fault=seller_fault)
    assert res.status_code == 200, res.get_json()
    dec = res.get_json()["dispute"]["decision"]
    assert dec["refund_status"] == "manual_due" and dec["refund_amount"] == "10000.00"
    assert "Paystack dashboard" in dec["refund_instructions"] and "PSK-" in dec["refund_instructions"]

    # Until a person records the refund, the escrow stays frozen.
    db.session.expire_all()
    escrow = EscrowTransaction.query.filter_by(order_id=p.order.id).first()
    assert escrow.status == EscrowStatus.DISPUTED
    assert execute_order_escrow_release(p.order, escrow, source="sneaky") is False
    assert credits(p.vendor.id) == []

    h = admin_headers(sa)
    assert client.post(f"/api/admin/disputes/{case.id}/refund-sent", headers=h, json={}).status_code == 400
    assert client.post(f"/api/admin/disputes/{case.id}/refund-sent", headers=admin_headers(make_admin("ADMIN")),
                       json={"reference": "x"}).status_code == 403
    res = client.post(f"/api/admin/disputes/{case.id}/refund-sent", headers=h, json={"reference": "RF-123"})
    assert res.status_code == 200 and res.get_json()["dispute"]["decision"]["refund_status"] == "manual_sent"
    again = client.post(f"/api/admin/disputes/{case.id}/refund-sent", headers=h, json={"reference": "RF-123"})
    assert again.get_json()["already_recorded"] is True
    db.session.expire_all()
    escrow = EscrowTransaction.query.filter_by(order_id=p.order.id).first()
    assert escrow.status == EscrowStatus.REFUNDED and escrow.order.status == "CANCELLED"
    assert execute_order_escrow_release(escrow.order, escrow, source="after-refund") is False
    assert credits(p.vendor.id) == []

    # Evidence: the outcome row, and a "dispute_lost" only when the seller was at fault.
    resolved = TrustEvidence.query.filter_by(evidence_type="dispute_resolved", source_id=str(case.id)).one()
    assert resolved.properties["outcome"] == "refund_full" and resolved.properties["seller_fault"] is seller_fault
    lost = TrustEvidence.query.filter_by(evidence_type="dispute_lost", business_id=p.vendor.id).count()
    assert lost == (1 if seller_fault else 0)
    assert AdminAuditLog.query.filter_by(action="DISPUTE_REFUND_SENT").count() == 1


# ── Exact splits ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("total,fee,refund,seller,kept", [
    ("10000.00", "300.00", "2500.00", "7275.00", "225.00"),
    ("10000.00", "300.00", "0.00", "9700.00", "300.00"),
    ("10000.00", "300.00", "10000.00", "0.00", "0.00"),
    ("999.99", "30.00", "333.30", "646.69", "20.00"),
    ("1.00", "0.03", "0.33", "0.65", "0.02"),
    ("5000.00", "0.00", "1234.56", "3765.44", "0.00"),
])
def test_split_arithmetic_is_exact(total, fee, refund, seller, kept):
    out = svc.split_amounts(total, fee, refund)
    assert out["seller_net"] == Decimal(seller) and out["fee_kept"] == Decimal(kept)
    assert out["refund"] + out["seller_net"] + out["fee_kept"] == Decimal(total)


def test_split_percent_rounds_half_up():
    escrow = SimpleNamespace(amount=Decimal("999.99"), fee_amount=Decimal("30.00"))
    plan = svc.plan_outcome(escrow, "split", buyer_share_percent="33.33")
    assert plan["refund"] == Decimal("333.30")      # 333.296667 -> 333.30
    assert plan["refund"] + plan["seller_net"] + plan["fee_kept"] == Decimal("999.99")
    for bad in ("0", "100", "-5", "abc"):
        with pytest.raises(svc.DisputeError):
            svc.plan_outcome(escrow, "split", buyer_share_percent=bad)
    for bad in ("0", "999.99", "1000", None):
        with pytest.raises(svc.DisputeError):
            svc.plan_outcome(escrow, "refund_partial", refund_amount=bad)


@pytest.mark.parametrize("outcome,extra", [
    ("refund_partial", {"refund_amount": "2500"}),
    ("split", {"buyer_share_percent": 25}),
])
def test_partial_and_split_pay_seller_once(client, paid, auth_headers, make_admin, outcome, extra):
    from app.routes.escrow import execute_order_escrow_release
    from app.tasks.escrow_tasks import auto_release_escrow
    p = paid()
    case = to_review(db.session.get(DisputeCase, raise_dispute(client, auth_headers, p).get_json()["dispute"]["id"]))
    sa = make_admin()
    res = _decide(client, sa, case.id, outcome=outcome, seller_fault=True, **extra)
    assert res.status_code == 200, res.get_json()
    dec = res.get_json()["dispute"]["decision"]
    assert (dec["refund_amount"], dec["seller_amount"], dec["fee_kept"]) == ("2500.00", "7275.00", "225.00")
    assert dec["seller_payout_status"] == "credited" and dec["refund_status"] == "manual_due"

    # Repeat the decision, run every release path again: still one credit.
    assert _decide(client, sa, case.id, outcome=outcome, seller_fault=True, **extra).get_json()["already_decided"]
    db.session.expire_all()
    escrow = EscrowTransaction.query.filter_by(order_id=p.order.id).first()
    assert escrow.status == EscrowStatus.RELEASED
    assert execute_order_escrow_release(escrow.order, escrow, source="again") is True
    auto_release_escrow()
    rows = credits(p.vendor.id)
    assert [(r.amount, r.reference_id) for r in rows] == [(Decimal("7275.00"), f"DISPUTE-{case.reference}-SELLER")]
    # Refund for the buyer's part is recorded once a person sends it.
    h = admin_headers(sa)
    assert client.post(f"/api/admin/disputes/{case.id}/refund-sent", headers=h,
                       json={"reference": "RF-9"}).status_code == 200
    db.session.expire_all()
    assert EscrowTransaction.query.filter_by(order_id=p.order.id).first().status == EscrowStatus.RELEASED


def test_split_when_seller_was_paid_at_checkout(client, paid, auth_headers, make_admin):
    p = paid()
    p.store.paystack_subaccount_code = "ACCT_test"
    db.session.commit()
    case = to_review(db.session.get(DisputeCase, raise_dispute(client, auth_headers, p).get_json()["dispute"]["id"]))
    res = _decide(client, make_admin(), case.id, outcome="split", seller_fault=False, buyer_share_percent=50)
    dec = res.get_json()["dispute"]["decision"]
    assert dec["seller_payout_status"] == "already_settled"
    assert "recover" in dec["seller_payout_note"] and "recover" in dec["refund_instructions"]
    assert credits(p.vendor.id) == []                      # never paid twice


def test_daya_refund_is_manual_with_instructions(client, paid, auth_headers, make_admin):
    p = paid(payment_method="DAYA_BANK_TRANSFER")
    case = to_review(db.session.get(DisputeCase, raise_dispute(client, auth_headers, p).get_json()["dispute"]["id"]))
    res = _decide(client, make_admin(), case.id, outcome="refund_full", seller_fault=True)
    assert "Daya" in res.get_json()["dispute"]["decision"]["refund_instructions"]


# ── Legacy disputes ─────────────────────────────────────────────────────────
def test_legacy_disputed_escrow_is_adopted_for_review(client, paid, make_admin):
    p = paid()
    p.escrow.status = EscrowStatus.DISPUTED
    p.escrow.dispute_id = "DISP-OLD1"
    p.escrow.dispute_reason = "Wrong item"
    db.session.commit()
    h = admin_headers(make_admin("ADMIN"))
    listing = client.get("/api/admin/disputes", headers=h).get_json()
    assert listing["counts"]["under_review"] == 1
    case = DisputeCase.query.filter_by(order_id=p.order.id).one()
    assert case.status == DisputeStatus.UNDER_REVIEW and case.review_reason == "legacy"
    client.get("/api/admin/disputes", headers=h)
    assert DisputeCase.query.filter_by(order_id=p.order.id).count() == 1
    detail = client.get(f"/api/admin/disputes/{case.id}", headers=h).get_json()["dispute"]
    assert detail["money"]["total"] == "10000.00" and detail["can_decide"] is True
    assert any(e["party"] == "system" for e in detail["evidence"])
