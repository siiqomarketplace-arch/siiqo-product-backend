"""Where a buyer came from (BUYING_FIX_PLAN A2).

The website saves utm_source / utm_campaign / ref / the landing page for 30
days and sends them with checkout as ``attribution``. The bot sends the header
``X-Siiqo-Client: telegram|whatsapp``. This module cleans those values and
picks ``orders.source`` / ``orders.campaign``. Values are ids and labels only,
never personal data.
"""
from __future__ import annotations

import re

SOURCE_MAX = 40
CAMPAIGN_MAX = 100
_FIELD_MAX = {"source": SOURCE_MAX, "medium": 40, "campaign": CAMPAIGN_MAX, "ref": 100, "landing": 200}
BOT_CLIENTS = ("telegram", "whatsapp")


def clean_token(value, max_len: int) -> str | None:
    """Lowercase, only [a-z0-9_.-] (spaces become '_'), cut to max_len."""
    if not isinstance(value, str):
        return None
    v = re.sub(r"\s+", "_", value.strip().lower())
    v = re.sub(r"[^a-z0-9_.\-]", "", v)[:max_len].strip("_.-")
    return v or None


def clean_attribution(raw) -> dict:
    """The optional checkout ``attribution`` object, cleaned. Unknown keys are dropped."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, max_len in _FIELD_MAX.items():
        val = raw.get(key)
        if key == "landing":
            # Keep the path shape for the market/store hint; never stored as-is.
            if isinstance(val, str) and val.strip():
                out[key] = val.strip().lower()[:max_len]
            continue
        val = clean_token(val, max_len)
        if val:
            out[key] = val
    return out


def _landing_hint(landing: str | None) -> str | None:
    if not landing:
        return None
    path = re.sub(r"^[a-z]+://[^/]+", "", landing).split("?")[0].strip("/")
    first = path.split("/")[0] if path else ""
    if first in ("market", "marketplace", "search"):
        return "market"
    if first in ("store", "stores", "s", "shop") or landing in ("store", "storefront"):
        return "store"
    if first in ("product", "products"):
        return "product"
    return None


def client_header_source(headers) -> str | None:
    """'telegram' / 'whatsapp' from the bot's X-Siiqo-Client header."""
    try:
        val = clean_token(headers.get("X-Siiqo-Client"), SOURCE_MAX)
    except Exception:
        return None
    return val if val in BOT_CLIENTS else None


def order_source(attribution: dict, headers=None, *, pay_link: bool = False) -> str:
    """orders.source: attribution.source, else pay_link, else the bot client,
    else the landing page hint (market/store), else 'direct'."""
    if attribution.get("source"):
        return attribution["source"]
    if pay_link:
        return "pay_link"
    bot = client_header_source(headers) if headers is not None else None
    if bot:
        return bot
    return _landing_hint(attribution.get("landing")) or "direct"


def apply_to_order(order, payload, headers=None, *, pay_link: bool = False) -> dict:
    """Set order.source / order.campaign from a request payload. Returns the
    cleaned attribution (without the landing path) for event properties."""
    attribution = clean_attribution((payload or {}).get("attribution") if isinstance(payload, dict) else None)
    order.source = order_source(attribution, headers, pay_link=pay_link)
    order.campaign = attribution.get("campaign")
    # The marketing code (siiqo.com/r/<CODE>) the buyer came through, if any.
    ref = re.sub(r"[^A-Z0-9\-]", "", (attribution.get("ref") or "").upper())
    order.ref_code = ref if 3 <= len(ref) <= 24 else None
    return {k: v for k, v in attribution.items() if k != "landing"}


# ── Where a seller came from (campaign plan) ────────────────────────────────
# Every new account gets a UserAttribution row (first touch, never
# overwritten); the store creation adds store_created_at. The channel is one
# of CHANNELS. "existing" is not stored from a signup: activation reports call
# a store "existing" when it was created before CAMPAIGN_START_DATE.

import json as _json  # noqa: E402
import logging as _logging  # noqa: E402
import os as _os  # noqa: E402
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta, timezone as _timezone  # noqa: E402

_log = _logging.getLogger(__name__)

CHANNELS = ("agent", "influencer", "pr", "referral", "existing", "facebook_sellers",
            "market_activation", "content", "ads", "organic")
CHANNEL_LABELS = {
    "agent": "Agents", "influencer": "Influencers", "pr": "PR (press / blogs)",
    "referral": "Seller referrals", "existing": "Existing sellers (before campaign)",
    "facebook_sellers": "Facebook sellers programme", "market_activation": "Market activations",
    "content": "Content / staff posts", "ads": "Ads", "organic": "Organic (no code or tag)",
}
DEFAULT_CAMPAIGN_START = "2026-12-05"

# Marketing code kind -> channel. A "campaign" code uses its campaign's channel.
CODE_KIND_CHANNEL = {
    "agent": "agent", "influencer": "influencer", "media": "pr", "seller_invite": "referral",
    "location": "market_activation", "flyer": "market_activation",
}
CAMPAIGN_CHANNEL = {
    "agent": "agent", "influencer": "influencer", "radio": "pr", "print": "pr", "article": "pr",
    "event": "market_activation", "flyer": "market_activation",
}
_FB_SELLERS = ("facebook_sellers", "facebook-sellers", "fb_sellers", "fb-sellers", "fbsellers")
_PAID_MEDIUMS = ("cpc", "ppc", "paid", "paid_social", "paid-social", "paidsocial", "ads", "ad", "display",
                 "sponsored", "cpm", "boost", "boosted")
_PAID_SOURCES = ("google_ads", "googleads", "adwords", "meta_ads", "fb_ads", "facebook_ads", "tiktok_ads",
                 "instagram_ads")
_PR_WORDS = ("pr", "press", "blog", "news", "article", "media", "radio", "tv", "podcast", "newspaper")
_ACTIVATION_WORDS = ("event", "market", "activation", "flyer", "poster", "qr", "booth")
_SOCIAL_SOURCES = ("facebook", "instagram", "tiktok", "twitter", "x", "whatsapp", "telegram", "youtube",
                   "linkedin", "threads", "snapchat", "pinterest", "ig", "fb", "t")
_CONTENT_MEDIUMS = ("social", "organic_social", "post", "content", "staff", "bio", "story", "status",
                    "email", "newsletter")


def campaign_start_date() -> _datetime:
    """CAMPAIGN_START_DATE (YYYY-MM-DD, a Lagos day) as a UTC datetime. Default 2026-12-05."""
    raw = (_os.environ.get("CAMPAIGN_START_DATE") or DEFAULT_CAMPAIGN_START).strip()
    try:
        d = _date.fromisoformat(raw)
    except ValueError:
        d = _date.fromisoformat(DEFAULT_CAMPAIGN_START)
    # Midnight in Lagos (UTC+1) is 23:00 UTC the day before.
    return _datetime(d.year, d.month, d.day, tzinfo=_timezone.utc) - _timedelta(hours=1)


def _aware_dt(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_timezone.utc)


def is_existing_store(store_created_at) -> bool:
    dt = _aware_dt(store_created_at)
    return dt is not None and dt < campaign_start_date()


def _host_word(source: str) -> str:
    """'l.instagram.com' -> 'instagram'; 'facebook' -> 'facebook'."""
    skip = ("www", "m", "l", "lm", "com", "co", "ng", "net", "org", "me")
    parts = [p for p in source.split(".") if p and p not in skip]
    return parts[0] if parts else source


def channel_from_utm(source=None, medium=None, campaign=None) -> str:
    """The channel for utm tags alone (no code). No tags -> organic. An
    untagged visit from a social site (the website saves medium 'referral'
    with the referrer host) is organic too."""
    s = (source or "").lower()
    m = (medium or "").lower()
    c = (campaign or "").lower()
    if not (s or m or c):
        return "organic"
    if any(k in v for v in (s, c) for k in _FB_SELLERS):
        return "facebook_sellers"
    if m == "referral" and not c:
        return "organic"   # saved from document.referrer, not a tagged link
    word = _host_word(s) if s else ""
    if m in _PAID_MEDIUMS or s in _PAID_SOURCES or "paid" in m:
        return "ads"
    if "influenc" in s or "influenc" in m or "creator" in s or "creator" in m:
        return "influencer"
    if s == "agent" or m == "agent":
        return "agent"
    if s in _PR_WORDS or m in _PR_WORDS or word in _PR_WORDS:
        return "pr"
    if s in _ACTIVATION_WORDS or m in _ACTIVATION_WORDS:
        return "market_activation"
    if s in ("seller_invite", "invite") or m in ("invite", "seller_invite"):
        return "referral"
    if word in _SOCIAL_SOURCES or m in _CONTENT_MEDIUMS:
        return "content"
    if s == "ref" and not (m or c):
        return "organic"   # ?ref= whose code did not resolve
    return "content"       # another tagged link, most likely posted by staff


def channel_for_code(code) -> str | None:
    """The channel for a MarketingCode (None for a code that never credits a seller)."""
    if code is None:
        return None
    camp = getattr(code, "campaign", None) if getattr(code, "campaign_id", None) else None
    slug = (getattr(camp, "slug", "") or "").lower()
    if any(k in slug for k in _FB_SELLERS):
        return "facebook_sellers"
    if code.kind == "campaign":
        return CAMPAIGN_CHANNEL.get((getattr(camp, "channel", "") or "").lower(), "ads")
    return CODE_KIND_CHANNEL.get(code.kind)


def signup_utm(data) -> dict:
    """utm source/medium/campaign from a signup or store payload: ``utm`` (the
    website's signup field), else ``attribution``, else flat utm_* fields (forms)."""
    if data is None:
        return {}
    getter = data.get if hasattr(data, "get") else (lambda k, d=None: d)
    raw = {}
    for key in ("utm", "attribution"):
        val = getter(key)
        if isinstance(val, str):
            try:
                val = _json.loads(val)
            except (TypeError, ValueError):
                val = None
        if isinstance(val, dict) and any(val.get(k) for k in ("source", "medium", "campaign")):
            raw = val
            break
    if not raw:
        raw = {"source": getter("utm_source"), "medium": getter("utm_medium"), "campaign": getter("utm_campaign")}
    out = {}
    for key, max_len in (("source", SOURCE_MAX), ("medium", 40), ("campaign", CAMPAIGN_MAX)):
        v = clean_token(raw.get(key), max_len)
        if v:
            out[key] = v
    return out


def _code_for(user, data):
    """(code, personal_referrer) the signup came through. Read-only use of the
    marketing helpers: a credited SellerAcquisition wins, then the ref code,
    then the personal referral code (an approved agent's, else a seller's)."""
    from sqlalchemy import func
    from app.extensions import db
    from app.models.marketing import MarketingCode, SellerAcquisition
    from app.models.user import User
    from app.services import marketing as mk
    acq = SellerAcquisition.query.filter_by(seller_user_id=user.id).first()
    if acq is not None and acq.code_id:
        return db.session.get(MarketingCode, acq.code_id), None
    getter = data.get if hasattr(data, "get") else (lambda k, d=None: d)
    ref, _ = mk._ref_from(data)
    code = mk.find_code(ref) if ref else None
    if code is not None and code.kind != "seller_sticker":
        return code, None
    for personal in (getter("referral_code"), ref if code is None else None):
        if not isinstance(personal, str) or not personal.strip():
            continue
        agent_code = mk._agent_code_for_referral(personal)
        if agent_code is not None:
            return agent_code, None
        owner = User.query.filter(func.upper(User.referral_code) == personal.strip().upper()[:40]).first()
        if owner is not None and owner.id != user.id:
            return None, owner
    return None, None


def resolve_signup(user, data) -> dict:
    """{utm_source, utm_medium, utm_campaign, ref_code, code_kind, channel} for a signup."""
    utm = signup_utm(data)
    code, referrer = _code_for(user, data)
    channel = channel_for_code(code) if code is not None else None
    if channel is None and referrer is not None:
        channel = "referral"
    if channel is None:
        channel = channel_from_utm(utm.get("source"), utm.get("medium"), utm.get("campaign"))
    ref_code = code.code if code is not None else (referrer.referral_code if referrer is not None else None)
    return {"utm_source": utm.get("source"), "utm_medium": utm.get("medium"),
            "utm_campaign": utm.get("campaign"), "ref_code": ref_code[:40] if ref_code else None,
            "code_kind": code.kind if code is not None else ("referral" if referrer is not None else None),
            "channel": channel}


_SOURCE_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "ref_code", "code_kind")


def record_signup(user, data, *, store=None):
    """Save (or fill in) the first-touch source for ``user``. First touch wins:
    a field already set is never overwritten; only an "organic" channel may be
    resolved by a later touch (the store setup). With ``store``, also sets
    store_created_at. Runs in a savepoint and never raises; the caller commits."""
    from app.extensions import db
    from app.models.growth import UserAttribution
    try:
        if user is None or user.id is None:
            return None
        resolved = resolve_signup(user, data)
        now = _datetime.now(_timezone.utc)
        with db.session.begin_nested():
            row = UserAttribution.query.filter_by(user_id=user.id).first()
            if row is None:
                row = UserAttribution(user_id=user.id, signed_up_at=_aware_dt(user.created_at) or now,
                                      channel=resolved["channel"],
                                      **{k: resolved[k] for k in _SOURCE_FIELDS})
                db.session.add(row)
            else:
                for key in _SOURCE_FIELDS:
                    if not getattr(row, key) and resolved[key]:
                        setattr(row, key, resolved[key])
                if (row.channel or "organic") == "organic" and resolved["channel"] != "organic":
                    row.channel = resolved["channel"]
            if store is not None and row.store_created_at is None:
                row.store_created_at = _aware_dt(getattr(store, "created_at", None)) or now
            db.session.flush()
        return row
    except Exception as e:  # noqa: BLE001 - never blocks a signup or a store
        _log.warning(f"[ATTRIBUTION] signup source skipped for user {getattr(user, 'id', None)}: {e}")
        return None


def record_store_created(user, data, store=None):
    """Call when a store is created, with the store setup payload (the store is
    looked up by its owner when not given)."""
    if store is None and user is not None and user.id is not None:
        try:
            from app.models.user import Storefront
            store = Storefront.query.filter_by(vendor_id=user.id).first()
        except Exception:  # noqa: BLE001
            store = None
    return record_signup(user, data, store=store) if store is not None else record_signup(user, data)
