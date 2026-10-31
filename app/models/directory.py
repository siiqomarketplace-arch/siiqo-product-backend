"""
Business directory (Phase 3 item 3-5, docs/SIIQO_2.0_GAP_ANALYSIS_AND_PLAN.md §2.6).

Off-Siiqo businesses imported from permitted sources only. A listing holds
business contact points only (a public business phone, website or social
handle) and never a person's name, email, address or ID number. Listings
carry no trust signals; they read "Unclaimed · Not verified" until the
business proves ownership and claims it.

All tables are new (created by db.create_all()).
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class ListingSource:
    ADMIN_IMPORT = "admin_import"
    SELLER_SUGGESTED = "seller_suggested"
    PARTNER = "partner"
    ALL = (ADMIN_IMPORT, SELLER_SUGGESTED, PARTNER)


class ListingStatus:
    UNCLAIMED = "unclaimed"
    CLAIMED = "claimed"
    REMOVED = "removed"
    ALL = (UNCLAIMED, CLAIMED, REMOVED)


class BusinessListing(db.Model):
    __tablename__ = 'business_listings'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    # Lower-case letters and digits only, for exact name matching (/start name check).
    name_key = db.Column(db.String(200), nullable=False, index=True)
    category = db.Column(db.String(100), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(100), nullable=True)
    # Business contact points only, stored normalised.
    phone = db.Column(db.String(20), nullable=True)            # 234XXXXXXXXXX
    website = db.Column(db.String(255), nullable=True)         # domain only
    social_network = db.Column(db.String(20), nullable=True)   # instagram | tiktok | x | facebook
    social_handle = db.Column(db.String(100), nullable=True)   # without @

    source = db.Column(db.String(20), nullable=False, default=ListingSource.ADMIN_IMPORT)
    source_note = db.Column(db.String(500), nullable=False)
    import_batch_id = db.Column(db.Integer, nullable=True, index=True)

    status = db.Column(db.String(12), nullable=False, default=ListingStatus.UNCLAIMED, index=True)
    claimed_storefront_id = db.Column(db.Integer, nullable=True, index=True)
    claimed_user_id = db.Column(db.Integer, nullable=True, index=True)
    claimed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    removed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    removed_reason = db.Column(db.String(200), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class BusinessListingIdentifier(db.Model):
    """The directory side of the identifier graph: normalised phone, domain and
    handle values of a listing, same kinds and formats as seller_identifiers
    (app/services/foundations.py), source 'directory'."""
    __tablename__ = 'business_listing_identifiers'
    __table_args__ = (db.UniqueConstraint('listing_id', 'kind', 'value', name='uq_listing_identifier'),)

    id = db.Column(db.Integer, primary_key=True)
    listing_id = db.Column(db.Integer, nullable=False, index=True)
    kind = db.Column(db.String(30), nullable=False)    # phone | domain | handle
    value = db.Column(db.String(255), nullable=False, index=True)
    source = db.Column(db.String(30), nullable=False, default='directory')
    first_seen = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class DirectoryImportBatch(db.Model):
    """One CSV upload. The dry run stores the parsed rows; apply inserts exactly
    those rows (re-checking duplicates), once."""
    __tablename__ = 'directory_import_batches'

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, nullable=False)
    source = db.Column(db.String(20), nullable=False)
    source_note = db.Column(db.String(500), nullable=False)
    filename = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(12), nullable=False, default='dry_run')   # dry_run | applied
    rows = db.Column(db.JSON, nullable=False, default=list)
    summary = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    applied_at = db.Column(db.DateTime(timezone=True), nullable=True)
    applied_by = db.Column(db.Integer, nullable=True)


class DirectoryClaim(db.Model):
    """A business proving it owns a listing, either to claim it or to have it
    removed. Phone: a one-time code to the listing's public phone. Domain or
    handle: the Phase 2B ownership checks (DNS TXT, meta tag, bio code)."""
    __tablename__ = 'directory_claims'

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(32), nullable=False, unique=True, index=True)
    listing_id = db.Column(db.Integer, nullable=False, index=True)
    purpose = db.Column(db.String(10), nullable=False)          # claim | removal
    method = db.Column(db.String(10), nullable=False)           # phone | domain | handle
    user_id = db.Column(db.Integer, nullable=True, index=True)  # required for claims
    # Duck-typed for app/services/ownership.py: kind, network, value, token.
    kind = db.Column(db.String(10), nullable=True)              # domain | handle
    network = db.Column(db.String(20), nullable=False, default='')
    value = db.Column(db.String(255), nullable=True)
    token = db.Column(db.String(64), nullable=False)
    code_hash = db.Column(db.String(64), nullable=True)
    code_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    # pending | checking | verified | failed | expired
    status = db.Column(db.String(12), nullable=False, default='pending', index=True)
    proved_by = db.Column(db.String(12), nullable=True)         # otp | dns | meta | bio | ownership
    last_result = db.Column(db.String(40), nullable=True)
    reason = db.Column(db.String(300), nullable=True)           # removal: the business's own words
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_checked_at = db.Column(db.DateTime(timezone=True), nullable=True)


class DirectoryAuditEvent(db.Model):
    """Every directory change: imports, removals (admin or the business), claims.
    Admin actions are also written to admin_audit_logs."""
    __tablename__ = 'directory_audit_events'

    id = db.Column(db.Integer, primary_key=True)
    event = db.Column(db.String(40), nullable=False, index=True)
    listing_id = db.Column(db.Integer, nullable=True, index=True)
    batch_id = db.Column(db.Integer, nullable=True, index=True)
    claim_id = db.Column(db.Integer, nullable=True)
    actor_type = db.Column(db.String(10), nullable=False)   # ADMIN | USER | PUBLIC | SYSTEM
    actor_id = db.Column(db.Integer, nullable=True)
    details = db.Column(db.JSON, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
