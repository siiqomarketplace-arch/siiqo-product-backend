"""Verification 2-3: expiry reminders (30/7/1 days, once per check), expiry
lapses the mark, risk events open a re-check, grace then pause, re-verifying
clears it, and every run is idempotent."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.audit import AdminAuditLog
from app.models.communication import Notification
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.verification import (CaseStatus, VerificationAuditEvent, VerificationCase,
                                     VerificationReminder, VerificationStatus, VerificationWatch)
from app.models.withdrawal import VendorBankAccount
from app.services import billing
from app.services import entitlements as ent
from app.services.verification import rechecks


@pytest.fixture
def emails(monkeypatch):
    import app.utils.email as email
    sent = []
    monkeypatch.setattr(email, "send_siiqo_email", lambda **k: sent.append(k) or True)
    return sent


def _now():
    return datetime.now(timezone.utc)


def _verified_seller(make_vendor, *, valid_for_days=365, payout_name="Test User"):
    """A seller with a passed (non-mock) identity check and an active plan."""
    vendor, store = make_vendor()
    now = _now()
    db.session.add(VerificationCase(user_id=vendor.id, kind="IDENTITY", provider="youverify",
                                    status=CaseStatus.PASSED, verified_name="Test User",
                                    idempotency_key=f"t-{uuid.uuid4().hex}", completed_at=now))
    db.session.add(VerificationStatus(user_id=vendor.id, identity_verified_at=now,
                                      valid_until=now + timedelta(days=valid_for_days),
                                      source_provider="youverify"))
    if payout_name:
        db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Bank", bank_code="058",
                                         account_number="0123456789", account_name=payout_name,
                                         is_default=True))
    db.session.commit()
    billing.apply_subscription_payment(vendor, "PRO_MONTHLY", reference=f"R-{uuid.uuid4().hex[:8]}")
    rechecks.on_identity_passed(vendor.id)  # the baseline taken when the check passed
    db.session.commit()
    return vendor, store


def _flags(vendor, now=None):
    return VerificationStatus.query.filter_by(user_id=vendor.id).one().public_flags(now)


def _notes(vendor):
    return Notification.query.filter_by(user_id=vendor.id).filter(
        Notification.type == "VERIFICATION").order_by(Notification.id).all()


def _events(vendor, event):
    return VerificationAuditEvent.query.filter_by(user_id=vendor.id, event=event).all()


def _watch(vendor):
    return VerificationWatch.query.filter_by(user_id=vendor.id).one()


# ── expiry reminders ────────────────────────────────────────────────────────

def test_each_reminder_fires_once_on_its_day(make_vendor, emails):
    vendor, _ = _verified_seller(make_vendor)
    valid_until = VerificationStatus.query.filter_by(user_id=vendor.id).one().valid_until
    valid_until = valid_until.replace(tzinfo=timezone.utc) if valid_until.tzinfo is None else valid_until

    def run(days_left):
        return rechecks.send_expiry_reminders(valid_until - timedelta(days=days_left))

    assert run(31) == 0                     # not yet
    assert run(30) == 1                     # 30-day reminder
    assert run(29.5) == 0 and run(30) == 0  # once only, also on re-runs
    assert run(8) == 0                      # between windows: nothing new
    assert run(7) == 1                      # 7-day reminder
    assert run(6) == 0
    assert run(1) == 1                      # 1-day reminder
    assert run(0.5) == 0
    db.session.commit()

    kinds = [r.kind for r in VerificationReminder.query.filter_by(user_id=vendor.id).order_by(
        VerificationReminder.id)]
    assert kinds == ["D30", "D7", "D1"]
    notes = _notes(vendor)
    assert len(notes) == 3 and all("/vendor/trust?reverify=1" in n.message for n in notes)
    assert len(emails) == 3
    assert all(e["cta_url"].endswith("/vendor/trust?reverify=1") for e in emails)
    assert len(_events(vendor, "REMINDER_SENT")) == 3


def test_missed_day_sends_only_the_most_urgent_reminder(make_vendor, emails):
    vendor, _ = _verified_seller(make_vendor, valid_for_days=5)
    assert rechecks.send_expiry_reminders() == 1
    db.session.commit()
    assert [r.kind for r in VerificationReminder.query.filter_by(user_id=vendor.id)] == ["D7"]


def test_new_check_starts_a_new_set_of_reminders(make_vendor, emails):
    vendor, _ = _verified_seller(make_vendor, valid_for_days=20)
    assert rechecks.send_expiry_reminders() == 1
    row = VerificationStatus.query.filter_by(user_id=vendor.id).one()
    row.valid_until = _now() + timedelta(days=25)   # re-verified: a new validity date
    db.session.commit()
    assert rechecks.send_expiry_reminders() == 1


def test_expiry_lapses_the_mark_and_daily_job_is_idempotent(make_vendor, emails):
    vendor, _ = _verified_seller(make_vendor, valid_for_days=2)
    assert _flags(vendor)["identity_verified"] is True
    assert ent.has_feature(vendor.id, "verified_badge") is True

    row = VerificationStatus.query.filter_by(user_id=vendor.id).one()
    row.valid_until = _now() - timedelta(hours=2)
    db.session.commit()

    billing.run_daily_billing_tasks()
    billing.run_daily_billing_tasks()

    row = VerificationStatus.query.filter_by(user_id=vendor.id).one()
    assert row.subscription_active is True      # the plan is fine ...
    assert _flags(vendor)["identity_verified"] is False   # ... but the check ended
    assert ent.has_feature(vendor.id, "verified_badge") is False
    titles = [n.title for n in _notes(vendor)]
    assert titles == ["Your identity check has ended"]
    assert len(emails) == 1


def test_old_expiry_is_not_notified(make_vendor, emails):
    vendor, _ = _verified_seller(make_vendor)
    row = VerificationStatus.query.filter_by(user_id=vendor.id).one()
    row.valid_until = _now() - timedelta(days=30)
    db.session.commit()
    assert rechecks.send_expiry_reminders() == 0


def test_mark_also_lapses_with_the_plan(make_vendor, emails):
    """The existing sync_verification_flag path: plan lapse pauses the mark."""
    vendor, _ = _verified_seller(make_vendor)
    from app.models.admin import VendorSubscription
    sub = VendorSubscription.query.filter_by(vendor_id=vendor.id, status="ACTIVE").one()
    sub.end_date = _now() - timedelta(days=10)
    db.session.commit()
    billing.run_daily_billing_tasks()
    assert _flags(vendor)["identity_verified"] is False


# ── risk events ─────────────────────────────────────────────────────────────

def test_payout_account_in_a_different_name_opens_recheck(client, make_vendor, auth_headers, emails):
    vendor, _ = _verified_seller(make_vendor)
    other = VendorBankAccount(vendor_id=vendor.id, bank_name="Bank", bank_code="044",
                              account_number="9876543210", account_name="Ada Obi", is_default=False)
    db.session.add(other)
    db.session.commit()

    res = client.patch(f"/api/withdrawal/bank-accounts/{other.id}/set-default", headers=auth_headers(vendor))
    assert res.status_code == 200
    watch = _watch(vendor)
    assert watch.recheck_reason_code == rechecks.PAYOUT_NAME_CHANGED
    assert len(_events(vendor, "RECHECK_REQUESTED")) == 1
    assert [n.title for n in _notes(vendor)] == ["Re-check needed to keep your Verified mark"]
    assert len(emails) == 1
    assert _flags(vendor)["identity_verified"] is True   # still inside the grace period


def test_same_name_payout_account_is_not_a_risk_event(client, make_vendor, auth_headers, emails):
    vendor, _ = _verified_seller(make_vendor)
    other = VendorBankAccount(vendor_id=vendor.id, bank_name="Bank", bank_code="044",
                              account_number="9876543210", account_name="USER TEST", is_default=False)
    db.session.add(other)
    db.session.commit()
    client.patch(f"/api/withdrawal/bank-accounts/{other.id}/set-default", headers=auth_headers(vendor))
    assert _watch(vendor).recheck_reason_code is None


def test_store_name_change_opens_recheck(client, make_vendor, auth_headers, emails):
    vendor, _ = _verified_seller(make_vendor)
    res = client.patch("/api/vendor/update-settings", json={"business_name": "Brand New Name"},
                       headers=auth_headers(vendor))
    assert res.status_code == 200, res.get_json()
    assert _watch(vendor).recheck_reason_code == rechecks.STORE_NAME_CHANGED


def test_store_name_case_or_spacing_is_not_a_change(client, make_vendor, auth_headers, emails):
    vendor, store = _verified_seller(make_vendor)
    client.patch("/api/vendor/update-settings", json={"business_name": f"  {store.store_name.upper()} "},
                 headers=auth_headers(vendor))
    assert _watch(vendor).recheck_reason_code is None


def test_unverified_seller_is_never_rechecked(client, make_vendor, auth_headers, emails):
    vendor, _ = make_vendor()
    client.patch("/api/vendor/update-settings", json={"business_name": "Another"}, headers=auth_headers(vendor))
    assert VerificationWatch.query.filter_by(user_id=vendor.id).first() is None
    assert rechecks.evaluate_risk_events(vendor.id) is None


def _lost_dispute(vendor, make_order):
    order = make_order(vendor)
    db.session.add(EscrowTransaction(order_id=order.id, transaction_number=f"T-{uuid.uuid4().hex[:10]}",
                                     status=EscrowStatus.REFUNDED, amount=1000,
                                     dispute_id=f"DISP-{uuid.uuid4().hex[:8]}"))
    db.session.commit()


def test_seller_fault_disputes_open_recheck(make_vendor, make_order, emails, monkeypatch):
    monkeypatch.setenv("VERIFICATION_RECHECK_DISPUTES", "3")
    vendor, _ = _verified_seller(make_vendor)
    for _ in range(2):
        _lost_dispute(vendor, make_order)
    rechecks.run_verification_upkeep()
    assert _watch(vendor).recheck_reason_code is None      # below the threshold

    _lost_dispute(vendor, make_order)
    result = rechecks.run_verification_upkeep()
    assert result["rechecks_opened"] == 1
    assert _watch(vendor).recheck_reason_code == rechecks.SELLER_FAULT_DISPUTES
    event = _events(vendor, "RECHECK_REQUESTED")[0]
    assert event.details["disputes"] == 3


def test_disputes_before_the_last_check_do_not_count(make_vendor, make_order, emails, monkeypatch):
    monkeypatch.setenv("VERIFICATION_RECHECK_DISPUTES", "3")
    vendor, _ = _verified_seller(make_vendor)
    for _ in range(3):
        _lost_dispute(vendor, make_order)
    _watch(vendor).baseline_at = _now() + timedelta(seconds=5)   # check passed after the disputes
    db.session.commit()
    rechecks.run_verification_upkeep()
    assert _watch(vendor).recheck_reason_code is None


def test_daily_sweep_catches_changes_made_elsewhere(make_vendor, emails):
    vendor, store = _verified_seller(make_vendor)
    store.store_name = "Changed outside the hooked routes"
    db.session.commit()
    assert rechecks.run_verification_upkeep()["rechecks_opened"] == 1
    assert rechecks.run_verification_upkeep()["rechecks_opened"] == 0   # idempotent
    assert len(_events(vendor, "RECHECK_REQUESTED")) == 1
    assert len(emails) == 1


def test_first_sweep_sets_baseline_for_older_checks(make_vendor, emails):
    vendor, store = _verified_seller(make_vendor)
    db.session.delete(_watch(vendor))
    db.session.commit()
    store.store_name = "Renamed before this shipped"
    db.session.commit()
    rechecks.run_verification_upkeep()
    watch = _watch(vendor)
    assert watch.recheck_reason_code is None
    assert watch.baseline_store_name == "Renamed before this shipped"


# ── admin flag ──────────────────────────────────────────────────────────────

def _admin_headers(role="SUPERADMIN"):
    admin = AdminUser(name="Ops", email=f"{role.lower()}-{uuid.uuid4().hex[:4]}@example.com", role=role)
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return admin, {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def test_admin_flag_requires_superadmin(client, make_vendor, auth_headers, emails):
    vendor, _ = _verified_seller(make_vendor)
    _, admin_h = _admin_headers("ADMIN")
    url = f"/api/verification/admin/sellers/{vendor.id}/recheck"
    assert client.post(url, json={"note": "x"}, headers=admin_h).status_code == 403
    assert client.post(url, json={"note": "x"}, headers=auth_headers(vendor)).status_code == 403
    assert VerificationWatch.query.filter_by(user_id=vendor.id).one().recheck_reason_code is None


def test_admin_flag_opens_recheck_and_is_audited(client, make_vendor, make_user, emails):
    vendor, _ = _verified_seller(make_vendor)
    admin, h = _admin_headers()
    url = f"/api/verification/admin/sellers/{vendor.id}/recheck"

    assert client.post(url, json={}, headers=h).status_code == 400   # note required
    res = client.post(url, json={"note": "Buyer report #12"}, headers=h)
    assert res.status_code == 201, res.get_json()
    assert res.get_json()["reason_code"] == rechecks.ADMIN_REQUEST
    again = client.post(url, json={"note": "Again"}, headers=h)
    assert again.status_code == 200 and again.get_json()["status"] == "already_open"

    assert len(_events(vendor, "RECHECK_REQUESTED")) == 1
    assert _events(vendor, "RECHECK_REQUESTED")[0].actor_id == admin.id
    logs = AdminAuditLog.query.filter_by(action="VERIFICATION_RECHECK_REQUEST").all()
    assert len(logs) == 2
    # The internal note never reaches the seller.
    assert all("Buyer report" not in n.message for n in _notes(vendor))
    assert all("Buyer report" not in e["notice_text"] for e in emails)

    unverified = make_user()
    res = client.post(f"/api/verification/admin/sellers/{unverified.id}/recheck",
                      json={"note": "x"}, headers=h)
    assert res.status_code == 409


# ── grace, pause, re-verify ─────────────────────────────────────────────────

def test_grace_then_pause(make_vendor, emails, monkeypatch):
    monkeypatch.setenv("VERIFICATION_RECHECK_GRACE_DAYS", "7")
    vendor, _ = _verified_seller(make_vendor)
    now = _now()
    rechecks.request_recheck(vendor.id, rechecks.STORE_NAME_CHANGED, now=now)
    db.session.commit()

    assert _flags(vendor, now + timedelta(days=6))["identity_verified"] is True
    assert ent.has_feature(vendor.id, "verified_badge", now + timedelta(days=6)) is True
    assert _flags(vendor, now + timedelta(days=7, minutes=1))["identity_verified"] is False
    assert ent.has_feature(vendor.id, "verified_badge", now + timedelta(days=8)) is False

    assert rechecks.record_pauses(now + timedelta(days=6)) == 0
    assert rechecks.record_pauses(now + timedelta(days=8)) == 1
    assert rechecks.record_pauses(now + timedelta(days=9)) == 0   # logged once
    db.session.commit()
    assert len(_events(vendor, "MARK_PAUSED")) == 1
    assert "Your Verified mark is paused" in [n.title for n in _notes(vendor)]


def test_grace_days_are_configurable(make_vendor, emails, monkeypatch):
    monkeypatch.setenv("VERIFICATION_RECHECK_GRACE_DAYS", "2")
    vendor, _ = _verified_seller(make_vendor)
    now = _now()
    rechecks.request_recheck(vendor.id, rechecks.ADMIN_REQUEST, now=now)
    db.session.commit()
    assert _flags(vendor, now + timedelta(days=3))["identity_verified"] is False


def test_status_endpoint_shows_recheck_and_reverify_dates(client, make_vendor, auth_headers, emails):
    vendor, _ = _verified_seller(make_vendor)
    h = auth_headers(vendor)
    body = client.get("/api/verification/status", headers=h).get_json()
    assert body["recheck"] is None and body["reverify_by"] is None and body["can_reverify"] is False

    rechecks.request_recheck(vendor.id, rechecks.PAYOUT_NAME_CHANGED)
    db.session.commit()
    body = client.get("/api/verification/status", headers=h).get_json()
    assert body["recheck"]["reason_code"] == rechecks.PAYOUT_NAME_CHANGED
    assert body["recheck"]["reason"] == rechecks.RISK_EVENTS[rechecks.PAYOUT_NAME_CHANGED]
    assert body["reverify_by"] == body["recheck"]["reverify_by"]
    assert body["can_reverify"] is True and body["mark_paused"] is False


def test_expiring_check_shows_reverify_by(client, make_vendor, auth_headers, emails):
    vendor, _ = _verified_seller(make_vendor, valid_for_days=20)
    body = client.get("/api/verification/status", headers=auth_headers(vendor)).get_json()
    assert body["reverify_by"] == body["valid_until"]
    assert body["can_reverify"] is True


# Mock-provider flow (staff testing) for re-verification through the real routes.
NIN_PASS = "12345678900"


@pytest.fixture
def mock_seller(make_vendor, monkeypatch):
    vendor, store = make_vendor()
    monkeypatch.setenv("VERIFICATION_V2_ALLOWLIST", str(vendor.id))
    monkeypatch.delenv("VERIFICATION_V2_ENABLED", raising=False)
    monkeypatch.setenv("VERIFICATION_PROVIDER", "mock")
    return vendor, store


def _verify_via_mock(client, h):
    started = client.post("/api/verification/identity/start",
                          json={"nin": NIN_PASS, "seller_type": "INDIVIDUAL", "consent": True}, headers=h)
    assert started.status_code == 201, started.get_json()
    ref = db.session.get(VerificationCase, started.get_json()["case"]["id"]).provider_ref
    done = client.post("/api/verification/mock/complete", json={"ref": ref}, headers=h)
    assert done.status_code == 200
    assert done.get_json()["case"]["status"] == "PASSED"


def test_reverify_only_when_due_and_passing_clears_recheck(client, mock_seller, auth_headers, emails):
    vendor, store = mock_seller
    h = auth_headers(vendor)
    _verify_via_mock(client, h)

    # Not due: a second check is refused.
    again = client.post("/api/verification/identity/start",
                        json={"nin": NIN_PASS, "seller_type": "INDIVIDUAL", "consent": True}, headers=h)
    assert again.status_code == 409 and again.get_json()["code"] == "ALREADY_VERIFIED"

    store.store_name = "New store name"
    db.session.commit()
    rechecks.check_after_change(vendor.id)
    assert _watch(vendor).recheck_reason_code == rechecks.STORE_NAME_CHANGED

    _verify_via_mock(client, h)   # re-check open: a new check is allowed, and passes
    watch = _watch(vendor)
    assert watch.recheck_reason_code is None and watch.recheck_grace_until is None
    assert watch.baseline_store_name == "New store name"
    assert len(_events(vendor, "RECHECK_CLEARED")) == 1
    body = client.get("/api/verification/status", headers=h).get_json()
    assert body["recheck"] is None and body["identity_verified"] is True


def test_reverify_clears_a_paused_mark(make_vendor, emails):
    vendor, _ = _verified_seller(make_vendor)
    past = _now() - timedelta(days=10)
    rechecks.request_recheck(vendor.id, rechecks.ADMIN_REQUEST, now=past)
    db.session.commit()
    assert _flags(vendor)["identity_verified"] is False

    rechecks.on_identity_passed(vendor.id)
    db.session.commit()
    assert _flags(vendor)["identity_verified"] is True


# ── failed re-verification: fraud / face mismatch pause, technical keeps ────

from app.models.verification import CaseStatus as _CS  # noqa: E402
from app.services.verification import service as ver  # noqa: E402
from app.services.verification.base import IdentityResult, Outcome  # noqa: E402
from app.services.verification.youverify_provider import identity_result_from_data  # noqa: E402

RE_NIN = "12345678901"


def _reverify_case(vendor):
    case = VerificationCase(user_id=vendor.id, kind="IDENTITY", provider="youverify",
                            status=_CS.IN_PROGRESS, provider_ref=f"yv-{uuid.uuid4().hex[:8]}",
                            idempotency_key=f"t-{uuid.uuid4().hex}",
                            id_number_hash=ver.hash_id_number(RE_NIN))
    db.session.add(case)
    db.session.commit()
    return case


def _youverify(case, **data):
    return identity_result_from_data({"id": case.provider_ref, "firstName": "Test", "lastName": "User", **data})


def test_youverify_face_mismatch_pauses_mark_at_once(make_vendor, emails):
    vendor, _ = _verified_seller(make_vendor)
    case = _reverify_case(vendor)
    ver.apply_identity_result(case, _youverify(case, status="found", selfieValidation=False))

    assert case.status == _CS.FAILED and case.reason_code == "FACE_MISMATCH"
    assert _flags(vendor)["identity_verified"] is False          # no grace period
    assert ent.has_feature(vendor.id, "verified_badge") is False
    assert [e.reason_code for e in _events(vendor, "FACE_MISMATCH_RESULT")] == [rechecks.FACE_NOT_CONFIRMED]
    assert len(_events(vendor, "MARK_PAUSED")) == 1
    state = rechecks.seller_state(vendor.id)
    assert state["mark_paused"] is True and state["can_reverify"] is True   # may retry
    assert [n.title for n in _notes(vendor)] == ["We couldn't confirm your identity"]
    assert emails[0]["cta_url"].endswith("/vendor/trust?reverify=1")
    # Re-runs of the daily job do not log or notify again.
    rechecks.run_verification_upkeep()
    assert len(_events(vendor, "MARK_PAUSED")) == 1 and len(_notes(vendor)) == 1


def test_fraud_result_pauses_mark_and_sends_seller_to_support(make_vendor, emails, monkeypatch):
    vendor, _ = _verified_seller(make_vendor)
    rechecks.request_recheck(vendor.id, rechecks.STORE_NAME_CHANGED)   # an open re-check is superseded
    db.session.commit()
    case = _reverify_case(vendor)
    ver.apply_identity_result(case, IdentityResult(provider_ref=case.provider_ref, outcome=Outcome.FRAUD,
                                                   reason_code="SPOOF_DETECTED"))

    assert case.status == _CS.FAILED_FRAUD
    assert _flags(vendor)["identity_verified"] is False
    event = _events(vendor, "FRAUD_RESULT")[0]
    assert event.reason_code == rechecks.IDENTITY_NOT_CONFIRMED
    assert event.details["replaces"] == rechecks.STORE_NAME_CHANGED
    assert len(_events(vendor, "MARK_PAUSED")) == 1
    state = rechecks.seller_state(vendor.id)
    assert state["recheck"]["contact_support"] is True and state["can_reverify"] is False

    note = _notes(vendor)[-1]
    assert note.title == "We couldn't confirm your identity"
    assert "support@siiqo.com" in note.message and "Re-verify:" not in note.message
    assert "fraud" not in note.message.lower() and "spoof" not in note.message.lower()
    assert emails[-1]["cta_url"] is None

    # No self-service way back in after a fraud result.
    monkeypatch.setenv("VERIFICATION_V2_ALLOWLIST", str(vendor.id))
    monkeypatch.setenv("VERIFICATION_PROVIDER", "mock")
    with pytest.raises(ver.VerificationError) as exc:
        ver.start_identity(vendor, seller_type="INDIVIDUAL", nin=RE_NIN, consent=True)
    assert exc.value.code == "CONTACT_SUPPORT"

    # Idempotent: a second fraud signal does not log or notify again.
    assert rechecks.pause_after_failed_check(vendor.id, case) is False


def test_fraud_on_a_first_check_has_no_mark_to_pause(make_vendor, emails):
    vendor, _ = make_vendor()
    case = _reverify_case(vendor)
    ver.apply_identity_result(case, IdentityResult(provider_ref=case.provider_ref, outcome=Outcome.FRAUD,
                                                   reason_code="SPOOF_DETECTED"))
    assert case.status == _CS.FAILED_FRAUD
    assert _events(vendor, "MARK_PAUSED") == [] and _notes(vendor) == []


@pytest.mark.parametrize("data", [
    {"status": "failed"},          # provider error -> INCONCLUSIVE / PROVIDER_FAILED
    {"status": "something_new"},   # unknown provider status -> INCONCLUSIVE / STATUS_*
    {"status": "not_found"},       # NIN not found (typo) -> MISMATCH / NIN_NOT_FOUND
])
def test_technical_or_data_entry_failures_keep_the_mark(make_vendor, emails, data):
    vendor, _ = _verified_seller(make_vendor)
    case = _reverify_case(vendor)
    ver.apply_identity_result(case, _youverify(case, **data))
    assert _flags(vendor)["identity_verified"] is True
    assert _events(vendor, "MARK_PAUSED") == []


def test_unreadable_selfie_keeps_the_mark(make_vendor, emails):
    vendor, _ = _verified_seller(make_vendor)
    case = _reverify_case(vendor)
    ver.apply_identity_result(case, IdentityResult(provider_ref=case.provider_ref, outcome=Outcome.RETRY,
                                                   reason_code="REJECTED_400"))
    assert case.status == _CS.FAILED_RETRYABLE
    assert _flags(vendor)["identity_verified"] is True


def test_provider_timeout_keeps_the_mark_and_allows_retry(make_vendor, emails, monkeypatch):
    from app.services.verification.youverify_provider import ProviderUnavailable

    class _Down:
        name = "youverify"

        def submit_identity(self, **kwargs):
            raise ProviderUnavailable("timeout")

    monkeypatch.setattr(ver, "get_provider", lambda: _Down())
    vendor, _ = _verified_seller(make_vendor)
    case = _reverify_case(vendor)
    with pytest.raises(ver.VerificationError) as exc:
        ver.submit_identity_selfie(vendor, case.id, nin=RE_NIN, selfie_image="x" * 2000)
    assert exc.value.code == "PROVIDER_UNAVAILABLE"
    assert db.session.get(VerificationCase, case.id).status == _CS.IN_PROGRESS   # can retry
    assert _flags(vendor)["identity_verified"] is True


# ── privacy export ──────────────────────────────────────────────────────────

def test_privacy_export_includes_own_lifecycle_rows_without_admin_notes(client, make_vendor, auth_headers,
                                                                         emails):
    import json
    vendor, _ = _verified_seller(make_vendor, valid_for_days=20)
    other, _ = _verified_seller(make_vendor, valid_for_days=20)
    rechecks.send_expiry_reminders()
    db.session.commit()
    admin, h = _admin_headers()
    assert client.post(f"/api/verification/admin/sellers/{vendor.id}/recheck",
                       json={"note": "Internal: buyer report 77"}, headers=h).status_code == 201

    res = client.get("/api/privacy/export", headers=auth_headers(vendor))
    assert res.status_code == 200
    data = json.loads(res.data)["verification"]
    assert data["recheck"]["reason_code"] == rechecks.ADMIN_REQUEST
    assert data["recheck"]["reason"] == rechecks.RISK_EVENTS[rechecks.ADMIN_REQUEST]
    assert [r["kind"] for r in data["reminders"]] == ["D30"]
    names = [e["event"] for e in data["events"]]
    assert "REMINDER_SENT" in names and "RECHECK_REQUESTED" in names
    requested = next(e for e in data["events"] if e["event"] == "RECHECK_REQUESTED")
    assert requested["by"] == "Siiqo" and "note" not in (requested["details"] or {})

    raw = res.data.decode()
    assert "buyer report 77" not in raw
    assert admin.email not in raw
    # Only this seller's rows.
    assert VerificationAuditEvent.query.filter_by(user_id=other.id).count() >= 1
    assert len(data["events"]) == VerificationAuditEvent.query.filter_by(user_id=vendor.id).count()
    assert len(data["reminders"]) == 1


def test_dispute_cases_count_only_when_the_reviewer_found_seller_fault(make_vendor, make_order, emails, monkeypatch):
    from app.models.dispute import DisputeCase
    monkeypatch.setenv("VERIFICATION_RECHECK_DISPUTES", "2")
    vendor, _ = _verified_seller(make_vendor)

    def case(fault):
        order = make_order(vendor)
        db.session.add(DisputeCase(reference=f"D-{uuid.uuid4().hex[:8]}", order_id=order.id, vendor_id=vendor.id,
                                   status="resolved", outcome="refund_full", seller_fault=fault,
                                   decided_at=_now() + timedelta(seconds=1)))
        db.session.commit()

    case(False)
    case(False)
    rechecks.run_verification_upkeep()
    assert _watch(vendor).recheck_reason_code is None   # refunded but not the seller's fault

    case(True)
    case(True)
    assert rechecks.run_verification_upkeep()["rechecks_opened"] == 1
    assert _watch(vendor).recheck_reason_code == rechecks.SELLER_FAULT_DISPUTES
