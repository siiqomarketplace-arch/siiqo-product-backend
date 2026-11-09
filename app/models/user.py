from app.extensions import db
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
import uuid


def utcnow():
    return datetime.now(timezone.utc)


class UserRole:
    BUYER = 'BUYER'
    VENDOR = 'VENDOR'
    PARTNER = 'PARTNER'
    ADMIN = 'ADMIN'
    RIDER = 'RIDER'


class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)

    first_name = db.Column(db.String(50), nullable=True)
    last_name = db.Column(db.String(50), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    profile_pic = db.Column(db.String(255), nullable=True)

    role = db.Column(db.String(20), default=UserRole.BUYER, nullable=False)
    is_verified = db.Column(db.Boolean, default=False)   # email verified
    is_active = db.Column(db.Boolean, default=True)      # account not suspended

    # OTP — shared for email verification + password reset
    reset_otp = db.Column(db.String(10), nullable=True)
    otp_expiry = db.Column(db.DateTime(timezone=True), nullable=True)

    # Newsletter/Broadcasts
    is_subscribed_to_broadcasts = db.Column(db.Boolean, default=True)

    # Referral & Loyalty
    referral_code = db.Column(db.String(20), unique=True, nullable=True, index=True)
    points_balance = db.Column(db.Numeric(10, 2), default=0.00)
    nin = db.Column(db.String(11), nullable=True)
    telegram_id = db.Column(db.String(50), unique=True, nullable=True, index=True)
    telegram_notification_prefs = db.Column(db.JSON, nullable=True, default=dict)
    # Language for emails, notifications and the website (D19). NULL = not
    # chosen (English). Additive nullable column; see docs/TRANSLATIONS.md.
    preferred_locale = db.Column(db.String(8), nullable=True)

    # Location (for hyper-local features)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(100), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # Relationships
    storefront = db.relationship(
        'Storefront', back_populates='vendor', uselist=False, cascade="all, delete-orphan"
    )

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def generate_referral_code(self):
        if not self.referral_code:
            self.referral_code = str(uuid.uuid4()).replace('-', '')[:8].upper()

    @property
    def full_name(self) -> str:
        """Returns display name: full name if set, otherwise username from email prefix."""
        name = f"{self.first_name or ''} {self.last_name or ''}".strip()
        if name:
            return name
        # Generate a friendly username from email (e.g. "john.doe@gmail.com" → "john.doe")
        prefix = self.email.split('@')[0]
        # Replace dots/underscores/hyphens with spaces and title-case
        friendly = prefix.replace('.', ' ').replace('_', ' ').replace('-', ' ').title()
        return friendly

    @property
    def trust_score_or_default(self) -> int:
        from app.services import trust_v1
        if trust_v1.is_live():  # v1: 0-100, None below the minimum history
            return trust_v1.public_v1(self.id)["score"]
        if not self.trust_profile:
            return 500
        return self.trust_profile.total_trust_score

    @property
    def trust_score_max(self) -> int:
        """Top of the scale for trust_score_or_default: 100 for v1, 1000 for the old score."""
        from app.services import trust_v1
        return trust_v1.SCORE_MAX if trust_v1.is_live() else 1000

    @property
    def trust_tier_or_default(self) -> str:
        from app.services import trust_v1
        if trust_v1.is_live():  # v1: NEW below the minimum history
            return trust_v1.public_v1(self.id)["tier"]
        if not self.trust_profile:
            return 'SILVER'
        return self.trust_profile.trust_tier

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "role": self.role,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": self.full_name,  # always computed, never empty
            "name": self.full_name,       # alias for frontend compatibility
            "phone": self.phone,
            "profile_pic": self.profile_pic,
            "is_verified": self.is_verified,
            "is_active": self.is_active,
            "is_subscribed_to_broadcasts": self.is_subscribed_to_broadcasts,
            "referral_code": self.referral_code or "",
            "points_balance": float(self.points_balance or 0),
            "city": self.city,
            "state": self.state,
            "trust_score": self.trust_score_or_default,
            "trust_score_max": self.trust_score_max,
            "trust_tier": self.trust_tier_or_default,
            "telegram_id": self.telegram_id,
            "telegram_notification_prefs": self.telegram_notification_prefs or {},
            "preferred_locale": self.preferred_locale,
        }


def _storefront_trust(sf, with_label=True) -> dict:
    from app.services.search import storefront_trust
    return storefront_trust(sf, with_label=with_label)


class Storefront(db.Model):
    __tablename__ = 'storefronts'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)

    store_name = db.Column(db.String(100), nullable=False)
    store_slug = db.Column(db.String(100), unique=True, nullable=False, index=True)
    store_description = db.Column(db.Text, nullable=True)
    store_logo = db.Column(db.String(255), nullable=True)

    # Location
    address = db.Column(db.String(255), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(100), nullable=True)
    country = db.Column(db.String(100), default='Nigeria')

    # Bank Details for Escrow Payouts
    bank_code = db.Column(db.String(20), nullable=True)
    account_number = db.Column(db.String(50), nullable=True)
    account_name = db.Column(db.String(100), nullable=True)
    # Paystack subaccount code (for split payments at checkout)
    paystack_subaccount_code = db.Column(db.String(100), nullable=True)

    # Logistics Settings
    logistics_settings = db.Column(db.JSON, nullable=True, default=list)
    # Seller's own delivery fees and pickup area (app/services/delivery.py).
    delivery_settings = db.Column(db.JSON, nullable=True)

    # Storefront Builder & Customization
    theme_color = db.Column(db.String(20), default='#0b1b3b')
    banner_url = db.Column(db.String(255), nullable=True)
    custom_domain = db.Column(db.String(255), unique=True, nullable=True)

    # Status flags
    is_verified = db.Column(db.Boolean, default=False)   # admin approved
    is_published = db.Column(db.Boolean, default=False)  # vendor published
    is_pro_verified = db.Column(db.Boolean, default=False)  # Pro Verified badge subscription (₦2,500/yr)
    pro_verified_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Analytics & Onboarding
    view_count = db.Column(db.Integer, default=0)
    onboarding_emails_sent = db.Column(db.JSON, nullable=True, default=dict)

    # Extended profile
    phone = db.Column(db.String(20), nullable=True)
    website = db.Column(db.String(255), nullable=True)
    cac_reg = db.Column(db.String(100), nullable=True)
    account_type = db.Column(db.String(20), default='INDIVIDUAL', nullable=False)
    nin_document_url = db.Column(db.String(255), nullable=True)
    cac_document_url = db.Column(db.String(255), nullable=True)
    verification_status = db.Column(db.String(20), default='NOT_SUBMITTED', nullable=False)
    template_options = db.Column(db.JSON, nullable=True)
    social_links = db.Column(db.JSON, nullable=True)
    working_hours = db.Column(db.JSON, nullable=True)

    # SEO
    meta_title = db.Column(db.String(255), nullable=True)
    meta_description = db.Column(db.Text, nullable=True)

    # Ad Tracking Pixels — self-serve, vendor-owned
    # Vendors paste their pixel IDs in Settings → Marketing & Ads.
    # Siiqo injects the pixel scripts into their storefront automatically.
    meta_pixel_id      = db.Column(db.String(50), nullable=True)   # Meta / Facebook Pixel
    tiktok_pixel_id    = db.Column(db.String(50), nullable=True)   # TikTok Pixel
    ga4_measurement_id = db.Column(db.String(50), nullable=True)   # Google Analytics 4 (G-XXXXXXXX)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # Relationships
    vendor = db.relationship('User', back_populates='storefront')
    products = db.relationship('Product', back_populates='storefront', cascade="all, delete-orphan")

    @property
    def is_live(self) -> bool:
        """True when vendor has published their storefront and account is active."""
        vendor_active = self.vendor.is_active if self.vendor else True
        return bool(self.is_published and vendor_active)

    @property
    def is_pro_active(self) -> bool:
        """True when vendor is Pro Verified and subscription has not expired."""
        now_dt = utcnow()
        return bool(
            self.is_pro_verified and
            (not self.pro_verified_expires_at or self.pro_verified_expires_at > now_dt)
        )

    def to_public_dict(self, include_sensitive: bool = False, trust_label: bool = True) -> dict:
        # Avoid circular imports by loading models inline
        from app.models.community import Review
        from app.models.product import Product

        try:
            approved_reviews = Review.query.filter_by(vendor_id=self.vendor_id, is_approved=True).all()
            ratings = [r.vendor_rating for r in approved_reviews]
            avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None
            review_count = len(ratings)
        except Exception:
            avg_rating = None
            review_count = 0

        try:
            p = Product.query.filter(
                Product.storefront_id == self.id,
                Product.is_active == True,
                Product.latitude != None,
                Product.longitude != None
            ).first()
            lat = p.latitude if p else None
            lng = p.longitude if p else None
        except Exception:
            lat = None
            lng = None

        now_dt = utcnow()
        is_pro_active = bool(
            self.is_pro_verified and
            (not self.pro_verified_expires_at or self.pro_verified_expires_at > now_dt)
        )
        # Verification v2: a passed identity check shows the mark only while the
        # seller's Verified Pro plan is active (public_flags checks both).
        v2_verified = False
        try:
            from app.models.verification import VerificationStatus
            v2 = VerificationStatus.query.filter_by(user_id=self.vendor_id).first()
            v2_verified = bool(v2 and v2.public_flags()["identity_verified"])
        except Exception:
            v2_verified = False
        is_pro_active = bool(is_pro_active or v2_verified)
        badge_verified = bool(is_pro_active or self.verification_status == 'VERIFIED')

        data = {
            "id": self.id,
            "store_name": self.store_name,
            "store_slug": self.store_slug,
            "store_description": self.store_description,
            "store_logo": self.store_logo,
            "banner_url": self.banner_url,
            "city": self.city,
            "state": self.state,
            "address": ", ".join(filter(None, [self.address, self.city, self.state])) or None,
            "phone": self.phone,
            "website": self.website,
            "social_links": self.social_links or {},
            "working_hours": self.working_hours or {},
            "theme_color": self.theme_color,
            "template_options": self.template_options or {},
            "is_verified": self.is_verified,
            "is_published": self.is_published,
            "is_live": self.is_live,
            "is_pro_verified": is_pro_active,
            "badge_verified": badge_verified,
            "pro_verified_expires_at": self.pro_verified_expires_at.isoformat() if self.pro_verified_expires_at else None,
            "view_count": self.view_count or 0,
            # ── vendor identity (required for chat / messaging) ──
            "vendor_id": self.vendor_id,
            "user_id": self.vendor_id,
            "vendor_phone": self.phone,
            "whatsapp_link": (
                f"https://wa.me/{str((self.social_links or {}).get('whatsapp') or self.phone or '').strip().replace(' ', '').replace('+', '')}"
                if (self.social_links or {}).get('whatsapp') or self.phone
                else None
            ),
            # ── trust fields ──
            # Same trust as the Market card; no made-up default (NEW, no score).
            **_storefront_trust(self, with_label=trust_label),
            # ── verification fields ──
            "account_type": self.account_type,
            "verification_status": self.verification_status,
            # ── calculated fields ──
            "avg_rating": avg_rating,
            "rating": avg_rating,
            "review_count": review_count,
            "latitude": lat,
            "longitude": lng,
            # ── ad tracking pixels (self-serve) ──
            "meta_pixel_id":      self.meta_pixel_id or None,
            "tiktok_pixel_id":    self.tiktok_pixel_id or None,
            "ga4_measurement_id": self.ga4_measurement_id or None,
        }
        if include_sensitive:
            # Never in public store data: the owner's login email and CAC number.
            data["vendor_email"] = self.vendor.email if self.vendor else None
            data["cac_reg"] = self.cac_reg
            data["nin_document_url"] = self.nin_document_url
            data["cac_document_url"] = self.cac_document_url
        return data


class PasswordResetAttempt(db.Model):
    """Failed reset-code guesses, shared by every worker and server.
    The count belongs to one code (by fingerprint): a new code starts at 0."""
    __tablename__ = 'password_reset_attempts'

    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    otp_fingerprint = db.Column(db.String(64), nullable=False)
    failed_count = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
