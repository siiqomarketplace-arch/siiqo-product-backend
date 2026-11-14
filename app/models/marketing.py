from app.extensions import db
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


class Coupon(db.Model):
    __tablename__ = 'coupons'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    discount_type = db.Column(db.String(20), default='PERCENTAGE')
    # PERCENTAGE, FIXED_AMOUNT
    discount_value = db.Column(db.Numeric(10, 2), nullable=False)

    usage_limit = db.Column(db.Integer, nullable=True)   # None = unlimited
    times_used = db.Column(db.Integer, default=0)

    valid_from = db.Column(db.DateTime(timezone=True), default=utcnow)
    valid_until = db.Column(db.DateTime(timezone=True), nullable=True)

    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    # Relationships
    vendor = db.relationship('User')


class Campaign(db.Model):
    __tablename__ = 'campaigns'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    name = db.Column(db.String(100), nullable=False)
    target_segment = db.Column(db.String(50), nullable=True)
    # ALL, VIP, AT_RISK, NEW

    status = db.Column(db.String(50), default='DRAFT')
    # DRAFT, SCHEDULED, SENT, CANCELLED
    scheduled_date = db.Column(db.DateTime(timezone=True), nullable=True)

    subject = db.Column(db.String(255), nullable=True)
    body = db.Column(db.Text, nullable=True)

    sent_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    # Relationships
    vendor = db.relationship('User')


# ── Marketing system: codes, agents, credit and earnings ────────────────────
# See docs/MARKETING_SYSTEM_PLAN.md and app/services/marketing.py.

class MarketingCampaign(db.Model):
    __tablename__ = 'marketing_campaigns'

    CHANNELS = ('agent', 'flyer', 'influencer', 'radio', 'print', 'article', 'event', 'other')
    STATUSES = ('ACTIVE', 'PAUSED', 'ENDED')

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False, index=True)  # a-z0-9-
    channel = db.Column(db.String(20), nullable=False, default='other')
    location = db.Column(db.String(120), nullable=True)
    budget_ngn = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    spend_ngn = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    status = db.Column(db.String(10), nullable=False, default='ACTIVE')
    notes = db.Column(db.Text, nullable=True)
    created_by_admin_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class MarketingCode(db.Model):
    """One short code per person, place or printed thing: siiqo.com/r/<CODE>."""
    __tablename__ = 'marketing_codes'

    KINDS = ('agent', 'location', 'flyer', 'influencer', 'media', 'campaign',
             'seller_sticker', 'seller_invite')

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(24), unique=True, nullable=False, index=True)  # stored upper-case
    kind = db.Column(db.String(20), nullable=False)
    label = db.Column(db.String(120), nullable=True)
    destination = db.Column(db.String(300), nullable=False, default='/')  # site path, starts with "/"
    campaign_id = db.Column(db.Integer, db.ForeignKey('marketing_campaigns.id'), nullable=True, index=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    scan_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    campaign = db.relationship('MarketingCampaign')
    owner = db.relationship('User', foreign_keys=[owner_user_id])


class MarketingScan(db.Model):
    """One counted scan of a code. No IP address or personal data is kept;
    the session id is the anonymous visitor id sent by the website."""
    __tablename__ = 'marketing_scans'

    id = db.Column(db.Integer, primary_key=True)
    code_id = db.Column(db.Integer, db.ForeignKey('marketing_codes.id'), nullable=False, index=True)
    campaign_id = db.Column(db.Integer, nullable=True, index=True)
    session_id = db.Column(db.String(100), nullable=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class AgentProfile(db.Model):
    __tablename__ = 'agent_profiles'

    STATUSES = ('PENDING', 'APPROVED', 'PAUSED', 'REJECTED')
    TYPES = ('field', 'rider', 'campus', 'influencer', 'market')

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    status = db.Column(db.String(10), nullable=False, default='PENDING', index=True)
    agent_type = db.Column(db.String(20), nullable=False, default='field')
    team = db.Column(db.String(60), nullable=True)
    full_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    city = db.Column(db.String(100), nullable=False)
    bank_name = db.Column(db.String(100), nullable=False)
    bank_code = db.Column(db.String(10), nullable=True)
    account_number = db.Column(db.String(20), nullable=False)
    account_name = db.Column(db.String(255), nullable=False)
    code_id = db.Column(db.Integer, db.ForeignKey('marketing_codes.id'), nullable=True)
    agreed_conduct_at = db.Column(db.DateTime(timezone=True), nullable=True)
    approved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    approved_by_admin_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    user = db.relationship('User', foreign_keys=[user_id])
    code = db.relationship('MarketingCode', foreign_keys=[code_id])


class SellerAcquisition(db.Model):
    """Which code brought a seller. One row per seller; the first code wins."""
    __tablename__ = 'seller_acquisitions'

    id = db.Column(db.Integer, primary_key=True)
    seller_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    code_id = db.Column(db.Integer, db.ForeignKey('marketing_codes.id'), nullable=False, index=True)
    agent_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('marketing_campaigns.id'), nullable=True, index=True)
    signed_up_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    setup_at = db.Column(db.DateTime(timezone=True), nullable=True)
    pay_link_at = db.Column(db.DateTime(timezone=True), nullable=True)
    first_paid_order_at = db.Column(db.DateTime(timezone=True), nullable=True)
    first_paid_order_id = db.Column(db.Integer, nullable=True)
    flags = db.Column(db.JSON, nullable=False, default=list)  # list of strings
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    code = db.relationship('MarketingCode')
    seller = db.relationship('User', foreign_keys=[seller_user_id])


class AgentEarning(db.Model):
    """A ledger row. The system pays nothing out: an admin pays the agent by
    hand and marks the row PAID with a reference.

    Agents earn per sale: kind "sale", one row per qualifying order
    (order_id), numbered 1..AGENT_REWARDED_SALES per seller (sale_number).
    A REVERSED row frees its number for a later sale."""
    __tablename__ = 'agent_earnings'
    __table_args__ = (
        # Legacy (setup / first_sale rewards, no longer created). Partial, so it
        # never touches "sale" rows.
        db.Index('uq_agent_earning_once', 'agent_user_id', 'seller_user_id', 'kind', unique=True,
                 postgresql_where=db.text("kind IN ('setup', 'first_sale')"),
                 sqlite_where=db.text("kind IN ('setup', 'first_sale')")),
        # One earning per order, ever (a reversed order is never rewarded again).
        db.Index('uq_agent_earning_order', 'order_id', unique=True,
                 postgresql_where=db.text("order_id IS NOT NULL"),
                 sqlite_where=db.text("order_id IS NOT NULL")),
        # Each sale number is used once per seller among rows still standing,
        # so a seller can never have more live sale rewards than numbers.
        db.Index('uq_agent_earning_sale_slot', 'seller_user_id', 'sale_number', unique=True,
                 postgresql_where=db.text("kind = 'sale' AND status <> 'REVERSED'"),
                 sqlite_where=db.text("kind = 'sale' AND status <> 'REVERSED'")),
    )

    KINDS = ('sale', 'bonus', 'setup', 'first_sale')  # setup / first_sale: old rows only
    STATUSES = ('HELD', 'PAYABLE', 'PAID', 'REVERSED')

    id = db.Column(db.Integer, primary_key=True)
    agent_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    seller_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    kind = db.Column(db.String(12), nullable=False)
    amount_ngn = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(10), nullable=False, default='HELD', index=True)
    order_id = db.Column(db.Integer, nullable=True)       # the sale this rewards ("sale" rows)
    sale_number = db.Column(db.Integer, nullable=True)    # 1..AGENT_REWARDED_SALES ("sale" rows)
    earned_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    payable_at = db.Column(db.DateTime(timezone=True), nullable=True)
    paid_at = db.Column(db.DateTime(timezone=True), nullable=True)
    paid_reference = db.Column(db.String(120), nullable=True)
    reversed_reason = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
