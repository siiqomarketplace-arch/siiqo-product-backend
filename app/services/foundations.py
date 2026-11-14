"""
Phase 0B data foundations, recorded automatically.

Instead of adding calls to every payment path, SQLAlchemy flush hooks watch
orders, escrows, reviews, stores, products, Pay Links, bank accounts and
users, wherever in the code they change, and write:

- order_status_history, plus paid_at / delivered_at / released_at on orders
- the fee ledger (CHARGED when paid, REVERSED on refund)
- trust_evidence (paid, released, refunded, disputes, reviews, repeat buyers)
- funnel steps in platform_events as "funnel.<step>" (signup → store → listing → Pay Link → paid → delivered →
  released → reviewed → repeat → disputed)
- seller_identifiers (phones, emails, store slug, domain, handles, Pay Link
  slugs, hashed payout accounts)

Rows are inserted on the same connection and transaction as the change that
caused them, inside a savepoint: a recording problem is logged and never
breaks the payment or order it describes.
"""
import hashlib
import hmac
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_REGISTERED = False


def _now():
    return datetime.now(timezone.utc)


def _status_change(obj, attr="status", is_new=None):
    """(old, new) when `attr` changed in this flush, else None."""
    state = inspect(obj)
    if state.pending if is_new is None else is_new:
        return (None, getattr(obj, attr))
    hist = state.attrs[attr].history
    if not hist.has_changes() or not hist.added:
        return None
    old = hist.deleted[0] if hist.deleted else None
    new = hist.added[0]
    return None if old == new else (old, new)


def _changed(obj, attr, is_new=None):
    state = inspect(obj)
    return (state.pending if is_new is None else is_new) or state.attrs[attr].history.has_changes()


# ── normalisers for the identifier graph ──────────────────────────────────

def _norm_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("0"):
        digits = "234" + digits[1:]
    return digits if len(digits) >= 10 else None


def _norm_email(value):
    value = (value or "").strip().lower()
    return value if "@" in value and not value.endswith("@siiqo.app") else None


def _norm_domain(value):
    value = (value or "").strip().lower()
    if not value:
        return None
    host = urlparse(value if "://" in value else f"https://{value}").hostname or ""
    host = host[4:] if host.startswith("www.") else host
    return host or None


def _norm_handle(value):
    value = (value or "").strip().lower()
    if not value:
        return None
    if "://" in value or value.startswith("www."):
        path = urlparse(value if "://" in value else f"https://{value}").path.strip("/")
        value = path.split("/")[0] if path else ""
    return value.lstrip("@") or None


def payout_account_hash(bank_code, account_number):
    secret = (os.environ.get("SECRET_KEY") or "siiqo-secret-key-salt-2026").encode()
    digits = re.sub(r"\D", "", account_number or "")
    raw = f"{(bank_code or '').strip()}:{digits}"
    return hmac.new(secret, f"payout-account-{raw}".encode(), hashlib.sha256).hexdigest()


# Escrow states that mean the buyer's money has been received.
_ESCROW_PAID_STATUSES = ("IN_ESCROW", "SHIPPED", "DELIVERED", "RELEASED")


# Order status -> buying funnel step (BUYING_FIX_PLAN A1).
_ORDER_STEPS = {"SHIPPED": "shipped", "DELIVERED": "shipped", "COMPLETED": "confirmed", "DISPUTED": "disputed"}


def _default_order_source(order):
    """orders.source for an order no route attributed: a Pay Link, the bot
    (X-Siiqo-Client header), else 'direct'."""
    if order.payment_link_id:
        return "pay_link"
    try:
        from flask import has_request_context, request
        if has_request_context():
            from app.services.attribution import client_header_source
            bot = client_header_source(request.headers)
            if bot:
                return bot
    except Exception:
        pass
    return "direct"


def _order_kind(order):
    if order.payment_link_id:
        return "pay_link"
    return getattr(order, "_funnel_kind", None) or "order"


def _order_props(order, kind_only=False):
    props = {"source": order.source, "campaign": order.campaign, "kind": _order_kind(order)}
    if not kind_only:
        props.update({"payment_method": order.payment_method,
                      "amount": float(order.total_amount or 0) + float(order.logistics_fee or 0),
                      "currency": order.currency or "NGN"})
    return {k: v for k, v in props.items() if v is not None}


def _first_paid(order, is_new):
    """True in the flush where the order's paid_at is first set."""
    if order.paid_at is None:
        return False
    if is_new:
        return True
    hist = inspect(order).attrs.paid_at.history
    return bool(hist.added) and hist.added[0] is not None and not any(d is not None for d in hist.deleted)


# ── hooks ──────────────────────────────────────────────────────────────────

def _before_flush(session, flush_context, instances):
    from app.models.escrow import EscrowStatus, EscrowTransaction
    from app.models.order import Order
    try:
        now = _now()
        for obj in list(session.new) + list(session.dirty):
            if isinstance(obj, Order):
                if obj in session.new and not getattr(obj, "source", None):
                    obj.source = _default_order_source(obj)
                if obj.currency is None:
                    obj.currency = "NGN"
                if obj.currency == "NGN" and obj.fx_rate_to_ngn is None:
                    obj.fx_rate_to_ngn = 1
                change = _status_change(obj)
                if change:
                    new = (change[1] or "").upper()
                    if new in ("PAID", "SHIPPED", "DELIVERED", "COMPLETED") and obj.paid_at is None:
                        obj.paid_at = now
                    if new in ("DELIVERED", "COMPLETED") and obj.delivered_at is None:
                        obj.delivered_at = now
                    if new == "COMPLETED" and obj.released_at is None:
                        obj.released_at = now
            elif isinstance(obj, EscrowTransaction) and (obj.order is not None or obj.order_id):
                order = obj.order
                if order is None:
                    # New escrows are often created with only order_id set.
                    with session.no_autoflush:
                        order = session.get(Order, obj.order_id)
                    if order is None:
                        continue
                if obj.currency and obj.currency != order.currency and _changed(obj, "currency"):
                    order.currency = obj.currency
                    order.fx_rate_to_ngn = 1 if obj.currency == "NGN" else None
                change = _status_change(obj)
                # Safety net for every payment path: the first time an escrow
                # reaches a paid state, stamp paid_at on it and on its order.
                # Earlier values are never overwritten.
                if change and change[1] in _ESCROW_PAID_STATUSES:
                    if obj.paid_at is None:
                        obj.paid_at = now
                    if order.paid_at is None:
                        order.paid_at = obj.paid_at
                if change and change[1] == EscrowStatus.RELEASED and order.released_at is None:
                    order.released_at = now
    except Exception as e:  # never block the write that triggered this
        logger.warning(f"[FOUNDATIONS] before_flush skipped: {e}")


def _after_flush(session, flush_context):
    rows = _collect(session)
    if not any(rows.values()):
        return
    conn = session.connection()
    try:
        with conn.begin_nested():
            _write(conn, rows)
    except Exception as e:
        logger.warning(f"[FOUNDATIONS] recording skipped: {e}")


def _collect(session):
    from app.models.community import Review
    from app.models.escrow import EscrowStatus, EscrowTransaction
    from app.models.order import Order
    from app.models.payment_link import PaymentLink
    from app.models.product import Product
    from app.models.user import Storefront, User
    from app.models.withdrawal import VendorBankAccount

    rows = {"history": [], "fees": [], "evidence": [], "events": [], "identifiers": [], "product_events": []}
    now = _now()
    # In after_flush the session still lists pre-flush state: session.new holds
    # the objects that were just inserted.
    new_objs = set(session.new)

    def ev(name, **kw):
        rows["events"].append({"name": name, "created_at": now, **kw})

    def evidence(vendor_id, kind, **kw):
        rows["evidence"].append({"vendor_id": vendor_id, "kind": kind, "created_at": now, **kw})

    def ident(vendor_id, kind, value, source):
        if vendor_id and value:
            rows["identifiers"].append({"vendor_id": vendor_id, "kind": kind, "value": str(value)[:255],
                                        "source": source})

    for obj in list(session.new) + list(session.dirty):
        try:
            if isinstance(obj, Order):
                is_new = obj in new_objs
                change = _status_change(obj, is_new=is_new)
                buying = (obj.payment_method or "").upper() != "FREE"
                if is_new and buying:
                    ev("order_created", vendor_id=obj.vendor_id, user_id=obj.buyer_id, order_id=obj.id,
                       payment_link_id=obj.payment_link_id, session_id=getattr(obj, "_funnel_session_id", None),
                       props=_order_props(obj))
                if buying and _first_paid(obj, is_new):
                    ev("paid", vendor_id=obj.vendor_id, user_id=obj.buyer_id, order_id=obj.id,
                       payment_link_id=obj.payment_link_id, once=True,
                       props={**_order_props(obj), "_repeat_check": True, "buyer_email": obj.buyer_email})
                if change:
                    rows["history"].append({"order_id": obj.id, "from_status": change[0],
                                            "to_status": change[1], "created_at": now})
                    new_status = (change[1] or "").upper()
                    if new_status == "DELIVERED":
                        ev("delivered", vendor_id=obj.vendor_id, user_id=obj.buyer_id, order_id=obj.id)
                    step = _ORDER_STEPS.get(new_status)
                    if step and buying and not is_new:
                        ev(step, vendor_id=obj.vendor_id, user_id=obj.buyer_id, order_id=obj.id,
                           once=True, props=_order_props(obj, kind_only=True))

            elif isinstance(obj, EscrowTransaction):
                change = _status_change(obj, is_new=obj in new_objs)
                order = obj.order
                if not change or order is None:
                    continue
                new = change[1]
                currency = obj.currency or "NGN"
                if new == EscrowStatus.IN_ESCROW and change[0] != EscrowStatus.IN_ESCROW:
                    rows["fees"].append({"order_id": order.id, "escrow_id": obj.id, "vendor_id": order.vendor_id,
                                         "event": "CHARGED", "gross_amount": obj.amount or 0,
                                         "fee_amount": obj.fee_amount or 0, "fee_percent": obj.fee_percent,
                                         "currency": currency, "created_at": now})
                    evidence(order.vendor_id, "ORDER_PAID", order_id=order.id, value=obj.amount,
                             currency=currency, source="escrow")
                    # funnel.paid is recorded once per order, when the order's
                    # paid_at is first set (stamped from this escrow above).
                elif new == EscrowStatus.RELEASED:
                    evidence(order.vendor_id, "ORDER_RELEASED", order_id=order.id, value=obj.amount,
                             currency=currency, source="escrow")
                    ev("released", vendor_id=order.vendor_id, user_id=order.buyer_id, order_id=order.id)
                    ev("confirmed", vendor_id=order.vendor_id, user_id=order.buyer_id, order_id=order.id,
                       once=True, props=_order_props(order, kind_only=True))
                elif new in (EscrowStatus.SHIPPED, EscrowStatus.DELIVERED):
                    ev("shipped", vendor_id=order.vendor_id, user_id=order.buyer_id, order_id=order.id,
                       once=True, props=_order_props(order, kind_only=True))
                elif new == EscrowStatus.REFUNDED:
                    rows["fees"].append({"order_id": order.id, "escrow_id": obj.id, "vendor_id": order.vendor_id,
                                         "event": "REVERSED", "gross_amount": obj.amount or 0,
                                         "fee_amount": obj.fee_amount or 0, "fee_percent": obj.fee_percent,
                                         "currency": currency, "created_at": now})
                    evidence(order.vendor_id, "ORDER_REFUNDED", order_id=order.id, value=obj.amount,
                             currency=currency, source="escrow")
                    # A dispute case marks a refund where the seller was not at
                    # fault (app/services/disputes.py); that is not a lost dispute.
                    if obj.dispute_id and getattr(obj, "_dispute_seller_fault", True) is not False:
                        evidence(order.vendor_id, "DISPUTE_LOST", order_id=order.id, source="escrow")
                elif new == EscrowStatus.DISPUTED:
                    evidence(order.vendor_id, "DISPUTE_OPENED", order_id=order.id, source="escrow")
                    ev("disputed", vendor_id=order.vendor_id, user_id=order.buyer_id, order_id=order.id,
                       once=True, props=_order_props(order, kind_only=True))

            elif isinstance(obj, Review) and obj in new_objs:
                evidence(obj.vendor_id, "REVIEW", order_id=obj.order_id, value=obj.vendor_rating, source="review")
                ev("reviewed", vendor_id=obj.vendor_id, user_id=obj.buyer_id, order_id=obj.order_id)

            elif isinstance(obj, User):
                if obj in new_objs:
                    ev("signup", user_id=obj.id, props={"role": str(obj.role or "")})
                if obj.storefront is not None:
                    if _changed(obj, "email", obj in new_objs):
                        ident(obj.id, "email", _norm_email(obj.email), "account")
                    if _changed(obj, "phone", obj in new_objs):
                        ident(obj.id, "phone", _norm_phone(obj.phone), "account")

            elif isinstance(obj, Storefront):
                vid = obj.vendor_id
                if obj in new_objs:
                    ev("store", user_id=vid, vendor_id=vid)
                    owner = obj.vendor
                    if owner is not None:
                        ident(vid, "email", _norm_email(owner.email), "account")
                        ident(vid, "phone", _norm_phone(owner.phone), "account")
                if _changed(obj, "store_slug", obj in new_objs):
                    ident(vid, "store_slug", (obj.store_slug or "").lower(), "store")
                if _changed(obj, "phone", obj in new_objs):
                    ident(vid, "phone", _norm_phone(obj.phone), "store")
                if _changed(obj, "website", obj in new_objs):
                    ident(vid, "domain", _norm_domain(obj.website), "store")
                if _changed(obj, "social_links", obj in new_objs):
                    for network, handle in (obj.social_links or {}).items():
                        value = _norm_phone(handle) if network == "whatsapp" else _norm_handle(handle)
                        ident(vid, "phone" if network == "whatsapp" else "handle",
                              value if network == "whatsapp" else (f"{network}:{value}" if value else None),
                              "store")

            elif isinstance(obj, Product) and obj in new_objs:
                rows["product_events"].append({"storefront_id": obj.storefront_id, "product_id": obj.id})

            elif isinstance(obj, PaymentLink) and obj in new_objs:
                ev("paylink_created", user_id=obj.vendor_id, vendor_id=obj.vendor_id, payment_link_id=obj.id)
                ident(obj.vendor_id, "paylink_slug", (obj.slug or "").lower(), "paylink")

            elif isinstance(obj, VendorBankAccount) and (_changed(obj, "account_number", obj in new_objs) or _changed(obj, "bank_code", obj in new_objs)):
                ident(obj.vendor_id, "payout_account_hash",
                      payout_account_hash(obj.bank_code, obj.account_number), "payout")
        except Exception as e:
            logger.warning(f"[FOUNDATIONS] skipped {type(obj).__name__}: {e}")
    return rows


def _write(conn, rows):
    from app.models.foundations import FeeLedgerEntry, OrderStatusHistory, SellerIdentifier
    from app.models.telemetry import PlatformEvent, TrustEvidence
    from app.models.order import Order
    from app.models.user import Storefront

    now = _now()
    if rows["history"]:
        conn.execute(OrderStatusHistory.__table__.insert(), rows["history"])

    fee_t = FeeLedgerEntry.__table__
    for fee in rows["fees"]:
        exists = conn.execute(select(fee_t.c.id).where(fee_t.c.escrow_id == fee["escrow_id"],
                                                       fee_t.c.event == fee["event"])).first()
        if not exists:
            conn.execute(fee_t.insert(), fee)

    evidence = [_evidence_row(e) for e in rows["evidence"]]

    for pe in rows["product_events"]:
        vid = conn.execute(select(Storefront.__table__.c.vendor_id)
                           .where(Storefront.__table__.c.id == pe["storefront_id"])).scalar()
        rows["events"].append({"name": "listing", "user_id": vid, "vendor_id": vid,
                               "props": {"product_id": pe["product_id"]}, "created_at": now})

    events = []
    order_t = Order.__table__
    event_t = PlatformEvent.__table__
    once_seen = set()
    for e in rows["events"]:
        if e.get("once") and e.get("order_id"):
            # One funnel.<step> per order, whichever path gets there (webhook
            # retries, SHIPPED then DELIVERED, escrow and order both changing).
            key = (e["name"], e["order_id"])
            if key in once_seen or conn.execute(select(event_t.c.id).where(
                    event_t.c.event_name == f"funnel.{e['name']}",
                    event_t.c.order_id == e["order_id"])).first():
                continue
            once_seen.add(key)
        props = dict(e.get("props") or {})
        if props.pop("_repeat_check", False):
            # A repeat purchase: this buyer already paid this seller before.
            buyer_match = (order_t.c.buyer_id == e["user_id"]) if e.get("user_id") else \
                (order_t.c.buyer_email == props.get("buyer_email"))
            prior = conn.execute(select(order_t.c.id).where(
                order_t.c.vendor_id == e["vendor_id"], order_t.c.id != e["order_id"], buyer_match,
                order_t.c.paid_at.isnot(None))).first()
            props.pop("buyer_email", None)
            if prior:
                events.append({**e, "name": "repeat", "props": None})
                evidence.append(_evidence_row({"vendor_id": e["vendor_id"], "kind": "REPEAT_PURCHASE",
                                               "order_id": e["order_id"], "source": "orders",
                                               "created_at": now}))
        events.append({**e, "props": props or None})
    if evidence:
        conn.execute(TrustEvidence.__table__.insert(), evidence)
    if events:
        conn.execute(PlatformEvent.__table__.insert(), [_event_row(e) for e in events])

    ident_t = SellerIdentifier.__table__
    seen = set()
    for i in rows["identifiers"]:
        key = (i["vendor_id"], i["kind"], i["value"])
        if key in seen:
            continue
        seen.add(key)
        existing = conn.execute(select(ident_t.c.id).where(ident_t.c.vendor_id == i["vendor_id"],
                                                           ident_t.c.kind == i["kind"],
                                                           ident_t.c.value == i["value"])).first()
        if existing:
            conn.execute(ident_t.update().where(ident_t.c.id == existing.id).values(last_seen=now))
        else:
            conn.execute(ident_t.insert(), {**i, "first_seen": now, "last_seen": now})


_EVIDENCE_TYPES = {
    "ORDER_PAID": "order_paid",
    "ORDER_RELEASED": "escrow_released",
    "ORDER_REFUNDED": "order_refunded",
    "DISPUTE_OPENED": "dispute_opened",
    "DISPUTE_LOST": "dispute_lost",
    "REVIEW": "review_received",
    "REPEAT_PURCHASE": "repeat_purchase",
}


def _evidence_row(e):
    props = {}
    if e.get("value") is not None:
        props["value"] = float(e["value"])
    if e.get("currency"):
        props["currency"] = e["currency"]
    return {
        "business_id": e["vendor_id"],
        "evidence_type": _EVIDENCE_TYPES.get(e["kind"], e["kind"].lower()),
        "provenance": "siiqo_transaction_verified",
        "source": e.get("source") or "siiqo",
        "source_id": str(e["order_id"]) if e.get("order_id") else None,
        "status": "ACTIVE",
        "confidence": 1,
        "properties": props,
        "created_at": e["created_at"],
        "verified_at": e["created_at"],
    }


def _event_row(e):
    props = dict(e.get("props") or {})
    if e.get("payment_link_id"):
        props["payment_link_id"] = e["payment_link_id"]
    return {
        "event_name": f"funnel.{e['name']}",
        "timestamp": e["created_at"],
        "session_id": str(e["session_id"])[:100] if e.get("session_id") else None,
        "user_id": e.get("user_id"),
        "business_id": e.get("vendor_id"),
        "order_id": e.get("order_id"),
        "source": "server",
        "properties": props,
    }


def record_event(name, **fields):
    """Record a funnel event that has no database change behind it (e.g. a Pay
    Link being viewed). Never raises."""
    from app.extensions import db
    from app.models.telemetry import PlatformEvent
    try:
        with db.engine.begin() as conn:
            conn.execute(PlatformEvent.__table__.insert(),
                         _event_row({"name": name, "created_at": _now(), **fields}))
    except Exception as e:
        logger.warning(f"[FOUNDATIONS] event {name} not recorded: {e}")


def backfill_identifiers(batch=500):
    """Build the identifier graph for existing sellers. Idempotent. Run once
    after deploy (e.g. `flask shell` → backfill_identifiers()); never runs on its own."""
    from app.extensions import db
    from app.models.payment_link import PaymentLink
    from app.models.user import Storefront
    from app.models.withdrawal import VendorBankAccount

    rows = []
    for sf in Storefront.query.yield_per(batch):
        vid = sf.vendor_id
        owner = sf.vendor
        candidates = [
            ("email", _norm_email(owner.email) if owner else None, "account"),
            ("phone", _norm_phone(owner.phone) if owner else None, "account"),
            ("store_slug", (sf.store_slug or "").lower(), "store"),
            ("phone", _norm_phone(sf.phone), "store"),
            ("domain", _norm_domain(sf.website), "store"),
        ]
        for network, handle in (sf.social_links or {}).items():
            if network == "whatsapp":
                candidates.append(("phone", _norm_phone(handle), "store"))
            else:
                value = _norm_handle(handle)
                candidates.append(("handle", f"{network}:{value}" if value else None, "store"))
        rows += [{"vendor_id": vid, "kind": k, "value": v, "source": s} for k, v, s in candidates if v]
    for link in PaymentLink.query.yield_per(batch):
        if link.slug:
            rows.append({"vendor_id": link.vendor_id, "kind": "paylink_slug", "value": link.slug.lower(),
                         "source": "paylink"})
    for acc in VendorBankAccount.query.yield_per(batch):
        if acc.account_number:
            rows.append({"vendor_id": acc.vendor_id, "kind": "payout_account_hash",
                         "value": payout_account_hash(acc.bank_code, acc.account_number), "source": "payout"})
    with db.engine.begin() as conn:
        _write(conn, {"history": [], "fees": [], "evidence": [], "events": [], "identifiers": rows,
                      "product_events": []})
    return len(rows)


def register():
    """Attach the hooks once (called from create_app)."""
    global _REGISTERED
    if _REGISTERED:
        return
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "after_flush", _after_flush)
    # Marketing milestones (store set up, first Pay Link, first paid order,
    # including funnel.paid) are recomputed in the same transaction.
    from app.services.marketing import register_hooks as _register_marketing_hooks
    _register_marketing_hooks()
    _REGISTERED = True
