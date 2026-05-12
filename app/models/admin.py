from app.extensions import db
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash


def utcnow():
    return datetime.now(timezone.utc)


class AdminUser(db.Model):
    __tablename__ = 'admin_users'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)

    role = db.Column(db.String(20), default='ADMIN')
    # SUPERADMIN, ADMIN, SUPPORT

    is_active = db.Column(db.Boolean, default=True)
    last_login = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class PlatformSetting(db.Model):
    __tablename__ = 'platform_settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=False)
    description = db.Column(db.String(255), nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SubscriptionPlan(db.Model):
    __tablename__ = 'subscription_plans'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    # FREE, BASIC, PRO, ENTERPRISE
    price_ngn = db.Column(db.Numeric(10, 2), nullable=False)
    features = db.Column(db.JSON, nullable=True)
    is_active = db.Column(db.Boolean, default=True)


class VendorSubscription(db.Model):
    __tablename__ = 'vendor_subscriptions'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey('subscription_plans.id'), nullable=False)

    status = db.Column(db.String(20), default='ACTIVE')
    # ACTIVE, EXPIRED, CANCELLED
    start_date = db.Column(db.DateTime(timezone=True), default=utcnow)
    end_date = db.Column(db.DateTime(timezone=True), nullable=False)

    # Relationships
    vendor = db.relationship('User', backref='subscriptions')
    plan = db.relationship('SubscriptionPlan')


class SponsoredListing(db.Model):
    __tablename__ = 'sponsored_listings'

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    amount_paid = db.Column(db.Numeric(10, 2), nullable=False)
    impressions = db.Column(db.Integer, default=0)
    clicks = db.Column(db.Integer, default=0)

    start_date = db.Column(db.DateTime(timezone=True), default=utcnow)
    end_date = db.Column(db.DateTime(timezone=True), nullable=False)
    is_active = db.Column(db.Boolean, default=True)


class Favorite(db.Model):
    __tablename__ = 'favourites'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    storefront_id = db.Column(db.Integer, db.ForeignKey('storefronts.id'), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
