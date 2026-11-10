"""
Ask (AI search, Phase 3 item 3-3) and Market (paginated browsing, item 3-4).

Ask, in order (AI_ORCHESTRATION_OPTIONS.md section 2, row 1):
  1. regex parse (app/services/intent_parser.py), with a confidence score;
  2. only when confidence is low: Haiku parses the text into fields
     (prompt ``search_parse``; 1 retry on invalid output; per-IP daily cap);
  3. retrieve candidates: Postgres full-text (``search_tsv`` columns) plus
     trigram matching when the Phase 3 migration has run, otherwise ILIKE
     (SQLite tests, or Postgres before the migration);
  4. rank in code: relevance, then fit (price range, location), then a
     trust boost from the public score and tier. Verified status and any
     paid plan never change the order;
  5. one Haiku call writes a reason line for each of the top results
     (prompt ``search_reasons``; 0 retries);
  6. fact check: a reason line may only use words and numbers from that
     result's own fields; a failing line is dropped.

With AI off (or failing) the answer is the keyword search without reason
lines. Answers are cached in process for 10 minutes by normalised query.

Market shows only active, non-draft products (drafts are is_active=False)
from published stores whose owner account is active, one page at a time.
"""
from __future__ import annotations

import difflib
import logging
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone

from sqlalchemy import String, and_, cast, func, or_, text
from sqlalchemy.orm import contains_eager

from app.extensions import db
from app.models.product import Category, Product
from app.models.user import Storefront, User
from app.services.intent_parser import NIGERIAN_CITIES, parse_buyer_intent

log = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────────────
CACHE_TTL_SECONDS = 600            # 10 minutes (doc)
CACHE_MAX_ENTRIES = 512
LOW_CONFIDENCE = 0.6               # below this the AI parser is asked
AI_PARSE_DAILY_CAP_PER_IP = 30     # AI_ORCHESTRATION_OPTIONS Q2: 30 search parses per day
AI_PARSE_TIMEOUT_SECONDS = 3.0      # inline in the search answer
AI_REASONS_TIMEOUT_SECONDS = 6.0    # fetched after results show; no retries
REASON_COUNT = 10                  # "top ~10"
RESULT_LIMIT = 20
PRODUCT_CANDIDATES = 150
STORE_CANDIDATES = 50
REASON_MAX_CHARS = 120
MARKET_PER_PAGE_DEFAULT = 24
MARKET_PER_PAGE_MAX = 48

# Ranking weights: relevance dominates, then fit, then trust.
W_RELEVANCE, W_FIT, W_TRUST = 0.60, 0.25, 0.15
STORE_PREFERENCE_BONUS = 0.05      # the buyer asked for a seller/shop

DIGITAL_TYPES = ("digital", "service", "event")
PRODUCT_TYPES = ("physical",) + DIGITAL_TYPES
_TIER_RANK = {"BRONZE": 1, "SILVER": 2, "GOLD": 3, "PLATINUM": 4}

STOP_WORDS = frozenset("""
a an the and or of for to in at on near around within by with from under below above over
less more than max maximum min minimum price prices cost costs budget naira ngn
i im me my we you want wants need needs looking look find get buy buying some any good
best affordable cheap cheapest nice quality please pls abeg dey wan where who which what
is are am be can could would should do does sell sells selling seller sellers vendor vendors
store stores shop shops business company around
""".split())


# ── Text helpers ────────────────────────────────────────────────────────────
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?k?", re.IGNORECASE)


def _fold(value) -> str:
    raw = "" if value is None else str(value)
    return unicodedata.normalize("NFKC", raw).lower()


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("es") and word[-3] in "sxz":
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _words(value) -> list[str]:
    return [w for w in _WORD_RE.findall(_fold(value)) if not w.isdigit()]


def _terms(keyword: str) -> list[str]:
    """Search terms: lowercase words, no stop words, no numbers, de-duplicated."""
    seen, out = set(), []
    for w in _words(keyword):
        if w in STOP_WORDS or len(w) < 2:
            continue
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:8]


def normalise_query(q: str) -> str:
    text_ = " ".join(_fold(q).split())
    return text_.strip(" .,!?;:'\"")


def _num_str(raw: str) -> str | None:
    s = raw.lower().replace(",", "")
    mult = 1
    if s.endswith("k"):
        s, mult = s[:-1], 1000
    try:
        n = float(s) * mult
    except ValueError:
        return None
    return str(int(n)) if n == int(n) else str(n)


# ── Postgres search features (detected once, re-checked every 10 minutes) ───
_features_lock = threading.Lock()
_features: dict | None = None
_features_at = 0.0


def pg_features() -> dict:
    """{"fts": bool, "trgm": bool}. Both False on SQLite or before the
    Phase 3 search migration has run, so every caller falls back to ILIKE."""
    global _features, _features_at
    with _features_lock:
        if _features is not None and time.monotonic() - _features_at < CACHE_TTL_SECONDS:
            return _features
    feats = {"fts": False, "trgm": False}
    try:
        if db.engine.dialect.name == "postgresql":
            tables = set(db.session.execute(text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND column_name = 'search_tsv' "
                "AND table_name IN ('products', 'storefronts')"
            )).scalars().all())
            feats["fts"] = {"products", "storefronts"} <= tables
            feats["trgm"] = bool(db.session.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
            ).scalar())
    except Exception:  # noqa: BLE001 - detection must never break search
        db.session.rollback()
        log.warning("search: could not detect Postgres search features; using ILIKE")
    with _features_lock:
        _features, _features_at = feats, time.monotonic()
    return feats


def reset_features() -> None:
    global _features
    with _features_lock:
        _features = None


def _tsquery(terms: list[str]) -> str:
    # Terms are \w-only words, so they are safe inside to_tsquery syntax.
    return " | ".join(f"{t}:*" for t in terms)


# ── Visibility ──────────────────────────────────────────────────────────────
def visible_products(query=None):
    """Active, non-draft, not deleted, sellable products from published
    stores whose owner account is active."""
    q = query if query is not None else Product.query
    return (
        q.join(Storefront, Product.storefront_id == Storefront.id)
        .join(User, Storefront.vendor_id == User.id)
        .filter(
            Product.is_active.is_(True),
            or_(Product.is_deleted.is_(False), Product.is_deleted.is_(None)),
            or_(Product.stock_quantity > 0, Product.product_type.in_(DIGITAL_TYPES)),
            Storefront.is_published.is_(True),
            User.is_active.is_(True),
        )
    )


def has_sellable_products():
    """SQL condition: the store has at least one active, in-stock product
    (digital, service and event listings count without stock)."""
    return (
        db.session.query(Product.id)
        .filter(
            Product.storefront_id == Storefront.id,
            Product.is_active.is_(True),
            or_(Product.is_deleted.is_(False), Product.is_deleted.is_(None)),
            or_(Product.stock_quantity > 0, Product.product_type.in_(DIGITAL_TYPES)),
        )
        .exists()
    )


def visible_stores(query=None):
    """Published stores of active owners with something to buy. Empty stores
    stay reachable at their own link; they are only left out of lists."""
    q = query if query is not None else Storefront.query
    return q.join(User, Storefront.vendor_id == User.id).filter(
        Storefront.is_published.is_(True), User.is_active.is_(True), has_sellable_products()
    )


# ── Trust and verification facts (public claims only) ───────────────────────
def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _legacy_badge_active(sf, now) -> bool:
    expiry = _aware(sf.pro_verified_expires_at)
    return bool(sf.is_pro_verified and (expiry is None or expiry > now))


def verified_vendor_ids(candidate_ids=None) -> set[int]:
    """Vendors whose Verified claim is true, by the same rule as the Trust
    Passport (app/services/passport.build_passport): a current identity check
    under an active Verified Pro plan, or an unexpired legacy badge."""
    from app.models.verification import VerificationStatus

    now = datetime.now(timezone.utc)
    q = VerificationStatus.query.filter(VerificationStatus.identity_verified_at.isnot(None))
    if candidate_ids is not None:
        ids = list(candidate_ids)
        if not ids:
            return set()
        q = q.filter(VerificationStatus.user_id.in_(ids))
    out = {s.user_id for s in q.all() if s.public_flags(now)["identity_verified"]}
    lq = Storefront.query.filter(Storefront.is_pro_verified.is_(True))
    if candidate_ids is not None:
        lq = lq.filter(Storefront.vendor_id.in_(list(candidate_ids)))
    out |= {sf.vendor_id for sf in lq.all() if _legacy_badge_active(sf, now)}
    return out


def trust_facts(stores) -> dict[int, dict]:
    """{vendor_id: {"verified", "score", "score_max", "tier"}} for the given
    storefronts. Scores come from trust_score_or_default / trust_score_max,
    shown only when earned (Passport rule: no made-up score; a seller without
    completed orders, or below the v1 minimum history, is NEW)."""
    from app.models.order import Order
    from app.services import trust_v1

    by_vendor = {sf.vendor_id: sf for sf in stores if sf is not None and sf.vendor is not None}
    ids = list(by_vendor)
    if not ids:
        return {}
    verified = verified_vendor_ids(ids)
    live = trust_v1.is_live()
    done = {}
    if not live:
        done = dict(
            db.session.query(Order.vendor_id, func.count(Order.id))
            .filter(Order.vendor_id.in_(ids), Order.status == "COMPLETED")
            .group_by(Order.vendor_id)
            .all()
        )
    out = {}
    for vid, sf in by_vendor.items():
        user = sf.vendor
        if live:
            score = user.trust_score_or_default            # None below minimum history
            tier = user.trust_tier_or_default if score is not None else "NEW"
        else:
            earned = bool(user.trust_profile is not None and done.get(vid, 0) > 0)
            score = int(user.trust_score_or_default) if earned else None
            tier = user.trust_tier_or_default if earned else "NEW"
        out[vid] = {
            "verified": vid in verified,
            "score": score,
            "score_max": user.trust_score_max,
            "tier": tier or "NEW",
        }
    return out


def storefront_trust(sf, with_label: bool = True) -> dict:
    """Trust fields for product and store payloads, identical to the Market
    card (trust_facts) plus the Check-a-seller verdict label. No made-up
    default: a seller with no earned score is tier "NEW" with score None."""
    facts = trust_facts([sf]).get(getattr(sf, "vendor_id", None)) if sf is not None else None
    facts = facts or {}
    label = None
    if with_label and sf is not None and sf.vendor is not None:
        try:
            from app.services import trust_report
            label = trust_report.verdict(trust_report.gather(sf))["verdict"]["label"]
        except Exception as e:  # noqa: BLE001 - a label must never break the page
            log.warning("trust label skipped for store %s: %s", getattr(sf, "id", None), e)
    return {
        "trust_score": facts.get("score"),
        "trust_score_max": facts.get("score_max") or (sf.vendor.trust_score_max if sf is not None and sf.vendor else 100),
        "trust_tier": facts.get("tier") or "NEW",
        "trust_label": label,
    }


def trust_boost(facts: dict | None) -> float:
    """0..1 from the public score and tier only. NEW (no score) gets 0."""
    if not facts or facts.get("score") is None:
        return 0.0
    score_max = facts.get("score_max") or 1000
    norm = max(0.0, min(1.0, float(facts["score"]) / float(score_max)))
    tier = _TIER_RANK.get(str(facts.get("tier") or "").upper(), 0) / 4
    return round(0.8 * norm + 0.2 * tier, 4)


def _labels(facts: dict | None) -> dict:
    facts = facts or {}
    return {
        "on_siiqo": True,
        "verified": bool(facts.get("verified")),
        "trust": {
            "score": facts.get("score"),
            "score_max": facts.get("score_max") or 1000,
            "tier": facts.get("tier") or "NEW",
        },
    }


# ── Result shapes ───────────────────────────────────────────────────────────
def _price_str(p) -> str:
    return str(p.price) if p.price is not None else "0"


def product_card(p, facts: dict | None) -> dict:
    sf = p.storefront
    images = p.images or []
    return {
        "kind": "product",
        "id": f"p{p.id}",
        "product_id": p.id,
        "name": p.name,
        "price": _price_str(p),
        "currency": p.currency or "NGN",
        "image": images[0] if images else None,
        "category": p.category.name if p.category else None,
        "product_type": p.product_type or "physical",
        "condition": p.condition,
        "store": {
            "name": sf.store_name if sf else None,
            "slug": sf.store_slug if sf else None,
            "city": _place(sf)[0],
            "state": _place(sf)[1],
        },
        "href": f"/products/{p.id}",
        "store_href": f"/{sf.store_slug}" if sf else None,
        "labels": _labels(facts),
    }


def store_card(sf, facts: dict | None) -> dict:
    desc = (sf.store_description or "").strip()
    return {
        "kind": "store",
        "id": f"s{sf.id}",
        "store_id": sf.id,
        "name": sf.store_name,
        "image": sf.store_logo,
        "description": (desc[:157] + "...") if len(desc) > 160 else (desc or None),
        "store": {"name": sf.store_name, "slug": sf.store_slug, "city": _place(sf)[0], "state": _place(sf)[1]},
        "href": f"/{sf.store_slug}",
        "store_href": f"/{sf.store_slug}",
        "labels": _labels(facts),
    }


def _product_fields(p) -> dict:
    sf = p.storefront
    attrs = p.attributes or {}
    if isinstance(attrs, dict):
        attr_text = " ".join(f"{k} {v}" for k, v in attrs.items())
    elif isinstance(attrs, list):
        attr_text = " ".join(
            " ".join(str(x) for x in (a.values() if isinstance(a, dict) else [a])) for a in attrs
        )
    else:
        attr_text = str(attrs)
    return {
        "name": p.name or "",
        "category": p.category.name if p.category else "",
        "store": sf.store_name if sf else "",
        "attributes": attr_text,
        "description": p.description or "",
        "place": " ".join(filter(None, [sf.city if sf else None, sf.state if sf else None, p.location])),
    }


def _store_fields(sf) -> dict:
    return {
        "name": sf.store_name or "",
        "category": "",
        "store": sf.store_name or "",
        "attributes": "",
        "description": sf.store_description or "",
        "place": " ".join(filter(None, [sf.city, sf.state, sf.address])),
    }


# ── Parsing ─────────────────────────────────────────────────────────────────
_QUESTION_WORDS = {"who", "where", "which", "what", "how", "abeg", "dey", "wan", "una", "biko", "ejo"}
_PRICE_WORDS = {"k", "naira", "ngn", "thousand", "million", "budget"}


def regex_parse(q: str) -> tuple[dict, float]:
    """The existing regex parser, reshaped, with a confidence score in 0..1."""
    raw = parse_buyer_intent(q)
    terms = _terms(raw.get("keyword") or "")
    intent = {
        "keyword": " ".join(terms),
        "city": raw.get("city"),
        "min_price": raw.get("min_price"),
        "max_price": raw.get("max_price"),
        "category": raw.get("category_hint"),
        "looking_for": "store" if raw.get("entity_preference") == "vendor" else "any",
    }
    words = _words(q)
    conf = 1.0
    if len(terms) > 3:
        conf -= 0.4                      # long free text: the regex keeps every word
    if not terms and not intent["city"]:
        conf -= 0.5                      # nothing usable found
    if any(w in _QUESTION_WORDS for w in words) or "?" in (q or ""):
        conf -= 0.3                      # conversational or Pidgin phrasing
    has_number = bool(re.search(r"\d", q or ""))
    if (has_number or any(w in _PRICE_WORDS for w in words)) and intent["max_price"] is None:
        conf -= 0.3                      # a budget the regex did not catch
    return intent, round(max(0.0, conf), 2)


def _known_city(value) -> str | None:
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if v.lower() in NIGERIAN_CITIES:
        return NIGERIAN_CITIES[v.lower()]
    if re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,39}", v):
        return " ".join(part.capitalize() for part in v.split())
    return None


def _clean_ai_intent(data: dict, fallback: dict) -> dict | None:
    """Code critic for the AI parse. Returns None when unusable."""
    if not isinstance(data, dict):
        return None
    terms = _terms(str(data.get("keyword") or "")[:60])
    if not terms:
        return None

    def price(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return float(v) if 0 < v <= 1_000_000_000 else None

    lo, hi = price(data.get("min_price_ngn")), price(data.get("max_price_ngn"))
    if lo is not None and hi is not None and lo > hi:
        lo, hi = None, None
    cat = data.get("category")
    cat = cat.strip()[:60] if isinstance(cat, str) and cat.strip() else None
    looking = data.get("looking_for") if data.get("looking_for") in ("product", "store") else "any"
    return {
        "keyword": " ".join(terms),
        "city": _known_city(data.get("city")) or fallback.get("city"),
        "min_price": lo,
        "max_price": hi if hi is not None else fallback.get("max_price"),
        "category": cat or fallback.get("category"),
        "looking_for": looking,
    }


# Per-IP daily counter for AI parses (in process; resets per day and on restart).
_ip_lock = threading.Lock()
_ip_parses: dict[str, tuple[str, int]] = {}


def _take_ai_parse_slot(ip: str) -> bool:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _ip_lock:
        if len(_ip_parses) > 10_000:
            for k in [k for k, (d, _) in _ip_parses.items() if d != day]:
                _ip_parses.pop(k, None)
        d, n = _ip_parses.get(ip or "?", (day, 0))
        if d != day:
            n = 0
        if n >= AI_PARSE_DAILY_CAP_PER_IP:
            return False
        _ip_parses[ip or "?"] = (day, n + 1)
        return True


def get_ai_client():
    """The Anthropic client handed to the gateway. None = the gateway's default.
    Tests monkeypatch this to return a fake client."""
    return None


def ai_parse(q: str, ip: str, fallback: dict) -> tuple[dict | None, str]:
    """(intent or None, status). Status: ok | off | capped | failed."""
    from app.services.ai import AIDisabled, AIError, AIInvalidOutput, run

    if not _take_ai_parse_slot(ip):
        return None, "capped"
    for attempt in range(2):                         # 1 retry on invalid output
        try:
            res = run("search_parse", None, {"query": (q or "")[:300]},
                      client=get_ai_client(), timeout=AI_PARSE_TIMEOUT_SECONDS)
        except AIDisabled:
            return None, "off"
        except AIInvalidOutput:
            if attempt == 0:
                continue
            return None, "failed"
        except AIError as exc:
            log.info("search: AI parse unavailable (%s)", getattr(exc, "reason", exc))
            return None, "failed"
        cleaned = _clean_ai_intent(res.data, fallback)
        return (cleaned, "ok") if cleaned else (None, "failed")
    return None, "failed"


# ── Retrieval ───────────────────────────────────────────────────────────────
def _place(sf) -> tuple:
    """(city, state) for buyers: one spelling per state (places.clean_place)."""
    if not sf:
        return None, None
    from app.services.places import clean_place
    return clean_place(sf.city, sf.state)


def _place_likes(city: str) -> list[str]:
    """LIKE patterns for a place: a state matches every way sellers spelt it."""
    from app.services.places import place_terms
    return [f"%{t}%" for t in (place_terms(city) or [city])]


def _city_filter_products(city: str):
    return or_(*[c.ilike(like) for like in _place_likes(city)
                 for c in (Storefront.city, Storefront.state, Product.location)])


def _city_filter_stores(city: str):
    return or_(*[c.ilike(like) for like in _place_likes(city)
                 for c in (Storefront.city, Storefront.state)])


def _product_match(terms: list[str], feats: dict):
    if feats.get("fts"):
        conds = [
            text("products.search_tsv @@ to_tsquery('simple', :tsq)"),
            text("storefronts.search_tsv @@ to_tsquery('simple', :tsq)"),
        ]
        conds += [Category.name.ilike(f"%{t}%") for t in terms]
        if feats.get("trgm"):
            conds.append(text("word_similarity(:kw, products.name) > 0.5"))
        return or_(*conds)
    conds = []
    for t in terms:
        like = f"%{t}%"
        conds += [
            Product.name.ilike(like), Product.description.ilike(like),
            cast(Product.attributes, String).ilike(like), Category.name.ilike(like),
            Storefront.store_name.ilike(like), Storefront.city.ilike(like),
            Storefront.state.ilike(like),
        ]
    return or_(*conds)


def _store_match(terms: list[str], feats: dict):
    if feats.get("fts"):
        conds = [text("storefronts.search_tsv @@ to_tsquery('simple', :tsq)")]
        if feats.get("trgm"):
            conds.append(text("word_similarity(:kw, storefronts.store_name) > 0.5"))
        return or_(*conds)
    conds = []
    for t in terms:
        like = f"%{t}%"
        conds += [Storefront.store_name.ilike(like), Storefront.store_description.ilike(like),
                  Storefront.city.ilike(like), Storefront.state.ilike(like)]
    return or_(*conds)


def _base_product_query():
    return (
        visible_products(Product.query)
        .outerjoin(Category, Product.category_id == Category.id)
        .options(contains_eager(Product.storefront).contains_eager(Storefront.vendor),
                 contains_eager(Product.category))
    )


def _apply_product_filters(q, f: dict):
    if f.get("max_price") is not None:
        q = q.filter(Product.currency == "NGN", Product.price <= f["max_price"])
    if f.get("min_price") is not None:
        q = q.filter(Product.currency == "NGN", Product.price >= f["min_price"])
    if f.get("verified"):
        q = q.filter(Storefront.vendor_id.in_(list(verified_vendor_ids()) or [-1]))
    ptype = f.get("type")
    if ptype == "physical":
        q = q.filter(or_(Product.product_type == "physical", Product.product_type.is_(None),
                         Product.product_type == ""))
    elif ptype in DIGITAL_TYPES:
        q = q.filter(Product.product_type == ptype)
    return q


def _run_with_fallback(build, params: dict, feats: dict, limit: int):
    """Run a candidate query with Postgres search; on any SQL error retry it with ILIKE."""
    try:
        q = build(feats)
        if feats.get("fts"):
            q = q.params(**params)
        return q.limit(limit).all()
    except Exception:  # noqa: BLE001
        if not feats.get("fts"):
            raise
        db.session.rollback()
        log.warning("search: Postgres full-text query failed; falling back to ILIKE", exc_info=True)
        return build({"fts": False, "trgm": False}).limit(limit).all()


def retrieve(intent: dict, filters: dict) -> tuple[list, list]:
    terms = _terms(intent.get("keyword") or "")
    city = intent.get("city")
    feats = pg_features()
    params = {"tsq": _tsquery(terms), "kw": " ".join(terms)}
    products, stores = [], []
    want_products = intent.get("looking_for") != "store" or filters.get("type")
    want_stores = intent.get("looking_for") != "product" and not filters.get("type") and \
        intent.get("min_price") is None and intent.get("max_price") is None

    if terms or city:
        if want_products:
            def build_p(fs):
                q = _apply_product_filters(_base_product_query(), {**filters, **{
                    "max_price": intent.get("max_price"), "min_price": intent.get("min_price")}})
                if terms:
                    q = q.filter(_product_match(terms, fs))
                elif city:
                    q = q.filter(_city_filter_products(city))
                return q.order_by(Product.created_at.desc(), Product.id.desc())
            products = _run_with_fallback(build_p, params, feats, PRODUCT_CANDIDATES)
        if want_stores:
            def build_s(fs):
                q = visible_stores(Storefront.query).options(contains_eager(Storefront.vendor))
                if filters.get("verified"):
                    q = q.filter(Storefront.vendor_id.in_(list(verified_vendor_ids()) or [-1]))
                if terms:
                    q = q.filter(_store_match(terms, fs))
                elif city:
                    q = q.filter(_city_filter_stores(city))
                return q.order_by(Storefront.created_at.desc(), Storefront.id.desc())
            stores = _run_with_fallback(build_s, params, feats, STORE_CANDIDATES)
    return products, stores


# ── Ranking (code only) ─────────────────────────────────────────────────────
_FIELD_WEIGHTS = (("name", 1.0), ("category", 0.7), ("store", 0.6), ("attributes", 0.6),
                  ("description", 0.4), ("place", 0.3))


def relevance(terms: list[str], fields: dict) -> float:
    """0..1: how well the search terms match this result's fields."""
    if not terms:
        return 0.5
    field_words = {f: {_stem(w) for w in _words(fields.get(f))} for f, _ in _FIELD_WEIGHTS}
    total = 0.0
    for t in terms:
        st = _stem(t)
        best = 0.0
        for f, weight in _FIELD_WEIGHTS:
            words = field_words[f]
            if st in words or (len(st) >= 3 and any(w.startswith(st) for w in words)):
                best = weight
                break
        if best == 0.0 and len(st) >= 4:          # typo tolerance on names only
            for f, weight in (("name", 1.0), ("store", 0.6)):
                if difflib.get_close_matches(st, list(field_words[f]), n=1, cutoff=0.8):
                    best = max(best, weight * 0.7)
        total += best
    score = total / len(terms)
    if len(terms) > 1 and " ".join(terms) in " ".join(_words(fields.get("name"))):
        score += 0.1
    return round(min(1.0, score), 4)


def fit(item: dict, intent: dict) -> float:
    """0..1: price range and location fit. 1 when the buyer set neither."""
    parts = []
    city = intent.get("city")
    if city:
        place = _fold(item.get("_place"))
        parts.append(1.0 if _fold(city) in place else 0.0)
    lo, hi = intent.get("min_price"), intent.get("max_price")
    if item["kind"] == "product" and (lo is not None or hi is not None):
        price = item.get("_price")
        ok = price is not None and item.get("currency") == "NGN" and \
            (lo is None or price >= lo) and (hi is None or price <= hi)
        parts.append(1.0 if ok else 0.0)
    return round(sum(parts) / len(parts), 4) if parts else 1.0


def rank(items: list[dict], intent: dict) -> list[dict]:
    """Final order: weighted relevance, fit and trust boost; ties by id."""
    for it in items:
        bonus = STORE_PREFERENCE_BONUS if (it["kind"] == "store" and intent.get("looking_for") == "store") else 0.0
        it["_score"] = round(W_RELEVANCE * it["_relevance"] + W_FIT * it["_fit"]
                             + W_TRUST * it["_trust"] + bonus, 6)
    return sorted(items, key=lambda it: (-it["_score"], -it["_relevance"], -it["_fit"], it["id"]))


def build_results(intent: dict, filters: dict) -> list[dict]:
    products, stores = retrieve(intent, filters)
    terms = _terms(intent.get("keyword") or "")
    facts = trust_facts([p.storefront for p in products] + list(stores))
    items = []
    for p in products:
        fields = _product_fields(p)
        rel = relevance(terms, fields)
        if terms and rel <= 0:
            continue
        card = product_card(p, facts.get(p.storefront.vendor_id))
        card.update(_relevance=rel, _fields=fields, _place=fields["place"],
                    _price=float(p.price) if p.price is not None else None,
                    _trust=trust_boost(facts.get(p.storefront.vendor_id)))
        items.append(card)
    for sf in stores:
        fields = _store_fields(sf)
        rel = relevance(terms, fields)
        if terms and rel <= 0:
            continue
        card = store_card(sf, facts.get(sf.vendor_id))
        card.update(_relevance=rel, _fields=fields, _place=fields["place"], _price=None,
                    _trust=trust_boost(facts.get(sf.vendor_id)))
        items.append(card)
    for it in items:
        it["_fit"] = fit(it, intent)
    return rank(items, intent)[:RESULT_LIMIT]


def public_result(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


# ── Reason lines and the fact check ─────────────────────────────────────────
REASON_GLUE = frozenset("""
a an the and or with in at for from of on by to per each plus also its their this
sells sold offers offered has have is are listed listing price priced item items
product products service services store shop
""".split())
REASON_BANNED = frozenset("""
verified verify verification trusted trust trustworthy guarantee guaranteed guarantees
escrow safe safely safety secure scam legit legitimate certified approved official
best cheapest fastest top number popular reviews rated rating fast delivery delivers
""".split())


def _fact_words_and_numbers(item: dict) -> tuple[set[str], set[str]]:
    fields = item.get("_fields") or {}
    texts = list(fields.values())
    texts += [item.get("name"), item.get("category"), item.get("product_type"),
              item.get("condition"), item.get("currency")]
    store = item.get("store") or {}
    texts += [store.get("name"), store.get("city"), store.get("state")]
    words = {_stem(w) for t in texts for w in _words(t)}
    numbers = set()
    for t in texts:
        for m in _NUM_RE.findall(_fold(t)):
            n = _num_str(m)
            if n:
                numbers.add(n)
    if item.get("_price") is not None:
        price = float(item["_price"])
        numbers.add(str(int(price)) if price == int(price) else str(price))
    if (item.get("currency") or "NGN") == "NGN":
        words |= {"ngn", "naira"}
    return words, numbers


def fact_check(line: str, item: dict) -> bool:
    """True when every word and number in the line comes from this result's
    own fields (plus a few connecting words), and no claim word is used."""
    if not isinstance(line, str):
        return False
    line = line.strip()
    if not line or len(line) > REASON_MAX_CHARS:
        return False
    words, numbers = _fact_words_and_numbers(item)
    folded = _fold(line)
    for m in _NUM_RE.findall(folded):
        n = _num_str(m)
        if n is None or n not in numbers:
            return False
    for w in _WORD_RE.findall(_NUM_RE.sub(" ", folded)):
        if w.isdigit():
            return False
        if w in REASON_BANNED:
            return False
        if w in REASON_GLUE or _stem(w) in words:
            continue
        return False
    return True


def _reason_inputs(item: dict) -> dict:
    fields = item.get("_fields") or {}
    store = item.get("store") or {}
    out = {
        "id": item["id"],
        "kind": item["kind"],
        "name": item.get("name"),
        "store": store.get("name"),
        "city": store.get("city"),
        "state": store.get("state"),
        "description": (fields.get("description") or "")[:300],
    }
    if item["kind"] == "product":
        out.update(price=item.get("price"), currency=item.get("currency"),
                   category=item.get("category"), product_type=item.get("product_type"),
                   condition=item.get("condition"), attributes=fields.get("attributes") or None)
    return {k: v for k, v in out.items() if v not in (None, "")}


def write_reasons(items: list[dict]) -> tuple[dict, str, int]:
    """({result_id: line}, status, dropped). Status: ok | off | failed | empty."""
    from app.services.ai import AIDisabled, AIError, run

    top = items[:REASON_COUNT]
    if not top:
        return {}, "empty", 0
    try:
        res = run("search_reasons", None, {"results": [_reason_inputs(it) for it in top]},
                  client=get_ai_client(), timeout=AI_REASONS_TIMEOUT_SECONDS)
    except AIDisabled:
        return {}, "off", 0
    except AIError as exc:                         # 0 retries: no lines this time
        log.info("search: AI reasons unavailable (%s)", getattr(exc, "reason", exc))
        return {}, "failed", 0
    by_id = {it["id"]: it for it in top}
    out, dropped = {}, 0
    for entry in (res.data or {}).get("reasons", []):
        rid, line = entry.get("id"), (entry.get("text") or "").strip()
        item = by_id.get(rid)
        if item is None or rid in out or not line:
            continue
        if fact_check(line, item):
            out[rid] = line
        else:
            dropped += 1
    return out, "ok", dropped


def reasons_enabled() -> bool:
    from app.services.ai import kill_switch_state
    try:
        return kill_switch_state("search_reasons")[0]
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return False


# ── Cache ───────────────────────────────────────────────────────────────────
class _TTLCache:
    def __init__(self, ttl: float, max_entries: int):
        self.ttl, self.max = ttl, max_entries
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            at, value = hit
            if time.monotonic() - at > self.ttl:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key, value):
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max:
                self._data.popitem(last=False)

    def clear(self):
        with self._lock:
            self._data.clear()


_cache = _TTLCache(CACHE_TTL_SECONDS, CACHE_MAX_ENTRIES)


def clear_cache() -> None:
    _cache.clear()
    with _ip_lock:
        _ip_parses.clear()


def _cache_key(q: str, filters: dict):
    return (normalise_query(q), tuple(sorted((k, v) for k, v in filters.items() if v not in (None, "", False))))


# ── Entry points ────────────────────────────────────────────────────────────
def parse_only(q: str) -> dict:
    """Instant regex parse for the intent chips (no AI, no database)."""
    intent, conf = regex_parse(q)
    return {"intent": intent, "confidence": conf}


def _answer(q: str, ip: str, filters: dict) -> dict:
    started = time.monotonic()
    intent, conf = regex_parse(q)
    source, ai_status = "regex", "not_needed"
    if conf < LOW_CONFIDENCE:
        ai_intent, ai_status = ai_parse(q, ip, intent)
        if ai_intent:
            intent, source = ai_intent, "ai"
    # Refinement chips override the parsed values.
    if filters.get("city"):
        intent["city"] = filters["city"]
    if filters.get("max_price") is not None:
        intent["max_price"] = filters["max_price"]
    if filters.get("min_price") is not None:
        intent["min_price"] = filters["min_price"]

    items = build_results(intent, filters)
    return {
        "query": q,
        "intent": intent,
        "parse": {"source": source, "confidence": conf, "ai": ai_status},
        "items": items,
        "reasons": None,                         # filled by with_reasons()
        "reasons_status": None,
        "reasons_dropped": 0,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def _public(answer: dict, *, cached: bool, reasons_on: bool) -> dict:
    reasons = answer.get("reasons") or {}
    results = []
    for it in answer["items"]:
        r = public_result(it)
        r["reason"] = reasons.get(it["id"])
        results.append(r)
    status = answer.get("reasons_status") or ("pending" if reasons_on and results else "off")
    return {
        "query": answer["query"],
        "intent": answer["intent"],
        "parse": answer["parse"],
        "results": results,
        "total": len(results),
        "zero_results": not results,
        "reasons_status": status,
        "cached": cached,
        "elapsed_ms": answer["elapsed_ms"],
    }


def ask(q: str, *, ip: str, filters: dict | None = None, with_reasons: bool = False) -> dict:
    filters = {k: v for k, v in (filters or {}).items() if v not in (None, "", False)}
    key = _cache_key(q, filters)
    answer = _cache.get(key)
    cached = answer is not None
    if answer is None:
        answer = _answer(q, ip, filters)
        _cache.set(key, answer)
    reasons_on = reasons_enabled()
    if with_reasons and reasons_on and answer.get("reasons_status") is None:
        _fill_reasons(answer)
    elif not reasons_on and answer.get("reasons_status") is None:
        answer["reasons_status"] = "off"
    return _public(answer, cached=cached, reasons_on=reasons_on)


def _fill_reasons(answer: dict) -> None:
    lines, status, dropped = write_reasons(answer["items"])
    answer["reasons"], answer["reasons_status"], answer["reasons_dropped"] = lines, status, dropped


def reasons(q: str, *, ip: str, filters: dict | None = None) -> dict:
    """Reason lines for the (cached) answer to this query."""
    filters = {k: v for k, v in (filters or {}).items() if v not in (None, "", False)}
    key = _cache_key(q, filters)
    answer = _cache.get(key)
    if answer is None:
        answer = _answer(q, ip, filters)
        _cache.set(key, answer)
    if answer.get("reasons_status") in (None, "pending"):
        if reasons_enabled():
            _fill_reasons(answer)
        else:
            answer["reasons_status"] = "off"
    return {
        "query": answer["query"],
        "reasons": answer.get("reasons") or {},
        "status": answer["reasons_status"],
        "dropped": answer.get("reasons_dropped", 0),
    }


# ── Market (server-paginated browsing) ──────────────────────────────────────
MARKET_SORTS = ("newest", "price_asc", "price_desc")


def _page_args(page, per_page) -> tuple[int, int]:
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(per_page or MARKET_PER_PAGE_DEFAULT)
    except (TypeError, ValueError):
        per_page = MARKET_PER_PAGE_DEFAULT
    return page, max(1, min(MARKET_PER_PAGE_MAX, per_page))


def find_category(value: str | None):
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    return Category.query.filter(
        or_(func.lower(Category.slug) == v.lower(), func.lower(Category.name) == v.lower())
    ).first()


def _page(total: int, page: int, per_page: int) -> dict:
    pages = (total + per_page - 1) // per_page if total else 0
    return {"total": total, "page": page, "per_page": per_page, "pages": pages,
            "has_next": page < pages, "has_prev": page > 1}


def market_products(args: dict) -> dict:
    page, per_page = _page_args(args.get("page"), args.get("per_page"))
    applied, ignored = {}, []
    q = _base_product_query()

    cat_arg = (args.get("category") or "").strip()
    if cat_arg:
        cat = find_category(cat_arg)
        if cat:
            q = q.filter(Product.category_id == cat.id)
            applied["category"] = {"slug": cat.slug, "name": cat.name}
        else:
            ignored.append("category")
    city = (args.get("city") or "").strip()[:60]
    if city:
        q = q.filter(_city_filter_products(city))
        applied["city"] = city
    filters = {}
    for name in ("min_price", "max_price"):
        raw = args.get(name)
        if raw not in (None, ""):
            try:
                val = float(raw)
                if val >= 0:
                    filters[name] = val
                    applied[name] = val
            except (TypeError, ValueError):
                ignored.append(name)
    ptype = (args.get("type") or "").strip().lower()
    if ptype in PRODUCT_TYPES:
        filters["type"] = ptype
        applied["type"] = ptype
    elif ptype:
        ignored.append("type")
    if str(args.get("verified") or "").lower() in ("1", "true", "yes", "on"):
        filters["verified"] = True
        applied["verified"] = True
    q = _apply_product_filters(q, filters)

    search = (args.get("q") or "").strip()[:120]
    terms = _terms(search)
    feats = {"fts": False, "trgm": False} if args.get("_no_fts") else pg_features()
    if terms:
        applied["q"] = search
        q_fts = q.filter(_product_match(terms, feats))
        if feats.get("fts"):
            q_fts = q_fts.params(tsq=_tsquery(terms), kw=" ".join(terms))
        q = q_fts

    sort = (args.get("sort") or "newest").strip().lower()
    if sort not in MARKET_SORTS:
        ignored.append("sort")
        sort = "newest"
    if sort == "price_asc":
        order = (Product.price.asc(), Product.id.desc())
    elif sort == "price_desc":
        order = (Product.price.desc(), Product.id.desc())
    else:
        order = (Product.created_at.desc(), Product.id.desc())
    applied["sort"] = sort

    def fetch(query):
        total = query.order_by(None).count()
        rows = query.order_by(*order).offset((page - 1) * per_page).limit(per_page).all()
        return total, rows

    try:
        total, rows = fetch(q)
    except Exception:  # noqa: BLE001 - Postgres search failed: retry with ILIKE
        if not (terms and feats.get("fts")):
            raise
        db.session.rollback()
        log.warning("market: full-text query failed; retrying with ILIKE", exc_info=True)
        return market_products({**args, "_no_fts": True})

    facts = trust_facts([p.storefront for p in rows])
    return {
        "products": [product_card(p, facts.get(p.storefront.vendor_id)) for p in rows],
        **_page(total, page, per_page),
        "applied": applied,
        "ignored": ignored,
    }


def market_stores(args: dict) -> dict:
    page, per_page = _page_args(args.get("page"), args.get("per_page"))
    applied, ignored = {}, []
    q = visible_stores(Storefront.query).options(contains_eager(Storefront.vendor))
    city = (args.get("city") or "").strip()[:60]
    if city:
        q = q.filter(_city_filter_stores(city))
        applied["city"] = city
    if str(args.get("verified") or "").lower() in ("1", "true", "yes", "on"):
        q = q.filter(Storefront.vendor_id.in_(list(verified_vendor_ids()) or [-1]))
        applied["verified"] = True
    search = (args.get("q") or "").strip()[:120]
    terms = _terms(search)
    if terms:
        # ILIKE here: the store list is small and this avoids a second FTS path.
        q = q.filter(_store_match(terms, {"fts": False}))
        applied["q"] = search
    sort = (args.get("sort") or "newest").strip().lower()
    if sort != "newest":
        ignored.append("sort")
    applied["sort"] = "newest"
    total = q.order_by(None).count()
    rows = (q.order_by(Storefront.created_at.desc(), Storefront.id.desc())
            .offset((page - 1) * per_page).limit(per_page).all())
    facts = trust_facts(rows)
    return {
        "stores": [store_card(sf, facts.get(sf.vendor_id)) for sf in rows],
        **_page(total, page, per_page),
        "applied": applied,
        "ignored": ignored,
    }


def market_categories() -> list[dict]:
    """Categories with at least one visible product, with counts."""
    rows = (
        visible_products(db.session.query(Category.id, Category.name, Category.slug, Category.icon,
                                          func.count(Product.id)).select_from(Product))
        .join(Category, Product.category_id == Category.id)
        .group_by(Category.id, Category.name, Category.slug, Category.icon)
        .order_by(func.count(Product.id).desc(), Category.name.asc())
        .all()
    )
    return [{"id": r[0], "name": r[1], "slug": r[2], "icon": r[3], "count": int(r[4])} for r in rows]
