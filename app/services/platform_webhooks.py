"""
Trust Platform webhooks (docs/PLATFORM_API_V1.md).

Events: business.trust_changed (tier or score band changed), business.verified,
business.verification_lapsed, business.passport_hidden.

- Body: {"id", "type", "created_at", "data": {"slug", ...public claims}}.
  For business.passport_hidden, data is {"slug"} only: the seller chose to
  stop sharing, so no claims are sent.
- Header: Siiqo-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">
  with the endpoint secret. Receivers reject signatures older than 5 minutes.
- Delivered through the job runner. A failed attempt is retried after 1m, 5m,
  30m, 2h and 12h (6 attempts in all). When a delivery uses up its retries
  and the endpoint has had no successful delivery since that event, the
  endpoint is disabled. Every attempt is kept in the delivery log.
- Changes are detected by comparing each seller's public state with the last
  state stored in platform_passport_event_state. Hooks in the Passport,
  verification and trust code ask for a comparison; an hourly sweep catches
  changes that happen with time (expiry, pauses, plan lapses).
- Only endpoints of active live keys receive real events. Test keys can ask
  for a sample event (fixture data) with send_test_event.
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import update

from app.extensions import db
from app.services import jobs

logger = logging.getLogger(__name__)

EVENTS = ("business.trust_changed", "business.verified", "business.verification_lapsed",
          "business.passport_hidden")
BACKOFF = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2),
           timedelta(hours=12))
MAX_ATTEMPTS = len(BACKOFF) + 1
SIGNATURE_TOLERANCE = 300           # seconds (5 minutes)
MAX_ENDPOINTS_PER_KEY = 5
DELIVERY_TIMEOUT = 10
SIGNATURE_HEADER = "Siiqo-Signature"


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


class WebhookError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# ── signing ────────────────────────────────────────────────────────────────

def signature_header(secret, body, timestamp=None):
    t = int(timestamp if timestamp is not None else time.time())
    raw = body if isinstance(body, bytes) else body.encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), f"{t}.".encode("utf-8") + raw, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify_signature(secret, header, body, now=None, tolerance=SIGNATURE_TOLERANCE):
    """What a receiver does: recompute the HMAC and refuse old timestamps."""
    try:
        parts = dict(p.split("=", 1) for p in (header or "").split(","))
        t = int(parts["t"])
        given = parts["v1"]
    except (ValueError, KeyError):
        return False
    now = int(now if now is not None else time.time())
    if abs(now - t) > tolerance:
        return False
    expected = signature_header(secret, body, t).split("v1=", 1)[1]
    return hmac.compare_digest(expected, given)


# ── endpoints ──────────────────────────────────────────────────────────────

def _encrypt(secret):
    from app.services.totp import encrypt_secret
    return encrypt_secret(secret)


def _decrypt(token):
    from app.services.totp import decrypt_secret
    return decrypt_secret(token)


def validate_url(url):
    url = (url or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        parsed = None
    if not parsed or parsed.scheme != "https" or not parsed.hostname or len(url) > 500:
        raise WebhookError("INVALID_URL", "Use an https:// address.")
    if parsed.username or parsed.password:
        raise WebhookError("INVALID_URL", "Don't put a username or password in the address.")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local") or "." not in host:
        raise WebhookError("INVALID_URL", "Use a public https:// address.")
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise WebhookError("INVALID_URL", "Use a public https:// address.")
    except ValueError:
        pass
    return url


def endpoints_for(client):
    from app.models.platform import WebhookEndpoint
    return (WebhookEndpoint.query.filter_by(client_id=client.id).filter(WebhookEndpoint.deleted_at.is_(None))
            .order_by(WebhookEndpoint.id.asc()).all())


def create_endpoint(client, url, events=None):
    """Returns (endpoint, secret). The secret is shown once."""
    from app.models.platform import WebhookEndpoint
    url = validate_url(url)
    if events in (None, [], ""):
        events = list(EVENTS)
    if not isinstance(events, list) or any(e not in EVENTS for e in events):
        raise WebhookError("INVALID_EVENTS", f"events must be a list of: {', '.join(EVENTS)}")
    if len(endpoints_for(client)) >= MAX_ENDPOINTS_PER_KEY:
        raise WebhookError("TOO_MANY", f"Up to {MAX_ENDPOINTS_PER_KEY} webhooks per key. Delete one first.", 409)
    secret = f"whsec_{secrets.token_urlsafe(32)}"
    endpoint = WebhookEndpoint(client_id=client.id, url=url, secret_enc=_encrypt(secret),
                               secret_prefix=secret[:12], events=sorted(set(events)), is_active=True)
    db.session.add(endpoint)
    db.session.commit()
    return endpoint, secret


def delete_endpoint(endpoint):
    from app.models.platform import WebhookDelivery
    endpoint.deleted_at = _now()
    endpoint.is_active = False
    WebhookDelivery.query.filter(WebhookDelivery.endpoint_id == endpoint.id,
                                 WebhookDelivery.status.in_(("pending", "retrying"))) \
        .update({"status": "cancelled", "next_attempt_at": None}, synchronize_session=False)
    db.session.commit()


def endpoint_public(endpoint):
    def iso(dt):
        return _aware(dt).isoformat() if dt else None
    return {
        "id": endpoint.id, "url": endpoint.url, "events": list(endpoint.events or []),
        "secret_prefix": endpoint.secret_prefix, "is_active": bool(endpoint.is_active),
        "disabled_reason": endpoint.disabled_reason, "disabled_at": iso(endpoint.disabled_at),
        "last_success_at": iso(endpoint.last_success_at), "last_failure_at": iso(endpoint.last_failure_at),
        "created_at": iso(endpoint.created_at),
    }


def delivery_public(d):
    def iso(dt):
        return _aware(dt).isoformat() if dt else None
    return {
        "id": d.id, "event_id": d.event_id, "event_type": d.event_type, "status": d.status,
        "attempts": d.attempts, "last_status_code": d.last_status_code, "last_error": d.last_error,
        "created_at": iso(d.created_at), "last_attempt_at": iso(d.last_attempt_at),
        "next_attempt_at": iso(d.next_attempt_at), "delivered_at": iso(d.delivered_at),
    }


def deliveries_for(endpoint, limit=50):
    from app.models.platform import WebhookDelivery
    return (WebhookDelivery.query.filter_by(endpoint_id=endpoint.id)
            .order_by(WebhookDelivery.id.desc()).limit(limit).all())


# ── events ─────────────────────────────────────────────────────────────────

def _event_body(event_type, data, event_id=None):
    event = {"id": event_id or f"evt_{uuid.uuid4().hex}", "type": event_type,
             "created_at": _now().isoformat(), "data": data}
    return event["id"], json.dumps(event, separators=(",", ":"), ensure_ascii=False)


def _live_endpoints(event_type=None):
    from app.models.passport import PassportApiClient
    from app.models.platform import WebhookEndpoint
    rows = (db.session.query(WebhookEndpoint, PassportApiClient)
            .join(PassportApiClient, PassportApiClient.id == WebhookEndpoint.client_id)
            .filter(WebhookEndpoint.is_active.is_(True), WebhookEndpoint.deleted_at.is_(None),
                    PassportApiClient.is_active.is_(True)).all())
    from app.services.passport import key_mode
    out = []
    for endpoint, client in rows:
        if key_mode(client) != "live":
            continue
        if event_type and event_type not in (endpoint.events or []):
            continue
        out.append(endpoint)
    return out


def has_live_endpoints():
    return bool(_live_endpoints())


def emit(event_type, data):
    """Create one delivery per subscribed live endpoint and queue them."""
    from app.models.platform import WebhookDelivery
    if event_type not in EVENTS:
        raise ValueError(event_type)
    endpoints = _live_endpoints(event_type)
    if not endpoints:
        return []
    event_id, body = _event_body(event_type, data)
    deliveries = []
    for endpoint in endpoints:
        d = WebhookDelivery(endpoint_id=endpoint.id, event_id=event_id, event_type=event_type, body=body,
                            status="pending")
        db.session.add(d)
        deliveries.append(d)
    db.session.commit()
    for d in deliveries:
        _queue(d.id)
    return deliveries


def send_test_event(endpoint):
    """A sample business.trust_changed event with fixture data (`test: true`)."""
    from app.models.platform import WebhookDelivery
    from app.services.platform import fixture
    claims = fixture("test-trusted-store")
    event_id, body = _event_body("business.trust_changed", {"slug": claims["store_slug"], **claims})
    d = WebhookDelivery(endpoint_id=endpoint.id, event_id=event_id, event_type="business.trust_changed",
                        body=body, status="pending")
    db.session.add(d)
    db.session.commit()
    _queue(d.id)
    return d


def _queue(delivery_id):
    try:
        jobs.enqueue("webhook_deliver", {"delivery_id": delivery_id})
    except Exception as e:  # noqa: BLE001 — the sweep picks it up later
        db.session.rollback()
        logger.warning(f"[WEBHOOKS] could not queue delivery {delivery_id}: {e}")


# ── delivery ───────────────────────────────────────────────────────────────

def _post(url, body, headers):
    """POST to a public address only, never following redirects."""
    import requests
    from app.utils.scraper import _check_public_url
    _check_public_url(url)
    return requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=DELIVERY_TIMEOUT,
                         allow_redirects=False)


def attempt(delivery_id, now=None):
    from app.models.platform import WebhookDelivery, WebhookEndpoint
    t = WebhookDelivery.__table__
    # Claim: only one worker sends a given attempt.
    now = now or _now()
    res = db.session.execute(update(t).where(t.c.id == delivery_id, t.c.status == "pending")
                             .values(status="sending", last_attempt_at=now))
    db.session.commit()
    if res.rowcount != 1:
        return None
    d = db.session.get(WebhookDelivery, delivery_id)
    db.session.refresh(d)
    endpoint = db.session.get(WebhookEndpoint, d.endpoint_id)
    if endpoint is None or not endpoint.is_active or endpoint.deleted_at is not None:
        d.status = "cancelled"
        d.next_attempt_at = None
        db.session.commit()
        return d
    secret = _decrypt(endpoint.secret_enc)
    d.attempts = (d.attempts or 0) + 1
    d.last_attempt_at = now
    ok, code, error = False, None, None
    if not secret:
        error = "secret_unavailable"
    else:
        headers = {"Content-Type": "application/json", "User-Agent": "Siiqo-Webhooks/1.0",
                   SIGNATURE_HEADER: signature_header(secret, d.body, int(now.timestamp())),
                   "Siiqo-Event-Id": d.event_id, "Siiqo-Event-Type": d.event_type}
        try:
            response = _post(endpoint.url, d.body, headers)
            code = int(response.status_code)
            ok = 200 <= code < 300
            if not ok:
                error = f"http_{code}"
        except Exception as e:  # noqa: BLE001
            error = type(e).__name__
    d.last_status_code = code
    d.last_error = None if ok else (error or "failed")[:255]
    if ok:
        d.status = "delivered"
        d.delivered_at = now
        d.next_attempt_at = None
        endpoint.last_success_at = now
    else:
        endpoint.last_failure_at = now
        if d.attempts < MAX_ATTEMPTS:
            d.status = "retrying"
            d.next_attempt_at = now + BACKOFF[d.attempts - 1]
        else:
            d.status = "failed"
            d.next_attempt_at = None
            last_ok = _aware(endpoint.last_success_at)
            if last_ok is None or last_ok < _aware(d.created_at):
                _disable(endpoint, now)
    db.session.commit()
    return d


def _disable(endpoint, now):
    from app.models.platform import WebhookDelivery
    endpoint.is_active = False
    endpoint.disabled_at = now
    endpoint.disabled_reason = "too_many_failures"
    WebhookDelivery.query.filter(WebhookDelivery.endpoint_id == endpoint.id,
                                 WebhookDelivery.status.in_(("pending", "retrying"))) \
        .update({"status": "cancelled", "next_attempt_at": None}, synchronize_session=False)
    logger.warning(f"[WEBHOOKS] endpoint {endpoint.id} disabled after repeated failures")


@jobs.handler("webhook_deliver")
def _deliver_job(payload, job):
    d = attempt(int(payload.get("delivery_id") or 0))
    return {"delivery_id": payload.get("delivery_id"), "status": d.status if d else "skipped"}


def run_due_deliveries(now=None, limit=200):
    """Scheduled every minute: queue retries whose backoff has passed."""
    from app.models.platform import WebhookDelivery
    now = now or _now()
    t = WebhookDelivery.__table__
    # A worker that died mid-send leaves "sending": retry it.
    stuck_before = now - timedelta(minutes=15)
    db.session.execute(update(t).where(t.c.status == "sending",
                                       db.func.coalesce(t.c.last_attempt_at, t.c.created_at) < stuck_before)
                       .values(status="retrying", next_attempt_at=now))
    db.session.commit()
    due = [row.id for row in WebhookDelivery.query.filter(WebhookDelivery.status == "retrying",
                                                           WebhookDelivery.next_attempt_at <= now)
           .order_by(WebhookDelivery.next_attempt_at.asc()).limit(limit).all()]
    queued = 0
    for delivery_id in due:
        res = db.session.execute(update(t).where(t.c.id == delivery_id, t.c.status == "retrying")
                                 .values(status="pending"))
        db.session.commit()
        if res.rowcount == 1:
            _queue(delivery_id)
            queued += 1
    return queued


# ── change detection ───────────────────────────────────────────────────────

def _score_band(score, score_max):
    if score is None or not score_max:
        return None
    return min(9, int(score * 10 // score_max))


def _current_state(vendor_id):
    from app.models.user import Storefront
    from app.services import passport as passport_svc
    sf = Storefront.query.filter_by(vendor_id=vendor_id).first()
    if not passport_svc.is_visible(sf):
        return {"visible": False, "slug": sf.store_slug if sf else None, "claims": None}
    claims = passport_svc.build_passport(sf)
    return {"visible": True, "slug": sf.store_slug, "claims": claims, "tier": claims.get("tier"),
            "band": _score_band(claims.get("score"), claims.get("score_max")),
            "verified": bool(claims.get("verified"))}


def evaluate(vendor_id, hint=None):
    """Compare the seller's public state with the last stored one, emit the
    events that follow, and store the new state. Returns the event types."""
    from app.models.platform import PassportEventState
    state = _current_state(vendor_id)
    prev = PassportEventState.query.filter_by(vendor_id=vendor_id).first()
    events = []
    if prev is None:
        # First look at this seller: a baseline, except for what the hook reports.
        if hint == "passport" and not state["visible"]:
            events.append("business.passport_hidden")
        if hint == "verification" and state["visible"] and state["verified"]:
            events.append("business.verified")
        prev = PassportEventState(vendor_id=vendor_id, visible=state["visible"], verified=False)
        db.session.add(prev)
    else:
        if prev.visible and not state["visible"]:
            events.append("business.passport_hidden")
        if state["visible"]:
            if state["verified"] and not prev.verified:
                events.append("business.verified")
            elif prev.verified and not state["verified"]:
                events.append("business.verification_lapsed")
            if prev.tier is not None and (state["tier"] != prev.tier or state["band"] != prev.score_band):
                events.append("business.trust_changed")
    prev.visible = state["visible"]
    if state["visible"]:
        prev.tier, prev.score_band, prev.verified = state["tier"], state["band"], state["verified"]
    db.session.commit()

    for event_type in events:
        if event_type == "business.passport_hidden":
            data = {"slug": state["slug"]}
        else:
            data = {"slug": state["slug"], **state["claims"]}
        emit(event_type, data)
    return events


@jobs.handler("platform_passport_check")
def _passport_check_job(payload, job):
    events = evaluate(int(payload["vendor_id"]), hint=payload.get("hint"))
    return {"events": events}


def notify_passport_change(vendor_id, hint=None):
    """Called from the Passport, verification and trust code after they
    commit. Costs one query when no platform has a live webhook; never raises."""
    try:
        if not vendor_id or not has_live_endpoints():
            return None
        return jobs.enqueue("platform_passport_check", {"vendor_id": int(vendor_id), "hint": hint},
                            user_id=int(vendor_id))
    except Exception as e:  # noqa: BLE001 — webhooks must never break the caller
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning(f"[WEBHOOKS] passport check for vendor {vendor_id} not queued: {e}")
        return None


def run_passport_sweep(batch=500):
    """Scheduled hourly: catches changes that happen with time (verification
    expiry or pause, plan lapse, Trust Score v1). Skipped when no platform has
    a live webhook."""
    from app.models.user import Storefront
    if not has_live_endpoints():
        return 0
    checked = 0
    last_id = 0
    while True:
        rows = (db.session.query(Storefront.id, Storefront.vendor_id).filter(Storefront.id > last_id)
                .order_by(Storefront.id.asc()).limit(batch).all())
        if not rows:
            break
        for sf_id, vendor_id in rows:
            last_id = sf_id
            try:
                evaluate(vendor_id)
                checked += 1
            except Exception as e:  # noqa: BLE001
                db.session.rollback()
                logger.warning(f"[WEBHOOKS] sweep failed for vendor {vendor_id}: {e}")
    return checked
