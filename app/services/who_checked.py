"""
"Who checked you" (Siiqo 2.0, Phase 3 item 3-6a).

Three separate, honest figures for the seller, each one ``platform_events`` row:

    Checks          funnel.seller_checked    someone looked the seller up:
                                             /check (source "check") or the Trust
                                             Platform API (source "api")
    Passport views  funnel.passport_viewed   a person opened the public Passport
                                             page (the website marks real views;
                                             link-preview bots, crawlers, share
                                             images and metadata fetches never count)
    Badge views     funnel.badge_viewed      the trust.js badge loaded on the
                                             seller's own verified website

    business_id  the seller
    properties   {"channel": ..., "matched_on": <identifier TYPE or null>}

Privacy: nothing about the searcher is stored - no IP, user agent, user,
session, query text or identifier value; only the identifier's type
("phone", "store_slug"). Sellers see only their own counts.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.extensions import db

log = logging.getLogger(__name__)

EVENT_NAME = "funnel.seller_checked"          # checks: /check and the API
PASSPORT_VIEW_EVENT = "funnel.passport_viewed"
BADGE_VIEW_EVENT = "funnel.badge_viewed"
EVENT_FOR_CHANNEL = {"check": EVENT_NAME, "api": EVENT_NAME,
                     "passport": PASSPORT_VIEW_EVENT, "widget": BADGE_VIEW_EVENT}
CHANNELS = tuple(EVENT_FOR_CHANNEL)
CHECK_CHANNELS = ("check", "api")

# Link-preview bots and crawlers: never a Passport view. Keep in step with
# src/lib/previewBots.ts on the website. The user agent is only matched, never stored.
PREVIEW_BOT_RE = re.compile(
    r"whatsapp|facebookexternalhit|facebookcatalog|meta-externalagent|twitterbot|slackbot|slack-imgproxy|"
    r"telegrambot|linkedinbot|discordbot|googlebot|google-inspectiontool|googleother|adsbot-google|"
    r"mediapartners-google|bingbot|bingpreview|yandex|baiduspider|duckduckbot|applebot|pinterest|redditbot|"
    r"skypeuripreview|embedly|iframely|vkshare|snapchat|viber|line-poker|outbrain|quora link preview|"
    r"w3c_validator|petalbot|semrushbot|ahrefsbot|mj12bot|bytespider|gptbot|claudebot|ccbot|headlesschrome|"
    r"lighthouse|\bbot\b|crawler|spider|preview",
    re.IGNORECASE,
)


def is_preview_bot(user_agent):
    return bool(user_agent) and bool(PREVIEW_BOT_RE.search(user_agent))
# Identifier types a searcher can paste (values are never stored).
IDENTIFIER_TYPES = ("phone", "handle", "store_slug", "paylink_slug", "domain", "payout_account_hash")
WEEKS = 8
TOP_IDENTIFIERS = 3


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _row(vendor_id, channel, matched_on=None, storefront_id=None, now=None):
    kind = matched_on if channel in CHECK_CHANNELS and matched_on in IDENTIFIER_TYPES else None
    return {
        "event_name": EVENT_FOR_CHANNEL[channel],
        "timestamp": now or _now(),
        "business_id": vendor_id,
        "storefront_id": storefront_id,
        "source": channel,
        "properties": {"channel": channel, "matched_on": kind},
    }


def _insert(rows):
    """Own connection and transaction, like foundations.record_event. Never raises."""
    if not rows:
        return
    from app.models.telemetry import PlatformEvent
    try:
        with db.engine.begin() as conn:
            conn.execute(PlatformEvent.__table__.insert(), rows)
    except Exception as e:  # noqa: BLE001 - a counter must never break a lookup
        log.warning(f"[WHO CHECKED] not recorded: {e}")


def record(vendor_id, channel, matched_on=None, storefront_id=None):
    if channel not in CHANNELS or not vendor_id:
        return
    try:
        _insert([_row(vendor_id, channel, matched_on, storefront_id)])
    except Exception as e:  # noqa: BLE001
        log.warning(f"[WHO CHECKED] not recorded: {e}")


def record_slug(store_slug, channel, matched_on=None):
    from app.services import passport as passport_svc
    try:
        sf = passport_svc.find_storefront(store_slug)
    except Exception as e:  # noqa: BLE001
        log.warning(f"[WHO CHECKED] storefront lookup failed: {e}")
        return
    if sf is not None:
        record(sf.vendor_id, channel, matched_on, sf.id)


def record_results(results, channel):
    """/check and /v1/lookup results: [{"passport": {"store_slug": ...}, "matched_on": ...}].
    Results without a Siiqo Passport (e.g. directory listings) are skipped. Never raises."""
    try:
        _record_results(results, channel)
    except Exception as e:  # noqa: BLE001 - a counter must never break a lookup
        log.warning(f"[WHO CHECKED] not recorded: {e}")


def _record_results(results, channel):
    if channel not in CHANNELS:
        return
    from app.models.user import Storefront
    wanted = []
    for r in results or []:
        passport = r.get("passport") if isinstance(r, dict) else None
        slug = passport.get("store_slug") if isinstance(passport, dict) else None
        if slug:
            wanted.append((slug.lower(), r.get("matched_on")))
    if not wanted:
        return
    try:
        stores = {sf.store_slug: sf for sf in
                  Storefront.query.filter(Storefront.store_slug.in_([s for s, _ in wanted])).all()}
    except Exception as e:  # noqa: BLE001
        log.warning(f"[WHO CHECKED] storefront lookup failed: {e}")
        return
    now = _now()
    rows, seen = [], set()
    for slug, kind in wanted:
        sf = stores.get(slug)
        if sf is not None and sf.vendor_id not in seen:
            seen.add(sf.vendor_id)
            rows.append(_row(sf.vendor_id, channel, kind, sf.id, now))
    _insert(rows)


def week_start(d):
    """Monday of the week holding date ``d`` (UTC)."""
    return d - timedelta(days=d.weekday())


def for_vendor(vendor_id, now=None, weeks=WEEKS):
    """The seller's own counts for the last ``weeks`` weeks (this week included).
    Checks, Passport views and badge views are kept apart. Counts and
    identifier types only."""
    from app.models.telemetry import PlatformEvent

    now = now or _now()
    first_week = week_start(now.date()) - timedelta(weeks=weeks - 1)
    since = datetime(first_week.year, first_week.month, first_week.day, tzinfo=timezone.utc)
    week_list = [first_week + timedelta(weeks=i) for i in range(weeks)]
    rows = (db.session.query(PlatformEvent.event_name, PlatformEvent.timestamp, PlatformEvent.source)
            .filter(PlatformEvent.event_name.in_((EVENT_NAME, PASSPORT_VIEW_EVENT, BADGE_VIEW_EVENT)),
                    PlatformEvent.business_id == vendor_id,
                    PlatformEvent.timestamp >= since).all())

    weekly = {name: Counter() for name in (EVENT_NAME, PASSPORT_VIEW_EVENT, BADGE_VIEW_EVENT)}
    by_channel = Counter()
    for name, ts, source in rows:
        ts = _aware(ts)
        if ts is None:
            continue
        weekly[name][week_start(ts.date())] += 1
        if name == EVENT_NAME:
            by_channel[source if source in CHECK_CHANNELS else "check"] += 1

    kinds = Counter()
    props_rows = (db.session.query(PlatformEvent.properties)
                  .filter(PlatformEvent.event_name == EVENT_NAME,
                          PlatformEvent.business_id == vendor_id,
                          PlatformEvent.timestamp >= since).all())
    for (props,) in props_rows:
        kind = (props or {}).get("matched_on")
        if kind in IDENTIFIER_TYPES:
            kinds[kind] += 1

    def figure(name):
        counts = weekly[name]
        return {"total": sum(counts.values()), "this_week": counts.get(week_list[-1], 0),
                "weeks": [{"week_start": w.isoformat(), "count": counts.get(w, 0)} for w in week_list]}

    checks = figure(EVENT_NAME)
    checks["by_channel"] = [{"channel": c, "count": by_channel.get(c, 0)} for c in CHECK_CHANNELS]
    return {
        "since": first_week.isoformat(),
        "weeks_shown": weeks,
        "checks": checks,
        "passport_views": figure(PASSPORT_VIEW_EVENT),
        "badge_views": figure(BADGE_VIEW_EVENT),
        "top_identifiers": [{"type": k, "count": n} for k, n in
                            sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_IDENTIFIERS]],
    }
