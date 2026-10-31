"""
Trust Platform API v1 (docs/PLATFORM_API_V1.md): keys, tiers, quotas,
test-mode fixtures and the Growth subscription.

- Live keys (`sqp_live_…`, and the older plain `sqp_…` keys) read real public
  Passports. Test keys (`sqp_test_…`) only ever answer from FIXTURES and are
  never billed.
- Tiers are per API client: Free, Growth (paid via a Paystack plan) and Scale
  (limits set by a superadmin). Test keys have their own small quota.
- Monthly counters live in platform_usage; per-minute rates use the limiter's
  storage (Redis in production, memory locally).
"""
import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from dateutil.relativedelta import relativedelta
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.services import passport as passport_svc

logger = logging.getLogger(__name__)

TIERS = {
    # tier: (requests per month, requests per minute)
    "free": (5_000, 60),
    "growth": (100_000, 600),
}
TEST_LIMITS = (1_000, 30)
GROWTH_GRACE = timedelta(days=3)
PAYSTACK_BASE_URL = "https://api.paystack.co"
GROWTH_REF_PREFIX = "PLAT-GROWTH-"

TEST_ISSUER = "https://siiqo.com/test"
TEST_KID = "siiqo-test"


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


# ── test-mode fixtures ─────────────────────────────────────────────────────

FIXTURE_SLUGS = ("test-trusted-store", "test-new-seller", "test-unverified")

FIXTURES = {
    "test-trusted-store": {
        "v": passport_svc.CLAIMS_VERSION, "store_slug": "test-trusted-store", "store_name": "Test Trusted Store",
        "url": "https://siiqo.com/id/test-trusted-store", "tier": "GOLD", "score": 82, "score_max": 100,
        "verified": True, "business_registered": True, "completed_orders": 128, "member_since": "2025-03-02",
    },
    "test-new-seller": {
        "v": passport_svc.CLAIMS_VERSION, "store_slug": "test-new-seller", "store_name": "Test New Seller",
        "url": "https://siiqo.com/id/test-new-seller", "tier": "NEW", "score": None, "score_max": 100,
        "verified": False, "business_registered": False, "completed_orders": 0, "member_since": "2026-10-01",
    },
    "test-unverified": {
        "v": passport_svc.CLAIMS_VERSION, "store_slug": "test-unverified", "store_name": "Test Unverified Store",
        "url": "https://siiqo.com/id/test-unverified", "tier": "SILVER", "score": 55, "score_max": 100,
        "verified": False, "business_registered": False, "completed_orders": 24, "member_since": "2025-11-20",
    },
}

# Identifiers that match a fixture in test mode (documented on /developers).
FIXTURE_IDENTIFIERS = {
    "test-trusted-store": "test-trusted-store",
    "siiqo.com/id/test-trusted-store": "test-trusted-store",
    "@test_trusted_store": "test-trusted-store",
    "+2348000000001": "test-trusted-store",
    "test-new-seller": "test-new-seller",
    "@test_new_seller": "test-new-seller",
    "+2348000000002": "test-new-seller",
    "test-unverified": "test-unverified",
    "@test_unverified": "test-unverified",
    "+2348000000003": "test-unverified",
    "058:0000000001": "test-trusted-store",
}


def fixture(slug):
    claims = FIXTURES.get((slug or "").strip().lower())
    return {**claims, "test": True} if claims else None


def fixture_for_identifier(identifier):
    text = (identifier or "").strip().lower()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.startswith("www."):
        text = text[4:]
    text = text.rstrip("/")
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits.startswith("0") and len(digits) == 11:
        digits = "234" + digits[1:]
    if text not in FIXTURE_IDENTIFIERS and digits and f"+{digits}" in FIXTURE_IDENTIFIERS:
        text = f"+{digits}"
    slug = FIXTURE_IDENTIFIERS.get(text)
    return fixture(slug) if slug else None


def _test_secret():
    base = os.environ.get("PASSPORT_HMAC_SECRET") or os.environ.get("SECRET_KEY") or "siiqo-secret-key-salt-2026"
    return hashlib.sha256(f"passport-test-statement:{base}".encode()).digest()


def sign_test(claims):
    """Test statements use their own issuer, key id and secret: they never
    verify as real statements on /api/passport/verify."""
    now = _now()
    payload = {**claims, "iss": TEST_ISSUER, "sub": claims["store_slug"], "test": True,
               "iat": int(now.timestamp()), "exp": int((now + passport_svc.STATEMENT_TTL).timestamp())}
    return jwt.encode(payload, _test_secret(), algorithm="HS256", headers={"kid": TEST_KID}), "HS256"


# ── keys ───────────────────────────────────────────────────────────────────

def raw_key_from_request(request):
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-Siiqo-Api-Key") or "").strip()


def is_test(client):
    return passport_svc.key_mode(client) == "test"


# ── tiers and limits ───────────────────────────────────────────────────────

def active_growth(client_id, now=None):
    from app.models.platform import PlatformSubscription
    now = now or _now()
    for sub in (PlatformSubscription.query
                .filter(PlatformSubscription.client_id == client_id,
                        PlatformSubscription.status.in_(("ACTIVE", "CANCELLED_PENDING_EXPIRY")))
                .order_by(PlatformSubscription.end_date.desc()).all()):
        end = _aware(sub.end_date)
        if end and end + GROWTH_GRACE > now:
            return sub
    return None


def limits_for(client, now=None):
    """{"tier", "monthly_limit", "rate_per_minute"} for an API client."""
    from app.models.platform import PlatformClientLimits
    if is_test(client):
        return {"tier": "test", "monthly_limit": TEST_LIMITS[0], "rate_per_minute": TEST_LIMITS[1]}
    custom = PlatformClientLimits.query.filter_by(client_id=client.id).first()
    if custom is not None:
        return {"tier": custom.tier or "scale", "monthly_limit": int(custom.monthly_limit),
                "rate_per_minute": int(custom.rate_per_minute)}
    tier = "growth" if (active_growth(client.id, now) or (client.plan or "").lower() in ("paid",)) else "free"
    monthly, rate = TIERS[tier]
    return {"tier": tier, "monthly_limit": monthly, "rate_per_minute": rate}


def period(now=None):
    return (now or _now()).strftime("%Y-%m")


def period_reset(now=None):
    now = now or _now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start + relativedelta(months=1)


def usage(client_id, now=None):
    from app.models.platform import PlatformUsage
    row = PlatformUsage.query.filter_by(client_id=client_id, period=period(now)).first()
    return int(row.count) if row else 0


def _increment(client_id, now=None):
    from app.models.platform import PlatformUsage
    p = period(now)
    t = PlatformUsage.__table__
    res = db.session.execute(update(t).where(t.c.client_id == client_id, t.c.period == p)
                             .values(count=t.c.count + 1, updated_at=_now()))
    if res.rowcount == 0:
        try:
            db.session.add(PlatformUsage(client_id=client_id, period=p, count=1))
            db.session.commit()
            return
        except IntegrityError:
            # Another request created this month's row first.
            db.session.rollback()
            db.session.execute(update(t).where(t.c.client_id == client_id, t.c.period == p)
                               .values(count=t.c.count + 1, updated_at=_now()))
    db.session.commit()


def _rate_strategy():
    from app.extensions import limiter
    try:
        strategy = limiter.limiter
        if strategy is not None:
            return strategy
    except Exception:  # noqa: BLE001
        pass
    global _FALLBACK_STRATEGY
    if _FALLBACK_STRATEGY is None:
        from limits.storage import MemoryStorage
        from limits.strategies import FixedWindowRateLimiter
        _FALLBACK_STRATEGY = FixedWindowRateLimiter(MemoryStorage())
    return _FALLBACK_STRATEGY


_FALLBACK_STRATEGY = None


def rate_hit(client, per_minute):
    """True when this request is within the client's per-minute rate."""
    from limits import RateLimitItemPerMinute
    item = RateLimitItemPerMinute(max(1, int(per_minute)))
    # key_hash (not the id) so a recycled id never inherits another key's window.
    return _rate_strategy().hit(item, "siiqo-v1", client.key_hash[:24])


def quota_headers(limits, used, now=None):
    return {
        "X-Siiqo-Quota-Limit": str(limits["monthly_limit"]),
        "X-Siiqo-Quota-Remaining": str(max(0, limits["monthly_limit"] - used)),
        "X-Siiqo-Quota-Reset": str(int(period_reset(now).timestamp())),
        "X-Siiqo-Quota-Tier": limits["tier"],
    }


def admit(client, count=True, now=None):
    """Rate and quota check for one request. Returns (error_code or None,
    headers). Counted requests that are refused are not added to the total."""
    now = now or _now()
    lim = limits_for(client, now)
    used = usage(client.id, now)
    if not rate_hit(client, lim["rate_per_minute"]):
        headers = quota_headers(lim, used, now)
        headers["Retry-After"] = "60"
        return "RATE_LIMITED", headers
    if count:
        if used >= lim["monthly_limit"]:
            return "QUOTA_EXCEEDED", quota_headers(lim, used, now)
        _increment(client.id, now)
        used += 1
    return None, quota_headers(lim, used, now)


def usage_summary(client, now=None):
    now = now or _now()
    lim = limits_for(client, now)
    used = usage(client.id, now)
    growth = None if is_test(client) else active_growth(client.id, now)
    return {
        "tier": lim["tier"], "monthly_limit": lim["monthly_limit"], "rate_per_minute": lim["rate_per_minute"],
        "used": used, "remaining": max(0, lim["monthly_limit"] - used), "period": period(now),
        "resets_at": period_reset(now).isoformat(),
        "growth": ({"status": growth.status, "renews_or_ends_at": _aware(growth.end_date).isoformat()}
                   if growth else None),
    }


# ── Scale tier (superadmin) ────────────────────────────────────────────────

def set_scale_limits(client, monthly_limit, rate_per_minute, note, admin_id):
    from app.models.platform import PlatformClientLimits
    row = PlatformClientLimits.query.filter_by(client_id=client.id).first()
    before = None
    if row is None:
        row = PlatformClientLimits(client_id=client.id)
        db.session.add(row)
    else:
        before = {"tier": row.tier, "monthly_limit": row.monthly_limit, "rate_per_minute": row.rate_per_minute}
    row.tier = "scale"
    row.monthly_limit = int(monthly_limit)
    row.rate_per_minute = int(rate_per_minute)
    row.note = (note or None) and str(note)[:255]
    row.set_by_admin_id = admin_id
    client.plan = "scale"
    return row, before


def clear_scale_limits(client):
    from app.models.platform import PlatformClientLimits
    row = PlatformClientLimits.query.filter_by(client_id=client.id).first()
    before = None
    if row is not None:
        before = {"tier": row.tier, "monthly_limit": row.monthly_limit, "rate_per_minute": row.rate_per_minute}
        db.session.delete(row)
    client.plan = "growth" if active_growth(client.id) else "free"
    return before


# ── Growth subscription (Paystack plan) ────────────────────────────────────

class GrowthUnavailable(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.message = message
        self.status = status


def growth_plan_code():
    return os.environ.get("PAYSTACK_PLAN_PLATFORM_GROWTH", "").strip()


def growth_price_ngn():
    raw = os.environ.get("PLATFORM_GROWTH_PRICE_NGN", "").strip()
    try:
        value = int(float(raw))
    except ValueError:
        return None
    return value if value > 0 else None


def start_growth_checkout(client, user):
    """Paystack transaction for the Growth plan on one live key. Returns
    {authorization_url, reference}. Raises GrowthUnavailable."""
    import requests
    if is_test(client):
        raise GrowthUnavailable("Test keys are free and can't be upgraded. Upgrade a live key.", 400)
    if not client.is_active:
        raise GrowthUnavailable("This key is revoked.", 400)
    if active_growth(client.id) or limits_for(client)["tier"] == "scale":
        raise GrowthUnavailable("This key is already on a paid tier.", 409)
    key = os.environ.get("PAYSTACK_SECRET_KEY", "")
    plan_code = growth_plan_code()
    price = growth_price_ngn()
    if not key or not plan_code or not price:
        raise GrowthUnavailable("Growth isn't available yet. Please try again later.", 503)
    reference = f"{GROWTH_REF_PREFIX}{client.id}-{uuid.uuid4().hex[:8].upper()}"
    site = os.environ.get("SITE_URL", "https://siiqo.com").rstrip("/")
    payload = {
        "email": user.email,
        "amount": price * 100,
        "plan": plan_code,
        "reference": reference,
        "callback_url": f"{site}/developers?growth=done#keys",
        "metadata": {"type": "platform_growth", "client_id": client.id, "user_id": user.id},
    }
    try:
        res = requests.post(f"{PAYSTACK_BASE_URL}/transaction/initialize", json=payload,
                            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                            timeout=15)
        body = res.json()
    except Exception as e:  # noqa: BLE001
        logger.error(f"[PLATFORM] Growth checkout failed for client {client.id}: {type(e).__name__}")
        raise GrowthUnavailable("Payment gateway error. Please try again.", 503)
    if not res.ok or not body.get("status"):
        raise GrowthUnavailable("Couldn't start the payment. Please try again.", 400)
    return {"authorization_url": body["data"]["authorization_url"], "reference": body["data"].get("reference", reference)}


def is_growth_event(data):
    """True for Paystack events that belong to the platform Growth plan."""
    data = data or {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    reference = str(data.get("reference") or "")
    plan = data.get("plan")
    plan_code = (plan.get("plan_code") if isinstance(plan, dict) else (str(plan) if plan else "")) or \
        str(data.get("plan_code") or "")
    sub = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
    sub_plan = sub.get("plan") if isinstance(sub.get("plan"), dict) else {}
    code = growth_plan_code()
    if (metadata.get("type") == "platform_growth" or reference.startswith(GROWTH_REF_PREFIX)
            or (code and (plan_code == code or sub_plan.get("plan_code") == code))):
        return True
    sub_code = data.get("subscription_code") or sub.get("subscription_code")
    if sub_code:
        from app.models.platform import PlatformSubscription
        return PlatformSubscription.query.filter_by(paystack_subscription_code=str(sub_code)).first() is not None
    return False


def _client_for_growth_event(data):
    from app.models.passport import PassportApiClient
    from app.models.platform import PlatformSubscription
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    client_id = metadata.get("client_id")
    reference = str(data.get("reference") or "")
    if not client_id and reference.startswith(GROWTH_REF_PREFIX):
        try:
            client_id = int(reference[len(GROWTH_REF_PREFIX):].split("-")[0])
        except ValueError:
            client_id = None
    if client_id:
        try:
            return db.session.get(PassportApiClient, int(client_id))
        except (TypeError, ValueError):
            return None
    sub_code = data.get("subscription_code") or (data.get("subscription") or {}).get("subscription_code")
    if sub_code:
        sub = PlatformSubscription.query.filter_by(paystack_subscription_code=sub_code).first()
        if sub:
            return db.session.get(PassportApiClient, sub.client_id)
    # Renewal without metadata: the owner's only Growth subscription.
    email = ((data.get("customer") or {}).get("email") or "").strip().lower()
    if email:
        from app.models.user import User
        user = User.query.filter(db.func.lower(User.email) == email).first()
        if user:
            subs = (PlatformSubscription.query.filter_by(owner_user_id=user.id)
                    .filter(PlatformSubscription.status.in_(("ACTIVE", "CANCELLED_PENDING_EXPIRY"))).all())
            if len(subs) == 1:
                return db.session.get(PassportApiClient, subs[0].client_id)
    return None


def apply_growth_payment(data):
    """A successful Growth payment (first charge or renewal). Idempotent per
    reference. Returns the subscription or None."""
    from app.models.platform import PlatformSubscription, PlatformSubscriptionPayment
    reference = str(data.get("reference") or "") or \
        str((data.get("transaction") or {}).get("reference") or "") or str(data.get("invoice_code") or "")
    if reference and PlatformSubscriptionPayment.query.filter_by(reference=reference).first():
        return None
    client = _client_for_growth_event(data)
    if client is None or is_test(client):
        logger.warning(f"[PLATFORM] Growth payment {reference} matched no live key; ignored")
        return None
    now = _now()
    sub = (PlatformSubscription.query
           .filter(PlatformSubscription.client_id == client.id,
                   PlatformSubscription.status.in_(("ACTIVE", "CANCELLED_PENDING_EXPIRY")))
           .order_by(PlatformSubscription.end_date.desc()).first())
    if sub is not None and sub.end_date:
        sub.end_date = max(_aware(sub.end_date), now) + relativedelta(months=1)
        sub.status = "ACTIVE"
    else:
        sub = PlatformSubscription(client_id=client.id, owner_user_id=client.owner_user_id, status="ACTIVE",
                                   start_date=now, end_date=now + relativedelta(months=1))
        db.session.add(sub)
    db.session.flush()
    amount = data.get("amount")
    db.session.add(PlatformSubscriptionPayment(
        client_id=client.id, subscription_id=sub.id, reference=reference or f"noref-{uuid.uuid4().hex}",
        amount_ngn=(amount / 100) if isinstance(amount, (int, float)) and amount else None))
    if (client.plan or "free") != "scale":
        client.plan = "growth"
    db.session.commit()
    logger.info(f"[PLATFORM] Growth active for client {client.id} until {sub.end_date}")
    return sub


def remember_growth_subscription(data):
    from app.models.platform import PlatformSubscription
    client = _client_for_growth_event(data)
    if client is None:
        return
    sub = (PlatformSubscription.query.filter_by(client_id=client.id, status="ACTIVE")
           .order_by(PlatformSubscription.end_date.desc()).first())
    if sub and data.get("subscription_code"):
        sub.paystack_subscription_code = data.get("subscription_code")
        sub.paystack_email_token = data.get("email_token")
        db.session.commit()


def stop_growth_renewal(data):
    """Paystack says the Growth plan won't renew: keep it until the paid period ends."""
    from app.models.platform import PlatformSubscription
    client = _client_for_growth_event(data)
    if client is None:
        return False
    changed = PlatformSubscription.query.filter_by(client_id=client.id, status="ACTIVE").update(
        {"status": "CANCELLED_PENDING_EXPIRY"})
    db.session.commit()
    return bool(changed)
