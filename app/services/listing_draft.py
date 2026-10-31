"""
"Fill from photo" for the product form (Siiqo 2.0, Phase 1, item 1-3).

One product photo -> a listing draft (title, description, category,
attributes) the seller edits before saving. Never a price.

Reuses the onboarding build's listing path (``onboarding_build.listing_from_image``):
the ``listing_from_photo`` prompt through the AI gateway (kill switch, PII
strip, per-seller daily cap of the prompt), then the code critic
``check_listing`` with our category names.

The result is cached in this process by (seller, image SHA-256, note), so
picking the same photo again costs nothing. Only answers the AI actually
produced are cached (a draft, or a critic rejection); "AI off", "capped" and
provider errors are not, so they clear as soon as the cause does.

With AI off, capped, failing or rejected, the caller gets ``draft: None`` and
a short ``reason``; the form simply stays manual.
"""
from __future__ import annotations

import hashlib
import io
import threading
import time
from collections import OrderedDict

from app.services import onboarding_build as build

RETRIES = 1                       # critic retries (2 attempts): the seller is waiting on the form
CACHE_TTL_SECONDS = 24 * 3600
CACHE_MAX_ENTRIES = 256
MAX_NOTE_CHARS = 200

# reason codes returned with draft: None
REASON_AI_OFF = "ai_off"
REASON_CAPPED = "capped"
REASON_REJECTED = "rejected"
REASON_UNAVAILABLE = "unavailable"
REASON_UNREADABLE = "unreadable"

_cache: "OrderedDict[tuple, tuple[float, dict]]" = OrderedDict()
_lock = threading.Lock()


def image_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _cache_get(key: tuple) -> dict | None:
    now = time.monotonic()
    with _lock:
        item = _cache.get(key)
        if item is None:
            return None
        stored_at, value = item
        if now - stored_at > CACHE_TTL_SECONDS:
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return value


def _cache_put(key: tuple, value: dict) -> None:
    with _lock:
        _cache[key] = (time.monotonic(), value)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)


def _reason(outcome) -> str:
    why = outcome.reason or ""
    if why.startswith("AIDisabled"):
        return REASON_AI_OFF
    if why.startswith("AICapExceeded"):
        return REASON_CAPPED
    if outcome.problems or why.startswith("critic"):
        return REASON_REJECTED
    return REASON_UNAVAILABLE


def draft_from_photo(seller_id: int, data: bytes, *, business_name: str = "", note: str = "") -> dict:
    """``{"draft": {...} | None, "reason": str | None, "cached": bool}``."""
    note = (note or "").strip()[:MAX_NOTE_CHARS]
    key = (seller_id, image_hash(data), note)
    hit = _cache_get(key)
    if hit is not None:
        return {**hit, "cached": True}

    source = build.image_source_from(io.BytesIO(data))
    if source is None:
        return {"draft": None, "reason": REASON_UNREADABLE, "cached": False}

    categories = build.category_names()
    answers = {"business_name": business_name} if business_name else {}
    outcome = build.listing_from_image(seller_id, source, answers, seller_note=note,
                                       categories=categories, retries=RETRIES)
    if outcome.source == "ai" and isinstance(outcome.value, dict):
        result = {"draft": {**build.listing_fields(outcome.value, categories), "source": "ai"},
                  "reason": None}
        _cache_put(key, result)
    else:
        result = {"draft": None, "reason": _reason(outcome)}
        if result["reason"] == REASON_REJECTED:
            _cache_put(key, result)
    return {**result, "cached": False}
