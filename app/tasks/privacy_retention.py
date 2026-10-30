"""
Data-retention sweep: removes or reduces personal data we no longer need.

Not scheduled here. Dry run by default (counts only). From a `flask shell`
or a scheduler job inside an app context:

    from app.tasks.privacy_retention import run
    run()              # dry run: what would change
    run(apply=True)    # make the changes

Rules (each count in the report is rows affected / that would be affected):
  password_reset_attempts      rows not updated for 1 day          -> deleted
  delivery_confirmation_codes  expired more than 7 days ago        -> deleted
  platform_events              older than 13 months                -> `properties` cleared
                                                                       (rows kept, so counts stay)
  post_views                   older than 90 days                  -> ip_address, user_agent cleared
  admin_audit_logs             older than 13 months                -> ip_address reduced to its
                                                                       network (IPv4 /24, IPv6 /48),
                                                                       user_agent cleared; the
                                                                       action record itself stays

These are the only tables with IP-address / user-agent columns (checked
across app/models). The periods are product decisions, not legal minimums.
"""
import ipaddress
from datetime import datetime, timedelta, timezone

from sqlalchemy import null

from app.extensions import db

RESET_ATTEMPT_DAYS = 1
DELIVERY_CODE_EXPIRED_DAYS = 7
EVENT_PROPERTIES_DAYS = 396        # ~13 months
POST_VIEW_IP_DAYS = 90
AUDIT_IP_DAYS = 396                # ~13 months
_BATCH = 1000


def _utcnow():
    return datetime.now(timezone.utc)


def truncate_ip(value):
    """Keep only the network part of an IP: 203.0.113.57 -> 203.0.113.0,
    2001:db8:abcd:12::1 -> 2001:db8:abcd::. Anything unparseable -> 'redacted'."""
    if not value:
        return value
    raw = str(value).strip()
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return "redacted"
    prefix = 24 if ip.version == 4 else 48
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False).network_address)


def _is_truncated(value):
    return not value or value == "redacted" or truncate_ip(value) == value


def run(apply=False, now=None):
    from app.models.audit import AdminAuditLog
    from app.models.escrow import DeliveryConfirmationCode
    from app.models.social import PostView
    from app.models.telemetry import PlatformEvent
    from app.models.user import PasswordResetAttempt

    now = now or _utcnow()
    report = {"applied": False}

    # 1. Old reset-code attempt counters.
    q = PasswordResetAttempt.query.filter(
        PasswordResetAttempt.updated_at < now - timedelta(days=RESET_ATTEMPT_DAYS))
    report["password_reset_attempts_deleted"] = q.count()
    if apply:
        q.delete(synchronize_session=False)

    # 2. Delivery confirmation codes long past their expiry.
    q = DeliveryConfirmationCode.query.filter(
        DeliveryConfirmationCode.expires_at < now - timedelta(days=DELIVERY_CODE_EXPIRED_DAYS))
    report["delivery_confirmation_codes_deleted"] = q.count()
    if apply:
        q.delete(synchronize_session=False)

    # 3. Old analytics events: drop the free-form context, keep the row.
    q = PlatformEvent.query.filter(
        PlatformEvent.timestamp < now - timedelta(days=EVENT_PROPERTIES_DAYS),
        PlatformEvent.properties.isnot(None))
    report["platform_events_properties_cleared"] = q.count()
    if apply:
        q.update({PlatformEvent.properties: null()}, synchronize_session=False)

    # 4. Post views: IP and browser string are only useful while fresh.
    q = PostView.query.filter(
        PostView.viewed_at < now - timedelta(days=POST_VIEW_IP_DAYS),
        (PostView.ip_address.isnot(None)) | (PostView.user_agent.isnot(None)))
    report["post_views_ip_cleared"] = q.count()
    if apply:
        q.update({PostView.ip_address: None, PostView.user_agent: None}, synchronize_session=False)

    # 5. Admin audit log: keep who did what, reduce where from.
    cutoff = now - timedelta(days=AUDIT_IP_DAYS)
    cutoff_naive = cutoff.replace(tzinfo=None)  # the column is timezone-naive UTC
    audit_count = 0
    last_id = 0
    while True:
        rows = (AdminAuditLog.query
                .filter(AdminAuditLog.timestamp < cutoff_naive, AdminAuditLog.id > last_id)
                .order_by(AdminAuditLog.id.asc()).limit(_BATCH).all())
        if not rows:
            break
        for row in rows:
            last_id = row.id
            if _is_truncated(row.ip_address) and row.user_agent is None:
                continue
            audit_count += 1
            if apply:
                row.ip_address = truncate_ip(row.ip_address) or "redacted"
                row.user_agent = None
        if apply:
            db.session.flush()
    report["admin_audit_logs_ip_reduced"] = audit_count

    if apply:
        db.session.commit()
        report["applied"] = True
    else:
        db.session.rollback()
    return report


def run_scheduled():
    """Scheduler entry point. Changes data only when PRIVACY_RETENTION_APPLY is
    on (default off), so the periods above can be confirmed before they act."""
    import logging
    import os
    apply = os.environ.get("PRIVACY_RETENTION_APPLY", "false").strip().lower() in ("1", "true", "yes", "on")
    report = run(apply=apply)
    logging.getLogger(__name__).info(f"[PRIVACY_RETENTION] {report}")
    return report
