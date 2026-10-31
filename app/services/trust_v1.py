"""
Trust Score v1: earned from evidence only (docs/SIIQO_2.0_MASTER_PLAN.md §8.3,
docs/SIIQO_2.0_GAP_ANALYSIS_AND_PLAN.md §3).

What counts
-----------
Only what happened on real orders, read from the append-only `trust_evidence`
ledger (rows with status ACTIVE; a REVOKED row, e.g. after an appeal, stops
counting). The ledger says which orders exist and how they ended; the order
row it points to supplies who the buyer was and when delivery was confirmed.

    order_paid        → a sale started (time, amount, currency)
    escrow_released   → the sale went well
    order_refunded    → the sale did not go well
    dispute_opened    → a dispute was raised (no penalty on its own)
    dispute_lost      → a dispute ended in a refund: seller at fault
    dispute_resolved  → a reviewer's dispute decision (app/services/disputes.py):
                        source_id is the case, properties.order_id the order,
                        properties.seller_fault true/false. It overrides
                        dispute_lost; a refund the seller wasn't at fault for
                        is neutral.

What never counts (founder rules): payment, plan or subscription, the paid
Verified mark or identity verification (D3: "contributes zero points"; it is a
separate fact on the Passport), logos, banners or any store decoration, typed
NIN/CAC/bank numbers, reviews' star text, social activity.

Score (0-100)
-------------
    score = 100 × Σ WEIGHTS[c] × component[c]      every component is 0..1

Components:
  outcomes      Bayesian-smoothed weighted share of resolved orders that went
                well. Refund = 1 bad unit, seller-fault dispute = 5 bad units,
                dispute not the seller's fault = no penalty.
  on_time       weighted share of delivery-confirmed orders delivered within
                ON_TIME_DAYS of payment (smoothed).
  repeat_buyers share of distinct buyers who came back on a later day;
                REPEAT_TARGET_SHARE or more scores full marks.
  tenure        months since the first completed sale, full at 12 months.
  consistency   share of the last 6 months (30-day buckets) with a completed
                sale, out of the months the seller has been selling.

Order weight = recency × size × per-buyer × anti-gaming factors:
  recency       0.5 ** (age_days / 182.5)               (6-month half-life)
  size          min(3, 1 + ln(1 + amount_ngn / 10,000))  (log scale)
  per-buyer     1/k for the buyer's k-th order           (diminishing returns)

Anti-gaming rules
-----------------
  self-dealing   the buyer is the seller, or shares an identifier with the
                 seller in the identifier graph (seller_identifiers: email,
                 phone, payout account hash, domain, handle…; buyer side: the
                 buyer account's email/phone, the order's buyer email/phone
                 and delivery phone, and the buyer's own seller identifiers).
                 Those orders count for nothing, good or bad.
  tiny orders    under ₦1,000 weigh 0.1 and never count toward the minimum
                 history.
  new-buyer bursts  5+ orders from buyer accounts younger than 7 days within
                 72 hours: each weighs 0.25 and none counts toward the
                 minimum history or repeat buyers. A guest's age is taken
                 from their first order anywhere on Siiqo.
  diminishing    a buyer's k-th order weighs 1/k (good and bad alike, so one
  returns        buyer can neither inflate nor sink a seller alone).
  recency decay  6-month half-life.
  dispute caps   seller-fault disputes per 100 orders over the last 6 months
                 (weighted, no recency): >= 2 caps the tier at SILVER, >= 5 at
                 BRONZE (score lowered to that tier's top: 74 / 54).
  minimum history  no score below 5 qualifying sales (completed, not linked,
                 not tiny, not in a burst) from 3 distinct buyers: tier NEW,
                 shown as "New seller".

Changing any weight or rule means a new VERSION; shadow rows keep the version
that produced them.

Shadow mode and go-live
-----------------------
`run_shadow()` (daily job, app/tasks/trust_v1_shadow.py) stores one row per
seller per day in `trust_score_shadow`. The public score and the Passport keep
the old score until TRUST_SCORE_V1_LIVE is on; see docs/TRUST_SCORE_V1.md for
the 4-week shadow plan.
"""
import logging
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

VERSION = "v1.0"
SCORE_MAX = 100

# Component weights; they sum to 1.0.
WEIGHTS = {
    "outcomes": 0.70,
    "on_time": 0.12,
    "repeat_buyers": 0.08,
    "tenure": 0.05,
    "consistency": 0.05,
}

RULES = {
    "half_life_days": 182.5,
    "outcome_prior_mean": 0.75,
    "outcome_prior_weight": 5.0,
    "refund_bad_units": 1.0,
    # A seller-fault dispute cancels 5 good sales (was 2). The Phase 2 target is
    # < 2 seller-fault disputes per 100 orders. Simulated on 100 recent orders
    # from 100 buyers: at 2 per 100 the outcome share falls from 0.99 to 0.88
    # (about -8 score points), at 7 per 100 to 0.72 (about -19, score ~70),
    # so the score alone already drops such a seller out of GOLD; with 2 units
    # the same seller stayed GOLD (79). Voluntary refunds stay at 1 unit so
    # sellers are never pushed away from refunding without a fight.
    "seller_fault_dispute_bad_units": 5.0,
    # Tier caps from the seller-fault dispute rate over the last 6 months
    # (weighted by order size, tiny and burst factors; no recency, no 1/k): at or above 2 per 100 the
    # tier is at most SILVER, at or above 5 per 100 at most BRONZE. The score is
    # lowered to the top of the capped tier so score and tier agree.
    "dispute_window_days": 182.5,
    "dispute_cap_silver_per_100": 2.0,
    "dispute_cap_bronze_per_100": 5.0,
    "on_time_days": 7,
    "on_time_prior_mean": 0.5,
    "on_time_prior_weight": 3.0,
    "size_reference_ngn": 10_000,
    "size_max": 3.0,
    "tiny_order_ngn": 1_000,
    "tiny_order_weight": 0.1,
    "new_buyer_days": 7,
    "burst_window_hours": 72,
    "burst_min_orders": 5,
    "burst_weight": 0.25,
    "repeat_min_gap_hours": 24,
    "repeat_target_share": 0.30,
    "tenure_full_months": 12,
    "consistency_months": 6,
    "min_qualifying_sales": 5,
    "min_distinct_buyers": 3,
}

# Lowest score for each tier, highest first. Below the minimum history: NEW.
TIERS = [(90, "PLATINUM"), (75, "GOLD"), (55, "SILVER"), (0, "BRONZE")]
NEW_TIER = "NEW"
_TIER_RANK = {"BRONZE": 0, "SILVER": 1, "GOLD": 2, "PLATINUM": 3}
# Highest score each tier allows (one below the next tier's floor).
_TIER_CEILING = {name: (TIERS[i - 1][0] - 1 if i else 100) for i, (_, name) in enumerate(TIERS)}
NEW_LABEL = "New seller"

_EVIDENCE_TYPES = ("order_paid", "escrow_released", "order_refunded", "dispute_opened",
                   "dispute_lost", "dispute_resolved")
_DAY = timedelta(days=1)
_MONTH_DAYS = 30.44


def is_live():
    """TRUST_SCORE_V1_LIVE, read on every call so it can be switched without a code change."""
    return os.environ.get("TRUST_SCORE_V1_LIVE", "").strip().lower() in ("1", "true", "yes", "on")


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def tier_for(score):
    if score is None:
        return NEW_TIER
    for floor, name in TIERS:
        if score >= floor:
            return name
    return TIERS[-1][1]


# ── pure scoring ───────────────────────────────────────────────────────────

def score_orders(orders, now=None):
    """Score a seller from a list of order facts. Pure: no database access.

    Each fact is a dict:
        order_id, buyer_key, linked (bool), buyer_new (bool), time (aware dt),
        amount_ngn (float or None), outcome ("good" | "bad" | "neutral" | "pending"),
        seller_fault (bool), refunded (bool), dispute_opened (bool),
        delivered_on_time (True | False | None), evidence_ids (list)
    """
    now = now or _now()
    R = RULES
    counts = defaultdict(int)
    evidence_ids = []

    usable = []
    for o in orders:
        evidence_ids += list(o.get("evidence_ids") or [])
        if o.get("linked"):
            counts["self_dealing_orders"] += 1
            continue
        usable.append(o)

    # New-buyer bursts: any 72-hour window with 5+ orders from new buyer accounts.
    window = timedelta(hours=R["burst_window_hours"])
    fresh = sorted((o for o in usable if o.get("buyer_new")), key=lambda o: o["time"])
    in_burst = set()
    start = 0
    for end in range(len(fresh)):
        while fresh[end]["time"] - fresh[start]["time"] > window:
            start += 1
        if end - start + 1 >= R["burst_min_orders"]:
            for o in fresh[start:end + 1]:
                in_burst.add(o["order_id"])
    counts["burst_orders"] = len(in_burst)

    resolved = sorted((o for o in usable if o["outcome"] in ("good", "bad")), key=lambda o: o["time"])
    counts["pending_orders"] = sum(1 for o in usable if o["outcome"] == "pending")
    counts["neutral_orders"] = sum(1 for o in usable if o["outcome"] == "neutral")

    per_buyer = defaultdict(int)
    good_w = bad_w = 0.0
    window_w = window_fault_w = 0.0
    on_time_w = late_w = 0.0
    qualifying = []
    good_times_by_buyer = defaultdict(list)
    for o in resolved:
        per_buyer[o["buyer_key"]] += 1
        k = per_buyer[o["buyer_key"]]
        if k > 1:
            counts["diminished_orders"] += 1
        age_days = max(0.0, (now - o["time"]).total_seconds() / 86400.0)
        recency = 0.5 ** (age_days / R["half_life_days"])
        amount = o.get("amount_ngn")
        size = 1.0 if amount is None else min(R["size_max"], 1.0 + math.log1p(max(0.0, amount) / R["size_reference_ngn"]))
        tiny = amount is not None and amount < R["tiny_order_ngn"]
        burst = o["order_id"] in in_burst
        base = size  # order size with the tiny/burst factors; no recency, no 1/k
        if tiny:
            base *= R["tiny_order_weight"]
            counts["tiny_orders"] += 1
        if burst:
            base *= R["burst_weight"]
        w = recency * base / k
        if age_days <= R["dispute_window_days"]:
            unaged = base  # the dispute rate counts every order once
            window_w += unaged
            if o["outcome"] == "bad" and o.get("seller_fault"):
                window_fault_w += unaged

        if o["outcome"] == "good":
            good_w += w
            counts["completed_orders"] += 1
            if not tiny and not burst:
                qualifying.append(o)
                good_times_by_buyer[o["buyer_key"]].append(o["time"])
        else:
            units = R["seller_fault_dispute_bad_units"] if o.get("seller_fault") else R["refund_bad_units"]
            bad_w += w * units
            if o.get("seller_fault"):
                counts["seller_fault_disputes"] += 1
            else:
                counts["refunds"] += 1
        if o.get("dispute_opened") and not o.get("seller_fault"):
            counts["disputes_not_seller_fault"] += 1

        if o.get("delivered_on_time") is True:
            on_time_w += w
            counts["delivered_on_time"] += 1
        elif o.get("delivered_on_time") is False:
            late_w += w
            counts["delivered_late"] += 1

    outcomes = (good_w + R["outcome_prior_mean"] * R["outcome_prior_weight"]) / \
               (good_w + bad_w + R["outcome_prior_weight"])
    on_time = (on_time_w + R["on_time_prior_mean"] * R["on_time_prior_weight"]) / \
              (on_time_w + late_w + R["on_time_prior_weight"])

    # Repeat buyers: came back on a later day (not two orders in one sitting).
    gap = timedelta(hours=R["repeat_min_gap_hours"])
    buyers = len(good_times_by_buyer)
    repeaters = sum(1 for times in good_times_by_buyer.values() if times[-1] - times[0] >= gap)
    repeat_share = repeaters / buyers if buyers else 0.0
    repeat = min(1.0, repeat_share / R["repeat_target_share"]) if buyers else 0.0

    good_times = sorted(o["time"] for o in qualifying)
    if good_times:
        months_active = (now - good_times[0]).total_seconds() / 86400.0 / _MONTH_DAYS
        tenure = min(1.0, months_active / R["tenure_full_months"])
        n_months = R["consistency_months"]
        buckets = {int((now - t).total_seconds() // (86400 * _MONTH_DAYS)) for t in good_times}
        possible = min(n_months, int(months_active) + 1)
        consistency = len([b for b in buckets if b < possible]) / possible
    else:
        months_active = tenure = consistency = 0.0

    components = {
        "outcomes": round(outcomes, 4),
        "on_time": round(on_time, 4),
        "repeat_buyers": round(repeat, 4),
        "tenure": round(tenure, 4),
        "consistency": round(consistency, 4),
    }
    distinct_buyers = len({o["buyer_key"] for o in qualifying})
    enough = len(qualifying) >= R["min_qualifying_sales"] and distinct_buyers >= R["min_distinct_buyers"]
    raw = 100.0 * sum(WEIGHTS[c] * components[c] for c in WEIGHTS)
    score = int(round(raw)) if enough else None
    tier = tier_for(score)

    dispute_rate = round(100.0 * window_fault_w / window_w, 6) if window_w else None
    cap = None
    if dispute_rate is not None and dispute_rate >= R["dispute_cap_bronze_per_100"]:
        cap = "BRONZE"
    elif dispute_rate is not None and dispute_rate >= R["dispute_cap_silver_per_100"]:
        cap = "SILVER"
    capped = False
    if score is not None and cap and _TIER_RANK[tier] > _TIER_RANK[cap]:
        score = min(score, _TIER_CEILING[cap])
        tier = cap
        capped = True

    resolved_n = counts["completed_orders"] + counts["refunds"] + counts["seller_fault_disputes"]
    delivered_n = counts["delivered_on_time"] + counts["delivered_late"]
    indicators = {
        "qualifying_sales": len(qualifying),
        "distinct_buyers": distinct_buyers,
        "repeat_buyers": repeaters,
        "months_active": round(months_active, 1),
        "on_time_rate": round(counts["delivered_on_time"] / delivered_n, 4) if delivered_n else None,
        "seller_fault_disputes_per_100": round(100.0 * counts["seller_fault_disputes"] / resolved_n, 2)
        if resolved_n else None,
        "seller_fault_disputes_per_100_6m_weighted": round(dispute_rate, 2) if dispute_rate is not None else None,
    }
    return {
        "version": VERSION,
        "score": score,
        "tier": tier,
        "label": NEW_LABEL if score is None else None,
        "dispute_cap": {"cap": cap, "applied": capped,
                        "rate_per_100": round(dispute_rate, 2) if dispute_rate is not None else None},
        "raw_score": round(raw, 2),
        "components": components,
        "weights": dict(WEIGHTS),
        "indicators": indicators,
        "counts": dict(counts),
        "anti_gaming": {
            "self_dealing_orders": counts["self_dealing_orders"],
            "tiny_orders": counts["tiny_orders"],
            "burst_orders": counts["burst_orders"],
            "diminished_orders": counts["diminished_orders"],
        },
        "minimum_history": {
            "met": enough,
            "qualifying_sales": len(qualifying),
            "distinct_buyers": distinct_buyers,
            "needs_sales": R["min_qualifying_sales"],
            "needs_buyers": R["min_distinct_buyers"],
        },
        "reasons": _reasons(components, counts, enough),
        "evidence_ids": sorted(set(evidence_ids)),
    }


def _reasons(components, counts, enough):
    """Up to three reason codes, strongest first."""
    if not enough:
        return ["not_enough_history"]
    out = []
    if counts.get("seller_fault_disputes"):
        out.append("seller_fault_disputes")
    if counts.get("refunds"):
        out.append("refunds")
    ranked = sorted(((components[c], c) for c in ("on_time", "repeat_buyers", "tenure", "consistency")),
                    reverse=True)
    out += [{"on_time": "on_time_delivery", "repeat_buyers": "repeat_buyers", "tenure": "months_active",
             "consistency": "steady_sales"}[c] for value, c in ranked if value >= 0.6]
    return out[:3]


# ── evidence → order facts ─────────────────────────────────────────────────

def _identity_pairs(email=None, phones=()):
    from app.services.foundations import _norm_email, _norm_phone
    out = set()
    e = _norm_email(email)
    if e:
        out.add(("email", e))
    for p in phones:
        p = _norm_phone(p)
        if p:
            out.add(("phone", p))
    return out


def gather_orders(vendor_id):
    """Turn a seller's trust evidence into the order facts score_orders() needs."""
    from sqlalchemy import func
    from app.extensions import db
    from app.models.foundations import SellerIdentifier
    from app.models.order import Order
    from app.models.telemetry import TrustEvidence
    from app.models.user import User

    rows = (TrustEvidence.query
            .filter(TrustEvidence.business_id == vendor_id,
                    TrustEvidence.status == "ACTIVE",
                    TrustEvidence.evidence_type.in_(_EVIDENCE_TYPES))
            .order_by(TrustEvidence.created_at, TrustEvidence.id).all())
    by_order = defaultdict(list)
    for r in rows:
        try:
            # Escrow evidence points at the order; a dispute decision points at
            # its dispute case and names the order in properties.order_id.
            if r.evidence_type == "dispute_resolved":
                by_order[int((r.properties or {})["order_id"])].append(r)
            else:
                by_order[int(r.source_id)].append(r)
        except (KeyError, TypeError, ValueError):
            continue
        except (TypeError, ValueError):
            continue
    if not by_order:
        return []

    orders = {o.id: o for o in Order.query.filter(Order.id.in_(list(by_order))).all()}
    buyer_ids = {o.buyer_id for o in orders.values() if o.buyer_id}
    users = {u.id: u for u in User.query.filter(User.id.in_(buyer_ids)).all()} if buyer_ids else {}
    seller = db.session.get(User, vendor_id)

    seller_ids = {(i.kind, i.value) for i in SellerIdentifier.query.filter_by(vendor_id=vendor_id).all()}
    if seller is not None:
        seller_ids |= _identity_pairs(seller.email, [seller.phone])
    buyer_graph = defaultdict(set)
    if buyer_ids:
        for i in SellerIdentifier.query.filter(SellerIdentifier.vendor_id.in_(buyer_ids)).all():
            buyer_graph[i.vendor_id].add((i.kind, i.value))

    # A guest buyer's "account age" starts at their first order anywhere on Siiqo.
    guest_emails = {(o.buyer_email or "").strip().lower() for o in orders.values()
                    if not o.buyer_id and o.buyer_email}
    first_seen = {}
    if guest_emails:
        q = (db.session.query(func.lower(Order.buyer_email), func.min(Order.created_at))
             .filter(func.lower(Order.buyer_email).in_(guest_emails))
             .group_by(func.lower(Order.buyer_email)))
        first_seen = {email: _aware(at) for email, at in q.all()}

    R = RULES
    facts = []
    for order_id, evs in by_order.items():
        order = orders.get(order_id)
        if order is None or order.vendor_id != vendor_id:
            continue
        types = defaultdict(list)
        for e in evs:
            types[e.evidence_type].append(e)
        paid = types.get("order_paid")
        props = (paid[0].properties or {}) if paid else {}
        when = _aware(paid[0].created_at) if paid else _aware(order.paid_at) or _aware(order.created_at)
        if when is None:
            continue

        amount = None
        value = props.get("value")
        currency = (props.get("currency") or order.currency or "NGN").upper()
        if value is None and order.total_amount is not None:
            value = float(order.total_amount)
        if value is not None:
            if currency == "NGN":
                amount = float(value)
            elif order.fx_rate_to_ngn:
                amount = float(value) * float(order.fx_rate_to_ngn)

        decisions = types.get("dispute_resolved", [])
        decided_fault = None  # None = no decision, True = seller at fault, False = not
        if decisions:
            p = decisions[-1].properties or {}
            if "seller_fault" in p:
                decided_fault = bool(p.get("seller_fault"))
            else:
                decided_fault = str(p.get("fault") or "").lower() == "seller"
        if decided_fault is None:
            seller_fault = bool(types.get("dispute_lost"))
        else:
            seller_fault = decided_fault  # the reviewer's decision wins over the escrow-side guess
        cleared = decided_fault is False
        refunded = bool(types.get("order_refunded"))
        released = bool(types.get("escrow_released"))
        if seller_fault:
            outcome = "bad"
        elif refunded:
            outcome = "neutral" if cleared else "bad"
        elif released:
            outcome = "good"
        else:
            outcome = "pending"

        user = users.get(order.buyer_id) if order.buyer_id else None
        email = (user.email if user else None) or order.buyer_email
        buyer_pairs = _identity_pairs(user.email if user else None, [user.phone if user else None]) \
            | _identity_pairs(order.buyer_email, [order.buyer_phone, order.delivery_phone])
        if order.buyer_id:
            buyer_pairs |= buyer_graph.get(order.buyer_id, set())
        linked = bool(order.buyer_id and order.buyer_id == vendor_id) or bool(buyer_pairs & seller_ids)

        norm_email = next((v for k, v in buyer_pairs if k == "email"), None)
        if norm_email:
            buyer_key = f"e:{norm_email}"
        elif order.buyer_id:
            buyer_key = f"u:{order.buyer_id}"
        else:
            phone = next((v for k, v in sorted(buyer_pairs) if k == "phone"), None)
            buyer_key = f"p:{phone}" if phone else f"o:{order_id}"

        born = _aware(user.created_at) if user else first_seen.get((email or "").strip().lower())
        buyer_new = born is not None and when - born < timedelta(days=R["new_buyer_days"])

        delivered = _aware(order.delivered_at)
        delivered_on_time = None
        if delivered is not None and outcome in ("good", "bad"):
            delivered_on_time = delivered - when <= timedelta(days=R["on_time_days"])

        facts.append({
            "order_id": order_id,
            "buyer_key": buyer_key,
            "linked": linked,
            "buyer_new": buyer_new,
            "time": when,
            "amount_ngn": amount,
            "outcome": outcome,
            "seller_fault": seller_fault,
            "refunded": refunded,
            "dispute_opened": bool(types.get("dispute_opened")) or bool(decisions),
            "delivered_on_time": delivered_on_time,
            "evidence_ids": [e.id for e in evs],
        })
    return facts


def compute(vendor_id, now=None):
    return score_orders(gather_orders(vendor_id), now=now)


# ── old score, as buyers see it today ──────────────────────────────────────

def legacy_public(user):
    """The old public score and tier (incl. the SILVER-500 default), read without the flag."""
    profile = getattr(user, "trust_profile", None) if user is not None else None
    if not profile:
        return 500, "SILVER"
    return profile.total_trust_score, profile.trust_tier


# ── shadow mode ────────────────────────────────────────────────────────────

def _seller_ids():
    from app.extensions import db
    from app.models.telemetry import TrustEvidence
    from app.models.user import User, UserRole
    ids = {uid for (uid,) in db.session.query(User.id).filter(User.role == UserRole.VENDOR).all()}
    ids |= {bid for (bid,) in db.session.query(TrustEvidence.business_id).distinct().all()}
    return sorted(ids)


def store_shadow(vendor_id, now=None):
    """Compute v1 for one seller and upsert today's shadow row. Returns the row."""
    from app.extensions import db
    from app.models.trust import TrustScoreShadow
    from app.models.user import User

    now = now or _now()
    result = compute(vendor_id, now=now)
    old_score, old_tier = legacy_public(db.session.get(User, vendor_id))
    day = now.date()
    row = TrustScoreShadow.query.filter_by(vendor_id=vendor_id, version=VERSION, computed_on=day).first()
    if row is None:
        row = TrustScoreShadow(vendor_id=vendor_id, version=VERSION, computed_on=day)
        db.session.add(row)
    row.score = result["score"]
    row.tier = result["tier"]
    row.components = result
    row.computed_at = now
    row.old_score = old_score
    row.old_tier = old_tier
    return row


def run_shadow(now=None, batch=200):
    """Daily: compute v1 for every seller and store it. Safe to re-run the same
    day (rows are updated, not duplicated). Never changes the public score."""
    from app.extensions import db
    now = now or _now()
    done = failed = 0
    for i, vid in enumerate(_seller_ids(), 1):
        try:
            store_shadow(vid, now=now)
            done += 1
        except Exception as e:
            db.session.rollback()
            failed += 1
            logger.warning(f"[TRUST_V1] seller {vid} skipped: {e}")
            continue
        if i % batch == 0:
            db.session.commit()
    db.session.commit()
    logger.info(f"[TRUST_V1] shadow run {VERSION} {now.date()}: {done} stored, {failed} failed")
    return {"stored": done, "failed": failed, "version": VERSION, "day": now.date().isoformat()}


def latest_row(vendor_id):
    from app.models.trust import TrustScoreShadow
    return (TrustScoreShadow.query.filter_by(vendor_id=vendor_id, version=VERSION)
            .order_by(TrustScoreShadow.computed_on.desc()).first())


COMPONENT_LABELS = {
    "outcomes": "Orders that went well",
    "on_time": "On-time delivery",
    "repeat_buyers": "Repeat buyers",
    "tenure": "Months active",
    "consistency": "Steady sales",
}


def pillars_for(row):
    """v1 components as display pillars (0-100 each); none below the minimum history."""
    if row is None or row.score is None:
        return []
    comps = (row.components or {}).get("components") or {}
    return [{"key": k, "label": COMPONENT_LABELS[k], "score": int(round(100 * comps[k])), "max": 100}
            for k in WEIGHTS if k in comps]


def seller_view(vendor_id):
    """What a seller sees about their own v1 score (the stored daily row, or a
    fresh computation that is not stored when there is none yet)."""
    row = latest_row(vendor_id)
    if row is not None:
        result = row.components or {}
        at = row.computed_at
    else:
        from app.models.trust import TrustScoreShadow
        result = compute(vendor_id)
        row = TrustScoreShadow(vendor_id=vendor_id, score=result["score"], tier=result["tier"],
                               components=result, version=VERSION)
        at = None
    return {
        "vendor_id": vendor_id,
        "total_trust_score": row.score,
        "trust_tier": row.tier,
        "score_max": SCORE_MAX,
        "score_version": VERSION,
        "label": NEW_LABEL if row.score is None else None,
        "pillars": pillars_for(row),
        "minimum_history": result.get("minimum_history"),
        "dispute_cap": result.get("dispute_cap"),
        "reasons": result.get("reasons") or [],
        "last_recalculated": at.isoformat() if at else None,
    }


def public_v1(vendor_id, compute_if_missing=False):
    """v1 as the public sees it once live: the latest stored daily score.
    Without a stored row, NEW (or a fresh computation, not stored, when asked)."""
    row = latest_row(vendor_id)
    if row is not None:
        score, tier = row.score, row.tier
    elif compute_if_missing:
        result = compute(vendor_id)
        score, tier = result["score"], result["tier"]
    else:
        score, tier = None, NEW_TIER
    return {"score": score, "tier": tier, "score_max": SCORE_MAX, "version": VERSION,
            "label": NEW_LABEL if score is None else None}
