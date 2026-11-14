"""
Safe Pay requests (docs/SAFE_PAY_REQUEST_PLAN.md): buyers bring sellers to Siiqo.

Flow:
  1. create   the buyer checked a seller who isn't on Siiqo (/check) and asks
              them to accept Safe Pay. Siiqo stores the request and returns a
              link; the buyer sends it from their own WhatsApp or DM.
  2. accept   the seller opens the link, signs in, and (if they have no store)
              gives a business name and bank account. One transaction creates
              the store (as /start publish does) and a Pay Link for this buyer.
  3. the buyer is emailed (when they left an email) and their status page
     shows "Pay now". Paid is read from the Pay Link's orders.

Siiqo never sends anything to the seller. What the buyer checked is kept as a
keyed hash plus a display form (handles and websites in clear, phones masked).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.pay_request import PayRequest, PayRequestStatus
from app.models.payment_link import PaymentLink
from app.services import foundations

log = logging.getLogger(__name__)

TTL = timedelta(days=14)
MAX_PER_IP_PER_DAY = 10
MAX_OPEN_PER_IDENTIFIER = 50
NUDGE_EVERY = timedelta(hours=24)
MAX_ITEM_LEN = 140
MAX_AMOUNT = Decimal("10000000")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,120}\.[a-z]{2,24}$", re.I)
_UNPAID_ORDER_STATUSES = ("PENDING", "PENDING_PAYMENT", "CANCELLED", "FAILED", "EXPIRED")


class PayRequestError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def body(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hash(purpose: str, value: str) -> str:
    key = (current_app.config.get("SECRET_KEY") or "siiqo").encode("utf-8")
    return hmac.new(key, f"pay-request:{purpose}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


# ── What the buyer checked ─────────────────────────────────────────────────
def mask_phone(normalised: str) -> str:
    """'2348031234567' -> '+234 803 *** **67'."""
    d = re.sub(r"\D", "", normalised or "")
    if len(d) < 6:
        return "phone number"
    if d.startswith("234") and len(d) == 13:
        return f"+234 {d[3:6]} *** **{d[-2:]}"
    return f"{d[:3]} *** **{d[-2:]}"


def identify(raw: str) -> dict:
    """{query_type, kind, value, display, lookups, extras} for what was checked."""
    from app.routes.check import classify
    query_type, lookups, extras = classify(raw)
    kind = value = display = None
    for k, (op, v) in lookups:
        if k == "phone":
            kind, value, display = "phone", v, mask_phone(v)
        elif k == "handle":
            handle = v.split(":", 1)[-1].lstrip(":")
            kind, value, display = "handle", handle, f"@{handle}"
        elif k == "domain":
            kind, value, display = "domain", v, v
        elif k == "store_slug":
            kind, value, display = "store_slug", v, v
        if kind:
            break
    return {"query_type": query_type, "kind": kind or "unknown", "value": value,
            "display": display, "lookups": lookups, "extras": extras}


def _seller_on_siiqo(ident: dict):
    """The Siiqo store matching what was checked, or None."""
    if not ident["lookups"]:
        return None
    from app.models.user import Storefront
    from app.routes.check import _matches
    for vendor_id, _kind in _matches(ident["lookups"], ident["extras"]):
        sf = Storefront.query.filter_by(vendor_id=vendor_id).first()
        if sf is not None and sf.is_published:
            return sf
    return None


# ── Status ─────────────────────────────────────────────────────────────────
def _link_paid(link_id: int | None) -> bool:
    if not link_id:
        return False
    from app.models.order import Order
    return db.session.query(Order.id).filter(
        Order.payment_link_id == link_id, Order.status.notin_(_UNPAID_ORDER_STATUSES)
    ).first() is not None


def effective_status(req: PayRequest, now=None) -> str:
    now = now or _now()
    if req.status == PayRequestStatus.READY and _link_paid(req.payment_link_id):
        return "paid"
    if req.status == PayRequestStatus.OPEN and _aware(req.expires_at) <= now:
        return "expired"
    return req.status


def get(public_id: str) -> PayRequest | None:
    return PayRequest.query.filter_by(public_id=(public_id or "")[:32]).first()


def public(req: PayRequest) -> dict:
    """What anyone holding the link may see (seller and buyer). Never the
    buyer's email or the raw identifier."""
    from app.services.onboarding_publish import frontend_url, pay_link_url, store_url
    status = effective_status(req)
    out = {
        "id": req.public_id,
        "url": f"{frontend_url()}/pay/request/{req.public_id}",
        "status": status,
        "item": req.item,
        "amount": str(req.amount) if req.amount is not None else None,
        "currency": req.currency,
        "for": req.identifier_display if req.identifier_kind in ("handle", "domain") else None,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "expires_at": _aware(req.expires_at).isoformat() if req.expires_at else None,
        "store": None,
        "pay_link_url": None,
    }
    if req.listing_id:
        from app.models.directory import BusinessListing
        listing = db.session.get(BusinessListing, req.listing_id)
        if listing is not None:
            out["listing_name"] = listing.name
    if status in (PayRequestStatus.READY, "paid") and req.seller_user_id:
        from app.models.user import Storefront
        sf = Storefront.query.filter_by(vendor_id=req.seller_user_id).first()
        if sf is not None:
            out["store"] = {"name": sf.store_name, "slug": sf.store_slug, "url": store_url(sf.store_slug),
                            "passport_url": f"/id/{sf.store_slug}"}
        link = db.session.get(PaymentLink, req.payment_link_id) if req.payment_link_id else None
        if link is not None:
            out["pay_link_url"] = pay_link_url(link.slug)
    return out


# ── Create ─────────────────────────────────────────────────────────────────
def _clean_amount(value):
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise PayRequestError(400, "INVALID_AMOUNT", "Enter the amount in naira, or leave it empty.")
    if not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT:
        raise PayRequestError(400, "INVALID_AMOUNT", "Enter the amount in naira, or leave it empty.")
    return amount.quantize(Decimal("0.01"))


def create(raw: str, *, item=None, amount=None, buyer_email=None, buyer_user_id=None, ip=None) -> tuple[PayRequest, str]:
    """(request, buyer_token). The buyer token lets the buyer nudge later."""
    raw = (raw or "").strip()
    if not raw or len(raw) > 300:
        raise PayRequestError(400, "INVALID_SELLER", "Paste the seller's phone number, @handle or link.")
    ident = identify(raw)
    if ident["kind"] == "unknown" and not ident["value"]:
        raise PayRequestError(400, "INVALID_SELLER", "Paste the seller's phone number, @handle or link.")
    on_siiqo = _seller_on_siiqo(ident)
    if on_siiqo is not None:
        raise PayRequestError(409, "SELLER_ON_SIIQO", "This seller is already on Siiqo.",
                              store_slug=on_siiqo.store_slug)

    item = (str(item or "").strip()[:MAX_ITEM_LEN]) or None
    amount = _clean_amount(amount)
    email = (str(buyer_email or "").strip().lower()) or None
    if email and (len(email) > 120 or not _EMAIL_RE.match(email)):
        raise PayRequestError(400, "INVALID_EMAIL", "Check your email address, or leave it empty.")

    now = _now()
    ip_hash = _hash("ip", ip) if ip else None
    if ip_hash:
        recent = PayRequest.query.filter(PayRequest.ip_hash == ip_hash,
                                         PayRequest.created_at >= now - timedelta(days=1)).count()
        if recent >= MAX_PER_IP_PER_DAY:
            raise PayRequestError(429, "TOO_MANY_REQUESTS", "You've sent a lot of requests today. Try again tomorrow.")
    id_hash = _hash(ident["kind"], ident["value"]) if ident["value"] else None
    if id_hash:
        open_now = PayRequest.query.filter(PayRequest.identifier_hash == id_hash,
                                           PayRequest.status == PayRequestStatus.OPEN,
                                           PayRequest.expires_at > now).count()
        if open_now >= MAX_OPEN_PER_IDENTIFIER:
            raise PayRequestError(429, "TOO_MANY_REQUESTS", "This seller already has many open requests.")

    listing_id = None
    from app.services import directory
    listing = directory.listing_for_lookups(ident["lookups"], query_type=ident["query_type"], raw=raw)
    if listing is not None:
        listing_id = listing.id

    token = secrets.token_urlsafe(24)
    for _ in range(3):
        req = PayRequest(
            public_id=secrets.token_urlsafe(9),
            buyer_token_hash=_hash("buyer", token),
            status=PayRequestStatus.OPEN,
            identifier_kind=ident["kind"],
            identifier_hash=id_hash,
            identifier_display=ident["display"],
            item=item, amount=amount, currency="NGN",
            buyer_email=email, buyer_user_id=buyer_user_id, listing_id=listing_id,
            ip_hash=ip_hash, created_at=now, expires_at=now + TTL,
        )
        db.session.add(req)
        try:
            db.session.commit()
            break
        except IntegrityError:          # public_id collision; try another
            db.session.rollback()
    else:
        raise PayRequestError(503, "TRY_AGAIN", "Please try again.")
    foundations.record_event("pay_request_created", user_id=buyer_user_id,
                             props={"kind": ident["kind"], "has_amount": amount is not None,
                                    "has_email": bool(email), "listing": bool(listing_id)})
    return req, token


# ── Seller side ────────────────────────────────────────────────────────────
def _require_open(req: PayRequest):
    status = effective_status(req)
    if status != PayRequestStatus.OPEN:
        raise PayRequestError(409, "NOT_OPEN", "This request is no longer open.", status=status)


def accept(req: PayRequest, user, body: dict | None = None) -> dict:
    """Seller accepts. Without a store, ``body`` carries business_name and the
    bank fields (as /start's bank step); the store and the Pay Link are created
    in one transaction."""
    from app.services import onboarding_publish as op
    body = body or {}
    if req.status == PayRequestStatus.READY and req.seller_user_id == user.id:
        return public(req)                        # repeat tap: same result
    _require_open(req)
    if req.buyer_user_id and req.buyer_user_id == user.id:
        raise PayRequestError(403, "OWN_REQUEST", "You can't accept your own request.")

    storefront = user.storefront
    created_store = False
    if storefront is None:
        if not user.is_verified:
            raise PayRequestError(403, "EMAIL_NOT_VERIFIED", "Please verify your email first.")
        name = str(body.get("business_name") or "").strip()
        if not (2 <= len(name) <= 100):
            raise PayRequestError(400, "NEEDS_STORE", "Tell us your business name and bank account.",
                                  fields=["business_name", "bank"])
        slug = str(body.get("store_slug") or "").strip().lower() or op.slugify(name)
        reason = op.slug_status(slug)
        if reason is not None:
            raise PayRequestError(409, "SLUG_UNAVAILABLE", f"The store link '{slug}' is not available.",
                                  reason=reason, slug=slug, suggestions=op.suggest_slugs(slug))
        try:
            bank = op._confirm_with_bank(op.validate_bank(body))
        except op.PublishError as e:
            raise PayRequestError(e.status, e.code, e.message, **e.extra)
        storefront = op._new_store(user, name, slug, bank)
        created_store = True
        from app.services.marketing import credit_signup
        credit_signup(user, body)

    title = (req.item or f"Order from {storefront.store_name}")[:100]
    link = PaymentLink(
        vendor_id=user.id,
        # One buyer, one payment: an invoice when the amount is known (marked
        # PAID once settled), otherwise an open-amount Pay Link.
        link_type="INVOICE" if req.amount is not None else "PAY_LINK",
        title=title,
        description="Requested through a Siiqo Safe Pay request.",
        amount=req.amount,
        currency=req.currency or "NGN",
        buyer_email=req.buyer_email,
        status="ACTIVE",
        slug=op._pay_link_slug(title),
        product_type="physical",
        file_url=None,
    )
    db.session.add(link)
    db.session.flush()
    req.status = PayRequestStatus.READY
    req.seller_user_id = user.id
    req.payment_link_id = link.id
    req.accepted_at = _now()
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        log.warning("pay request accept conflict %s: %s", req.public_id, exc.orig)
        raise PayRequestError(409, "CONFLICT", "Something changed. Please try again.")

    if created_store:
        op._attach_paystack_subaccount(storefront.id, user.id)
    foundations.record_event("pay_request_accepted", user_id=user.id, vendor_id=user.id,
                             payment_link_id=link.id, props={"new_store": created_store})
    result = public(req)
    _email_buyer(req, result)
    return result


def _email_buyer(req: PayRequest, result: dict) -> None:
    """Best effort; accepting never fails because of email."""
    if not req.buyer_email or not result.get("pay_link_url"):
        return
    try:
        from app.utils.email import send_siiqo_email
        store = (result.get("store") or {}).get("name") or "Your seller"
        what = f" for {req.item}" if req.item else ""
        send_siiqo_email(
            to_email=req.buyer_email,
            subject=f"{store} is ready for your Safe Pay order",
            template_name="base_notification",
            first_name="there",
            title="Your seller is ready",
            message=(f"{store} accepted your request to pay through Siiqo{what}. "
                     "For physical orders, Safe Pay holds your money until you confirm delivery, "
                     "or 72 hours after the seller marks it delivered."),
            cta_text="Pay safely now",
            cta_url=result["pay_link_url"],
        )
    except Exception as exc:  # noqa: BLE001
        log.info("pay request: buyer email not sent: %s", exc)


def close(req: PayRequest, outcome: str, reason=None) -> dict:
    """Seller declines, or reports "not my business / abuse"."""
    if outcome not in (PayRequestStatus.DECLINED, PayRequestStatus.REPORTED):
        raise PayRequestError(400, "INVALID_OUTCOME", "Unknown outcome.")
    _require_open(req)
    req.status = outcome
    req.closed_at = _now()
    req.close_reason = (str(reason or "").strip()[:200]) or None
    db.session.commit()
    foundations.record_event(f"pay_request_{outcome}", props={"kind": req.identifier_kind})
    return public(req)


# ── Buyer side ─────────────────────────────────────────────────────────────
def nudge(req: PayRequest, token: str) -> dict:
    """The buyer re-shares the link (at most once a day). Siiqo sends nothing."""
    if not token or not hmac.compare_digest(_hash("buyer", token), req.buyer_token_hash):
        raise PayRequestError(403, "NOT_YOURS", "Only the buyer who sent this request can nudge.")
    _require_open(req)
    now = _now()
    if req.nudged_at and _aware(req.nudged_at) > now - NUDGE_EVERY:
        raise PayRequestError(429, "TOO_SOON", "You can nudge again tomorrow.",
                              next_at=(_aware(req.nudged_at) + NUDGE_EVERY).isoformat())
    req.nudged_at = now
    req.nudge_count = (req.nudge_count or 0) + 1
    db.session.commit()
    foundations.record_event("pay_request_nudged", props={"count": req.nudge_count})
    return public(req)


# ── Admin ──────────────────────────────────────────────────────────────────
def most_wanted(limit: int = 50, days: int = 60) -> list[dict]:
    """Businesses buyers asked for most (open or declined requests), newest
    demand first among equals. Phones appear masked only."""
    since = _now() - timedelta(days=days)
    rows = db.session.query(
        PayRequest.identifier_hash, func.count(PayRequest.id), func.max(PayRequest.created_at),
    ).filter(
        PayRequest.identifier_hash.isnot(None), PayRequest.created_at >= since,
        PayRequest.status != PayRequestStatus.REPORTED,
    ).group_by(PayRequest.identifier_hash).order_by(
        func.count(PayRequest.id).desc(), func.max(PayRequest.created_at).desc()
    ).limit(max(1, min(limit, 200))).all()
    out = []
    for id_hash, count, last in rows:
        reqs = PayRequest.query.filter_by(identifier_hash=id_hash).order_by(PayRequest.id.desc()).limit(20).all()
        latest = reqs[0]
        ready = next((r for r in reqs if r.status == PayRequestStatus.READY), None)
        out.append({
            "kind": latest.identifier_kind,
            "display": latest.identifier_display,
            "requests": int(count),
            "last_requested_at": _aware(last).isoformat() if last else None,
            "items": [r.item for r in reqs if r.item][:3],
            "total_amount": str(sum((r.amount for r in reqs if r.amount is not None), Decimal("0"))),
            "listing_id": next((r.listing_id for r in reqs if r.listing_id), None),
            "joined": ready is not None,
        })
    return out
