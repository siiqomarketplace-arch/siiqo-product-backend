"""
Feature catalogue and entitlement checks (docs/VERIFIED_PRO_PLAN.md).

Every paid or limited feature has a key and a rule. Code asks
``has_feature(user_id, key)`` instead of checking subscriptions itself, so
plans and add-ons can change in one place:

- FREE            everyone
- PLAN            a paid plan is active (Pro tools unlock on payment)
- PLAN_VERIFIED   a paid plan is active AND the identity check has passed
                  (anything that puts buyers at risk)
- ADD_ON          bought separately (reserved for later add-ons)
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from app.extensions import db

FREE = "free"
PLAN = "plan"
PLAN_VERIFIED = "plan+verified"
ADD_ON = "add_on"

# A cancelled subscription keeps its features until the paid period ends.
PAID_STATUSES = ("ACTIVE", "CANCELLED_PENDING_EXPIRY")


@dataclass(frozen=True)
class Feature:
    key: str
    rule: str
    label: str
    # The old paid "Pro Verified" badge (storefront.is_pro_verified) also
    # unlocked a few tools; kept so nobody loses access they paid for.
    legacy_badge_grants: bool = False


FEATURES = {f.key: f for f in (
    Feature("unlimited_products", PLAN, "Unlimited product listings"),
    Feature("crm", PLAN, "CRM customer records and marketing tools"),
    Feature("invoices_unlimited", PLAN, "Unlimited invoices and billing tools", legacy_badge_grants=True),
    # Pay on Delivery stays a Pro feature for now; it moves to PLAN_VERIFIED
    # when Verified Pro billing ships (build step 3).
    Feature("pod", PLAN, "Accept Pay on Delivery"),
    Feature("verified_badge", PLAN_VERIFIED, "Verified mark on your store and Trust Passport"),
)}

FREE_LIMITS = {
    "products": 10,             # lifetime count, including deleted listings
    "invoices_per_month": 6,
}


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def active_subscription(user_id, now=None):
    """The seller's current paid subscription (latest end date), or None."""
    from app.models.admin import VendorSubscription
    now = now or _now()
    subs = (VendorSubscription.query
            .filter(VendorSubscription.vendor_id == int(user_id),
                    VendorSubscription.status.in_(PAID_STATUSES))
            .order_by(VendorSubscription.end_date.desc())
            .all())
    for sub in subs:
        if sub.end_date and _aware(sub.end_date) > now:
            return sub
    return None


def plan_active(user_id, now=None) -> bool:
    return active_subscription(user_id, now) is not None


def _legacy_badge_active(user_id, now=None) -> bool:
    from app.models.user import Storefront
    sf = Storefront.query.filter_by(vendor_id=int(user_id)).first()
    if not sf or not sf.is_pro_verified:
        return False
    expiry = _aware(sf.pro_verified_expires_at)
    return expiry is None or expiry > (now or _now())


def identity_verified(user_id, now=None) -> bool:
    from app.models.verification import VerificationStatus
    status = VerificationStatus.query.filter_by(user_id=int(user_id)).first()
    return bool(status and status.public_flags(now)["identity_verified"])


def has_feature(user_id, key, now=None) -> bool:
    feature = FEATURES.get(key)
    if feature is None:
        raise KeyError(f"Unknown feature: {key}")
    if feature.rule == FREE:
        return True
    if feature.rule == ADD_ON:
        return False  # no add-ons are sold yet
    paid = plan_active(user_id, now) or (feature.legacy_badge_grants and _legacy_badge_active(user_id, now))
    if feature.rule == PLAN:
        return paid
    if feature.rule == PLAN_VERIFIED:
        return paid and identity_verified(user_id, now)
    return False


def plan_summary(user_id, now=None) -> dict:
    sub = active_subscription(user_id, now)
    if not sub:
        return {"name": "FREE", "status": None, "billing_cycle": None, "end_date": None}
    name = sub.plan.name if sub.plan else "FREE"
    cycle = "annual" if "ANNUAL" in name else "lifetime" if "LIFETIME" in name else "monthly"
    return {
        "name": name,
        "status": sub.status,
        "billing_cycle": cycle,
        "end_date": _aware(sub.end_date).isoformat() if sub.end_date else None,
    }


def entitlements(user_id, now=None) -> dict:
    """Everything a client needs to show what the seller can use."""
    now = now or _now()
    return {
        "plan": plan_summary(user_id, now),
        "features": {key: has_feature(user_id, key, now) for key in FEATURES},
        "limits": {
            "products": None if has_feature(user_id, "unlimited_products", now) else FREE_LIMITS["products"],
            "invoices_per_month": (None if has_feature(user_id, "invoices_unlimited", now)
                                   else FREE_LIMITS["invoices_per_month"]),
        },
    }
