"""
AI Trust Report for /check (Siiqo 2.0, Phase 3 item 3-2;
docs/AI_ORCHESTRATION_OPTIONS.md §2 row #2).

    evidence = gather(storefront)      # code: verification, score, orders, disputes, tenure
    report   = verdict(evidence)       # code: rules decide the verdict and the Safe Pay advice
    text     = narrative(storefront)   # AI words it; a code critic checks it; template fallback

The verdict is synchronous and never calls the AI. The narrative is fetched
separately, cached in Postgres by evidence hash (``trust_report_narratives``)
and regenerated only when the evidence changes.

Wording rules: verdicts are neutral. Nothing here (or in the AI text, which the
critic checks) says "scam", "fraud", "guaranteed" or "safe" ("Safe Pay", the
product name, is the only exception). Safe Pay holds money only for physical
orders; digital products, services and tickets are paid out when the buyer pays.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from app.extensions import db

log = logging.getLogger(__name__)

ACTION = "trust_report"
PROMPT_REF = "trust_report@v1"
TEMPLATE_RETRY_HOURS = 6
MAX_ATTEMPTS = 2          # loop limit: 1 retry

# ── Verdict rules (code, not AI) ───────────────────────────────────────────
RULES = {
    "early_below_orders": 5,          # fewer completed orders than this: early record
    "established_min_orders": 20,
    "established_min_days": 180,
    "established_tiers": ("SILVER", "GOLD", "PLATINUM"),
    "mixed_fault_per_100": 2.0,       # seller-fault disputes per 100 completed orders
    "mixed_open_disputes": 2,         # this many disputes open at once
    # A low tier means a poor record only on Trust Score v1 (0-100, built from outcomes and
    # dispute caps). On the original 0-1000 score a low tier just means little activity yet.
    "mixed_tiers": ("BRONZE",),
    "mixed_tiers_score_max": 100,
}

VERDICTS = {
    "established": ("Established seller with a good record", "positive"),
    "good": ("Good record so far", "positive"),
    "early": ("Early record — only a few orders so far", "neutral"),
    "building": ("Building a record — no trust score yet", "neutral"),
    "new": ("New seller — no history yet", "neutral"),
    "mixed": ("Mixed record — use Safe Pay", "caution"),
}
# A mixed record for a seller whose sales Safe Pay cannot hold must not say "use Safe Pay".
MIXED_LABELS = {
    "physical": "Mixed record — use Safe Pay",
    "mixed": "Mixed record — use Safe Pay for physical orders",
    "not_held": "Mixed record — take care before you pay",
}

SAFE_PAY_ADVICE = {
    "physical": ("Pay through Siiqo checkout or a Siiqo Pay Link. For physical orders, Safe Pay holds "
                 "your money until you confirm delivery, or 72 hours after the seller marks it delivered "
                 "if you don't report a problem."),
    "mixed": ("Pay through Siiqo checkout or a Siiqo Pay Link. Safe Pay holds your money until you confirm "
              "delivery (or 72 hours after the seller marks it delivered) for physical orders only. Digital products, services and tickets are paid to the "
              "seller when you pay."),
    "not_held": ("This seller sells digital products or services. Safe Pay does not hold these payments: "
                 "the seller is paid as soon as you pay."),
}
SAFE_PAY_CAUTION = {
    "physical": "Avoid paying by direct bank transfer.",
    "mixed": "Avoid paying by direct bank transfer.",
    "not_held": "Pay only an amount you are ready to pay up front.",
}

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else (plural or word + 's')}"


# ── Evidence (code, read only) ─────────────────────────────────────────────

def dispute_counts(vendor_id):
    """{"opened", "seller_fault", "open_now"} from trust_evidence and dispute_cases.
    Counted per order. A reviewer's decision wins over the escrow-side
    "dispute_lost" guess (same rule as trust_v1.gather_orders)."""
    from app.models.dispute import DisputeCase, DisputeStatus
    from app.models.telemetry import TrustEvidence

    opened, lost, decided = set(), set(), {}
    rows = (db.session.query(TrustEvidence.evidence_type, TrustEvidence.source_id, TrustEvidence.properties)
            .filter(TrustEvidence.business_id == vendor_id,
                    TrustEvidence.status == "ACTIVE",
                    TrustEvidence.evidence_type.in_(("dispute_opened", "dispute_lost", "dispute_resolved")))
            .all())
    for etype, source_id, props in rows:
        props = props or {}
        try:
            if etype == "dispute_resolved":
                order_id = int(props["order_id"])
                if "seller_fault" in props:
                    decided[order_id] = bool(props.get("seller_fault"))
                else:
                    decided[order_id] = str(props.get("fault") or "").lower() == "seller"
            else:
                order_id = int(source_id)
        except (KeyError, TypeError, ValueError):
            continue
        opened.add(order_id)
        if etype == "dispute_lost":
            lost.add(order_id)

    open_now = 0
    cases = (db.session.query(DisputeCase.order_id, DisputeCase.status, DisputeCase.seller_fault)
             .filter(DisputeCase.vendor_id == vendor_id).all())
    for order_id, status, seller_fault in cases:
        opened.add(order_id)
        if status in DisputeStatus.ACTIVE:
            open_now += 1
        elif status == DisputeStatus.RESOLVED and seller_fault is not None and order_id not in decided:
            decided[order_id] = bool(seller_fault)

    fault = {o for o in opened | lost if decided.get(o, o in lost)}
    return {"opened": len(opened), "seller_fault": len(fault), "open_now": open_now}


def product_mix(sf):
    """"physical", "not_held" (digital/service/tickets only) or "mixed" (both, or nothing listed)."""
    from app.models.payment_link import PaymentLink
    from app.models.product import Product

    types = {(t or "physical").lower() for (t,) in
             db.session.query(Product.product_type)
             .filter(Product.storefront_id == sf.id, Product.is_active.is_(True)).distinct().all()}
    types |= {(t or "service").lower() for (t,) in
              db.session.query(PaymentLink.product_type)
              .filter(PaymentLink.vendor_id == sf.vendor_id, PaymentLink.status == "ACTIVE").distinct().all()}
    physical = "physical" in types
    other = bool(types - {"physical"})
    if physical and not other:
        return "physical"
    if other and not physical:
        return "not_held"
    return "mixed"


def gather(sf, now=None):
    """Everything the rules use. Public claims come from the Trust Passport
    (Trust Score v1 when TRUST_SCORE_V1_LIVE is on, otherwise the old score;
    no default score). ``*_public`` records the seller's Passport choices: the
    rules use every fact, but hidden ones are never shown or sent to the AI."""
    from app.services import passport as passport_svc

    now = now or _now()
    claims = passport_svc.build_passport(sf)
    created = _aware(sf.vendor.created_at) if sf.vendor else None
    return {
        "vendor_id": sf.vendor_id,
        "store_slug": sf.store_slug,
        "store_name": sf.store_name,
        "verified": bool(claims.get("verified")),
        "business_registered": bool(claims.get("business_registered")),
        "tier": (claims.get("tier") or "NEW").upper(),
        "score": claims.get("score"),
        "score_max": claims.get("score_max"),
        "completed_orders": passport_svc.completed_orders(sf.vendor_id),
        "completed_orders_public": "completed_orders" in claims,
        "member_since": created.date() if created else None,
        "member_since_public": "member_since" in claims,
        "tenure_days": max(0, (now - created).days) if created else 0,
        "disputes": dispute_counts(sf.vendor_id),
        # No buyer-report table exists yet; when one does, its count goes here.
        "reports": None,
        "sells": product_mix(sf),
    }


# ── Verdict (rules) ────────────────────────────────────────────────────────

def fault_rate_per_100(ev):
    faults = ev["disputes"]["seller_fault"]
    if not faults:
        return 0.0
    return faults * 100.0 / max(ev["completed_orders"], 1)


def verdict_code(ev):
    R = RULES
    d = ev["disputes"]
    done = ev["completed_orders"]
    earned_tier = ev["tier"] if ev.get("score") is not None else "NEW"
    if (d["seller_fault"] and fault_rate_per_100(ev) >= R["mixed_fault_per_100"]) \
            or d["open_now"] >= R["mixed_open_disputes"] \
            or (done > 0 and earned_tier in R["mixed_tiers"]
                and ev.get("score_max") == R["mixed_tiers_score_max"]):
        return "mixed"
    if done == 0:
        return "new"
    if done < R["early_below_orders"]:
        return "early"
    if earned_tier == "NEW":
        return "building"
    if done >= R["established_min_orders"] and ev["tenure_days"] >= R["established_min_days"] \
            and d["open_now"] == 0 and earned_tier in R["established_tiers"]:
        return "established"
    return "good"


def _month_year(d):
    return f"{_MONTHS[d.month - 1]} {d.year}"


def reasons(ev):
    """[{"code", "text"}], public facts only."""
    out = []
    if ev["verified"]:
        out.append({"code": "verified", "text": "Identity verified by Siiqo"})
    else:
        out.append({"code": "not_verified", "text": "Identity not verified"})
    if ev["business_registered"]:
        out.append({"code": "business_registered", "text": "Registered business"})
    if ev["completed_orders_public"]:
        n = ev["completed_orders"]
        out.append({"code": "orders", "text": f"{_plural(n, 'completed order')} on Siiqo" if n
                    else "No completed orders on Siiqo yet"})
    else:
        out.append({"code": "orders_hidden", "text": "The seller keeps their order count private"})
    d = ev["disputes"]
    if d["seller_fault"]:
        out.append({"code": "seller_fault_disputes",
                    "text": f"{_plural(d['seller_fault'], 'dispute')} decided against the seller"})
    elif d["opened"] - d["open_now"] > 0:
        out.append({"code": "no_seller_fault_disputes", "text": "No disputes decided against the seller"})
    else:
        out.append({"code": "no_disputes", "text": "No disputes"})
    if d["open_now"]:
        out.append({"code": "open_disputes", "text": f"{_plural(d['open_now'], 'dispute')} open now"})
    if ev["member_since_public"] and ev["member_since"]:
        out.append({"code": "tenure", "text": f"On Siiqo since {_month_year(ev['member_since'])}"})
    if ev.get("score") is not None:
        out.append({"code": "tier", "text": f"Trust tier {ev['tier']} ({ev['score']}/{ev['score_max']})"})
    else:
        out.append({"code": "no_score", "text": "Not enough history for a trust score yet"})
    return out


def safe_pay(code, sells):
    advice = SAFE_PAY_ADVICE[sells]
    if code == "mixed":
        advice = f"{advice} {SAFE_PAY_CAUTION[sells]}"
    return {
        "applies": sells,                       # physical | mixed | not_held
        "holds_money": sells != "not_held",     # Safe Pay can hold this seller's physical orders
        "use_siiqo": True,                      # always pay through Siiqo, never by direct transfer
        "emphasis": code == "mixed",
        "advice": advice,
    }


def facts_line(ev):
    """The templated sentence, e.g. "Verified · 31 completed orders · 0 seller-fault disputes"."""
    parts = ["Verified" if ev["verified"] else "Not verified"]
    if ev["business_registered"]:
        parts.append("Registered business")
    if ev["completed_orders_public"]:
        parts.append(_plural(ev["completed_orders"], "completed order"))
    parts.append(_plural(ev["disputes"]["seller_fault"], "seller-fault dispute"))
    if ev["disputes"]["open_now"]:
        parts.append(f"{_plural(ev['disputes']['open_now'], 'dispute')} open")
    if ev["member_since_public"] and ev["member_since"]:
        parts.append(f"On Siiqo since {_month_year(ev['member_since'])}")
    if ev.get("score") is not None:
        parts.append(f"Trust tier {ev['tier']} ({ev['score']}/{ev['score_max']})")
    else:
        parts.append("No trust score yet")
    return " · ".join(parts)


def verdict(ev):
    code = verdict_code(ev)
    label, tone = VERDICTS[code]
    if code == "mixed":
        label = MIXED_LABELS[ev["sells"]]
    return {
        "store_slug": ev["store_slug"],
        "store_name": ev["store_name"],
        "verdict": {"code": code, "label": label, "tone": tone},
        "reasons": reasons(ev),
        "safe_pay": safe_pay(code, ev["sells"]),
        "facts_line": facts_line(ev),
        "evidence_hash": evidence_hash(ev),
        "passport_url": f"/id/{ev['store_slug']}",
    }


# ── What the AI sees (public facts only) and its hash ──────────────────────

def ai_evidence(ev):
    code = verdict_code(ev)
    label = MIXED_LABELS[ev["sells"]] if code == "mixed" else VERDICTS[code][0]
    out = {
        "store_name": ev["store_name"],
        "verdict": label,
        "identity_verified": ev["verified"],
        "registered_business": ev["business_registered"],
        "trust_tier": ev["tier"] if ev.get("score") is not None else "NEW",
        "trust_score": ev.get("score"),
        "trust_score_max": ev.get("score_max") if ev.get("score") is not None else None,
        "disputes": {"seller_fault": ev["disputes"]["seller_fault"], "open_now": ev["disputes"]["open_now"]},
        "sells": {"physical": "physical goods", "not_held": "digital products or services",
                  "mixed": "physical goods and digital products or services"}[ev["sells"]],
    }
    if ev["completed_orders_public"]:
        out["completed_orders"] = ev["completed_orders"]
    if ev["member_since_public"] and ev["member_since"]:
        out["on_siiqo_since"] = _month_year(ev["member_since"])
    return out


def evidence_hash(ev):
    payload = {"prompt": PROMPT_REF, "slug": ev["store_slug"], "evidence": ai_evidence(ev)}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def template_text(ev):
    return f"{ev['store_name']}: {facts_line(ev)}."


# ── Critic (code) ──────────────────────────────────────────────────────────

NEVER_SAY_RE = re.compile(
    r"\b(scam\w*|fraud\w*|guarantee\w*|safe|safely|safer|safest|safety|legit\w*|risk[- ]free|"
    r"trustworthy|untrustworthy|honest|dishonest|reliable|unreliable|recommend\w*|escrow\w*|refund\w*|"
    r"promise\w*|definitely|certainly)\b|100\s?%",
    re.IGNORECASE,
)
_SAFE_PAY_RE = re.compile(r"\bSafe Pay\b")
_NUMBER_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_NUMBER_WORDS = {
    "zero": 0, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000, "dozen": 12,
    "dozens": 12, "hundreds": 100, "thousands": 1000,
}
_NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.IGNORECASE)
_MONTH_RE = re.compile(r"\b(January|February|March|April|June|July|August|September|October|November|"
                       r"December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\b|\bMay\s+\d{4}\b")
_TIER_RE = re.compile(r"\b(platinum|gold|silver|bronze)\b", re.IGNORECASE)
_NEGATION_RE = re.compile(r"(\bnot\b|n't\b|\bnever\b|\bno\b|\byet to\b)", re.IGNORECASE)
_NO_DISPUTES_RE = re.compile(r"\b(no|zero|without( any)?|free of)\b[\w\s-]{0,20}\bdisputes?\b", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"[.!?](?=\s|$)")
MAX_CHARS = 600
MAX_SENTENCES = 4


def _allowed_numbers(evidence):
    nums = set()

    def walk(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            nums.add(float(v))
        elif isinstance(v, str):
            for m in _NUMBER_RE.findall(v):
                try:
                    nums.add(float(m.replace(",", "")))
                except ValueError:
                    pass
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
    walk(evidence)
    return nums


def _claim_problems(text, word_re, truth, what):
    problems = []
    for m in word_re.finditer(text):
        window = text[max(0, m.start() - 25):m.start()]
        negated = bool(_NEGATION_RE.search(window))
        if truth and negated:
            problems.append(f"says the seller is not {what}, but they are")
        if not truth and not negated:
            problems.append(f"says the seller is {what}, but they are not")
    return problems


def critique(text, evidence):
    """Problems with an AI narrative, [] when it passes. Every number, month,
    tier and verification claim must match ``evidence`` (the AI's input)."""
    if not isinstance(text, str) or not text.strip():
        return ["empty report"]
    text = text.strip()
    problems = []
    if len(text) > MAX_CHARS:
        problems.append(f"too long ({len(text)} > {MAX_CHARS} chars)")
    if len(_SENTENCE_RE.findall(text)) > MAX_SENTENCES:
        problems.append("too many sentences")

    banned = NEVER_SAY_RE.search(_SAFE_PAY_RE.sub("", text))
    if banned:
        problems.append(f"uses the word {banned.group(0)!r}")

    allowed = _allowed_numbers(evidence)
    for m in _NUMBER_RE.findall(text):
        try:
            value = float(m.replace(",", "").rstrip("."))
        except ValueError:
            continue
        if value not in allowed:
            problems.append(f"number {m!r} is not in the evidence")
    for m in _NUMBER_WORD_RE.findall(text):
        if float(_NUMBER_WORDS[m.lower()]) not in allowed:
            problems.append(f"number word {m!r} is not in the evidence")

    # Months, tiers and claims are read with the store name taken out ("Gold Mart" is a name).
    named = text.replace(evidence.get("store_name") or "\0", "")
    since = evidence.get("on_siiqo_since") or ""
    for m in _MONTH_RE.finditer(named):
        word = m.group(0).split()[0]
        if not since or not since.lower().startswith(word.lower()[:3]):
            problems.append(f"month {word!r} is not in the evidence")

    tier = (evidence.get("trust_tier") or "").lower()
    for m in _TIER_RE.findall(named):
        if m.lower() != tier:
            problems.append(f"tier {m!r} does not match the evidence")

    problems += _claim_problems(named, re.compile(r"\bverified\b", re.IGNORECASE),
                                bool(evidence.get("identity_verified")), "verified")
    problems += _claim_problems(named, re.compile(r"\bregistered\b", re.IGNORECASE),
                                bool(evidence.get("registered_business")), "a registered business")
    disputes = evidence.get("disputes") or {}
    if (disputes.get("seller_fault") or disputes.get("open_now")) and _NO_DISPUTES_RE.search(text):
        problems.append("says there are no disputes, but there are")
    return problems


# ── Narrative (AI, cached by evidence hash) ────────────────────────────────

def get_ai_client():
    """The Anthropic client handed to the gateway. None = the gateway's
    default. Tests monkeypatch this to return a fake client."""
    return None


def daily_narrative_limit():
    try:
        return max(1, int(os.environ.get("TRUST_REPORT_NARRATIVES_PER_DAY", "5")))
    except ValueError:
        return 5


def ai_available():
    """True when the gateway would run this action (kill switch read once, fail closed)."""
    from app.services.ai import kill_switch_state
    try:
        enabled, _ = kill_switch_state(ACTION)
    except Exception:  # noqa: BLE001 - settings unreadable: treat as off
        db.session.rollback()
        return False
    return enabled


def _cached(vendor_id, digest):
    from app.models.trust_report import TrustReportNarrative
    return TrustReportNarrative.query.filter_by(vendor_id=vendor_id, evidence_hash=digest).first()


def _fresh(row, now):
    if row is None:
        return False
    if row.source == "ai":
        return True
    return _aware(row.updated_at) > now - timedelta(hours=TEMPLATE_RETRY_HOURS)


def _store(vendor_id, digest, source, text, model=None, attempts=0):
    from app.models.trust_report import TrustReportNarrative
    now = _now()
    row = _cached(vendor_id, digest)
    if row is None:
        row = TrustReportNarrative(vendor_id=vendor_id, evidence_hash=digest, created_at=now)
        db.session.add(row)
    row.prompt_version = PROMPT_REF.split("@")[1]
    row.source, row.text, row.model, row.attempts, row.updated_at = source, text, model, attempts, now
    try:
        db.session.commit()
    except IntegrityError:          # another request stored it first
        db.session.rollback()


def generate(ev, evidence, digest):
    """One AI call, the critic, one retry, then the template. Returns (text, source)."""
    from app.services.ai import AICapExceeded, AIDisabled, AIError, AIUnavailable, run as ai_run

    db.session.commit()   # the gateway must be called with a clean session
    last = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            result = ai_run(ACTION, None, evidence, client=get_ai_client())
        except (AIDisabled, AICapExceeded) as exc:
            log.info("[TRUST REPORT] AI off for %s: %s", ev["store_slug"], exc.reason or exc)
            return template_text(ev), "template"
        except AIUnavailable as exc:
            if exc.reason in ("no_api_key", "sdk_missing"):
                return template_text(ev), "template"
            last = f"unavailable: {exc.reason}"
            continue
        except AIError as exc:
            last = f"{type(exc).__name__}: {getattr(exc, 'reason', None) or exc}"
            continue
        text = ((result.data or {}).get("report") or "").strip()
        problems = critique(text, evidence)
        if not problems:
            _store(ev["vendor_id"], digest, "ai", text, model=result.served_model, attempts=attempt + 1)
            return text, "ai"
        last = "critic: " + "; ".join(problems[:4])
        log.info("[TRUST REPORT] %s attempt %s rejected: %s", ev["store_slug"], attempt + 1, problems)
    log.info("[TRUST REPORT] %s falling back to the template (%s)", ev["store_slug"], last)
    text = template_text(ev)
    _store(ev["vendor_id"], digest, "template", text, attempts=MAX_ATTEMPTS)
    return text, "template"


def narrative(sf, allow_generation=None):
    """{"narrative", "source": "ai"|"template", "cached", "evidence_hash", ...}.

    ``allow_generation`` is called (with no arguments) only when a new AI
    narrative is needed; it returns False when the caller is over its rate
    limit, and the template is returned instead."""
    ev = gather(sf)
    evidence = ai_evidence(ev)
    digest = evidence_hash(ev)
    body = {"store_slug": ev["store_slug"], "evidence_hash": digest}
    now = _now()

    row = _cached(ev["vendor_id"], digest)
    if _fresh(row, now):
        return {**body, "narrative": row.text, "source": row.source, "cached": True}
    if not ai_available():
        return {**body, "narrative": template_text(ev), "source": "template", "cached": False, "ai": "off"}
    if allow_generation is not None and not allow_generation():
        return {**body, "narrative": row.text if row is not None else template_text(ev),
                "source": row.source if row is not None else "template", "cached": row is not None,
                "rate_limited": True}
    text, source = generate(ev, evidence, digest)
    return {**body, "narrative": text, "source": source, "cached": False}
