"""
Trust Platform API v1 (Phase 2B, docs/PLATFORM_API_V1.md).

All tables here are new and created by db.create_all(); none adds a column to
an existing table.
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class PlatformUsage(db.Model):
    """Monthly request counter per API client (period = 'YYYY-MM', UTC)."""
    __tablename__ = 'platform_usage'
    __table_args__ = (db.UniqueConstraint('client_id', 'period', name='uq_platform_usage_client_period'),)

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, nullable=False, index=True)
    period = db.Column(db.String(7), nullable=False)
    count = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PlatformClientLimits(db.Model):
    """Scale tier: limits a superadmin agreed with a platform (audit-logged)."""
    __tablename__ = 'platform_client_limits'

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    tier = db.Column(db.String(20), nullable=False, default='scale')
    monthly_limit = db.Column(db.Integer, nullable=False)
    rate_per_minute = db.Column(db.Integer, nullable=False)
    note = db.Column(db.String(255), nullable=True)
    set_by_admin_id = db.Column(db.Integer, nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PlatformSubscription(db.Model):
    """Growth tier for one live API client, paid through a Paystack plan."""
    __tablename__ = 'platform_subscriptions'

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, nullable=False, index=True)
    owner_user_id = db.Column(db.Integer, nullable=True, index=True)
    tier = db.Column(db.String(20), nullable=False, default='growth')
    # ACTIVE | CANCELLED_PENDING_EXPIRY | ENDED
    status = db.Column(db.String(30), nullable=False, default='ACTIVE')
    start_date = db.Column(db.DateTime(timezone=True), default=utcnow)
    end_date = db.Column(db.DateTime(timezone=True), nullable=True)
    paystack_subscription_code = db.Column(db.String(120), nullable=True, index=True)
    paystack_email_token = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)


class PlatformSubscriptionPayment(db.Model):
    """One row per Growth payment; the unique reference makes retries harmless."""
    __tablename__ = 'platform_subscription_payments'

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, nullable=False, index=True)
    subscription_id = db.Column(db.Integer, nullable=True)
    reference = db.Column(db.String(120), nullable=False, unique=True)
    amount_ngn = db.Column(db.Numeric(12, 2), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)


class WebhookEndpoint(db.Model):
    """A platform's HTTPS endpoint. The signing secret is stored encrypted and
    shown to the platform once, when the endpoint is created."""
    __tablename__ = 'platform_webhook_endpoints'

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, nullable=False, index=True)
    url = db.Column(db.String(500), nullable=False)
    secret_enc = db.Column(db.Text, nullable=False)
    secret_prefix = db.Column(db.String(16), nullable=False)
    events = db.Column(db.JSON, nullable=False, default=list)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    disabled_reason = db.Column(db.String(120), nullable=True)
    disabled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_success_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_failure_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)


class WebhookDelivery(db.Model):
    """One event for one endpoint, with its attempts (the delivery log)."""
    __tablename__ = 'platform_webhook_deliveries'

    id = db.Column(db.Integer, primary_key=True)
    endpoint_id = db.Column(db.Integer, nullable=False, index=True)
    event_id = db.Column(db.String(64), nullable=False, index=True)
    event_type = db.Column(db.String(64), nullable=False)
    body = db.Column(db.Text, nullable=False)          # the exact JSON body sent
    # pending | retrying | delivered | failed | cancelled
    status = db.Column(db.String(16), nullable=False, default='pending', index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    next_attempt_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    last_status_code = db.Column(db.Integer, nullable=True)
    last_error = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, index=True)
    last_attempt_at = db.Column(db.DateTime(timezone=True), nullable=True)
    delivered_at = db.Column(db.DateTime(timezone=True), nullable=True)


class PassportEventState(db.Model):
    """The last public state webhooks were computed from, per seller, so an
    event is sent only when something actually changed."""
    __tablename__ = 'platform_passport_event_state'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    visible = db.Column(db.Boolean, nullable=False, default=True)
    tier = db.Column(db.String(20), nullable=True)
    score_band = db.Column(db.Integer, nullable=True)
    verified = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OwnershipClaim(db.Model):
    """A seller proving a website domain or a social handle (DNS TXT, meta tag
    or bio code). Verified claims are added to seller_identifiers."""
    __tablename__ = 'seller_ownership_claims'
    __table_args__ = (db.UniqueConstraint('vendor_id', 'kind', 'network', 'value', name='uq_ownership_claim'),)

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False)          # domain | handle
    network = db.Column(db.String(20), nullable=False, default='')   # '' for domains
    value = db.Column(db.String(255), nullable=False)        # normalised domain or handle
    token = db.Column(db.String(64), nullable=False)
    # pending | checking | verified | lapsed | failed
    status = db.Column(db.String(16), nullable=False, default='pending', index=True)
    method = db.Column(db.String(10), nullable=True)         # dns | meta | bio (what proved it)
    verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    lapsed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_checked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_result = db.Column(db.String(40), nullable=True)
    error_count = db.Column(db.Integer, nullable=False, default=0)
    identifier_added = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)


# ── Evidence API (Phase 4, docs/PLATFORM_API_V1.md "Evidence API") ─────────

class PartnerEvidenceSecret(db.Model):
    """The secret a platform signs Evidence API requests with (one per API
    key). Stored encrypted, shown once; creating a new one replaces the old."""
    __tablename__ = 'partner_evidence_secrets'

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    secret_enc = db.Column(db.Text, nullable=False)
    secret_prefix = db.Column(db.String(16), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)


class PartnerEvidenceReceipt(db.Model):
    """One accepted partner event. The unique (client_id, event_id) makes a
    resent event harmless; body_hash tells a resend from a different event
    reusing the same id."""
    __tablename__ = 'partner_evidence_receipts'
    __table_args__ = (db.UniqueConstraint('client_id', 'event_id', name='uq_partner_evidence_event'),)

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, nullable=False, index=True)
    event_id = db.Column(db.String(100), nullable=False)
    vendor_id = db.Column(db.Integer, nullable=False, index=True)
    evidence_id = db.Column(db.Integer, nullable=True)
    event_type = db.Column(db.String(40), nullable=False)
    body_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)


class PartnerSellerLink(db.Model):
    """A seller's consent for one platform to report to their Passport.
    Created as 'pending' with the first report; partner evidence counts only
    while the seller has it 'accepted'."""
    __tablename__ = 'partner_seller_links'
    __table_args__ = (db.UniqueConstraint('vendor_id', 'client_id', name='uq_partner_seller_link'),)

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, nullable=False, index=True)
    client_id = db.Column(db.Integer, nullable=False, index=True)
    # pending | accepted | revoked
    status = db.Column(db.String(16), nullable=False, default='pending', index=True)
    disputed = db.Column(db.Boolean, nullable=False, default=False)   # seller said the reports are wrong
    report_count = db.Column(db.Integer, nullable=False, default=0)
    first_report_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_report_at = db.Column(db.DateTime(timezone=True), nullable=True)
    accepted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)


class PartnerEvidenceControl(db.Model):
    """Superadmin control over one reporting platform (audit-logged):
    active (default when no row), throttled (its evidence weighs `factor`
    of normal) or revoked (its evidence stops counting; new reports refused)."""
    __tablename__ = 'partner_evidence_controls'

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    status = db.Column(db.String(16), nullable=False, default='active')
    factor = db.Column(db.Numeric(4, 3), nullable=True)
    reason = db.Column(db.String(255), nullable=True)
    set_by_admin_id = db.Column(db.Integer, nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
