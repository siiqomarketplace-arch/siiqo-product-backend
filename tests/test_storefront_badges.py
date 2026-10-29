"""Store badges come from one source: a passed v2 check with an active
Verified Pro plan, or a legacy mark that hasn't expired."""
from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models.verification import VerificationStatus
from app.services import billing


def _passed_check(vendor):
    now = datetime.now(timezone.utc)
    db.session.add(VerificationStatus(user_id=vendor.id, identity_verified_at=now, valid_until=now + timedelta(days=365),
                                      source_provider="youverify"))
    db.session.commit()


def test_no_badge_by_default(make_vendor):
    _, store = make_vendor()
    d = store.to_public_dict()
    assert d["is_pro_verified"] is False and d["badge_verified"] is False


def test_passed_check_with_plan_shows_badge(make_vendor):
    vendor, store = make_vendor()
    _passed_check(vendor)
    billing.apply_subscription_payment(vendor, "PRO_MONTHLY", reference="B-1")
    d = store.to_public_dict()
    assert d["is_pro_verified"] is True and d["badge_verified"] is True


def test_passed_check_without_plan_shows_no_badge(make_vendor):
    vendor, store = make_vendor()
    _passed_check(vendor)
    assert store.to_public_dict()["badge_verified"] is False
