"""Siiqo marketing system (docs/MARKETING_SYSTEM_PLAN.md).

Every person, place and printed thing gets a short code (siiqo.com/r/<CODE>).
This module:

- resolves a code and counts the scan (no IP address or personal data);
- credits a new seller to the first code they signed up through
  (SellerAcquisition; first touch wins, never overwritten);
- records the seller's milestones (store set up, first Pay Link, first paid
  order from a separate buyer), idempotently, from session hooks and an
  hourly sweep;
- writes agent earnings to a ledger. Agents earn nothing for a signup or a
  store being set up: they earn a fixed amount for each of the first few
  qualifying sales of a seller they brought (paid, not free, from a separate
  buyer; by default each from a different buyer). A reward is HELD for the
  hold period, then PAYABLE; if its order is refunded, cancelled or disputed
  while HELD it is REVERSED and the slot is free for a later sale. Nothing is
  paid out by the system: an admin pays by hand and marks the rows PAID.

Settings (env): AGENT_SALE_REWARD_NGN (200), AGENT_REWARDED_SALES (5),
AGENT_SALES_DISTINCT_BUYERS (true), AGENT_REWARD_HOLD_DAYS (7),
SETUP_MIN_PRODUCTS (5), ATTRIBUTION_WINDOW_DAYS (30).
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import event, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.extensions import db
from app.models.marketing import (AgentEarning, AgentProfile, MarketingCampaign, MarketingCode,
                                  MarketingScan, SellerAcquisition)

logger = logging.getLogger(__name__)

LAGOS = timezone(timedelta(hours=1))
CODE_RE = re.compile(r"^[A-Z0-9-]{3,24}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Readable codes: no 0/O/1/I, so they can be typed from a flyer or said on radio.
READABLE = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SCAN_DEDUPE_MINUTES = 30
BURST_SIGNUPS_PER_DAY = 15
ADMIN_CODE_KINDS = ("agent", "location", "flyer", "influencer", "media", "campaign")
# Codes that put a seller's signup down to someone. A shop sticker lands on the
# seller's Passport for buyers, so it never credits a new seller.
CREDIT_KINDS = ("agent", "location", "flyer", "influencer", "media", "campaign", "seller_invite")
CLOSED_ORDER_STATUSES = ("CANCELLED", "REFUNDED")
BAD_ORDER_STATUSES = ("CANCELLED", "REFUNDED", "DISPUTED")
BAD_ESCROW_STATUSES = ("REFUNDED", "CANCELLED", "DISPUTED")


class MarketingError(Exception):
    def __init__(self, http_status, code, message, field=None, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.field, self.extra = http_status, code, message, field, extra

    def to_dict(self):
        out = {"code": self.code, "message": self.message, **self.extra}
        if self.field:
            out["field"] = self.field
        return out


# ── settings and small helpers ───────────────────────────────────────────────

def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def sale_reward():
    return max(0, _int_env("AGENT_SALE_REWARD_NGN", 200))


def rewarded_sales():
    """How many sales of each seller earn the agent a reward."""
    return max(0, _int_env("AGENT_REWARDED_SALES", 5))


def sales_distinct_buyers():
    """Each rewarded sale must come from a different buyer (default on)."""
    raw = os.environ.get("AGENT_SALES_DISTINCT_BUYERS")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def max_per_seller():
    return sale_reward() * rewarded_sales()


def hold_days():
    return _int_env("AGENT_REWARD_HOLD_DAYS", 7)


def min_products():
    return _int_env("SETUP_MIN_PRODUCTS", 5)


def attribution_window_days():
    return _int_env("ATTRIBUTION_WINDOW_DAYS", 30)


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    """SQLite gives back naive datetimes; everything here is UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso(dt):
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def _money(value):
    return float(value or 0)


def _norm_phone(value):
    from app.services.foundations import _norm_phone as norm
    return norm(value)


def _norm_email(value):
    from app.services.foundations import _norm_email as norm
    return norm(value)


def _digits(value):
    return re.sub(r"\D", "", str(value or ""))


def frontend_url():
    return (os.environ.get("FRONTEND_URL") or "https://siiqo.com").rstrip("/")


def code_link(code):
    return f"{frontend_url()}/r/{code}" if code else None


def page(args, default_per_page=20, max_per_page=100):
    try:
        p = max(1, int(args.get("page") or 1))
    except (TypeError, ValueError):
        p = 1
    try:
        per = int(args.get("per_page") or default_per_page)
    except (TypeError, ValueError):
        per = default_per_page
    return p, max(1, min(per, max_per_page))


def pages(total, per_page):
    return max(1, -(-total // per_page)) if total else 1


def short_name(full_name):
    """'Amaka Obi' -> 'Amaka O.' (first name + last initial)."""
    parts = (full_name or "").split()
    if not parts:
        return "Agent"
    return f"{parts[0]} {parts[-1][0].upper()}." if len(parts) > 1 else parts[0]


def period_start(period, now=None):
    """Start of today / this week (Monday) / this month, Lagos time, as UTC."""
    local = (now or _now()).astimezone(LAGOS).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        local -= timedelta(days=local.weekday())
    elif period == "month":
        local = local.replace(day=1)
    return local.astimezone(timezone.utc)


# ── codes ────────────────────────────────────────────────────────────────────

def normalize_code(raw):
    code = (raw or "").strip().upper() if isinstance(raw, str) else ""
    return code if CODE_RE.match(code) else None


def validate_destination(raw):
    """A site path only ("/sell", "/id/my-shop?x=1"). Never another site."""
    dest = raw.strip() if isinstance(raw, str) else ""
    if (not dest or not dest.startswith("/") or dest.startswith("//") or "\\" in dest
            or "://" in dest or len(dest) > 300 or re.search(r"[\s\x00-\x1f\x7f]", dest)):
        raise MarketingError(400, "INVALID_DESTINATION",
                             'Destination must be a page on Siiqo, starting with "/" (for example /sell).',
                             field="destination")
    return dest


def _random_part(length):
    return "".join(secrets.choice(READABLE) for _ in range(length))


def code_taken(code):
    return db.session.query(MarketingCode.id).filter(MarketingCode.code == code).first() is not None


def generate_code(prefix="", length=6):
    prefix = re.sub(r"[^A-Z0-9-]", "", (prefix or "").upper())[:16]
    for attempt in range(30):
        n = length + (attempt // 10)
        code = f"{prefix}{_random_part(n)}"[:24]
        if len(code) >= 3 and not code_taken(code):
            return code
    raise MarketingError(500, "CODE_GENERATION_FAILED", "Could not make a free code. Please try again.")


def create_code(kind, destination, *, label=None, campaign_id=None, owner_user_id=None, code=None,
                prefix=""):
    """Adds a MarketingCode to the session (caller commits)."""
    if kind not in MarketingCode.KINDS:
        raise MarketingError(400, "INVALID_KIND", "Pick a valid code type.", field="kind")
    destination = validate_destination(destination)
    if code is not None and str(code).strip():
        value = normalize_code(str(code))
        if not value:
            raise MarketingError(400, "INVALID_CODE",
                                 "A code is 3 to 24 letters, numbers or dashes.", field="code")
        if code_taken(value):
            raise MarketingError(409, "CODE_TAKEN", "That code is already in use.", field="code")
    else:
        value = generate_code(prefix)
    if campaign_id is not None and db.session.get(MarketingCampaign, campaign_id) is None:
        raise MarketingError(400, "CAMPAIGN_NOT_FOUND", "That campaign does not exist.", field="campaign_id")
    row = MarketingCode(code=value, kind=kind, label=(label or "").strip()[:120] or None,
                        destination=destination, campaign_id=campaign_id, owner_user_id=owner_user_id,
                        active=True, scan_count=0)
    db.session.add(row)
    return row


def find_code(raw):
    code = normalize_code(raw)
    if not code:
        return None
    return MarketingCode.query.filter_by(code=code).first()


def code_is_live(code):
    if not code or not code.active:
        return False
    if code.campaign_id:
        campaign = code.campaign
        if campaign is not None and campaign.status in ("PAUSED", "ENDED"):
            return False
    return True


def resolve_code(raw, sid=None):
    """(http_status, body) for GET /api/public/r/<code>. Counts the scan."""
    code = find_code(raw)
    if code is None:
        return 404, {"code": "CODE_NOT_FOUND", "message": "We couldn't find that code."}
    if not code_is_live(code):
        return 410, {"code": "CODE_INACTIVE", "message": "This code is no longer in use.", "destination": "/"}

    campaign_slug = code.campaign.slug if code.campaign_id and code.campaign else None
    sid = (sid or "").strip()[:100] or None
    counted = False
    try:
        recent = None
        if sid:
            recent = MarketingScan.query.filter(
                MarketingScan.code_id == code.id, MarketingScan.session_id == sid,
                MarketingScan.created_at >= _now() - timedelta(minutes=SCAN_DEDUPE_MINUTES)).first()
        if recent is None:
            db.session.add(MarketingScan(code_id=code.id, campaign_id=code.campaign_id, session_id=sid,
                                         created_at=_now()))
            db.session.execute(update(MarketingCode).where(MarketingCode.id == code.id)
                               .values(scan_count=MarketingCode.scan_count + 1))
            db.session.commit()
            counted = True
    except Exception as e:  # a counting problem never stops the visitor
        db.session.rollback()
        logger.warning(f"[MARKETING] scan not counted for {code.code}: {e}")
    if counted:
        from app.services.foundations import record_event
        record_event("code_scanned", session_id=sid,
                     props={"code": code.code, "kind": code.kind, "campaign": campaign_slug})
    return 200, {
        "code": code.code,
        "kind": code.kind,
        "label": code.label,
        "destination": code.destination,
        "campaign": campaign_slug,
        "utm": {"source": code.kind, "medium": "qr", "campaign": campaign_slug or code.code.lower()},
        "ref": code.code,
    }


# ── crediting a new seller ───────────────────────────────────────────────────

def _ref_from(data):
    """ref_code, else attribution.ref, from a request body (dict or form)."""
    if data is None:
        return None, None
    getter = data.get if hasattr(data, "get") else (lambda k, d=None: d)
    ref = getter("ref_code")
    attribution = getter("attribution")
    if isinstance(attribution, str):
        try:
            attribution = json.loads(attribution)
        except (TypeError, ValueError):
            attribution = None
    if not isinstance(attribution, dict):
        attribution = {}
    if not ref:
        ref = attribution.get("ref")
    ref_at = getter("ref_at") or attribution.get("ref_at")
    return (ref if isinstance(ref, str) else None), ref_at


def _too_old(ref_at):
    """Optional time the visitor came through the code (ISO). Older than the
    attribution window means no credit. Missing means the website kept it."""
    if not ref_at:
        return False
    try:
        when = _aware(datetime.fromisoformat(str(ref_at).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return False
    return when < _now() - timedelta(days=attribution_window_days())


def credit_signup(user, data):
    """Credit a new account (or a new store) to the code it came through.
    First touch wins; nothing is overwritten. Adds to the session in a
    savepoint and never raises: the caller commits. Returns the row or None."""
    try:
        ref, ref_at = _ref_from(data)
        if not ref or user is None or user.id is None or _too_old(ref_at):
            return None
        code = find_code(ref)
        if code is None or not code_is_live(code) or code.kind not in CREDIT_KINDS:
            return None
        if code.owner_user_id and code.owner_user_id == user.id:
            return None  # no self-credit
        if SellerAcquisition.query.filter_by(seller_user_id=user.id).first() is not None:
            return None
        agent_user_id = None
        if code.owner_user_id and code.kind != "seller_invite":
            profile = AgentProfile.query.filter_by(user_id=code.owner_user_id).first()
            if profile is not None and profile.status == "APPROVED":
                agent_user_id = code.owner_user_id
        now = _now()
        flags = []
        if agent_user_id:
            today = period_start("day", now)
            credited_today = SellerAcquisition.query.filter(
                SellerAcquisition.agent_user_id == agent_user_id,
                SellerAcquisition.signed_up_at >= today).count()
            if credited_today >= BURST_SIGNUPS_PER_DAY:
                flags.append("burst")
        acq = SellerAcquisition(seller_user_id=user.id, code_id=code.id, agent_user_id=agent_user_id,
                                campaign_id=code.campaign_id, signed_up_at=now, flags=flags, created_at=now)
        with db.session.begin_nested():
            db.session.add(acq)
            db.session.flush()
        return acq
    except IntegrityError:
        return None  # someone else credited this seller first
    except Exception as e:
        logger.warning(f"[MARKETING] signup credit skipped for user {getattr(user, 'id', None)}: {e}")
        return None


# ── milestones ───────────────────────────────────────────────────────────────

def _store(user_id):
    from app.models.user import Storefront
    return Storefront.query.filter_by(vendor_id=user_id).first()


def active_product_count(storefront):
    from app.models.product import Product
    if storefront is None:
        return 0
    return Product.query.filter(Product.storefront_id == storefront.id, Product.is_active.is_(True),
                                db.or_(Product.is_deleted.is_(False), Product.is_deleted.is_(None))).count()


def has_place(storefront):
    return bool(storefront and ((storefront.city or "").strip() or (storefront.state or "").strip()
                                or storefront.delivery_settings))


def bank_accounts(user_id, storefront=None):
    """Payout account numbers (digits) for a seller."""
    from app.models.withdrawal import VendorBankAccount
    numbers = {_digits(a.account_number) for a in VendorBankAccount.query.filter_by(vendor_id=user_id).all()}
    storefront = storefront if storefront is not None else _store(user_id)
    if storefront is not None and storefront.account_number:
        numbers.add(_digits(storefront.account_number))
    return {n for n in numbers if len(n) >= 6}


def setup_steps(user_id):
    sf = _store(user_id)
    return {
        "published": bool(sf and sf.is_published),
        "products": active_product_count(sf),
        "place": has_place(sf),
        "bank": bool(bank_accounts(user_id, sf)),
        "storefront": sf,
    }


def _people(seller_user_id, agent_user_id):
    """User ids, emails and phones that are not a separate buyer."""
    from app.models.user import User
    ids, emails, phones = {seller_user_id}, set(), set()
    seller = db.session.get(User, seller_user_id)
    if seller is not None:
        emails.add(_norm_email(seller.email))
        phones.add(_norm_phone(seller.phone))
    sf = _store(seller_user_id)
    if sf is not None:
        phones.add(_norm_phone(sf.phone))
        phones.add(_norm_phone((sf.social_links or {}).get("whatsapp") if isinstance(sf.social_links, dict) else None))
    if agent_user_id:
        ids.add(agent_user_id)
        agent = db.session.get(User, agent_user_id)
        if agent is not None:
            emails.add(_norm_email(agent.email))
            phones.add(_norm_phone(agent.phone))
        profile = AgentProfile.query.filter_by(user_id=agent_user_id).first()
        if profile is not None:
            phones.add(_norm_phone(profile.phone))
    emails.discard(None)
    phones.discard(None)
    return ids, emails, phones


def _separate_buyer(order, people):
    from app.models.user import User
    ids, emails, phones = people
    if order.buyer_id and order.buyer_id in ids:
        return False
    buyer_emails = {_norm_email(order.buyer_email)}
    buyer_phones = {_norm_phone(order.buyer_phone), _norm_phone(order.delivery_phone)}
    if order.buyer_id:
        buyer = db.session.get(User, order.buyer_id)
        if buyer is not None:
            buyer_emails.add(_norm_email(buyer.email))
            buyer_phones.add(_norm_phone(buyer.phone))
    buyer_emails.discard(None)
    buyer_phones.discard(None)
    return not (buyer_emails & emails) and not (buyer_phones & phones)


def paid_orders_query(vendor_id=None):
    from app.models.order import Order
    q = Order.query.filter(Order.paid_at.isnot(None),
                           func.upper(func.coalesce(Order.payment_method, "")) != "FREE",
                           func.upper(Order.status).notin_(CLOSED_ORDER_STATUSES))
    if vendor_id is not None:
        q = q.filter(Order.vendor_id == vendor_id)
    return q


def qualifying_orders(acq):
    """(qualifying, self_purchase) paid orders for an acquired seller, oldest first."""
    from app.models.order import Order
    people = _people(acq.seller_user_id, acq.agent_user_id)
    good, bad = [], []
    for order in paid_orders_query(acq.seller_user_id).order_by(Order.paid_at, Order.id).all():
        (good if _separate_buyer(order, people) else bad).append(order)
    return good, bad


def buyer_key(order):
    """Who bought: user id if any, else normalised email, else phone. None
    when the buyer can't be told apart from anyone."""
    if order.buyer_id:
        return f"u:{order.buyer_id}"
    email = _norm_email(order.buyer_email)
    if email:
        return f"e:{email}"
    phone = _norm_phone(order.buyer_phone) or _norm_phone(order.delivery_phone)
    return f"p:{phone}" if phone else None


def _add_flag(acq, flag):
    flags = list(acq.flags or [])
    if flag not in flags:
        acq.flags = flags + [flag]  # a new list, so the JSON change is saved


def _rewarded_identities(seller_user_id):
    """Phones and bank account numbers of other sellers that already produced
    an agent reward."""
    from app.models.user import User
    other_ids = [r[0] for r in db.session.query(AgentEarning.seller_user_id).filter(
        AgentEarning.seller_user_id.isnot(None), AgentEarning.seller_user_id != seller_user_id,
        AgentEarning.kind.in_(("sale", "setup", "first_sale"))).distinct().all()]
    phones, banks = set(), set()
    for uid in other_ids:
        user = db.session.get(User, uid)
        sf = _store(uid)
        phones.update({_norm_phone(user.phone) if user else None, _norm_phone(sf.phone) if sf else None})
        banks.update(bank_accounts(uid, sf))
    phones.discard(None)
    return phones, banks


def _seller_identities(seller_user_id):
    from app.models.user import User
    user = db.session.get(User, seller_user_id)
    sf = _store(seller_user_id)
    phones = {_norm_phone(user.phone) if user else None, _norm_phone(sf.phone) if sf else None}
    phones.discard(None)
    return phones, bank_accounts(seller_user_id, sf)


BLOCKING_FLAGS = ("duplicate_phone", "duplicate_bank", "shared_bank_with_agent")


def _may_reward(acq):
    """Only an APPROVED agent earns, and never for a seller whose phone or
    bank account matches another rewarded seller or the agent's own bank."""
    if not acq.agent_user_id:
        return False
    profile = AgentProfile.query.filter_by(user_id=acq.agent_user_id).first()
    if profile is None or profile.status != "APPROVED":
        return False
    phones, banks = _seller_identities(acq.seller_user_id)
    rewarded_phones, rewarded_banks = _rewarded_identities(acq.seller_user_id)
    if phones & rewarded_phones:
        _add_flag(acq, "duplicate_phone")
    if banks & rewarded_banks:
        _add_flag(acq, "duplicate_bank")
    return not set(acq.flags or []) & set(BLOCKING_FLAGS)


def _reverse_bad_sale_earnings(acq, earnings):
    """A HELD sale reward whose order went bad is reversed (its slot frees
    up). One that already cleared the hold is left for an admin, flagged."""
    changed = False
    for e in earnings:
        if e.status == "REVERSED" or not e.order_id:
            continue
        reason = order_went_bad(e.order_id)
        if not reason:
            continue
        if e.status == "HELD":
            e.status = "REVERSED"
            e.reversed_reason = reason[:255]
            changed = True
        elif e.status == "PAYABLE":
            _add_flag(acq, "payable_then_refunded")
        elif e.status == "PAID":
            _add_flag(acq, "paid_then_refunded")
    if changed:
        db.session.flush()


def _award_sales(acq, now):
    """Make sure the first AGENT_REWARDED_SALES qualifying sales of this seller
    (oldest paid first, skipping bad orders and, by default, repeat buyers)
    each have a "sale" reward. Idempotent."""
    from app.models.order import Order
    earnings = (AgentEarning.query.filter_by(seller_user_id=acq.seller_user_id, kind="sale")
                .order_by(AgentEarning.id).all())
    _reverse_bad_sale_earnings(acq, earnings)
    limit = rewarded_sales()
    live = [e for e in earnings if e.status != "REVERSED"]
    if len(live) >= limit:
        return []
    rewarded_order_ids = {e.order_id for e in earnings if e.order_id}
    used_slots = {e.sale_number for e in live if e.sale_number}
    distinct = sales_distinct_buyers()
    used_buyers = set()
    if distinct:
        for e in live:
            order = db.session.get(Order, e.order_id) if e.order_id else None
            key = buyer_key(order) if order is not None else None
            if key:
                used_buyers.add(key)
    people = _people(acq.seller_user_id, acq.agent_user_id)
    made = []
    allowed = None  # checked at the first candidate sale (it reads every rewarded seller)
    for order in paid_orders_query(acq.seller_user_id).order_by(Order.paid_at, Order.id).all():
        if len(live) + len(made) >= limit:
            break
        if order.id in rewarded_order_ids or not _separate_buyer(order, people) or order_went_bad(order.id):
            continue
        key = buyer_key(order)
        if distinct:
            if key is None or key in used_buyers:
                continue
            used_buyers.add(key)
        free = [n for n in range(1, limit + 1) if n not in used_slots]
        if not free:
            break
        if allowed is None:
            allowed = _may_reward(acq)
        if not allowed:
            break
        earning = AgentEarning(agent_user_id=acq.agent_user_id, seller_user_id=acq.seller_user_id, kind="sale",
                               order_id=order.id, sale_number=free[0], amount_ngn=Decimal(sale_reward()),
                               status="HELD", earned_at=now, payable_at=now + timedelta(days=hold_days()),
                               created_at=now)
        try:
            with db.session.begin_nested():
                db.session.add(earning)
                db.session.flush()
        except IntegrityError:
            continue  # rewarded at the same moment elsewhere
        used_slots.add(free[0])
        made.append(earning)
    return made


def recompute_for(seller_user_id, now=None):
    """Record any milestones this seller has reached and bring the agent's
    sale rewards up to date. Idempotent; never moves a milestone that is
    already set. Caller commits."""
    acq = SellerAcquisition.query.filter_by(seller_user_id=seller_user_id).first()
    if acq is None:
        return None
    now = now or _now()

    # Shared bank with the agent: flagged, and no rewards for this seller.
    if acq.agent_user_id:
        profile = AgentProfile.query.filter_by(user_id=acq.agent_user_id).first()
        if profile is not None and _digits(profile.account_number) in bank_accounts(seller_user_id):
            _add_flag(acq, "shared_bank_with_agent")

    # Set-up is still tracked (stats, the agent's checklist) but earns nothing.
    if acq.setup_at is None:
        steps = setup_steps(seller_user_id)
        if steps["published"] and steps["products"] >= min_products() and steps["place"] and steps["bank"]:
            acq.setup_at = now

    if acq.pay_link_at is None:
        from app.models.payment_link import PaymentLink
        link = (PaymentLink.query.filter_by(vendor_id=seller_user_id)
                .order_by(PaymentLink.created_at, PaymentLink.id).first())
        if link is not None:
            acq.pay_link_at = _aware(link.created_at) or now

    if acq.first_paid_order_at is None:
        good, bad = qualifying_orders(acq)
        if bad:
            _add_flag(acq, "self_purchase")
        if good:
            acq.first_paid_order_at = _aware(good[0].paid_at) or now
            acq.first_paid_order_id = good[0].id

    if acq.agent_user_id:
        _award_sales(acq, now)
    return acq


# ── session hooks: recompute when a seller's store, products, bank, Pay
#    Links or orders change, in the same transaction ─────────────────────────

_HOOKS_REGISTERED = False


def _after_flush(session, flush_context):
    try:
        from app.models.dispute import DisputeCase
        from app.models.escrow import EscrowTransaction
        from app.models.order import Order
        from app.models.payment_link import PaymentLink
        from app.models.product import Product
        from app.models.user import Storefront
        from app.models.withdrawal import VendorBankAccount
        sellers = session.info.setdefault("_mkt_sellers", set())
        stores = session.info.setdefault("_mkt_stores", set())
        orders = session.info.setdefault("_mkt_orders", set())
        for obj in list(session.new) + list(session.dirty):
            if isinstance(obj, (Storefront, VendorBankAccount, PaymentLink)):
                if obj.vendor_id:
                    sellers.add(obj.vendor_id)
            elif isinstance(obj, Product):
                if obj.storefront_id:
                    stores.add(obj.storefront_id)
            elif isinstance(obj, Order) and obj.paid_at is not None and obj.vendor_id:
                sellers.add(obj.vendor_id)
            elif isinstance(obj, DisputeCase) and obj.vendor_id:
                sellers.add(obj.vendor_id)
            elif isinstance(obj, EscrowTransaction) and obj.order_id:
                orders.add(obj.order_id)
    except Exception as e:
        logger.warning(f"[MARKETING] after_flush skipped: {e}")


def _before_commit(session):
    if session.info.get("_mkt_busy"):
        return
    try:
        if session is not db.session():  # only the app's own session
            return
    except Exception:
        return
    if not (session.info.get("_mkt_sellers") or session.info.get("_mkt_stores")
            or session.info.get("_mkt_orders")):
        # Pending objects are only seen once flushed.
        if not (session.new or session.dirty):
            return
    session.flush()
    sellers = set(session.info.pop("_mkt_sellers", set()) or ())
    stores = session.info.pop("_mkt_stores", set()) or set()
    orders = session.info.pop("_mkt_orders", set()) or set()
    if not sellers and not stores and not orders:
        return
    session.info["_mkt_busy"] = True
    try:
        with session.begin_nested():
            if stores:
                from app.models.user import Storefront
                sellers |= {r[0] for r in session.query(Storefront.vendor_id)
                            .filter(Storefront.id.in_(list(stores))).all()}
            if orders:
                from app.models.order import Order
                sellers |= {r[0] for r in session.query(Order.vendor_id)
                            .filter(Order.id.in_(list(orders))).all() if r[0]}
            ids = [r[0] for r in session.query(SellerAcquisition.seller_user_id)
                   .filter(SellerAcquisition.seller_user_id.in_(list(sellers))).all()]
            for seller_id in ids:
                recompute_for(seller_id)
            session.flush()
    except Exception as e:  # never block the change that triggered this
        logger.warning(f"[MARKETING] milestone update skipped: {e}")
    finally:
        session.info.pop("_mkt_busy", None)
        session.info.pop("_mkt_sellers", None)
        session.info.pop("_mkt_stores", None)
        session.info.pop("_mkt_orders", None)


def _clear(session, *args):
    session.info.pop("_mkt_sellers", None)
    session.info.pop("_mkt_stores", None)
    session.info.pop("_mkt_orders", None)


def register_hooks():
    global _HOOKS_REGISTERED
    if _HOOKS_REGISTERED:
        return
    event.listen(Session, "after_flush", _after_flush)
    event.listen(Session, "before_commit", _before_commit)
    event.listen(Session, "after_soft_rollback", _clear)
    _HOOKS_REGISTERED = True


# ── hourly sweep ─────────────────────────────────────────────────────────────

_BAD_REASONS = {"REFUNDED": "Order refunded", "CANCELLED": "Order cancelled", "DISPUTED": "Order disputed"}


def order_went_bad(order_id):
    """Reason text when an order was refunded, cancelled or disputed, else None."""
    from app.models.dispute import DisputeCase
    from app.models.escrow import EscrowTransaction
    from app.models.order import Order
    order = db.session.get(Order, order_id) if order_id else None
    if order is None:
        return "Order no longer exists"
    status = (order.status or "").upper()
    if status in BAD_ORDER_STATUSES:
        return _BAD_REASONS[status]
    escrow = EscrowTransaction.query.filter_by(order_id=order.id).first()
    if escrow is not None and (escrow.status or "").upper() in BAD_ESCROW_STATUSES:
        return _BAD_REASONS[(escrow.status or "").upper()]
    if DisputeCase.query.filter_by(order_id=order.id).first() is not None:
        return "Order disputed"
    return None


def run_marketing_sweep(now=None):
    """Hourly: catch any missed milestones and sale rewards, then release or
    reverse held rewards. Idempotent."""
    now = now or _now()
    recomputed = 0
    open_rows = SellerAcquisition.query.filter(
        db.or_(SellerAcquisition.setup_at.is_(None), SellerAcquisition.pay_link_at.is_(None),
               SellerAcquisition.first_paid_order_at.is_(None), SellerAcquisition.agent_user_id.isnot(None)),
        SellerAcquisition.signed_up_at >= now - timedelta(days=365)).all()
    for acq in open_rows:
        try:
            with db.session.begin_nested():
                recompute_for(acq.seller_user_id, now)
            recomputed += 1
        except Exception as e:
            logger.warning(f"[MARKETING] sweep skipped seller {acq.seller_user_id}: {e}")

    released = reversed_count = 0
    for earning in AgentEarning.query.filter_by(status="HELD").all():
        reason = None
        if earning.kind == "sale" and earning.order_id:
            reason = order_went_bad(earning.order_id)
        elif earning.kind == "first_sale" and earning.seller_user_id:  # old rows
            acq = SellerAcquisition.query.filter_by(seller_user_id=earning.seller_user_id).first()
            reason = order_went_bad(acq.first_paid_order_id if acq else None)
        if reason:
            earning.status = "REVERSED"
            earning.reversed_reason = reason[:255]
            reversed_count += 1
        elif earning.payable_at is not None and _aware(earning.payable_at) <= now:
            earning.status = "PAYABLE"
            released += 1
    db.session.commit()
    return {"recomputed": recomputed, "payable": released, "reversed": reversed_count}


# ── agents ───────────────────────────────────────────────────────────────────

def _clean_str(data, key, max_len, required=True, label=None):
    value = data.get(key)
    value = value.strip() if isinstance(value, str) else ""
    if required and not value:
        raise MarketingError(400, "VALIDATION_ERROR", f"Please enter your {label or key.replace('_', ' ')}.",
                             field=key)
    if len(value) > max_len:
        raise MarketingError(400, "VALIDATION_ERROR", f"{(label or key).capitalize()} is too long.", field=key)
    return value or None


def apply(user, data):
    data = data if isinstance(data, dict) else {}
    existing = AgentProfile.query.filter_by(user_id=user.id).first()
    if existing is not None and existing.status != "REJECTED":
        raise MarketingError(409, "ALREADY_APPLIED", "You have already applied.", status=existing.status)
    full_name = _clean_str(data, "full_name", 120, label="full name")
    phone_raw = _clean_str(data, "phone", 20, label="phone number")
    if not _norm_phone(phone_raw):
        raise MarketingError(400, "VALIDATION_ERROR", "Please enter a valid phone number.", field="phone")
    city = _clean_str(data, "city", 100)
    agent_type = (data.get("agent_type") or "").strip().lower() if isinstance(data.get("agent_type"), str) else ""
    if agent_type not in AgentProfile.TYPES:
        raise MarketingError(400, "VALIDATION_ERROR", "Pick the kind of agent you are.", field="agent_type")
    team = _clean_str(data, "team", 60, required=False)
    bank_name = _clean_str(data, "bank_name", 100, label="bank name")
    bank_code = _clean_str(data, "bank_code", 10, required=False, label="bank code")
    account_number = _digits(data.get("account_number"))
    if len(account_number) != 10:
        raise MarketingError(400, "VALIDATION_ERROR", "Your account number should be 10 digits.",
                             field="account_number")
    account_name = _clean_str(data, "account_name", 255, label="account name")
    if data.get("agree_conduct") is not True:
        raise MarketingError(400, "VALIDATION_ERROR", "Please agree to the agent code of conduct.",
                             field="agree_conduct")
    now = _now()
    profile = existing or AgentProfile(user_id=user.id, created_at=now)
    profile.status = "PENDING"
    profile.full_name, profile.phone, profile.city = full_name, phone_raw, city
    profile.agent_type, profile.team = agent_type, team
    profile.bank_name, profile.bank_code = bank_name, bank_code
    profile.account_number, profile.account_name = account_number, account_name
    profile.agreed_conduct_at = now
    profile.approved_at = None
    profile.approved_by_admin_id = None
    db.session.add(profile)
    db.session.commit()
    return profile


def _first_name_prefix(full_name):
    first = (full_name or "").split()[0] if (full_name or "").split() else ""
    letters = re.sub(r"[^A-Z]", "", first.upper())[:6]
    return letters if len(letters) >= 2 else "AG"


def approve_agent(profile, admin_id):
    code = profile.code if profile.code_id else None
    if code is None:
        code = create_code("agent", "/sell", label=f"Agent: {profile.full_name}"[:120],
                           owner_user_id=profile.user_id, prefix=_first_name_prefix(profile.full_name))
        db.session.flush()
        profile.code_id = code.id
    code.active = True
    profile.status = "APPROVED"
    profile.approved_at = _now()
    profile.approved_by_admin_id = admin_id
    return code


def notify(user_id, title, message):
    from app.models.communication import Notification
    db.session.add(Notification(user_id=user_id, title=title[:255], message=message, type="AGENT"))


def stats_for_agent(profile, since=None):
    agent_id = profile.user_id
    scans_q = MarketingScan.query.filter(MarketingScan.code_id == profile.code_id) if profile.code_id else None
    acq_q = SellerAcquisition.query.filter(SellerAcquisition.agent_user_id == agent_id)

    def count(col):
        q = acq_q.filter(col.isnot(None))
        return q.filter(col >= since).count() if since is not None else q.count()

    scans = 0
    if scans_q is not None:
        scans = (scans_q.filter(MarketingScan.created_at >= since) if since is not None else scans_q).count()
    sales_q = live_sale_earnings().filter(AgentEarning.agent_user_id == agent_id)
    if since is not None:
        sales_q = sales_q.filter(AgentEarning.earned_at >= since)
    return {"scans": scans, "signups": count(SellerAcquisition.signed_up_at),
            "setups": count(SellerAcquisition.setup_at), "first_sales": count(SellerAcquisition.first_paid_order_at),
            "rewarded_sales": sales_q.count()}


def live_sale_earnings():
    """Sale rewards that still stand (not reversed)."""
    return AgentEarning.query.filter(AgentEarning.kind == "sale", AgentEarning.status != "REVERSED")


def earning_totals(query):
    out = {"held": 0.0, "payable": 0.0, "paid": 0.0}
    rows = (query.with_entities(AgentEarning.status, func.coalesce(func.sum(AgentEarning.amount_ngn), 0))
            .group_by(AgentEarning.status).all())
    for status, amount in rows:
        key = (status or "").lower()
        if key in out:
            out[key] = _money(amount)
    return out


def agent_me(profile):
    now = _now()
    code = profile.code.code if profile.code_id and profile.code else None
    approved = profile.status == "APPROVED"
    return {
        "profile": {"status": profile.status, "agent_type": profile.agent_type, "team": profile.team,
                    "full_name": profile.full_name, "code": code,
                    "link": code_link(code) if approved and code else None},
        "rates": {"per_sale": sale_reward(), "rewarded_sales": rewarded_sales(),
                  "max_per_seller": max_per_seller(), "hold_days": hold_days(), "min_products": min_products()},
        "today": stats_for_agent(profile, period_start("day", now)),
        "week": stats_for_agent(profile, period_start("week", now)),
        "totals": stats_for_agent(profile),
        "earnings": earning_totals(AgentEarning.query.filter(AgentEarning.agent_user_id == profile.user_id)),
    }


def seller_status(acq, qualifying_count=None):
    if acq.first_paid_order_at is not None:
        if qualifying_count is None:
            qualifying_count = len(qualifying_orders(acq)[0])
        return "active" if qualifying_count >= 2 else "first_sale"
    if acq.setup_at is not None:
        return "set_up"
    return "signed_up"


def seller_row(acq):
    from app.services.onboarding_publish import store_url
    steps = setup_steps(acq.seller_user_id)
    sf = steps["storefront"]
    whatsapp = None
    if sf is not None:
        social = sf.social_links if isinstance(sf.social_links, dict) else {}
        whatsapp = _norm_phone(social.get("whatsapp")) or _norm_phone(sf.phone)
    if whatsapp is None and acq.seller is not None:
        whatsapp = _norm_phone(acq.seller.phone)
    rewarded, earned = 0, 0.0
    if acq.agent_user_id:
        mine = AgentEarning.query.filter(AgentEarning.seller_user_id == acq.seller_user_id,
                                         AgentEarning.agent_user_id == acq.agent_user_id,
                                         AgentEarning.status != "REVERSED")
        rewarded = mine.filter(AgentEarning.kind == "sale").count()
        earned = _money(mine.with_entities(func.coalesce(func.sum(AgentEarning.amount_ngn), 0)).scalar())
    return {
        "seller_id": acq.seller_user_id,
        "store_name": sf.store_name if sf else None,
        "store_slug": sf.store_slug if sf else None,
        "store_url": store_url(sf.store_slug) if sf and sf.store_slug else None,
        "joined_at": _iso(acq.signed_up_at),
        "steps": {
            "signed_up": True,
            "products": {"have": steps["products"], "need": min_products()},
            "place": steps["place"],
            "bank": steps["bank"],
            "pay_link": acq.pay_link_at is not None,
            "first_sale": acq.first_paid_order_at is not None,
        },
        "status": seller_status(acq),
        "rewarded_sales": rewarded,
        "rewarded_sales_max": rewarded_sales(),
        "earned": earned,
        "whatsapp": whatsapp,
        "flags": list(acq.flags or []),
    }


def earning_row(e, with_agent=False):
    from app.models.user import User
    sf = _store(e.seller_user_id) if e.seller_user_id else None
    row = {"id": e.id, "kind": e.kind, "sale_number": e.sale_number, "order_id": e.order_id,
           "amount": _money(e.amount_ngn), "status": e.status,
           "store_name": sf.store_name if sf else None, "seller_id": e.seller_user_id,
           "earned_at": _iso(e.earned_at), "payable_at": _iso(e.payable_at), "paid_at": _iso(e.paid_at),
           "reference": e.paid_reference, "reversed_reason": e.reversed_reason}
    if with_agent:
        profile = AgentProfile.query.filter_by(user_id=e.agent_user_id).first()
        user = db.session.get(User, e.agent_user_id)
        row["agent"] = {
            "user_id": e.agent_user_id,
            "name": profile.full_name if profile else (user.full_name if user else None),
            "bank_name": profile.bank_name if profile else None,
            "bank_code": profile.bank_code if profile else None,
            "account_number": profile.account_number if profile else None,
            "account_name": profile.account_name if profile else None,
        }
    return row


def leaderboard(viewer_user_id, period):
    since = period_start(period)
    profiles = AgentProfile.query.filter(AgentProfile.status.in_(("APPROVED", "PAUSED"))).all()
    rows = []
    teams = {}
    for p in profiles:
        s = stats_for_agent(p, since)
        if p.team:
            t = teams.setdefault(p.team, {"team": p.team, "setups": 0, "first_sales": 0})
            t["setups"] += s["setups"]
            t["first_sales"] += s["first_sales"]
        if s["setups"] or s["first_sales"]:
            rows.append({"name": short_name(p.full_name), "team": p.team, "setups": s["setups"],
                         "first_sales": s["first_sales"], "is_me": p.user_id == viewer_user_id,
                         "_created": p.created_at or since})
    rows.sort(key=lambda r: (-r["first_sales"], -r["setups"], _aware(r["_created"])))
    agents = []
    for i, r in enumerate(rows[:10], start=1):
        r.pop("_created")
        agents.append({"rank": i, **r})
    team_rows = sorted((t for t in teams.values() if t["setups"] or t["first_sales"]),
                       key=lambda t: (-t["first_sales"], -t["setups"], t["team"]))
    return {"period": period, "agents": agents, "teams": team_rows}


# ── seller growth kit ────────────────────────────────────────────────────────

def _seller_code(user, kind, destination, prefix, label):
    row = MarketingCode.query.filter_by(owner_user_id=user.id, kind=kind).order_by(MarketingCode.id).first()
    if row is None:
        row = create_code(kind, destination, label=label, owner_user_id=user.id, prefix=prefix)
        db.session.flush()
    elif kind == "seller_sticker" and row.destination != destination:
        row.destination = destination  # the store link changed
    return row


def growth_kit(user, storefront):
    from app.services.onboarding_publish import store_url
    slug = storefront.store_slug
    sticker = _seller_code(user, "seller_sticker", f"/id/{slug}", "S-", f"Sticker: {storefront.store_name}"[:120])
    invite = _seller_code(user, "seller_invite", "/sell", "J-", f"Invite: {storefront.store_name}"[:120])
    db.session.commit()

    invited = SellerAcquisition.query.filter_by(code_id=invite.id)
    since = _now() - timedelta(days=30)
    from app.models.order import Order
    by_source = (paid_orders_query(user.id).filter(Order.paid_at >= since)
                 .with_entities(func.coalesce(Order.source, "direct"), func.count(Order.id))
                 .group_by(func.coalesce(Order.source, "direct")).all())
    pro_active = bool(storefront.is_pro_verified and (not storefront.pro_verified_expires_at or
                                                      _aware(storefront.pro_verified_expires_at) > _now()))
    return {
        "store_url": store_url(slug) if slug else None,
        "passport_url": f"{frontend_url()}/id/{slug}" if slug else None,
        "sticker": {"code": sticker.code, "link": code_link(sticker.code)},
        "invite": {"code": invite.code, "link": code_link(invite.code)},
        "verified": bool(storefront.is_verified or storefront.verification_status == "VERIFIED" or pro_active),
        "results": {
            "sticker_scans": sticker.scan_count or 0,
            "invite_scans": invite.scan_count or 0,
            "invite_signups": invited.count(),
            "invite_first_sales": invited.filter(SellerAcquisition.first_paid_order_at.isnot(None)).count(),
            "orders_by_source": sorted(({"source": s, "orders": n} for s, n in by_source),
                                       key=lambda r: (-r["orders"], r["source"])),
        },
    }


# ── admin: campaigns, codes, board ───────────────────────────────────────────

def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:90]
    return s or "campaign"


def unique_campaign_slug(name):
    base = slugify(name)
    slug, n = base, 2
    while MarketingCampaign.query.filter_by(slug=slug).first() is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def campaign_dict(c):
    return {"id": c.id, "name": c.name, "slug": c.slug, "channel": c.channel, "location": c.location,
            "budget_ngn": _money(c.budget_ngn), "spend_ngn": _money(c.spend_ngn), "status": c.status,
            "notes": c.notes, "created_by_admin_id": c.created_by_admin_id, "created_at": _iso(c.created_at),
            "codes": MarketingCode.query.filter_by(campaign_id=c.id).count()}


def code_dict(c):
    owner = None
    if c.owner_user_id:
        profile = AgentProfile.query.filter_by(user_id=c.owner_user_id).first()
        user = c.owner
        owner = {"id": c.owner_user_id,
                 "name": profile.full_name if profile else (user.full_name if user else None)}
    return {"id": c.id, "code": c.code, "link": code_link(c.code), "kind": c.kind, "label": c.label,
            "destination": c.destination,
            "campaign": {"id": c.campaign.id, "name": c.campaign.name} if c.campaign_id and c.campaign else None,
            "owner": owner, "active": bool(c.active), "scan_count": c.scan_count or 0,
            "created_at": _iso(c.created_at)}


def parse_amount(value, field):
    if value is None or value == "":
        return Decimal(0)
    try:
        amount = Decimal(str(value))
    except Exception:
        raise MarketingError(400, "VALIDATION_ERROR", "Enter an amount in naira.", field=field)
    if amount < 0 or amount > Decimal("1000000000000"):
        raise MarketingError(400, "VALIDATION_ERROR", "Enter an amount in naira.", field=field)
    return amount.quantize(Decimal("0.01"))


def _parse_day(value, end=False):
    try:
        d = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    d = d.replace(tzinfo=LAGOS)
    if end:
        d += timedelta(days=1)
    return d.astimezone(timezone.utc)


def board(date_from=None, date_to=None):
    """The live results board. Dates are Lagos days, inclusive; default the
    last 30 days."""
    from app.models.order import Order
    now = _now()
    end = _parse_day(date_to, end=True) if date_to else None
    start = _parse_day(date_from) if date_from else None
    if end is None:
        end = period_start("day", now) + timedelta(days=1)
    if start is None:
        start = end - timedelta(days=30)
    if start >= end:
        raise MarketingError(400, "VALIDATION_ERROR", "'from' must be on or before 'to'.", field="from")

    def in_range(dt):
        dt = _aware(dt)
        return dt is not None and start <= dt < end

    campaigns = MarketingCampaign.query.order_by(MarketingCampaign.id).all()
    codes = MarketingCode.query.order_by(MarketingCode.id).all()
    code_by_id = {c.id: c for c in codes}
    code_by_text = {c.code: c for c in codes}
    campaign_by_slug = {c.slug: c for c in campaigns}

    scans = (MarketingScan.query.filter(MarketingScan.created_at >= start, MarketingScan.created_at < end)
             .with_entities(MarketingScan.code_id, MarketingScan.campaign_id).all())
    acqs = SellerAcquisition.query.all()
    orders = (paid_orders_query().filter(Order.paid_at >= start, Order.paid_at < end,
                                         db.or_(Order.ref_code.isnot(None), Order.campaign.isnot(None)))
              .with_entities(Order.id, Order.ref_code, Order.campaign, Order.total_amount,
                             Order.fx_rate_to_ngn, Order.currency).all())

    def blank():
        return {"scans": 0, "signups": 0, "setups": 0, "first_sales": 0, "orders": 0, "gmv_ngn": 0.0}

    by_code = {c.id: blank() for c in codes}
    by_campaign = {c.id: blank() for c in campaigns}
    totals = blank()
    # All-time first sales, to match spend (which is entered for a campaign's whole life).
    lifetime_first_sales = {c.id: 0 for c in campaigns}
    lifetime_first_sales_total = 0

    for code_id, campaign_id in scans:
        totals["scans"] += 1
        if code_id in by_code:
            by_code[code_id]["scans"] += 1
        if campaign_id in by_campaign:
            by_campaign[campaign_id]["scans"] += 1

    for a in acqs:
        if a.first_paid_order_at is not None and a.campaign_id in lifetime_first_sales:
            lifetime_first_sales[a.campaign_id] += 1
            lifetime_first_sales_total += 1
        for key, col in (("signups", a.signed_up_at), ("setups", a.setup_at),
                         ("first_sales", a.first_paid_order_at)):
            if in_range(col):
                totals[key] += 1
                if a.code_id in by_code:
                    by_code[a.code_id][key] += 1
                if a.campaign_id in by_campaign:
                    by_campaign[a.campaign_id][key] += 1

    for oid, ref, camp_slug, amount, fx, currency in orders:
        code = code_by_text.get((ref or "").upper()) if ref else None
        campaign = campaign_by_slug.get(camp_slug) if camp_slug else None
        if campaign is None and code is not None and code.campaign_id:
            campaign = next((c for c in campaigns if c.id == code.campaign_id), None)
        if code is None and campaign is None:
            continue
        rate = 1 if (currency or "NGN") == "NGN" else (fx or 0)
        gmv = _money(amount) * float(rate or 0)
        totals["orders"] += 1
        totals["gmv_ngn"] += gmv
        if code is not None:
            by_code[code.id]["orders"] += 1
            by_code[code.id]["gmv_ngn"] += gmv
        if campaign is not None:
            by_campaign[campaign.id]["orders"] += 1
            by_campaign[campaign.id]["gmv_ngn"] += gmv

    def cpa(spend, first_sales):
        return round(spend / first_sales, 2) if first_sales else None

    spend_total = sum(_money(c.spend_ngn) for c in campaigns)
    totals["spend_ngn"] = spend_total
    totals["cost_per_active_seller"] = cpa(spend_total, lifetime_first_sales_total)
    totals["spend_basis"] = "all_time"   # spend and its cost per active seller cover each campaign's whole life
    totals["gmv_ngn"] = round(totals["gmv_ngn"], 2)

    campaign_rows = []
    for c in campaigns:
        s = by_campaign[c.id]
        campaign_rows.append({"id": c.id, "name": c.name, "channel": c.channel, "location": c.location,
                              "budget_ngn": _money(c.budget_ngn), "spend_ngn": _money(c.spend_ngn),
                              **{**s, "gmv_ngn": round(s["gmv_ngn"], 2)},
                              "first_sales_all_time": lifetime_first_sales[c.id],
                              "cost_per_active_seller": cpa(_money(c.spend_ngn), lifetime_first_sales[c.id])})
    code_rows = []
    for c in codes:
        s = by_code[c.id]
        code_rows.append({"id": c.id, "code": c.code, "kind": c.kind, "label": c.label,
                          "campaign": c.campaign.name if c.campaign_id and c.campaign else None,
                          **{**s, "gmv_ngn": round(s["gmv_ngn"], 2)}})

    agent_rows = []
    for p in AgentProfile.query.filter(AgentProfile.status.in_(("APPROVED", "PAUSED"))).all():
        s = {"scans": 0, "signups": 0, "setups": 0, "first_sales": 0}
        if p.code_id in by_code:
            s["scans"] = by_code[p.code_id]["scans"]
        for a in acqs:
            if a.agent_user_id != p.user_id:
                continue
            s["signups"] += in_range(a.signed_up_at)
            s["setups"] += in_range(a.setup_at)
            s["first_sales"] += in_range(a.first_paid_order_at)
        earned = (db.session.query(func.coalesce(func.sum(AgentEarning.amount_ngn), 0))
                  .filter(AgentEarning.agent_user_id == p.user_id, AgentEarning.status != "REVERSED",
                          AgentEarning.earned_at >= start, AgentEarning.earned_at < end).scalar())
        s["rewarded_sales"] = live_sale_earnings().filter(
            AgentEarning.agent_user_id == p.user_id, AgentEarning.earned_at >= start,
            AgentEarning.earned_at < end).count()
        agent_rows.append({"agent_user_id": p.user_id, "name": p.full_name, "team": p.team,
                           "agent_type": p.agent_type, **s, "earned_ngn": _money(earned)})

    last_day = (end - timedelta(seconds=1)).astimezone(LAGOS).date().isoformat()
    return {"range": {"from": start.astimezone(LAGOS).date().isoformat(), "to": last_day},
            "totals": totals, "campaigns": campaign_rows, "codes": code_rows, "agents": agent_rows}
