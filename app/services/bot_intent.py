"""
Bot free-text intent router (Phase T, T-4; AI_ORCHESTRATION_OPTIONS.md section 2, row 7).

Buttons and slash commands are routed by code in the bot and never reach this
module. Free text arrives here and is resolved in this order:

  1. per-user rate limit (BOT_INTENT_RATE_PER_MINUTE / BOT_INTENT_RATE_PER_DAY);
  2. a fixed phrase table (code, free, works with AI off);
  3. the bot_intent_cache table (earlier AI answers for the same normalised text);
  4. the AI gateway: Haiku with prompt ``bot_intent`` (v1), 0 retries, short timeout.

The only intents are INTENTS below. Anything else, an AI error or AI switched
off returns ``fallback: "menu"`` so the bot shows the main menu as before.
A confident answer (>= BOT_INTENT_MIN_CONFIDENCE) is routed; a low-confidence
one comes back with ``needs_confirmation`` and up to MAX_CHOICES choices for
"Did you mean...?" buttons.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.extensions import db
from app.models.bot import BotIntentCache, BotRateCounter
from app.services import ai

log = logging.getLogger(__name__)

ACTION = "bot_intent"
INTENTS = ("check_seller", "search", "create_pay_link", "my_orders", "onboarding", "help")
MAX_TEXT_LEN = 500
MAX_QUERY_LEN = 100
MAX_CHOICES = 2          # WhatsApp allows 3 reply buttons: 2 choices + "Main menu"
AI_TIMEOUT_SECONDS = 4.0

# Exact phrases after normalise(). Kept small and unambiguous on purpose.
PHRASES: dict[str, str] = {
    "help": "help", "hi": "help", "hello": "help", "hey": "help", "start": "help",
    "what can you do": "help", "how does this work": "help", "commands": "help",
    "my orders": "my_orders", "orders": "my_orders", "show my orders": "my_orders",
    "where is my order": "my_orders", "track my order": "my_orders", "track order": "my_orders",
    "order status": "my_orders",
    "pay link": "create_pay_link", "paylink": "create_pay_link", "payment link": "create_pay_link",
    "create pay link": "create_pay_link", "create payment link": "create_pay_link",
    "send payment link": "create_pay_link", "generate payment link": "create_pay_link",
    "check seller": "check_seller", "check a seller": "check_seller", "verify seller": "check_seller",
    "is this seller legit": "check_seller", "is this seller real": "check_seller",
    "trust check": "check_seller",
    "open store": "onboarding", "open a store": "onboarding", "start selling": "onboarding",
    "create store": "onboarding", "create a store": "onboarding", "sell on siiqo": "onboarding",
    "i want to sell": "onboarding", "become a seller": "onboarding",
}
# "<prefix> <query>" -> search with that query.
SEARCH_PREFIXES = ("search for ", "search ", "find me ", "find ", "looking for ", "i need ",
                   "i want to buy ", "buy ", "where can i buy ")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def min_confidence() -> float:
    return _env_float("BOT_INTENT_MIN_CONFIDENCE", 0.7)


def normalise(text: str) -> str:
    t = (text or "").lower().strip()
    t = re.sub(r"[^\w\s']", " ", t)       # drop punctuation and emoji
    t = re.sub(r"\s+", " ", t).strip()
    return t[:MAX_TEXT_LEN]


def text_hash(normalised: str) -> str:
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


# ── Rate limiting (shared across workers through the database) ─────────────
def hit_rate_limit(kind: str, platform: str, user_ref: str, limits: list[tuple[str, int, int]]) -> str | None:
    """
    Count one event against each (label, window_seconds, max) window.
    Returns the label of the first window that is exceeded, else None.
    A max of 0 disables that window. Database errors fail open for the
    count (logged) because the AI gateway still meters and caps spend.
    """
    now = datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    exceeded = None
    try:
        BotRateCounter.query.filter(BotRateCounter.expires_at < now).delete(synchronize_session=False)
        for label, window, maximum in limits:
            if maximum <= 0:
                continue
            key = f"{kind}:{label}:{platform}:{user_ref}:{epoch // window}"[:160]
            row = db.session.get(BotRateCounter, key)
            if row is None:
                row = BotRateCounter(key=key, count=0, expires_at=now + timedelta(seconds=window))
                db.session.add(row)
                try:
                    db.session.flush()
                except IntegrityError:
                    db.session.rollback()
                    row = db.session.get(BotRateCounter, key)
            row.count = (row.count or 0) + 1
            if row.count > maximum and exceeded is None:
                exceeded = label
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        log.exception("[BOT-INTENT] rate counter unavailable")
    return exceeded


def intent_rate_limits() -> list[tuple[str, int, int]]:
    return [
        ("minute", 60, _env_int("BOT_INTENT_RATE_PER_MINUTE", 10)),
        ("day", 86400, _env_int("BOT_INTENT_RATE_PER_DAY", 150)),
    ]


# ── Resolution ──────────────────────────────────────────────────────────────
def _result(intent=None, confidence=0.0, query=None, source="none", choices=None,
            needs_confirmation=False, fallback=None, reason=None) -> dict:
    return {
        "intent": intent,
        "confidence": round(float(confidence or 0.0), 3),
        "query": (query or None) and str(query)[:MAX_QUERY_LEN],
        "source": source,
        "needs_confirmation": bool(needs_confirmation),
        "choices": list(choices or []),
        "fallback": fallback,
        "reason": reason,
    }


def _from_phrases(norm: str):
    if norm in PHRASES:
        return _result(PHRASES[norm], 1.0, source="phrase")
    for prefix in SEARCH_PREFIXES:
        if norm.startswith(prefix) and len(norm) > len(prefix) + 1:
            return _result("search", 1.0, query=norm[len(prefix):].strip(), source="phrase")
    return None


def _cache_get(h: str):
    try:
        row = db.session.get(BotIntentCache, h)
        if row is None:
            return None
        if row.expires_at and _aware(row.expires_at) < datetime.now(timezone.utc):
            db.session.delete(row)
            db.session.commit()
            return None
        row.hits = (row.hits or 0) + 1
        db.session.commit()
        return _result(row.intent, row.confidence, source="cache")
    except SQLAlchemyError:
        db.session.rollback()
        return None


def _cache_put(h: str, intent: str, confidence: float, version: str) -> None:
    days = _env_int("BOT_INTENT_CACHE_DAYS", 7)
    if days <= 0:
        return
    now = datetime.now(timezone.utc)
    try:
        row = db.session.get(BotIntentCache, h)
        if row is None:
            row = BotIntentCache(text_hash=h, hits=0)
            db.session.add(row)
        row.intent, row.confidence, row.prompt_version = intent, confidence, version
        row.created_at, row.expires_at = now, now + timedelta(days=days)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def classify(platform: str, user_ref: str, text: str, *, client=None) -> tuple[dict, int]:
    """(result dict, HTTP status)."""
    raw = (text or "").strip()[:MAX_TEXT_LEN]
    norm = normalise(raw)
    if len(norm) < 2 or norm.isdigit():
        return _result(fallback="menu", reason="too_short"), 200

    limited = hit_rate_limit("intent", platform, user_ref, intent_rate_limits())
    if limited:
        return _result(fallback="menu", reason=f"rate_limited_{limited}"), 429

    phrase = _from_phrases(norm)
    if phrase:
        return phrase, 200

    try:
        enabled, _why = ai.kill_switch_state(ACTION)
    except SQLAlchemyError:
        db.session.rollback()
        enabled = False
    if not enabled:
        return _result(fallback="menu", reason="ai_disabled"), 200

    h = text_hash(norm)
    cached = _cache_get(h)
    if cached:
        return cached, 200

    try:
        res = ai.run(ACTION, seller_id=None, inputs={"message": raw}, client=client,
                     timeout=_env_float("BOT_INTENT_TIMEOUT_SECONDS", AI_TIMEOUT_SECONDS))
    except ai.AIDisabled:
        return _result(fallback="menu", reason="ai_disabled"), 200
    except ai.AIError as exc:
        log.info("[BOT-INTENT] AI unavailable (%s); falling back to menu", exc.reason)
        return _result(fallback="menu", reason="ai_unavailable"), 200

    data = res.data or {}
    intent = data.get("intent")
    confidence = float(data.get("confidence") or 0.0)
    confidence = min(max(confidence, 0.0), 1.0)
    query = (data.get("query") or "").strip() or None
    alternatives = [a for a in (data.get("alternatives") or []) if a in INTENTS and a != intent]

    if intent in INTENTS and confidence >= min_confidence():
        if not query:
            # Only phrasings with no extracted details are cached (no personal data, reusable).
            _cache_put(h, intent, confidence, res.prompt_version)
        return _result(intent, confidence, query=query, source="ai"), 200

    choices = ([intent] if intent in INTENTS else []) + alternatives
    choices = list(dict.fromkeys(choices))[:MAX_CHOICES]
    if not choices:
        return _result(confidence=confidence, source="ai", fallback="menu", reason="unknown"), 200
    return _result(choices[0], confidence, query=query, source="ai", choices=choices,
                   needs_confirmation=True, reason="low_confidence"), 200
