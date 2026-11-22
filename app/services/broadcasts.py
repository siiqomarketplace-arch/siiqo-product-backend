"""Admin email broadcasts, sent in the background in batches.

    job = create_job("SELLERS_NO_SALE", subject, body, admin_id=1, api_base="https://api.siiqo.com")
    start(job.id)          # thread (or inline in tests / JOBS_MODE=sync)

Each recipient is a row (broadcast_recipients) with its own status, so a job
picks up where it stopped after a restart: ``resume_stalled`` (scheduled
every few minutes) runs any job whose lease has run out. A job is held by one
process at a time through a lease taken with a conditional UPDATE.

Audiences: everyone (ALL), role groups (VENDORS, BUYERS, PARTNERS), typed
addresses (CUSTOM) and seller segments: SELLERS_ALL (has a store),
SELLERS_NO_SALE (≥1 active product, never a real paid order),
SELLERS_NO_PRODUCTS (store, no active product) and SELLERS_ACTIVATED (≥1
real paid order). Unsubscribed people (users.is_subscribed_to_broadcasts
false, the marketing-email consent) and suspended accounts are never included.

Personalisation: {first_name} and {store_name} in the subject and body.
"""
from __future__ import annotations

import html
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import and_, exists, func, or_, update

from app.extensions import db
from app.models.growth import BroadcastJob, BroadcastRecipient

log = logging.getLogger(__name__)

SEGMENTS = {
    "ALL": "Everyone",
    "VENDORS": "All vendor accounts",
    "BUYERS": "All buyer accounts",
    "PARTNERS": "Logistics partners",
    "SELLERS_ALL": "All sellers (have a store)",
    "SELLERS_NO_SALE": "Sellers with products, no sale yet",
    "SELLERS_NO_PRODUCTS": "Sellers with no products",
    "SELLERS_ACTIVATED": "Activated sellers (have a paid order)",
    "CUSTOM": "Typed email addresses",
}
UNSUB_SALT = "siiqo-unsubscribe-v2"
LEASE = timedelta(minutes=3)


def batch_size() -> int:
    try:
        return max(1, min(500, int(os.environ.get("BROADCAST_BATCH_SIZE", 50))))
    except (TypeError, ValueError):
        return 50


def batch_pause_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("BROADCAST_BATCH_PAUSE_SECONDS", 2)))
    except (TypeError, ValueError):
        return 2.0


def _now():
    return datetime.now(timezone.utc)


# ── unsubscribe token ───────────────────────────────────────────────────────

def _serializer():
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt=UNSUB_SALT)


def unsubscribe_token(user_id: int) -> str:
    """A signed token holding the user id only (no email in the link). It does not expire."""
    return _serializer().dumps({"u": int(user_id)})


def read_unsubscribe_token(token: str) -> int | None:
    try:
        data = _serializer().loads(token or "")
    except (BadSignature, ValueError, TypeError):
        return None
    uid = data.get("u") if isinstance(data, dict) else None
    return int(uid) if isinstance(uid, int) else None


def unsubscribe_url(api_base: str, user_id: int) -> str:
    return f"{(api_base or '').rstrip('/')}/api/auth/unsubscribe?t={unsubscribe_token(user_id)}"


# ── audiences ───────────────────────────────────────────────────────────────

def is_valid_broadcast_email(email: str) -> bool:
    """False for placeholder / ghost addresses that should never get broadcasts."""
    if not email or '@' not in email:
        return False
    domain = email.rsplit('@', 1)[-1].lower()
    if domain.endswith('.local') or domain in ('example.com', 'test.com', 'localhost'):
        return False
    return '.' in domain


def _real_paid_exists(user_col):
    from app.models.order import Order
    from app.services.activation import real_paid_filter
    return exists().where(and_(Order.vendor_id == user_col, real_paid_filter()))


def _active_product_exists(store_col):
    from app.models.product import Product
    return exists().where(and_(Product.storefront_id == store_col, Product.is_active.is_(True),
                               or_(Product.is_deleted.is_(False), Product.is_deleted.is_(None))))


def audience_query(segment: str):
    """Query of (user_id, email, first_name, store_name) for a segment, before
    the subscription filter. None for CUSTOM or an unknown segment."""
    from app.models.user import Storefront, User
    segment = (segment or "").upper()
    cols = (User.id, User.email, User.first_name, Storefront.store_name)
    base = db.session.query(*cols).filter(or_(User.is_active.is_(True), User.is_active.is_(None)))
    if segment in ("ALL", "VENDORS", "BUYERS", "PARTNERS"):
        q = base.outerjoin(Storefront, Storefront.vendor_id == User.id)
        role = {"VENDORS": "VENDOR", "BUYERS": "BUYER", "PARTNERS": "PARTNER"}.get(segment)
        return q.filter(User.role == role) if role else q
    if not segment.startswith("SELLERS_") or segment not in SEGMENTS:
        return None
    q = base.join(Storefront, Storefront.vendor_id == User.id).filter(Storefront.closed_at.is_(None))
    if segment == "SELLERS_NO_SALE":
        q = q.filter(_active_product_exists(Storefront.id), ~_real_paid_exists(User.id))
    elif segment == "SELLERS_NO_PRODUCTS":
        q = q.filter(~_active_product_exists(Storefront.id))
    elif segment == "SELLERS_ACTIVATED":
        q = q.filter(_real_paid_exists(User.id))
    return q


def _subscribed():
    from app.models.user import User
    return or_(User.is_subscribed_to_broadcasts.is_(True), User.is_subscribed_to_broadcasts.is_(None))


def parse_custom_emails(raw) -> list[str]:
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = re.split(r"[,\s;]+", str(raw or ""))
    seen, out = set(), []
    for e in items:
        e = str(e).strip().lower()
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def audience_counts(segment: str, custom_emails=None) -> dict:
    """{segment, label, matching, unsubscribed, will_receive} - shown before sending."""
    from app.models.user import User
    segment = (segment or "ALL").upper()
    if segment == "CUSTOM":
        emails = parse_custom_emails(custom_emails)
        unsub = (User.query.filter(func.lower(User.email).in_(emails),
                                   User.is_subscribed_to_broadcasts.is_(False)).count() if emails else 0)
        valid = sum(1 for e in emails if is_valid_broadcast_email(e))
        return {"segment": segment, "label": SEGMENTS[segment], "matching": len(emails),
                "unsubscribed": unsub, "will_receive": max(0, valid - unsub)}
    q = audience_query(segment)
    if q is None:
        raise ValueError("Unknown audience")
    sub = q.subquery()
    matching = db.session.query(func.count()).select_from(sub).scalar() or 0
    rows = q.filter(_subscribed()).with_entities(User.email).all()
    will = sum(1 for (e,) in rows if is_valid_broadcast_email(e))
    return {"segment": segment, "label": SEGMENTS[segment], "matching": int(matching),
            "unsubscribed": int(matching) - len(rows), "will_receive": will}


# ── jobs ────────────────────────────────────────────────────────────────────

def create_job(segment, subject, body, *, critical=False, admin_id=None, api_base=None, custom_emails=None):
    """Make the job and one row per recipient (deduplicated by email). Commits."""
    from app.models.user import Storefront, User
    segment = (segment or "ALL").upper()
    if segment == "CUSTOM":
        emails = parse_custom_emails(custom_emails)
        if not emails:
            raise ValueError("No valid email addresses provided for custom broadcast")
        known = {u.email.lower(): (u, sf) for u, sf in
                 db.session.query(User, Storefront).outerjoin(Storefront, Storefront.vendor_id == User.id)
                 .filter(func.lower(User.email).in_(emails)).all()}
        rows = []
        for e in emails:
            u, sf = known.get(e, (None, None))
            if u is not None and u.is_subscribed_to_broadcasts is False:
                continue
            rows.append((u.id if u else None, e, u.first_name if u else None, sf.store_name if sf else None))
    else:
        q = audience_query(segment)
        if q is None:
            raise ValueError("Unknown audience")
        rows = q.filter(_subscribed()).all()

    job = BroadcastJob(audience=segment, subject=subject[:255], body=body, critical=bool(critical),
                       api_base=(api_base or "")[:255] or None, status="queued", created_by_admin_id=admin_id)
    db.session.add(job)
    db.session.flush()
    seen, recipients, skipped = set(), [], 0
    for uid, email, first_name, store_name in rows:
        email = (email or "").strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        ok = is_valid_broadcast_email(email)
        skipped += 0 if ok else 1
        recipients.append({"job_id": job.id, "user_id": uid, "email": email[:255],
                           "first_name": (first_name or None) and first_name[:80],
                           "store_name": (store_name or None) and store_name[:120],
                           "status": "pending" if ok else "skipped",
                           "error": None if ok else "Not a real address"})
    if recipients:
        db.session.bulk_insert_mappings(BroadcastRecipient, recipients)
    job.total = len(recipients)
    job.skipped = skipped
    if not recipients or skipped == len(recipients):
        job.status = "done"
        job.finished_at = _now()
    db.session.commit()
    return job


def personalise(text: str, first_name: str | None, store_name: str | None, *, escape: bool = True) -> str:
    fn = (first_name or "").strip() or "there"
    sn = (store_name or "").strip() or "your store"
    if escape:
        fn, sn = html.escape(fn), html.escape(sn)
    return (text or "").replace("{first_name}", fn).replace("{store_name}", sn)


def _take_lease(job_id: int, owner: str) -> bool:
    now = _now()
    res = db.session.execute(
        update(BroadcastJob)
        .where(BroadcastJob.id == job_id, BroadcastJob.status.in_(("queued", "sending")),
               or_(BroadcastJob.lease_until.is_(None), BroadcastJob.lease_until < now,
                   BroadcastJob.lease_owner == owner))
        .values(status="sending", lease_owner=owner, lease_until=now + LEASE,
                started_at=func.coalesce(BroadcastJob.started_at, now))
        .execution_options(synchronize_session=False))
    db.session.commit()
    return res.rowcount == 1


def _send_one(job: BroadcastJob, r: BroadcastRecipient, mailto: str | None) -> tuple[bool, str | None]:
    from app.utils.email import send_siiqo_email, unsubscribe_headers
    unsub = None
    if not job.critical and r.user_id and job.api_base:
        unsub = unsubscribe_url(job.api_base, r.user_id)
    headers = {} if job.critical else unsubscribe_headers(unsub, mailto)
    try:
        ok = send_siiqo_email(
            to_email=r.email,
            subject=personalise(job.subject, r.first_name, r.store_name, escape=False),
            template_name="broadcast",
            first_name=r.first_name or "Siiqo Member",
            body_content=personalise(job.body, r.first_name, r.store_name),
            unsubscribe_link=unsub,
            headers=headers,
            wait=True,
        )
        return bool(ok), None if ok else "Not sent"
    except Exception as e:  # noqa: BLE001 - one bad address never stops the job
        return False, type(e).__name__


def run_job(job_id: int, *, owner: str | None = None, max_batches: int | None = None,
            pause: float | None = None) -> BroadcastJob | None:
    """Send the job's pending recipients in batches. Safe to call again: only
    pending rows are sent, and the lease keeps a second process out."""
    from app.utils.email import unsubscribe_mailto
    owner = owner or f"{os.getpid()}-{secrets.token_hex(4)}"
    pause = batch_pause_seconds() if pause is None else pause
    if not _take_lease(job_id, owner):
        return db.session.get(BroadcastJob, job_id)
    mailto = unsubscribe_mailto()
    batches = 0
    while True:
        job = db.session.get(BroadcastJob, job_id)
        db.session.refresh(job)
        if job.status != "sending" or job.lease_owner != owner:
            return job  # cancelled, or another process took over
        batch = (BroadcastRecipient.query.filter_by(job_id=job_id, status="pending")
                 .order_by(BroadcastRecipient.id).limit(batch_size()).all())
        if not batch:
            job.status = "done"
            job.finished_at = _now()
            job.lease_owner = None
            job.lease_until = None
            db.session.commit()
            return job
        for r in batch:
            ok, err = _send_one(job, r, mailto)
            r.status = "sent" if ok else "failed"
            r.error = err
            r.attempted_at = _now()
            if ok:
                job.sent = (job.sent or 0) + 1
            else:
                job.failed = (job.failed or 0) + 1
        job.lease_until = _now() + LEASE
        db.session.commit()
        batches += 1
        if max_batches is not None and batches >= max_batches:
            # Stop here (tests, or a caller sharing time); the lease runs out
            # and resume_stalled picks the job up again.
            job.lease_until = _now() - timedelta(seconds=1)
            db.session.commit()
            return job
        if pause:
            time.sleep(pause)


def resume_stalled(now=None) -> int:
    """Run jobs left queued/sending whose lease ran out (after a restart). Scheduled."""
    now = now or _now()
    ids = [j.id for j in BroadcastJob.query.filter(
        BroadcastJob.status.in_(("queued", "sending")),
        or_(BroadcastJob.lease_until.is_(None), BroadcastJob.lease_until < now)).order_by(BroadcastJob.id).all()]
    for job_id in ids:
        run_job(job_id)
    return len(ids)


def cancel(job_id: int) -> BroadcastJob | None:
    job = db.session.get(BroadcastJob, job_id)
    if job is None:
        return None
    if job.status in ("queued", "sending"):
        job.status = "cancelled"
        job.finished_at = _now()
        job.lease_owner = None
        job.lease_until = None
        db.session.commit()
    return job


def resume(job_id: int) -> BroadcastJob | None:
    """Admin 'resume': a cancelled or failed job with pending rows goes back to queued."""
    job = db.session.get(BroadcastJob, job_id)
    if job is None:
        return None
    pending = BroadcastRecipient.query.filter_by(job_id=job_id, status="pending").count()
    if job.status in ("cancelled", "failed") and pending:
        job.status = "queued"
        job.finished_at = None
        job.lease_owner = None
        job.lease_until = None
        db.session.commit()
    return job


def _sync_mode() -> bool:
    raw = (os.environ.get("JOBS_MODE") or current_app.config.get("JOBS_MODE") or "").strip().lower()
    return raw == "sync" or (not raw and bool(current_app.config.get("TESTING")))


def start(job_id: int) -> None:
    """Send in the background (a daemon thread with its own app context), or
    inline when jobs run synchronously (tests)."""
    if _sync_mode():
        run_job(job_id, pause=0)
        return
    app = current_app._get_current_object()

    def _run():
        with app.app_context():
            try:
                run_job(job_id)
            except Exception:  # noqa: BLE001 - the sweep resumes it
                log.exception("Broadcast job %s stopped; it will resume from the sweep", job_id)
            finally:
                db.session.remove()

    threading.Thread(target=_run, name=f"broadcast-{job_id}", daemon=True).start()
