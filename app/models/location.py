"""
Shop location by NIPOST postcode (separate from identity verification).

New tables, created by db.create_all() at boot; no existing table is altered.

Only the postcode (a public reference), its segment codes and the public
state/LGA names from NIPOST's free reference lists are stored. Never stored:
an Autofill/house address, coordinates, NIN, widget tokens or any NIPOST
response body (NIPOST Acceptable Use Policy).
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class LocationStatus:
    VERIFIED = "verified"
    INVALID = "invalid"
    REMOVED = "removed"


class BusinessLocation(db.Model):
    """One shop location per storefront."""
    __tablename__ = "business_locations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    storefront_id = db.Column(db.Integer, db.ForeignKey("storefronts.id"), nullable=False, unique=True)

    postcode = db.Column(db.String(16), nullable=True)           # compact, upper: FC01A01KP27
    postcode_display = db.Column(db.String(20), nullable=True)   # dashed: FC-01-A01-KP-27
    state_code = db.Column(db.String(4), nullable=True)
    lga_code = db.Column(db.String(4), nullable=True)
    district_code = db.Column(db.String(6), nullable=True)
    area_code = db.Column(db.String(4), nullable=True)
    unit_code = db.Column(db.String(4), nullable=True)
    state_name = db.Column(db.String(80), nullable=True)         # public reference label
    lga_name = db.Column(db.String(120), nullable=True)          # public reference label

    method = db.Column(db.String(10), nullable=True)             # widget | typed
    status = db.Column(db.String(10), nullable=False, default=LocationStatus.VERIFIED, index=True)
    validated_at = db.Column(db.DateTime(timezone=True), nullable=True)
    validation_level = db.Column(db.Integer, nullable=True)      # always 1 (free Validator)
    consent_version = db.Column(db.String(20), nullable=True)
    consent_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class LocationAudit(db.Model):
    """What happened to a shop or venue postcode. No NIPOST payloads."""
    __tablename__ = "location_audit"

    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(30), nullable=False)    # save | remove | venue_save | venue_remove
    user_id = db.Column(db.Integer, nullable=True, index=True)
    postcode = db.Column(db.String(16), nullable=True)
    result = db.Column(db.String(30), nullable=True)     # ok | POSTCODE_INVALID | POSTCODE_BUSY | ...
    at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
