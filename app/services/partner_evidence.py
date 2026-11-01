"""
Evidence API (Phase 4, docs/PLATFORM_API_V1.md "Evidence API").

Platforms with a live key send signed sales outcomes about a Siiqo seller.
Founder rule: the trust score is earned by use and never bought, so partner
evidence can never buy or fake trust:

- Signed: `Siiqo-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">`
  with the key's evidence secret (the 2B webhook signing helpers), 5-minute
  replay window. The same partner event id is stored once (idempotent).
- Stored in `trust_evidence` with `source = "partner:<client_id>"`,
  provenance `partner_reported` and its own `partner_*` evidence types, so
  first-party scoring, the old score, disputes and reports never read it.
- Counts only after the seller accepts the platform on their Trust tab
  ("Platforms that report to your Passport"), and stops when they revoke.
- Never counts when the key is the seller's own: the key's owner is the
  seller, or shares an identifier with them in the identifier graph.
- A superadmin can throttle (weight × factor) or revoke a platform; both are
  audit-logged. A revoked platform's evidence stops counting and new reports
  are refused.
- Test keys: the request is checked in full (signature, body, fixture
  seller) and nothing is stored.

How partner evidence enters Trust Score v1 (weights, caps) is in
app/services/trust_v1.py (PARTNER_RULES) and docs/TRUST_SCORE_V1.md.
"""
import hashlib
import json
import logging
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.services import passport as passport_svc
from app.services import platform_webhooks as webhooks

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "Siiqo-Signature"
SIGNATURE_TOLERANCE = webhooks.SIGNATURE_TOLERANCE      # 300 s, as webhooks
MAX_BODY_BYTES = 8 * 1024
MAX_AGE = timedelta(days=730)
FUTURE_SKEW = timedelta(minutes=5)
SOURCE_PREFIX = "partner:"
PROVENANCE = "partner_reported"
CONFIDENCE = 0.5

# API event type → trust_evidence.evidence_type
EVENT_TYPES = {
    "order.completed": "partner_order_completed",
    "order.delivered": "partner_order_delivered",
    "order.refunded": "partner_order_refunded",
    "dispute.resolved": "partner_dispute_resolved",
}
EVIDENCE_TYPES = tuple(EVENT_TYPES.values())
FAULTS = ("seller", "buyer", "none")
NETWORKS = ("instagram", "tiktok", "x", "facebook")

LINK_PENDING, LINK_ACCEPTED, LINK_REVOKED = "pending", "accepted", "revoked"
CONTROL_ACTIVE, CONTROL_THROTTLED, CONTROL_REVOKED = "active", "throttled", "revoked"
DEFAULT_THROTTLE_FACTOR = 0.25

_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._:\-/#]{1,100}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class EvidenceError(Exception):
    def __init__(self, code, message, status=400, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra

    def body(self):
        return {"code": self.code, "message": self.message, **self.extra}


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _iso(dt):
    return _aware(dt).isoformat() if dt else None


def client_source(client_id):
    return f"{SOURCE_PREFIX}{int(client_id)}"


# ── signing secret ─────────────────────────────────────────────────────────

def secret_row(client):
    from app.models.platform import PartnerEvidenceSecret
    return PartnerEvidenceSecret.query.filter_by(client_id=client.id).first()


def create_secret(client):
    """A new evidence signing secret for this key; replaces any old one.
    Returns (row, secret); the secret is shown once."""
    from app.models.platform import PartnerEvidenceSecret
    secret = f"evsec_{secrets.token_urlsafe(32)}"
    row = secret_row(client)
    if row is None:
        row = PartnerEvidenceSecret(client_id=client.id)
        db.session.add(row)
    row.secret_enc = webhooks._encrypt(secret)
    row.secret_prefix = secret[:12]
    row.created_at = _now()
    db.session.commit()
    return row, secret


def secret_public(row):
    if row is None:
        return None
    return {"secret_prefix": row.secret_prefix, "created_at": _iso(row.created_at)}


def check_signature(secret, header, raw_body, now=None):
    """None when valid, else an error code. Reuses the 2B webhook helpers."""
    try:
        parts = dict(p.split("=", 1) for p in (header or "").split(","))
        t = int(parts["t"])
        parts["v1"]
    except (ValueError, KeyError):
        return "BAD_SIGNATURE"
    now = int(now if now is not None else time.time())
    if abs(now - t) > SIGNATURE_TOLERANCE:
        return "SIGNATURE_EXPIRED"
    if not webhooks.verify_signature(secret, header, raw_body, now=now, tolerance=SIGNATURE_TOLERANCE):
        return "BAD_SIGNATURE"
    return None


# ── partner control (superadmin) ───────────────────────────────────────────

def control_for(client_id):
    from app.models.platform import PartnerEvidenceControl
    return PartnerEvidenceControl.query.filter_by(client_id=client_id).first()


def control_state(client_id, row=None):
    """(status, factor): factor is what the partner's evidence is multiplied by."""
    row = row if row is not None else control_for(client_id)
    if row is None or row.status == CONTROL_ACTIVE:
        return CONTROL_ACTIVE, 1.0
    if row.status == CONTROL_REVOKED:
        return CONTROL_REVOKED, 0.0
    factor = float(row.factor) if row.factor is not None else DEFAULT_THROTTLE_FACTOR
    return CONTROL_THROTTLED, max(0.0, min(1.0, factor))


def set_control(client_id, action, admin, reason=None, factor=None):
    """action: throttle | revoke | restore. Audit-logged. Returns the row."""
    from app.models.audit import AdminAuditLog
    from app.models.platform import PartnerEvidenceControl
    if action not in ("throttle", "revoke", "restore"):
        raise EvidenceError("BAD_ACTION", "action must be throttle, revoke or restore.")
    reason = (str(reason or "").strip())[:255]
    if action in ("throttle", "revoke") and not reason:
        raise EvidenceError("REASON_REQUIRED", "Give a reason; it is kept in the audit log.")
    if action == "throttle":
        try:
            factor = DEFAULT_THROTTLE_FACTOR if factor in (None, "") else float(factor)
        except (TypeError, ValueError):
            raise EvidenceError("BAD_FACTOR", "factor must be a number between 0 and 1.")
        if not 0 < factor < 1:
            raise EvidenceError("BAD_FACTOR", "factor must be more than 0 and less than 1.")
    row = control_for(client_id)
    before = {"status": row.status, "factor": float(row.factor) if row and row.factor is not None else None} \
        if row else {"status": CONTROL_ACTIVE, "factor": None}
    if row is None:
        row = PartnerEvidenceControl(client_id=client_id)
        db.session.add(row)
    row.status = {"throttle": CONTROL_THROTTLED, "revoke": CONTROL_REVOKED, "restore": CONTROL_ACTIVE}[action]
    row.factor = factor if action == "throttle" else None
    row.reason = reason or None
    row.set_by_admin_id = admin.id
    row.updated_at = _now()
    db.session.flush()
    AdminAuditLog.log_action(
        admin, {"throttle": "EVIDENCE_PARTNER_THROTTLED", "revoke": "EVIDENCE_PARTNER_REVOKED",
                "restore": "EVIDENCE_PARTNER_RESTORED"}[action],
        "PassportApiClient", client_id,
        details={"before": before, "after": {"status": row.status, "factor": factor if action == "throttle" else None},
                 "reason": reason or None})
    return row


# ── self-dealing ───────────────────────────────────────────────────────────

def _identity_pairs(user):
    from app.models.foundations import SellerIdentifier
    from app.services.foundations import _norm_email, _norm_phone
    out = set()
    if user is None:
        return out
    e = _norm_email(user.email)
    if e:
        out.add(("email", e))
    p = _norm_phone(user.phone)
    if p:
        out.add(("phone", p))
    out |= {(i.kind, i.value) for i in SellerIdentifier.query.filter_by(vendor_id=user.id).all()}
    return out


def is_self_dealing(client, vendor_id, seller_pairs=None):
    """True when the key belongs to the seller: same account, or the key
    owner shares an identifier with the seller in the identifier graph."""
    from app.models.user import User
    owner_id = getattr(client, "owner_user_id", None)
    if not owner_id:
        return False                    # superadmin-issued platform key
    if int(owner_id) == int(vendor_id):
        return True
    if seller_pairs is None:
        seller_pairs = _identity_pairs(db.session.get(User, vendor_id))
    return bool(_identity_pairs(db.session.get(User, owner_id)) & seller_pairs)


# ── request validation ─────────────────────────────────────────────────────

def _parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _looks_personal(value):
    digits = re.sub(r"\D", "", value)
    return "@" in value or len(digits) >= 10


def validate(body, now=None):
    """Check the event body. Returns a normalised dict or raises EvidenceError
    with every problem found."""
    now = now or _now()
    if not isinstance(body, dict):
        raise EvidenceError("BAD_REQUEST", "Send a JSON object.")
    errors = {}
    event_id = body.get("id")
    if not isinstance(event_id, str) or not _EVENT_ID_RE.match(event_id):
        errors["id"] = "Your unique event id: 1-100 letters, numbers, . _ : -"
    etype = body.get("type")
    if etype not in EVENT_TYPES:
        errors["type"] = f"One of: {', '.join(EVENT_TYPES)}"
    seller = body.get("seller")
    seller_ref = None
    if not isinstance(seller, dict):
        errors["seller"] = "{\"slug\": ...} or {\"domain\": ...} or {\"handle\": ..., \"network\": ...}"
    else:
        keys = [k for k in ("slug", "domain", "handle") if seller.get(k)]
        if len(keys) != 1:
            errors["seller"] = "Give exactly one of slug, domain or handle."
        elif keys[0] == "handle" and seller.get("network") not in NETWORKS:
            errors["seller.network"] = f"One of: {', '.join(NETWORKS)}"
        elif not isinstance(seller[keys[0]], str) or len(seller[keys[0]]) > 255:
            errors["seller"] = "Must be text (up to 255 characters)."
        else:
            seller_ref = {"by": keys[0], "value": seller[keys[0]].strip(), "network": seller.get("network")}
    order_ref = body.get("order_ref")
    if not isinstance(order_ref, str) or not _REF_RE.match(order_ref):
        errors["order_ref"] = "Your order id: 1-100 characters (letters, numbers, . _ : - / #)."
    occurred = _parse_time(body.get("occurred_at"))
    if occurred is None:
        errors["occurred_at"] = "ISO 8601 time with a timezone, e.g. 2026-10-31T09:00:00Z."
    elif occurred > now + FUTURE_SKEW:
        errors["occurred_at"] = "Can't be in the future."
    elif occurred < now - MAX_AGE:
        errors["occurred_at"] = "Older than 2 years; not accepted."

    amount = body.get("amount")
    if amount is not None:
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0 or amount > 1e12:
            errors["amount"] = "A number of at least 0 (the order total, in currency units)."
    currency = body.get("currency", "NGN" if amount is not None else None)
    if currency is not None and (not isinstance(currency, str) or not _CURRENCY_RE.match(currency)):
        errors["currency"] = "Three-letter code, e.g. NGN."
    buyer_ref = body.get("buyer_ref")
    if buyer_ref is not None:
        if not isinstance(buyer_ref, str) or not _REF_RE.match(buyer_ref):
            errors["buyer_ref"] = "An opaque buyer id: 1-100 characters."
        elif _looks_personal(buyer_ref):
            errors["buyer_ref"] = "Don't send emails or phone numbers. Send your own opaque buyer id."
    on_time = body.get("on_time")
    if etype == "order.delivered" and not isinstance(on_time, bool):
        errors["on_time"] = "true or false: delivered within the time promised to the buyer."
    fault = body.get("fault")
    if etype == "dispute.resolved" and fault not in FAULTS:
        errors["fault"] = f"One of: {', '.join(FAULTS)}"
    if errors:
        raise EvidenceError("BAD_REQUEST", "Some fields are not valid.", 400, errors=errors)
    return {
        "id": event_id, "type": etype, "seller": seller_ref, "order_ref": order_ref,
        "occurred_at": occurred, "amount": float(amount) if amount is not None else None,
        "currency": currency, "buyer_ref": buyer_ref,
        "on_time": on_time if etype == "order.delivered" else None,
        "fault": fault if etype == "dispute.resolved" else None,
    }


# ── seller resolution ──────────────────────────────────────────────────────

def resolve_live_seller(ref):
    """The Storefront named by slug, or by a domain/handle the seller has
    proved (verified ownership claims only). None when not found or hidden."""
    from app.models.platform import OwnershipClaim
    from app.models.user import Storefront
    from app.services import ownership
    sf = None
    if ref["by"] == "slug":
        sf = passport_svc.find_storefront(ref["value"])
    else:
        if ref["by"] == "domain":
            value, network, kind = ownership.normalise_domain(ref["value"]), "", "domain"
        else:
            value, network, kind = ownership.normalise_handle(ref["value"]), ref["network"], "handle"
        if value:
            claim = OwnershipClaim.query.filter_by(kind=kind, network=network, value=value,
                                                   status="verified").first()
            if claim is not None:
                sf = Storefront.query.filter_by(vendor_id=claim.vendor_id).first()
    if sf is None or sf.vendor is None or not sf.vendor.is_active:
        return None
    return sf


def resolve_test_seller(ref):
    """Test keys: only the documented fixture slugs (by slug)."""
    from app.services import platform as platform_svc
    if ref["by"] != "slug":
        return None
    return platform_svc.fixture(ref["value"])


# ── ingest ─────────────────────────────────────────────────────────────────

def _body_hash(event):
    canonical = {**event, "occurred_at": _iso(event["occurred_at"])}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _buyer_key(client_id, buyer_ref):
    if not buyer_ref:
        return None
    return hashlib.sha256(f"{client_id}:{buyer_ref}".encode()).hexdigest()[:32]


def _receipt(client_id, event_id):
    from app.models.platform import PartnerEvidenceReceipt
    return PartnerEvidenceReceipt.query.filter_by(client_id=client_id, event_id=event_id).first()


def _link(vendor_id, client_id):
    from app.models.platform import PartnerSellerLink
    return PartnerSellerLink.query.filter_by(vendor_id=vendor_id, client_id=client_id).first()


def _duplicate_answer(client_id, existing, body_hash, slug):
    if existing.body_hash != body_hash:
        raise EvidenceError("IDEMPOTENCY_CONFLICT",
                            "This event id was already used for a different event. Use a new id.", 409)
    link = _link(existing.vendor_id, client_id)
    return {"status": "duplicate", "id": existing.event_id, "evidence_id": existing.evidence_id,
            "seller": {"slug": slug}, "seller_link": link.status if link else LINK_PENDING}, 200


def ingest(client, raw_body, signature, now=None):
    """Handle one POST /v1/evidence. Returns (body, http_status) or raises
    EvidenceError. Test keys: validated in full, nothing stored."""
    from app.models.platform import PartnerEvidenceReceipt, PartnerSellerLink
    from app.models.telemetry import TrustEvidence
    from app.services import platform as platform_svc

    now = now or _now()
    test = platform_svc.is_test(client)
    if not test and control_state(client.id)[0] == CONTROL_REVOKED:
        raise EvidenceError("PARTNER_REVOKED", "Siiqo has stopped accepting evidence from this key. "
                                               "Contact developers@siiqo.com.", 403)
    row = secret_row(client)
    secret = webhooks._decrypt(row.secret_enc) if row else None
    if not secret:
        raise EvidenceError("NO_SIGNING_SECRET", "Create an evidence signing secret first: "
                                                 "POST /v1/evidence/secret.", 409)
    problem = check_signature(secret, signature, raw_body, now=int(now.timestamp()))
    if problem == "SIGNATURE_EXPIRED":
        raise EvidenceError(problem, "The signature timestamp is more than 5 minutes from Siiqo's clock.", 401)
    if problem:
        raise EvidenceError(problem, f"Sign the raw body: {SIGNATURE_HEADER}: t=<unix>,v1=<hex HMAC-SHA256 "
                                     "of \"<t>.<raw body>\" with your evidence secret>.", 401)
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise EvidenceError("BAD_REQUEST", "The body is not valid JSON.")
    event = validate(body, now=now)

    if test:
        claims = resolve_test_seller(event["seller"])
        if claims is None:
            raise EvidenceError("SELLER_NOT_FOUND", "Test keys report about fixture sellers only: "
                                                    "test-trusted-store, test-new-seller, test-unverified.", 404)
        return {"status": "validated", "stored": False, "test": True, "id": event["id"], "type": event["type"],
                "seller": {"slug": claims["store_slug"]}}, 200

    sf = resolve_live_seller(event["seller"])
    if sf is None:
        raise EvidenceError("SELLER_NOT_FOUND", "No Siiqo seller matches. Use their store slug, or a website "
                                                "or social handle they have verified on Siiqo.", 404)
    body_hash = _body_hash(event)
    existing = _receipt(client.id, event["id"])
    if existing is not None:
        return _duplicate_answer(client.id, existing, body_hash, sf.store_slug)

    props = {"event_type": event["type"], "order_ref": event["order_ref"],
             "occurred_at": _iso(event["occurred_at"]), "client_id": client.id}
    if event["amount"] is not None:
        props["value"] = event["amount"]
        props["currency"] = event["currency"]
    if event["buyer_ref"]:
        props["buyer_key"] = _buyer_key(client.id, event["buyer_ref"])
    if event["on_time"] is not None:
        props["on_time"] = event["on_time"]
    if event["fault"] is not None:
        props["fault"] = event["fault"]
    try:
        evidence = TrustEvidence(business_id=sf.vendor_id, evidence_type=EVENT_TYPES[event["type"]],
                                 provenance=PROVENANCE, source=client_source(client.id),
                                 source_id=event["id"], status="ACTIVE", confidence=CONFIDENCE,
                                 properties=props, created_at=now, verified_at=None)
        db.session.add(evidence)
        db.session.flush()
        db.session.add(PartnerEvidenceReceipt(client_id=client.id, event_id=event["id"], vendor_id=sf.vendor_id,
                                              evidence_id=evidence.id, event_type=event["type"],
                                              body_hash=body_hash, created_at=now))
        link = _link(sf.vendor_id, client.id)
        if link is None:
            link = PartnerSellerLink(vendor_id=sf.vendor_id, client_id=client.id, status=LINK_PENDING,
                                     report_count=0, first_report_at=now, created_at=now)
            db.session.add(link)
        link.report_count = (link.report_count or 0) + 1
        link.last_report_at = now
        db.session.commit()
    except IntegrityError:
        # The same event id arrived twice at once: answer as a duplicate.
        db.session.rollback()
        existing = _receipt(client.id, event["id"])
        if existing is None:
            raise
        return _duplicate_answer(client.id, existing, body_hash, sf.store_slug)
    return {"status": "stored", "id": event["id"], "evidence_id": evidence.id, "seller": {"slug": sf.store_slug},
            "seller_link": link.status}, 201


# ── seller consent ─────────────────────────────────────────────────────────

def links_for_vendor(vendor_id):
    from app.models.passport import PassportApiClient
    from app.models.platform import PartnerSellerLink
    links = (PartnerSellerLink.query.filter_by(vendor_id=vendor_id)
             .order_by(PartnerSellerLink.created_at.desc(), PartnerSellerLink.id.desc()).all())
    clients = {c.id: c for c in PassportApiClient.query.filter(
        PassportApiClient.id.in_([l.client_id for l in links])).all()} if links else {}
    out = []
    for l in links:
        c = clients.get(l.client_id)
        status, _ = control_state(l.client_id)
        own = bool(c and is_self_dealing(c, vendor_id))
        out.append({
            "client_id": l.client_id,
            "platform": c.name if c else "Removed platform",
            "status": l.status,
            "disputed": bool(l.disputed),
            "reports": l.report_count or 0,
            "first_report_at": _iso(l.first_report_at),
            "last_report_at": _iso(l.last_report_at),
            "accepted_at": _iso(l.accepted_at),
            "revoked_at": _iso(l.revoked_at),
            "suspended_by_siiqo": status == CONTROL_REVOKED,
            "own_key": own,
            "counts": l.status == LINK_ACCEPTED and status != CONTROL_REVOKED and not own,
        })
    return out


def set_link(vendor_id, client_id, action, disputed=False):
    """action: accept | revoke. Revoking with disputed=True also tells Siiqo
    the reports were wrong (shown to superadmins)."""
    link = _link(vendor_id, client_id)
    if link is None:
        raise EvidenceError("NOT_FOUND", "This platform has not reported about your store.", 404)
    now = _now()
    if action == "accept":
        link.status = LINK_ACCEPTED
        link.accepted_at = now
        link.revoked_at = None
    elif action == "revoke":
        link.status = LINK_REVOKED
        link.revoked_at = now
        if disputed:
            link.disputed = True
    else:
        raise EvidenceError("BAD_ACTION", "action must be accept or revoke.")
    db.session.commit()
    return link


# ── evidence → partner order facts (for trust_v1) ──────────────────────────

def gather_partner_facts(vendor_id):
    """Partner order facts that may count for this seller: link accepted,
    platform not revoked, key not the seller's own. Returns (facts, excluded)
    where excluded counts the reports left out and why."""
    from app.models.passport import PassportApiClient
    from app.models.platform import PartnerSellerLink
    from app.models.telemetry import TrustEvidence
    from app.models.user import User

    rows = (TrustEvidence.query
            .filter(TrustEvidence.business_id == vendor_id, TrustEvidence.status == "ACTIVE",
                    TrustEvidence.evidence_type.in_(EVIDENCE_TYPES),
                    TrustEvidence.source.like(f"{SOURCE_PREFIX}%"))
            .order_by(TrustEvidence.created_at, TrustEvidence.id).all())
    excluded = defaultdict(int)
    if not rows:
        return [], dict(excluded)

    by_client = defaultdict(list)
    for r in rows:
        try:
            by_client[int(r.source[len(SOURCE_PREFIX):])].append(r)
        except ValueError:
            continue
    links = {l.client_id: l for l in PartnerSellerLink.query.filter(
        PartnerSellerLink.vendor_id == vendor_id, PartnerSellerLink.client_id.in_(list(by_client))).all()}
    clients = {c.id: c for c in PassportApiClient.query.filter(PassportApiClient.id.in_(list(by_client))).all()}
    seller_pairs = _identity_pairs(db.session.get(User, vendor_id))

    facts = []
    for client_id, evs in by_client.items():
        link = links.get(client_id)
        client = clients.get(client_id)
        status, factor = control_state(client_id)
        if client is None or passport_svc.key_mode(client) != "live":
            excluded["unknown_key"] += len(evs)
            continue
        if link is None or link.status != LINK_ACCEPTED:
            excluded["not_linked"] += len(evs)
            continue
        if status == CONTROL_REVOKED:
            excluded["partner_revoked"] += len(evs)
            continue
        if is_self_dealing(client, vendor_id, seller_pairs):
            excluded["self_dealing"] += len(evs)
            continue
        by_order = defaultdict(list)
        for e in evs:
            by_order[str((e.properties or {}).get("order_ref") or e.source_id)].append(e)
        for order_ref, oevs in by_order.items():
            facts.append(_partner_order_fact(client_id, order_ref, oevs, factor))
    return facts, dict(excluded)


def _partner_order_fact(client_id, order_ref, evs, factor):
    types = defaultdict(list)
    times, amount, currency, buyer_key, on_time = [], None, None, None, None
    for e in evs:
        p = e.properties or {}
        types[e.evidence_type].append(p)
        t = _parse_time(p.get("occurred_at")) or _aware(e.created_at)
        times.append(t)
        if p.get("value") is not None and amount is None:
            amount, currency = float(p["value"]), (p.get("currency") or "NGN").upper()
        buyer_key = buyer_key or p.get("buyer_key")
        if "on_time" in p:
            on_time = bool(p["on_time"])
    disputes = types.get("partner_dispute_resolved") or []
    fault = (disputes[-1].get("fault") if disputes else None)
    completed = bool(types.get("partner_order_completed"))
    refunded = bool(types.get("partner_order_refunded"))
    if fault == "seller":
        outcome = "bad"
    elif refunded:
        outcome = "neutral" if fault in ("buyer", "none") else "bad"
    elif completed:
        outcome = "good"
    else:
        outcome = "pending"
    return {
        "client_id": client_id,
        "order_ref": order_ref,
        "buyer_key": f"{client_id}:{buyer_key}" if buyer_key else f"{client_id}:o:{order_ref}",
        "time": min(times),
        "amount_ngn": amount if currency == "NGN" else None,
        "outcome": outcome,
        "seller_fault": fault == "seller",
        "delivered_on_time": on_time if outcome in ("good", "bad") else None,
        "factor": factor,
    }


# ── superadmin overview ────────────────────────────────────────────────────

def admin_overview():
    """Every platform that has reported, with the signals a superadmin needs:
    consent, seller disputes, conflicts with first-party data, own-key reports."""
    from sqlalchemy import func
    from app.models.passport import PassportApiClient
    from app.models.platform import PartnerEvidenceReceipt, PartnerSellerLink

    counts = dict(db.session.query(PartnerEvidenceReceipt.client_id, func.count(PartnerEvidenceReceipt.id))
                  .group_by(PartnerEvidenceReceipt.client_id).all())
    links = PartnerSellerLink.query.all()
    client_ids = set(counts) | {l.client_id for l in links}
    if not client_ids:
        return []
    clients = {c.id: c for c in PassportApiClient.query.filter(PassportApiClient.id.in_(list(client_ids))).all()}
    by_client = defaultdict(list)
    for l in links:
        by_client[l.client_id].append(l)
    out = []
    for cid in sorted(client_ids):
        c = clients.get(cid)
        ls = by_client.get(cid, [])
        row = control_for(cid)
        status, factor = control_state(cid, row)
        out.append({
            "client_id": cid,
            "platform": c.name if c else None,
            "key_prefix": c.key_prefix if c else None,
            "owner_user_id": c.owner_user_id if c else None,
            "key_active": bool(c and c.is_active),
            "status": status,
            "factor": factor,
            "reason": row.reason if row else None,
            "reports": int(counts.get(cid, 0)),
            "sellers": len(ls),
            "accepted": sum(1 for l in ls if l.status == LINK_ACCEPTED),
            "pending": sum(1 for l in ls if l.status == LINK_PENDING),
            "revoked_by_sellers": sum(1 for l in ls if l.status == LINK_REVOKED),
            "disputed_by_sellers": sum(1 for l in ls if l.disputed),
            "own_key_sellers": sum(1 for l in ls if c is not None and is_self_dealing(c, l.vendor_id)),
            "conflicts": _conflicts(cid, [l.vendor_id for l in ls if l.status == LINK_ACCEPTED]),
        })
    return out


CONFLICT_GAP = 0.35


def _conflicts(client_id, vendor_ids):
    """Sellers where this partner's good-outcome share differs from the
    first-party one by CONFLICT_GAP or more (first-party minimum history met)."""
    from app.services import trust_v1
    n = 0
    for vid in vendor_ids[:200]:
        first = trust_v1.score_orders(trust_v1.gather_orders(vid))
        if not first["minimum_history"]["met"]:
            continue
        facts, _ = gather_partner_facts(vid)
        mine = [f for f in facts if f["client_id"] == client_id and f["outcome"] in ("good", "bad")]
        if not mine:
            continue
        share = sum(1 for f in mine if f["outcome"] == "good") / len(mine)
        if abs(share - first["components"]["outcomes"]) >= CONFLICT_GAP:
            n += 1
    return n
