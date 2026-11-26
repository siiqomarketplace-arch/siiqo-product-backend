"""Seller activation and the founder's Monday numbers. Read-only.

Definitions (campaign plan):
- Registered seller: a store was created.
- Activated seller: the store's first real paid order came within
  ACTIVATION_DAYS (30) of the store being created. A real paid order has
  orders.paid_at set by a payment, is not FREE, is not cancelled/refunded,
  and the buyer is not the seller (or the seller's agent) - checked with the
  marketing self-purchase helpers, read-only.
- Completed protected order: escrow RELEASED (delivered and released).
- Channel: user_attributions.channel; "existing" for stores created before
  CAMPAIGN_START_DATE; a seller with no row is resolved from their
  SellerAcquisition code, else organic.

Everything is computed from orders, escrow, disputes, payouts and users with
grouped SQL. Python only walks rows already bounded by the date range (the
cohort's stores and their candidate orders), never every user.
"""
from __future__ import annotations

import csv
import io
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, and_, cast, func, literal, or_

from app.extensions import db
from app.services.attribution import (CAMPAIGN_CHANNEL, CHANNEL_LABELS, CHANNELS, campaign_start_date,
                                      channel_for_code)

log = logging.getLogger(__name__)
LAGOS = timezone(timedelta(hours=1))
ACTIVATION_DAYS = 30
CLOSED_ORDER_STATUSES = ("CANCELLED", "REFUNDED")
DAYA_PROBLEM_STATUSES = ("FAILED", "REQUIRES_REVIEW", "UNDERPAID")


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def payout_stuck_hours() -> int:
    """A released escrow with no payout after this many hours is listed (PAYOUT_STUCK_HOURS, 24)."""
    return max(1, _int_env("PAYOUT_STUCK_HOURS", 24))


def now_utc():
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def week_start(dt) -> datetime:
    """Monday 00:00 Lagos of the week holding ``dt``, as UTC."""
    local = _aware(dt).astimezone(LAGOS)
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.astimezone(timezone.utc)


def last_weeks(count=8, now=None):
    """[(start, end)] of the last ``count`` complete Lagos weeks, oldest first."""
    this_week = week_start(now or now_utc())
    return [(this_week - timedelta(weeks=i), this_week - timedelta(weeks=i - 1)) for i in range(count, 0, -1)]


def wow(cur, prev):
    """Week-on-week change in percent (None when last week was 0 or unknown)."""
    if cur is None or prev in (None, 0):
        return None
    return round(100.0 * (cur - prev) / prev, 1)


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


# ── real paid orders ────────────────────────────────────────────────────────

def real_paid_filter():
    """SQL filter for a real paid order (paid by a payment, not free, not closed)."""
    from app.models.order import Order
    return and_(Order.paid_at.isnot(None),
                func.upper(func.coalesce(Order.payment_method, "")) != "FREE",
                func.upper(Order.status).notin_(CLOSED_ORDER_STATUSES))


def _buyer_key():
    from app.models.order import Order
    return func.coalesce(literal("u:") + cast(Order.buyer_id, String),
                         literal("e:") + func.lower(func.trim(Order.buyer_email)))


# ── cohort: stores and their channel ────────────────────────────────────────

def cohort(start, end):
    """Stores created in [start, end): list of dicts with vendor_id, created_at, channel."""
    from app.models.growth import UserAttribution
    from app.models.marketing import MarketingCode, SellerAcquisition
    from app.models.user import Storefront

    rows = (db.session.query(Storefront.vendor_id, Storefront.created_at, UserAttribution.channel,
                             SellerAcquisition.agent_user_id, SellerAcquisition.code_id)
            .outerjoin(UserAttribution, UserAttribution.user_id == Storefront.vendor_id)
            .outerjoin(SellerAcquisition, SellerAcquisition.seller_user_id == Storefront.vendor_id)
            .filter(Storefront.created_at >= start, Storefront.created_at < end)
            .all())
    code_ids = {r.code_id for r in rows if r.code_id and not r.channel}
    codes = {c.id: c for c in MarketingCode.query.filter(MarketingCode.id.in_(code_ids)).all()} if code_ids else {}
    start_at = campaign_start_date()
    out = []
    for r in rows:
        created = _aware(r.created_at)
        if created < start_at:
            channel = "existing"
        elif r.channel and r.channel in CHANNELS:
            channel = r.channel
        elif r.code_id and r.code_id in codes:
            channel = channel_for_code(codes[r.code_id]) or "organic"
        else:
            channel = "organic"
        out.append({"vendor_id": r.vendor_id, "created_at": created, "channel": channel,
                    "agent_user_id": r.agent_user_id})
    return out


def first_real_sales(stores, *, window_days=ACTIVATION_DAYS, now=None):
    """{vendor_id: (order_id, paid_at)} for each store whose first real paid
    order from a separate buyer came within ``window_days`` of the store."""
    from app.models.order import Order
    from app.services import marketing as mk

    if not stores:
        return {}
    now = now or now_utc()
    by_vendor = {s["vendor_id"]: s for s in stores}
    latest_deadline = max(s["created_at"] for s in stores) + timedelta(days=window_days)
    candidates = (Order.query
                  .filter(Order.vendor_id.in_(list(by_vendor)), real_paid_filter(),
                          Order.paid_at < min(latest_deadline, now + timedelta(seconds=1)))
                  .order_by(Order.vendor_id, Order.paid_at, Order.id)
                  .all())
    out, people = {}, {}
    for order in candidates:
        vid = order.vendor_id
        if vid in out:
            continue
        store = by_vendor[vid]
        paid = _aware(order.paid_at)
        if paid > store["created_at"] + timedelta(days=window_days):
            continue
        if vid not in people:
            people[vid] = mk._people(vid, store.get("agent_user_id"))
        if not mk._separate_buyer(order, people[vid]):
            continue  # the seller (or their agent) buying from themselves
        out[vid] = (order.id, paid)
    return out


def activation(start, end, now=None):
    """Registered and activated sellers for stores created in [start, end), by channel."""
    now = now or now_utc()
    stores = cohort(start, end)
    firsts = first_real_sales(stores, now=now)
    by_channel = {c: {"registered": 0, "activated": 0, "still_in_window": 0} for c in CHANNELS}
    for s in stores:
        row = by_channel.setdefault(s["channel"], {"registered": 0, "activated": 0, "still_in_window": 0})
        row["registered"] += 1
        if s["vendor_id"] in firsts:
            row["activated"] += 1
        elif s["created_at"] + timedelta(days=ACTIVATION_DAYS) > now:
            row["still_in_window"] += 1
    registered = len(stores)
    activated = len(firsts)
    return {"registered": registered, "activated": activated, "activation_pct": _pct(activated, registered),
            "still_in_window": sum(r["still_in_window"] for r in by_channel.values()),
            "by_channel": by_channel}


# ── spend ───────────────────────────────────────────────────────────────────

def spend_by_channel():
    """Campaign spend entered in admin (MarketingCampaign.spend_ngn), all time, by channel."""
    from app.models.marketing import MarketingCampaign
    out = {}
    for ch, slug, total in (db.session.query(MarketingCampaign.channel, MarketingCampaign.slug,
                                             func.coalesce(MarketingCampaign.spend_ngn, 0)).all()):
        slug = (slug or "").lower()
        if any(k in slug for k in ("facebook_sellers", "facebook-sellers", "fb-sellers", "fb_sellers")):
            channel = "facebook_sellers"
        else:
            channel = CAMPAIGN_CHANNEL.get((ch or "").lower(), "ads")
        out[channel] = round(out.get(channel, 0.0) + float(total or 0), 2)
    return out


def cost_per_activated_seller(now=None):
    """All campaign spend ÷ sellers activated since CAMPAIGN_START_DATE."""
    now = now or now_utc()
    spend = round(sum(spend_by_channel().values()), 2)
    start = campaign_start_date()
    activated = activation(start, now, now=now)["activated"] if start < now else 0
    return {"spend_ngn": spend, "activated_sellers": activated,
            "cost_per_activated_seller_ngn": round(spend / activated, 2) if activated else None,
            "since": start.astimezone(LAGOS).date().isoformat()}


# ── weekly order numbers ────────────────────────────────────────────────────

def _bucket(weeks, dt):
    dt = _aware(dt)
    if dt is None:
        return None
    for i, (s, e) in enumerate(weeks):
        if s <= dt < e:
            return i
    return None


def completed_protected_orders(weeks):
    from app.models.escrow import EscrowStatus, EscrowTransaction
    start, end = weeks[0][0], weeks[-1][1]
    counts = [0] * len(weeks)
    for (released_at,) in (db.session.query(EscrowTransaction.released_at)
                           .filter(EscrowTransaction.status == EscrowStatus.RELEASED,
                                   EscrowTransaction.released_at >= start, EscrowTransaction.released_at < end)):
        i = _bucket(weeks, released_at)
        if i is not None:
            counts[i] += 1
    return counts


def buyers_and_orders(weeks):
    """(paid_orders, new_buyers, repeat_buyers) per week. A new buyer's first
    ever real paid order is in that week; a repeat buyer had one before."""
    from app.models.order import Order
    start, end = weeks[0][0], weeks[-1][1]
    key = _buyer_key()
    first = (db.session.query(key.label("k"), func.min(Order.paid_at).label("first_paid"))
             .filter(real_paid_filter()).group_by(key).subquery())
    rows = (db.session.query(key.label("k"), Order.paid_at, first.c.first_paid)
            .outerjoin(first, first.c.k == key)
            .filter(real_paid_filter(), Order.paid_at >= start, Order.paid_at < end)
            .all())
    paid = [0] * len(weeks)
    new = [set() for _ in weeks]
    repeat = [set() for _ in weeks]
    for k, paid_at, first_paid in rows:
        i = _bucket(weeks, paid_at)
        if i is None:
            continue
        paid[i] += 1
        if not k:
            continue
        s, _ = weeks[i]
        (new[i] if _aware(first_paid) is not None and _aware(first_paid) >= s else repeat[i]).add(k)
    repeat = [r - n for r, n in zip(repeat, new)]   # a first-timer who bought twice is still new
    return paid, [len(x) for x in new], [len(x) for x in repeat]


def disputes(weeks):
    from app.models.dispute import DisputeCase
    start, end = weeks[0][0], weeks[-1][1]
    counts = [0] * len(weeks)
    for (created,) in (db.session.query(DisputeCase.created_at)
                       .filter(DisputeCase.created_at >= start, DisputeCase.created_at < end)):
        i = _bucket(weeks, created)
        if i is not None:
            counts[i] += 1
    return counts


# ── payout failures ─────────────────────────────────────────────────────────

def payout_failures(since=None, now=None, limit=200):
    """Failed or stuck payouts, newest first:
    - Daya payments FAILED / REQUIRES_REVIEW / UNDERPAID;
    - withdrawals FAILED, or PENDING/PROCESSING longer than the stuck window;
    - escrows RELEASED more than PAYOUT_STUCK_HOURS ago with no payout DEBIT
      ("PAYOUT-<escrow ref or order id>" / "CRYPTO-PAYOUT-<order id>")."""
    from app.models.escrow import EscrowStatus, EscrowTransaction
    from app.models.finance import Ledger
    from app.models.order import Order
    from app.models.withdrawal import DayaPayment, Withdrawal

    now = now or now_utc()
    since = since or (now - timedelta(days=60))
    stuck = now - timedelta(hours=payout_stuck_hours())
    items = []

    for p in (DayaPayment.query.filter(DayaPayment.status.in_(DAYA_PROBLEM_STATUSES),
                                       DayaPayment.created_at >= since)
              .order_by(DayaPayment.created_at.desc()).limit(limit)):
        items.append({"kind": "daya_payment", "status": p.status, "order_id": p.order_id, "vendor_id": None,
                      "amount_ngn": float(p.amount_ngn or 0), "at": _aware(p.updated_at or p.created_at),
                      "reason": {"FAILED": "Crypto payment failed",
                                 "REQUIRES_REVIEW": "Crypto payment needs review",
                                 "UNDERPAID": "Crypto payment underpaid"}.get(p.status, p.status)})

    for w in (Withdrawal.query.filter(or_(Withdrawal.status == "FAILED",
                                          and_(Withdrawal.status.in_(("PENDING", "PROCESSING")),
                                               Withdrawal.requested_at < stuck)),
                                      Withdrawal.requested_at >= since)
              .order_by(Withdrawal.requested_at.desc()).limit(limit)):
        items.append({"kind": "withdrawal", "status": w.status, "order_id": None, "vendor_id": w.vendor_id,
                      "withdrawal_id": w.id, "amount_ngn": float(w.net_amount or w.amount or 0),
                      "at": _aware(w.processed_at or w.requested_at),
                      "reason": "Withdrawal failed" if w.status == "FAILED"
                      else f"Withdrawal {w.status.lower()} for more than {payout_stuck_hours()}h"})

    debit_exists = (db.session.query(Ledger.id)
                    .filter(Ledger.vendor_id == Order.vendor_id, Ledger.transaction_type == "DEBIT",
                            Ledger.reference_id.in_([
                                literal("PAYOUT-") + EscrowTransaction.transaction_number,
                                literal("PAYOUT-") + cast(Order.id, String),
                                literal("CRYPTO-PAYOUT-") + cast(Order.id, String),
                            ]))
                    .exists())
    for esc, order in (db.session.query(EscrowTransaction, Order)
                       .join(Order, Order.id == EscrowTransaction.order_id)
                       .filter(EscrowTransaction.status == EscrowStatus.RELEASED,
                               EscrowTransaction.released_at >= since, EscrowTransaction.released_at < stuck,
                               ~debit_exists)
                       .order_by(EscrowTransaction.released_at.desc()).limit(limit)):
        items.append({"kind": "released_not_paid", "status": esc.status, "order_id": order.id,
                      "vendor_id": order.vendor_id, "amount_ngn": float(esc.amount or 0),
                      "at": _aware(esc.released_at),
                      "reason": f"Released, no payout after {payout_stuck_hours()}h (money is in the seller's balance)"})

    # Payout transfers that failed / were reversed / are stuck pending, and
    # released orders whose payout is not proven, sent, held or recorded
    # (app/services/payout_tracking.py, app/services/payout_reconciliation.py).
    try:
        from app.services.payout_tracking import needs_a_look
        items.extend(needs_a_look(since=since, now=now, limit=limit))
    except Exception as exc:  # noqa: BLE001
        log.warning("[ACTIVATION] payout transfer check skipped: %s", exc)
    try:
        from app.services.payout_reconciliation import payout_gaps
        listed = {i["order_id"] for i in items if i.get("order_id")}
        items.extend(g for g in payout_gaps(now=now, limit=limit) if g["order_id"] not in listed
                     and (g["at"] is None or g["at"] >= since))
    except Exception as exc:  # noqa: BLE001
        log.warning("[ACTIVATION] payout gap check skipped: %s", exc)

    items.sort(key=lambda x: x["at"] or now, reverse=True)
    return items[:limit]


# ── the Monday dashboard ────────────────────────────────────────────────────

def _iso(dt):
    return dt.isoformat() if dt else None


def weekly_dashboard(weeks_count=8, now=None):
    now = now or now_utc()
    weeks_count = max(2, min(int(weeks_count or 8), 26))
    weeks = last_weeks(weeks_count, now)
    completed = completed_protected_orders(weeks)
    paid, new_buyers, repeat_buyers = buyers_and_orders(weeks)
    dispute_counts = disputes(weeks)
    failures = payout_failures(since=weeks[0][0], now=now)
    failure_counts = [0] * len(weeks)
    for f in failures:
        i = _bucket(weeks, f["at"])
        if i is not None:
            failure_counts[i] += 1

    rows = []
    for i, (s, e) in enumerate(weeks):
        act = activation(s, e, now=now)
        rows.append({
            "week_start": s.astimezone(LAGOS).date().isoformat(),
            "week_end": (e - timedelta(seconds=1)).astimezone(LAGOS).date().isoformat(),
            "registered_sellers": act["registered"],
            "activated_sellers": act["activated"],
            "activation_pct": act["activation_pct"],
            "still_in_window": act["still_in_window"],
            "completed_protected_orders": completed[i],
            "paid_orders": paid[i],
            "new_buyers": new_buyers[i],
            "repeat_buyers": repeat_buyers[i],
            "disputes": dispute_counts[i],
            "disputes_per_100_orders": round(100.0 * dispute_counts[i] / paid[i], 1) if paid[i] else None,
            "payout_failures": failure_counts[i],
        })
    keys = ("registered_sellers", "activated_sellers", "completed_protected_orders", "new_buyers",
            "repeat_buyers", "disputes_per_100_orders", "payout_failures", "paid_orders")
    for i, row in enumerate(rows):
        prev = rows[i - 1] if i else None
        row["wow_pct"] = {k: (wow(row[k], prev[k]) if prev else None) for k in keys}

    range_start, range_end = weeks[0][0], weeks[-1][1]
    act_range = activation(range_start, range_end, now=now)
    spend = spend_by_channel()
    channels = []
    for ch in CHANNELS:
        r = act_range["by_channel"].get(ch, {"registered": 0, "activated": 0, "still_in_window": 0})
        sp = spend.get(ch, 0.0)
        channels.append({"channel": ch, "label": CHANNEL_LABELS.get(ch, ch), "registered": r["registered"],
                         "activated": r["activated"], "activation_pct": _pct(r["activated"], r["registered"]),
                         "still_in_window": r["still_in_window"], "spend_ngn": sp,
                         "cost_per_activated_ngn": round(sp / r["activated"], 2) if r["activated"] and sp else None})

    return {
        "generated_at": _iso(now),
        "campaign_start": campaign_start_date().astimezone(LAGOS).date().isoformat(),
        "activation_days": ACTIVATION_DAYS,
        "payout_stuck_hours": payout_stuck_hours(),
        "weeks": rows,
        "latest": rows[-1] if rows else None,
        "channels": channels,
        "cost": cost_per_activated_seller(now),
        "top_agents": top_by_first_sales("agent", range_start, range_end),
        "top_influencers": top_by_first_sales("influencer", range_start, range_end),
        "payout_failures": [{**f, "at": _iso(f["at"])} for f in failures],
        "notes": [
            "Weeks run Monday to Sunday, Lagos time; only complete weeks are shown.",
            f"A seller is activated when their first real paid order (not free, not cancelled or refunded, "
            f"not bought by themselves) comes within {ACTIVATION_DAYS} days of the store being created. "
            "Recent weeks can still go up: 'still in window' sellers have time left.",
            "Spend is what was entered on marketing campaigns, for each campaign's whole life.",
            "Stores created before the campaign start date count as 'existing'.",
        ],
    }


def top_by_first_sales(kind, start, end, limit=5):
    """Top agents (kind='agent') or influencers by sellers whose first sale
    (SellerAcquisition.first_paid_order_at, already self-purchase checked) was in [start, end)."""
    from app.models.marketing import AgentProfile, MarketingCode, SellerAcquisition
    from app.models.user import User
    base = (SellerAcquisition.query
            .filter(SellerAcquisition.first_paid_order_at >= start, SellerAcquisition.first_paid_order_at < end))
    if kind == "agent":
        rows = (base.with_entities(SellerAcquisition.agent_user_id, func.count(SellerAcquisition.id).label("n"))
                .outerjoin(AgentProfile, AgentProfile.user_id == SellerAcquisition.agent_user_id)
                .filter(SellerAcquisition.agent_user_id.isnot(None),
                        func.coalesce(AgentProfile.agent_type, "field") != "influencer")
                .group_by(SellerAcquisition.agent_user_id)
                .order_by(func.count(SellerAcquisition.id).desc()).limit(limit).all())
        names = {p.user_id: p.full_name for p in
                 AgentProfile.query.filter(AgentProfile.user_id.in_([r[0] for r in rows])).all()} if rows else {}
        return [{"agent_user_id": uid, "name": names.get(uid) or f"Agent {uid}", "first_sales": n} for uid, n in rows]
    rows = (base.with_entities(SellerAcquisition.code_id, func.count(SellerAcquisition.id).label("n"))
            .join(MarketingCode, MarketingCode.id == SellerAcquisition.code_id)
            .outerjoin(AgentProfile, AgentProfile.user_id == SellerAcquisition.agent_user_id)
            .filter(or_(MarketingCode.kind == "influencer", AgentProfile.agent_type == "influencer"))
            .group_by(SellerAcquisition.code_id)
            .order_by(func.count(SellerAcquisition.id).desc()).limit(limit).all())
    codes = {c.id: c for c in MarketingCode.query.filter(MarketingCode.id.in_([r[0] for r in rows])).all()} if rows else {}
    out = []
    for code_id, n in rows:
        c = codes.get(code_id)
        owner = db.session.get(User, c.owner_user_id) if c is not None and c.owner_user_id else None
        name = (c.label if c is not None and c.label else None) or (owner.first_name if owner else None)
        out.append({"code": c.code if c is not None else None, "name": name or (c.code if c else str(code_id)),
                    "first_sales": n})
    return out


def dashboard_csv(data) -> str:
    """The weekly rows, the channel table and the payout problems as one CSV."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Weekly numbers"])
    cols = ["week_start", "week_end", "registered_sellers", "activated_sellers", "activation_pct",
            "still_in_window", "completed_protected_orders", "paid_orders", "new_buyers", "repeat_buyers",
            "disputes", "disputes_per_100_orders", "payout_failures"]
    w.writerow(cols + ["wow_completed_protected_orders_pct", "wow_activated_sellers_pct"])
    for r in data["weeks"]:
        w.writerow([r[c] if r[c] is not None else "" for c in cols]
                   + [r["wow_pct"]["completed_protected_orders"] if r["wow_pct"]["completed_protected_orders"] is not None else "",
                      r["wow_pct"]["activated_sellers"] if r["wow_pct"]["activated_sellers"] is not None else ""])
    w.writerow([])
    w.writerow(["Sellers by channel"])
    w.writerow(["channel", "registered", "activated", "activation_pct", "still_in_window", "spend_ngn",
                "cost_per_activated_ngn"])
    for c in data["channels"]:
        w.writerow([c["label"], c["registered"], c["activated"], c["activation_pct"] if c["activation_pct"] is not None else "",
                    c["still_in_window"], c["spend_ngn"],
                    c["cost_per_activated_ngn"] if c["cost_per_activated_ngn"] is not None else ""])
    w.writerow([])
    cost = data["cost"]
    w.writerow(["Cost per activated seller (since campaign start)", cost["spend_ngn"], cost["activated_sellers"],
                cost["cost_per_activated_seller_ngn"] if cost["cost_per_activated_seller_ngn"] is not None else ""])
    w.writerow([])
    w.writerow(["Payout problems"])
    w.writerow(["kind", "status", "order_id", "vendor_id", "amount_ngn", "at", "reason"])
    for f in data["payout_failures"]:
        w.writerow([f["kind"], f["status"], f.get("order_id") or "", f.get("vendor_id") or "", f["amount_ngn"],
                    f["at"] or "", f["reason"]])
    return buf.getvalue()
