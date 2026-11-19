"""
Shop location (NIPOST postcode). Separate from identity verification: own
card, own badge, free for every seller, never chained with NIN/CAC checks,
and never part of the 0-100 Trust Score.

- Off unless POSTCODE_VERIFICATION_ENABLED is true. When off, the endpoints
  answer 404 FEATURE_OFF and no location appears on the Passport or Check.
- Who is asked ("needed"): sellers of physical goods, sellers with their own
  delivery or pickup switched on, and service sellers whose store has a
  street address (customers come to them). Digital-only sellers, and
  service sellers with no address, are never asked.
- Public display: the area only (state + LGA names, e.g. "AMAC, FCT").
  The full postcode is never shown for a shop.
- Removal clears the postcode fields at once; only an audit row is kept.
- No expiry: a postcode stays until the seller changes or removes it.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models.location import BusinessLocation, LocationAudit, LocationStatus
from app.services import postcode as nipost
from app.services.postcode import PostcodeError

CONSENT_VERSION = "2026-11"
ATTEMPTS_PER_HOUR = 10
METHODS = ("widget", "typed")


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _iso(dt):
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def is_enabled() -> bool:
    return os.environ.get("POSTCODE_VERIFICATION_ENABLED", "").strip().lower() in ("1", "true", "yes")


def feature_off():
    return PostcodeError("FEATURE_OFF", "Shop location is not available yet.", 404)


def audit(action: str, user_id, postcode=None, result="ok"):
    db.session.add(LocationAudit(action=action, user_id=user_id, postcode=postcode, result=result))


# ── what the seller sells ───────────────────────────────────────────────────

def _signals(sf) -> dict:
    """Which kinds of selling this store does (active listings only).
    A service counts as on-site when the store has a street address."""
    from app.models.payment_link import PaymentLink
    from app.models.product import Product

    physical = service = digital = False
    rows = (db.session.query(Product.product_type)
            .filter(Product.storefront_id == sf.id, Product.is_active.is_(True),
                    (Product.is_deleted.is_(False)) | (Product.is_deleted.is_(None))).all())
    for (ptype,) in rows:
        ptype = (ptype or "physical").lower()
        if ptype == "physical":
            physical = True
        elif ptype == "service":
            service = True
        else:
            digital = True
    for (ptype,) in (db.session.query(PaymentLink.product_type)
                     .filter(PaymentLink.vendor_id == sf.vendor_id, PaymentLink.status == "ACTIVE")
                     .distinct().all()):
        ptype = (ptype or "service").lower()
        if ptype == "physical":
            physical = True
        elif ptype == "service":
            service = True
        else:
            digital = True

    ds = sf.delivery_settings if isinstance(getattr(sf, "delivery_settings", None), dict) else {}
    delivery = bool(((ds.get("self_delivery") or {}).get("enabled")) or ((ds.get("pickup") or {}).get("enabled")))
    has_address = bool((getattr(sf, "address", None) or "").strip())
    on_site = physical or delivery or (service and has_address)
    remote = digital or (service and not has_address)
    return {"on_site": on_site, "remote": remote}


def needed(sf) -> bool:
    if sf is None:
        return False
    return bool(_signals(sf)["on_site"])


def seller_kind(sf) -> str:
    """'physical' | 'digital' | 'mixed'. A store with nothing listed is 'mixed'
    (the buyer sees the cautious message)."""
    s = _signals(sf)
    if s["on_site"] and s["remote"]:
        return "mixed"
    if s["on_site"]:
        return "physical"
    if s["remote"]:
        return "digital"
    return "mixed"


# ── reading ─────────────────────────────────────────────────────────────────

def row_for(sf) -> BusinessLocation | None:
    if sf is None:
        return None
    return BusinessLocation.query.filter_by(storefront_id=sf.id).first()


def _is_live(row) -> bool:
    return bool(row is not None and row.status == LocationStatus.VERIFIED and row.postcode)


def _label(row) -> str:
    return nipost.area_label(row.state_name, row.lga_name, row.state_code, row.lga_code)


def public_location(sf) -> dict | None:
    """The buyer-facing location claim (area only), or None (flag off, none, removed)."""
    if not is_enabled():
        return None
    row = row_for(sf)
    if not _is_live(row):
        return None
    return {"verified": True, "area_label": _label(row),
            "verified_at": _aware(row.validated_at).date().isoformat() if row.validated_at else None}


def seller_status(user) -> dict:
    sf = user.storefront
    row = row_for(sf)
    location = None
    if _is_live(row):
        location = {
            "postcode_display": row.postcode_display,
            "area_label": _label(row),
            "status": row.status,
            "validated_at": _iso(row.validated_at),
        }
    return {
        "enabled": True,
        "needed": needed(sf),
        "consent_given": bool(row and row.consent_at and row.consent_version == CONSENT_VERSION),
        "location": location,
    }


# ── writing ─────────────────────────────────────────────────────────────────

def _check_rate(user_id):
    since = _now() - timedelta(hours=1)
    count = (LocationAudit.query
             .filter(LocationAudit.user_id == user_id, LocationAudit.action.in_(("save", "venue_save")),
                     LocationAudit.at >= since).count())
    if count >= ATTEMPTS_PER_HOUR:
        raise PostcodeError("TOO_MANY_ATTEMPTS", "Too many tries. Please wait an hour and try again.", 429)


def _validate_logged(user_id, raw, action):
    """Validate with NIPOST; every attempt leaves an audit row (no payloads)."""
    if not is_enabled():
        raise feature_off()
    _check_rate(user_id)
    compact = nipost.normalise(raw) if isinstance(raw, str) else None
    try:
        result = nipost.validate(raw)
    except PostcodeError as exc:
        audit(action, user_id, compact, exc.code)
        db.session.commit()
        raise
    audit(action, user_id, result.postcode, "ok")
    return result


def save(user, *, postcode, consent, method=None) -> BusinessLocation:
    if not is_enabled():
        raise feature_off()
    sf = user.storefront
    if sf is None:
        raise PostcodeError("STORE_REQUIRED", "Set up your store first.", 400)
    row = row_for(sf)
    already = bool(row and row.consent_at and row.consent_version == CONSENT_VERSION)
    if consent is not True and not already:
        raise PostcodeError("CONSENT_REQUIRED", "Tick the box to agree to the postcode check.", 400)

    result = _validate_logged(user.id, postcode, "save")

    now = _now()
    if row is None:
        row = BusinessLocation(user_id=user.id, storefront_id=sf.id)
        db.session.add(row)
    row.user_id = user.id
    row.postcode, row.postcode_display = result.postcode, result.display
    row.state_code, row.lga_code = result.state_code, result.lga_code
    row.district_code, row.area_code, row.unit_code = result.district_code, result.area_code, result.unit_code
    row.state_name, row.lga_name = result.state_name, result.lga_name
    row.method = method if method in METHODS else "typed"
    row.status = LocationStatus.VERIFIED
    row.validated_at = now
    row.validation_level = 1
    if consent is True or not already:
        row.consent_version, row.consent_at = CONSENT_VERSION, now
    db.session.commit()
    return row


def _clear(row):
    row.postcode = row.postcode_display = None
    row.state_code = row.lga_code = row.district_code = row.area_code = row.unit_code = None
    row.state_name = row.lga_name = None
    row.validated_at = None


def remove(user) -> None:
    """Immediate: the postcode fields are cleared now; an audit row remains."""
    if not is_enabled():
        raise feature_off()
    row = row_for(user.storefront)
    if row is None or row.status == LocationStatus.REMOVED:
        return
    audit("remove", user.id, row.postcode)
    _clear(row)
    row.status = LocationStatus.REMOVED
    db.session.commit()


# ── events: the venue's postcode ───────────────────────────────────────────

def save_venue(user, event, postcode) -> None:
    if not is_enabled():
        raise feature_off()
    result = _validate_logged(user.id, postcode, "venue_save")
    event.venue_postcode = result.postcode
    event.venue_postcode_display = result.display
    event.venue_lga_name = result.lga_name
    event.venue_state_name = result.state_name
    event.venue_postcode_validated_at = _now()
    db.session.commit()


def clear_venue(user, event) -> None:
    if not is_enabled():
        raise feature_off()
    if event.venue_postcode:
        audit("venue_remove", user.id, event.venue_postcode)
    event.venue_postcode = event.venue_postcode_display = None
    event.venue_lga_name = event.venue_state_name = None
    event.venue_postcode_validated_at = None
    db.session.commit()


def venue_public(event) -> dict:
    """Venues are public places, so the exact postcode is shown."""
    if not is_enabled() or not getattr(event, "venue_postcode", None):
        return {"venue_postcode_verified": False, "venue_area_label": None, "venue_postcode_display": None,
                "venue_postcode_verified_at": None}
    at = _aware(event.venue_postcode_validated_at)
    return {
        "venue_postcode_verified": True,
        "venue_area_label": nipost.area_label(event.venue_state_name, event.venue_lga_name,
                                              event.venue_postcode[0:2], event.venue_postcode[2:4]),
        "venue_postcode_display": event.venue_postcode_display,
        "venue_postcode_verified_at": at.date().isoformat() if at else None,
    }


# ── privacy ─────────────────────────────────────────────────────────────────

def export_for(user) -> dict:
    rows = BusinessLocation.query.filter_by(user_id=user.id).order_by(BusinessLocation.id.asc()).all()
    audits = LocationAudit.query.filter_by(user_id=user.id).order_by(LocationAudit.id.asc()).all()
    return {
        "shop_locations": [{
            "postcode": r.postcode_display, "state": r.state_name, "lga": r.lga_name,
            "status": r.status, "method": r.method,
            "validated_at": _iso(r.validated_at),
            "consent_version": r.consent_version, "consent_at": _iso(r.consent_at),
            "created_at": _iso(r.created_at), "updated_at": _iso(r.updated_at),
        } for r in rows],
        "history": [{"action": a.action, "postcode": a.postcode, "result": a.result, "at": _iso(a.at)}
                    for a in audits],
    }


def delete_for(uid) -> None:
    """Account deletion: the location rows go and audit rows lose their postcode."""
    BusinessLocation.query.filter_by(user_id=uid).delete(synchronize_session=False)
    LocationAudit.query.filter_by(user_id=uid).update({"postcode": None}, synchronize_session=False)
