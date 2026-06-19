from app.extensions import db
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


class PartnerApplication(db.Model):
    __tablename__ = 'partner_applications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    business_name = db.Column(db.String(255), nullable=False)
    service_type = db.Column(db.String(100), nullable=False)
    # LOGISTICS, MARKETING, CREATIVE, FINANCE, CONSULTING

    experience_years = db.Column(db.Integer, default=0)
    portfolio_url = db.Column(db.String(255), nullable=True)
    state_of_operation = db.Column(db.String(100), nullable=True)

    status = db.Column(db.String(50), default='PENDING')
    # PENDING, APPROVED, REJECTED

    # Bank Payout details for logistics partners
    bank_code = db.Column(db.String(20), nullable=True)
    account_number = db.Column(db.String(50), nullable=True)
    account_name = db.Column(db.String(100), nullable=True)

    # Pricing settings for logistics model (FLAT, DISTANCE, API)
    pricing_settings = db.Column(db.JSON, nullable=True, default=dict)

    applied_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Relationships
    user = db.relationship('User', backref='partner_application', uselist=False)


class Referral(db.Model):
    __tablename__ = 'referrals'

    id = db.Column(db.Integer, primary_key=True)
    referrer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    referred_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)

    referral_code_used = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(50), default='PENDING')
    # PENDING → QUALIFIED (after first purchase)

    reward_earned = db.Column(db.Numeric(10, 2), default=0.00)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    # Relationships
    referrer = db.relationship('User', foreign_keys=[referrer_id], backref='referrals_made')
    referred = db.relationship('User', foreign_keys=[referred_id])


class PartnerStaff(db.Model):
    """
    For logistics partners to add riders/staff.
    """
    __tablename__ = 'partner_staff'

    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    staff_name = db.Column(db.String(100), nullable=False)
    staff_phone = db.Column(db.String(20), nullable=False)
    staff_email = db.Column(db.String(120), nullable=True)
    staff_role = db.Column(db.String(50), default='RIDER')  # RIDER, DISPATCHER, MANAGER

    is_active = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # Relationships
    partner = db.relationship('User', backref='staff_members')
