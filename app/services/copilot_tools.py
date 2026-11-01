"""
READ tools for the seller copilot (Phase 4, item 8b).

Rules every tool follows:
  * READ only. No tool writes, flushes or commits anything. There is no write
    tool: actions are suggested as buttons and run by existing endpoints when
    the seller taps them (app/services/copilot.py).
  * Scoped to the signed-in seller. Every query filters on ``vendor.id`` (or
    the seller's own storefront id) taken from the session, never from the
    model's arguments. An id the model passes that belongs to another seller
    is answered exactly like an id that does not exist.
  * No buyer personal data: no buyer name, email, phone or delivery address;
    no bank account numbers. Only business facts.

``TOOLS`` holds the definitions sent to the model; ``run_tool`` dispatches.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db

MAX_ROWS = 25
ORDER_STATUSES = ("PENDING", "PAID", "PROCESSING", "PENDING_DELIVERY", "SHIPPED", "DELIVERED",
                  "COMPLETED", "CANCELLED")
PAID_STATUSES = ("PAID", "PROCESSING", "PENDING_DELIVERY", "SHIPPED", "DELIVERED", "COMPLETED")
TO_SHIP_STATUSES = ("PAID", "PROCESSING", "PENDING_DELIVERY")


class ToolError(Exception):
    """Bad arguments: sent back to the model as an is_error tool_result."""


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [],
            "additionalProperties": False}


TOOLS: list[dict] = [
    {
        "name": "list_orders",
        "description": ("List this seller's orders, newest first, with counts by status. Filter by status "
                        "and by created date. Returns id, status, total, currency, dates, payment method, "
                        "items and whether the buyer has a Siiqo account. No buyer contact details."),
        "input_schema": _obj({
            "status": {"type": "string", "enum": list(ORDER_STATUSES) + ["TO_SHIP", "UNPAID", "ANY"],
                       "description": "TO_SHIP = paid and not yet sent; UNPAID = PENDING."},
            "created_from": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            "created_to": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            "limit": {"type": "integer", "description": "1-25, default 10"},
        }),
    },
    {
        "name": "get_order",
        "description": "One of this seller's orders by id, with items, escrow status and any dispute.",
        "input_schema": _obj({"order_id": {"type": "integer"}}, ["order_id"]),
    },
    {
        "name": "payout_status",
        "description": ("Money owed to this seller: amounts held in escrow by stage, released this month, "
                        "recent withdrawals and whether a payout bank account is set. Read only."),
        "input_schema": _obj({}),
    },
    {
        "name": "list_products",
        "description": ("This seller's products with price, stock, type, active and negotiable flags. "
                        "Optional name search and low-stock filter."),
        "input_schema": _obj({
            "search": {"type": "string"},
            "low_stock_below": {"type": "integer", "description": "only physical products with stock below this"},
            "include_inactive": {"type": "boolean"},
            "limit": {"type": "integer", "description": "1-25, default 15"},
        }),
    },
    {
        "name": "analytics_summary",
        "description": ("Store numbers for the last N days (default 30): orders, paid orders, paid revenue, "
                        "store views, most viewed products, and trust checks on the seller."),
        "input_schema": _obj({"days": {"type": "integer", "description": "1-365, default 30"}}),
    },
    {
        "name": "trust_drivers",
        "description": ("This seller's trust score, tier, the parts that make it up and the reasons "
                        "behind it (evidence-based)."),
        "input_schema": _obj({}),
    },
    {
        "name": "list_disputes",
        "description": "Disputes on this seller's orders with status, reason, deadline and outcome.",
        "input_schema": _obj({"active_only": {"type": "boolean"}}),
    },
    {
        "name": "list_pay_links",
        "description": ("This seller's Pay Links with amount, status and paid orders, plus store-chat "
                        "price offers waiting for the seller's approval."),
        "input_schema": _obj({"status": {"type": "string", "enum": ["ACTIVE", "PAID", "EXPIRED", "ANY"]}}),
    },
]

TOOL_NAMES = frozenset(t["name"] for t in TOOLS)


# ── helpers ────────────────────────────────────────────────────────────────
def _money(v) -> str:
    return f"{Decimal(str(v or 0)):.2f}"


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _int(args: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = args.get(name, default)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ToolError(f"{name} must be an integer")
    return max(lo, min(hi, raw))


def _date(args: dict, name: str) -> date | None:
    raw = args.get(name)
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError as exc:
        raise ToolError(f"{name} must be YYYY-MM-DD") from exc


def _now():
    return datetime.now(timezone.utc)


def _order_row(o) -> dict:
    return {
        "order_id": o.id,
        "status": o.status,
        "total": _money(o.total_amount),
        "currency": o.currency or "NGN",
        "payment_method": o.payment_method,
        "created_at": _iso(o.created_at),
        "paid_at": _iso(o.paid_at),
        "delivered_at": _iso(o.delivered_at),
        "has_buyer_account": o.buyer_id is not None,
        "from_pay_link": o.payment_link_id is not None,
        "items": [{"product_id": i.product_id, "name": i.product.name if i.product else None,
                   "quantity": i.quantity, "unit_price": _money(i.price_at_purchase)}
                  for i in (o.items or [])][:10],
    }


# ── tools ──────────────────────────────────────────────────────────────────
def list_orders(vendor, args: dict) -> dict:
    from app.models.order import Order

    status = (args.get("status") or "ANY").upper()
    if status not in ORDER_STATUSES + ("TO_SHIP", "UNPAID", "ANY"):
        raise ToolError("unknown status")
    limit = _int(args, "limit", 10, 1, MAX_ROWS)
    start, end = _date(args, "created_from"), _date(args, "created_to")

    base = Order.query.filter(Order.vendor_id == vendor.id)
    if start:
        base = base.filter(Order.created_at >= datetime(start.year, start.month, start.day))
    if end:
        nxt = end + timedelta(days=1)
        base = base.filter(Order.created_at < datetime(nxt.year, nxt.month, nxt.day))

    counts = dict(base.with_entities(Order.status, func.count(Order.id)).group_by(Order.status).all())
    q = base
    if status == "TO_SHIP":
        q = q.filter(Order.status.in_(TO_SHIP_STATUSES))
    elif status == "UNPAID":
        q = q.filter(Order.status == "PENDING")
    elif status != "ANY":
        q = q.filter(Order.status == status)
    total_matching = q.count()
    rows = q.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit).all()
    return {
        "filters": {"status": status, "created_from": start and start.isoformat(),
                    "created_to": end and end.isoformat()},
        "counts_by_status": {k: int(v) for k, v in counts.items()},
        "total_matching": total_matching,
        "orders": [_order_row(o) for o in rows],
    }


def get_order(vendor, args: dict) -> dict:
    from app.models.dispute import DisputeCase
    from app.models.order import Order

    oid = args.get("order_id")
    if isinstance(oid, bool) or not isinstance(oid, int):
        raise ToolError("order_id must be an integer")
    order = Order.query.filter(Order.id == oid, Order.vendor_id == vendor.id).first()
    if order is None:
        return {"found": False, "order_id": oid}
    row = _order_row(order)
    escrow = order.escrow
    row["escrow_status"] = escrow.status if escrow else None
    row["tracking_number_set"] = bool(order.tracking_number)
    dispute = (DisputeCase.query.filter_by(order_id=order.id, vendor_id=vendor.id)
               .order_by(DisputeCase.id.desc()).first())
    row["dispute"] = ({"reference": dispute.reference, "status": dispute.status} if dispute else None)
    return {"found": True, "order": row}


def payout_status(vendor, args: dict) -> dict:
    from app.models.escrow import EscrowStatus, EscrowTransaction
    from app.models.order import Order
    from app.models.withdrawal import Withdrawal
    from app.services.seller_home import has_payout_account, released_this_month

    held = dict(
        db.session.query(EscrowTransaction.status, func.coalesce(func.sum(EscrowTransaction.amount), 0))
        .join(Order, EscrowTransaction.order_id == Order.id)
        .filter(Order.vendor_id == vendor.id,
                EscrowTransaction.status.in_((EscrowStatus.IN_ESCROW, EscrowStatus.SHIPPED,
                                              EscrowStatus.DELIVERED, EscrowStatus.DISPUTED)))
        .group_by(EscrowTransaction.status).all()
    )
    withdrawals = (Withdrawal.query.filter_by(vendor_id=vendor.id)
                   .order_by(Withdrawal.requested_at.desc()).limit(5).all())
    return {
        "has_payout_account": has_payout_account(vendor),
        "held_in_escrow_by_stage": {k: _money(v) for k, v in held.items()},
        "released_this_month_net": _money(released_this_month(vendor.id)),
        "recent_withdrawals": [{
            "reference": w.withdrawal_number, "amount": _money(w.amount),
            "net_amount": _money(w.net_amount), "currency": w.currency or "NGN",
            "status": w.status, "requested_at": _iso(w.requested_at),
            "completed_at": _iso(w.completed_at),
        } for w in withdrawals],
        "currency_note": "Escrow amounts are gross, before Siiqo's fee.",
    }


def list_products(vendor, args: dict) -> dict:
    from app.models.product import Product

    sf = vendor.storefront
    if sf is None:
        return {"products": [], "total": 0, "note": "no store yet"}
    limit = _int(args, "limit", 15, 1, MAX_ROWS)
    q = Product.query.filter(Product.storefront_id == sf.id,
                             db.or_(Product.is_deleted.is_(False), Product.is_deleted.is_(None)))
    if not args.get("include_inactive"):
        q = q.filter(Product.is_active.is_(True))
    search = (args.get("search") or "").strip()[:60]
    if search:
        q = q.filter(Product.name.ilike(f"%{search}%"))
    if args.get("low_stock_below") is not None:
        below = _int(args, "low_stock_below", 5, 1, 100000)
        q = q.filter(Product.product_type == "physical", Product.stock_quantity < below)
    total = q.count()
    rows = q.order_by(Product.created_at.desc(), Product.id.desc()).limit(limit).all()
    return {"total": total, "products": [{
        "product_id": p.id, "name": p.name, "price": _money(p.price), "currency": p.currency or "NGN",
        "stock": int(p.stock_quantity or 0) if (p.product_type or "physical") == "physical" else None,
        "product_type": p.product_type or "physical", "active": bool(p.is_active),
        "negotiable": bool(p.is_negotiable),
        "floor_price": _money(p.floor_price) if p.floor_price is not None else None,
        "views": int(p.view_count or 0),
    } for p in rows]}


def analytics_summary(vendor, args: dict) -> dict:
    from app.models.order import Order
    from app.models.product import Product
    from app.services import who_checked

    days = _int(args, "days", 30, 1, 365)
    since = (_now() - timedelta(days=days)).replace(tzinfo=None)
    q = Order.query.filter(Order.vendor_id == vendor.id, Order.created_at >= since)
    orders = q.count()
    paid = q.filter(Order.status.in_(PAID_STATUSES))
    revenue = paid.with_entities(func.coalesce(func.sum(Order.total_amount), 0)).scalar()
    sf = vendor.storefront
    top = []
    if sf is not None:
        top = (Product.query.filter(Product.storefront_id == sf.id, Product.is_active.is_(True))
               .order_by(Product.view_count.desc(), Product.id.asc()).limit(5).all())
    checks = {}
    try:
        figures = who_checked.for_vendor(vendor.id)
        checks = {"trust_checks_total_recent_weeks": figures.get("checks", {}).get("total"),
                  "trust_checks_this_week": figures.get("checks", {}).get("this_week")}
    except Exception:  # noqa: BLE001 - analytics extras must not break the tool
        db.session.rollback()
    return {
        "days": days,
        "orders": orders,
        "paid_orders": paid.count(),
        "paid_revenue": _money(revenue),
        "currency_note": "Revenue sums order totals as stored (mostly NGN).",
        "store_views_all_time": int(sf.view_count or 0) if sf else 0,
        "most_viewed_products": [{"product_id": p.id, "name": p.name, "views": int(p.view_count or 0)}
                                 for p in top],
        **checks,
    }


def trust_drivers(vendor, args: dict) -> dict:
    from app.services import trust_v1

    view = trust_v1.seller_view(vendor.id)
    return {
        "score": view.get("total_trust_score"),
        "score_max": view.get("score_max"),
        "tier": view.get("trust_tier"),
        "label": view.get("label"),
        "parts": view.get("pillars"),
        "reasons": view.get("reasons"),
        "minimum_history": view.get("minimum_history"),
        "dispute_cap": view.get("dispute_cap"),
        "last_recalculated": view.get("last_recalculated"),
    }


def list_disputes(vendor, args: dict) -> dict:
    from app.models.dispute import DisputeCase, DisputeStatus

    q = DisputeCase.query.filter(DisputeCase.vendor_id == vendor.id)
    if args.get("active_only"):
        q = q.filter(DisputeCase.status.in_(DisputeStatus.ACTIVE))
    rows = q.order_by(DisputeCase.created_at.desc()).limit(MAX_ROWS).all()
    return {"disputes": [{
        "reference": d.reference, "order_id": d.order_id, "status": d.status, "reason": d.reason,
        "raised_by": d.raised_by, "deadline_at": _iso(d.deadline_at), "outcome": d.outcome,
        "created_at": _iso(d.created_at),
    } for d in rows]}


def list_pay_links(vendor, args: dict) -> dict:
    from app.models.assistant import ConciergeOffer
    from app.models.order import Order
    from app.models.payment_link import PaymentLink

    status = (args.get("status") or "ANY").upper()
    if status not in ("ACTIVE", "PAID", "EXPIRED", "ANY"):
        raise ToolError("unknown status")
    q = PaymentLink.query.filter(PaymentLink.vendor_id == vendor.id)
    if status != "ANY":
        q = q.filter(PaymentLink.status == status)
    links = q.order_by(PaymentLink.created_at.desc()).limit(MAX_ROWS).all()
    paid_counts = dict(
        db.session.query(Order.payment_link_id, func.count(Order.id))
        .filter(Order.vendor_id == vendor.id, Order.payment_link_id.in_([l.id for l in links] or [0]),
                Order.status.in_(PAID_STATUSES))
        .group_by(Order.payment_link_id).all()
    )
    offers = (ConciergeOffer.query.filter_by(vendor_id=vendor.id, status="pending_seller")
              .order_by(ConciergeOffer.id.desc()).limit(10).all())
    return {
        "pay_links": [{
            "id": l.id, "title": l.title, "amount": _money(l.amount) if l.amount is not None else None,
            "currency": l.currency or "NGN", "status": l.status, "slug": l.slug,
            "created_at": _iso(l.created_at), "paid_orders": int(paid_counts.get(l.id, 0)),
        } for l in links],
        "offers_waiting_for_you": [{
            "offer_id": o.id, "product_id": o.product_id,
            "product_name": o.product.name if o.product else None, "quantity": o.quantity,
            "agreed_unit_price": _money(o.agreed_price), "list_price": _money(o.list_price),
            "currency": o.currency, "created_at": _iso(o.created_at),
        } for o in offers],
    }


HANDLERS = {
    "list_orders": list_orders,
    "get_order": get_order,
    "payout_status": payout_status,
    "list_products": list_products,
    "analytics_summary": analytics_summary,
    "trust_drivers": trust_drivers,
    "list_disputes": list_disputes,
    "list_pay_links": list_pay_links,
}
assert set(HANDLERS) == TOOL_NAMES


def run_tool(vendor, name: str, args) -> dict:
    """Run one READ tool for ``vendor`` (the signed-in seller). Raises
    ToolError for an unknown tool or bad arguments."""
    handler = HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"unknown tool {name!r}")
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    allowed = set(next(t for t in TOOLS if t["name"] == name)["input_schema"]["properties"])
    extra = set(args) - allowed
    if extra:
        raise ToolError(f"unknown arguments: {sorted(extra)}")
    return handler(vendor, args)
