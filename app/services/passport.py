"""
Trust Passport (docs/VERIFIED_PRO_PLAN.md §5).

Free for every seller and earned by usage. The public card never carries
NIN, CAC numbers, legal names or documents. Each response includes a signed
statement other platforms can trust:

- ES256 when PASSPORT_SIGNING_KEY (an EC P-256 private key, PEM) is set and
  the `cryptography` package is installed: platforms verify offline with the
  public key from /api/passport/.well-known/jwks.json.
- Otherwise HS256 with a server secret: platforms verify by calling
  POST /api/passport/verify.
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.extensions import db

STATEMENT_TTL = timedelta(hours=24)
ISSUER = "https://siiqo.com"
CLAIMS_VERSION = "1"


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


# ── data ───────────────────────────────────────────────────────────────────

def settings_for(vendor_id, create=False):
    from app.models.passport import PassportSettings
    row = PassportSettings.query.filter_by(vendor_id=vendor_id).first()
    if row is None and create:
        row = PassportSettings(vendor_id=vendor_id)
        db.session.add(row)
        db.session.flush()
    return row


def completed_orders(vendor_id):
    from app.models.order import Order
    return Order.query.filter_by(vendor_id=vendor_id, status="COMPLETED").count()


def find_storefront(slug):
    from app.models.user import Storefront
    return Storefront.query.filter_by(store_slug=(slug or "").strip().lower()).first()


def is_visible(sf):
    if sf is None or sf.vendor is None or not sf.vendor.is_active:
        return False
    s = settings_for(sf.vendor_id)
    return s is None or s.is_public


def build_passport(sf):
    """The public Passport claims for a store."""
    from app.models.trust import VendorTrustProfile
    from app.models.verification import VerificationStatus

    from app.services import trust_v1

    vendor = sf.vendor
    s = settings_for(sf.vendor_id)
    done = completed_orders(sf.vendor_id)
    if trust_v1.is_live():
        # Trust Score v1 (TRUST_SCORE_V1_LIVE): earned from evidence; below the
        # minimum history the tier is NEW and there is no score.
        v1 = trust_v1.public_v1(sf.vendor_id, compute_if_missing=True)
        tier, score, score_max = v1["tier"], v1["score"], trust_v1.SCORE_MAX
    else:
        # Old score, kept until the v1 shadow period ends.
        profile = VendorTrustProfile.query.filter_by(vendor_id=sf.vendor_id).first()
        # No made-up score: a seller without completed orders is shown as new.
        earned = bool(profile and done > 0)
        tier = profile.trust_tier if earned else "NEW"
        score = int(profile.total_trust_score) if earned else None
        score_max = 1000

    status = VerificationStatus.query.filter_by(user_id=sf.vendor_id).first()
    flags = status.public_flags() if status else {"identity_verified": False, "business_registered": False}
    legacy_expiry = _aware(sf.pro_verified_expires_at)
    legacy = bool(sf.is_pro_verified and (legacy_expiry is None or legacy_expiry > _now()))

    claims = {
        "v": CLAIMS_VERSION,
        "store_slug": sf.store_slug,
        "store_name": sf.store_name,
        "url": f"https://siiqo.com/id/{sf.store_slug}",
        "tier": tier,
        "score": score,
        "score_max": score_max,
        "verified": bool(flags["identity_verified"] or legacy),
        "business_registered": bool(flags["business_registered"]),
    }
    if s is None or s.show_completed_orders:
        claims["completed_orders"] = done
    if (s is None or s.show_member_since) and vendor.created_at:
        claims["member_since"] = _aware(vendor.created_at).date().isoformat()
    return claims


# ── signed statements ──────────────────────────────────────────────────────

def _private_key():
    pem = os.environ.get("PASSPORT_SIGNING_KEY", "").strip()
    if not pem:
        return None
    try:
        from cryptography.hazmat.primitives import serialization
        return serialization.load_pem_private_key(pem.replace("\\n", "\n").encode(), password=None)
    except Exception:
        return None


def _hmac_secret():
    base = os.environ.get("PASSPORT_HMAC_SECRET") or os.environ.get("SECRET_KEY") or "siiqo-secret-key-salt-2026"
    return hashlib.sha256(f"passport-statement:{base}".encode()).digest()


def _kid(public_key):
    from cryptography.hazmat.primitives import serialization
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()[:16]


def sign(claims):
    now = _now()
    payload = {**claims, "iss": ISSUER, "sub": claims["store_slug"],
               "iat": int(now.timestamp()), "exp": int((now + STATEMENT_TTL).timestamp())}
    key = _private_key()
    if key is not None:
        return jwt.encode(payload, key, algorithm="ES256", headers={"kid": _kid(key.public_key())}), "ES256"
    return jwt.encode(payload, _hmac_secret(), algorithm="HS256"), "HS256"


def verify(token):
    """Check a statement and that the seller still holds what it claims."""
    header = jwt.get_unverified_header(token)
    if header.get("alg") == "ES256":
        key = _private_key()
        if key is None:
            raise jwt.InvalidTokenError("ES256 statements can't be checked on this server")
        claims = jwt.decode(token, key.public_key(), algorithms=["ES256"], issuer=ISSUER)
    else:
        claims = jwt.decode(token, _hmac_secret(), algorithms=["HS256"], issuer=ISSUER)
    sf = find_storefront(claims.get("store_slug"))
    current = build_passport(sf) if is_visible(sf) else None
    # Revocation: a Verified claim that no longer holds is reported as revoked.
    revoked = current is None or (claims.get("verified") and not current["verified"])
    return claims, bool(revoked)


def jwks():
    key = _private_key()
    if key is None:
        return {"keys": []}
    from jwt.algorithms import ECAlgorithm
    import json
    jwk = json.loads(ECAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": _kid(key.public_key()), "alg": "ES256", "use": "sig"})
    return {"keys": [jwk]}


# ── platform API keys ──────────────────────────────────────────────────────

def _key_hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


MAX_KEYS_PER_USER = 3


def create_api_client(name, plan="free", owner_user_id=None):
    from app.models.passport import PassportApiClient
    raw = f"sqp_{secrets.token_urlsafe(32)}"
    client = PassportApiClient(name=name, key_prefix=raw[:12], key_hash=_key_hash(raw), plan=plan,
                               owner_user_id=owner_user_id)
    db.session.add(client)
    db.session.commit()
    return client, raw


def keys_for_user(user_id, active_only=False):
    from app.models.passport import PassportApiClient
    q = PassportApiClient.query.filter_by(owner_user_id=user_id)
    if active_only:
        q = q.filter_by(is_active=True)
    return q.order_by(PassportApiClient.created_at.desc(), PassportApiClient.id.desc()).all()


def key_public(client):
    """A developer key as listed back to its owner (never the raw key or its hash)."""
    def iso(dt):
        return _aware(dt).isoformat() if dt else None
    return {"id": client.id, "name": client.name, "key_prefix": client.key_prefix, "plan": client.plan,
            "is_active": bool(client.is_active), "created_at": iso(client.created_at),
            "last_used_at": iso(client.last_used_at)}


def client_for_key(raw):
    from app.models.passport import PassportApiClient
    if not raw:
        return None
    client = PassportApiClient.query.filter_by(key_hash=_key_hash(raw), is_active=True).first()
    if client:
        client.last_used_at = _now()
        db.session.commit()
    return client


# ── embeddable badge ───────────────────────────────────────────────────────

_TIER_COLOURS = {"NEW": "#64748B", "BRONZE": "#B45309", "SILVER": "#64748B", "GOLD": "#E0921C",
                 "PLATINUM": "#0E7490"}


def badge_svg(claims):
    from html import escape
    tier = claims.get("tier") or "NEW"
    colour = _TIER_COLOURS.get(tier, "#64748B")
    score = claims.get("score")
    right = f"{tier.title()} · {score}" if score is not None else "New seller"
    if claims.get("verified"):
        right += " · Verified"
    name = escape((claims.get("store_name") or "")[:28])
    right = escape(right)
    width = 120 + 7 * len(right)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="28" role="img" aria-label="Siiqo Trust Passport: {name}, {right}">
<title>Siiqo Trust Passport: {name} — {right}</title>
<rect width="{width}" height="28" rx="6" fill="#070D1F"/>
<rect x="112" width="{width - 112}" height="28" rx="6" fill="{colour}"/>
<text x="12" y="18" fill="#fff" font-family="Arial,sans-serif" font-size="12" font-weight="700">Siiqo Passport</text>
<text x="122" y="18" fill="#fff" font-family="Arial,sans-serif" font-size="12">{right}</text>
</svg>'''
