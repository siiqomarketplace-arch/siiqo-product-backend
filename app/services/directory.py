"""
Business directory and claim flow (Phase 3 item 3-5).

- Listings come only from permitted sources: a superadmin CSV import (with a
  source note on every import) or a partner feed imported the same way.
  Nothing is scraped, and only business contact points are kept: a public
  business phone, website or social handle. Any other CSV column is ignored
  and reported in the dry run.
- Every import is a dry run first (rows to add, duplicates against existing
  stores and listings matched by identifier, invalid rows), then applied once.
- A listing's normalised identifiers (same normalisers and formats as
  seller_identifiers) are kept in business_listing_identifiers with source
  'directory'. When a listing is claimed and linked to a store they are copied
  into seller_identifiers for that seller, source 'directory'.
- A business proves ownership to claim a listing, or to have it removed:
    phone   a 6-digit code sent to the listing's public phone. No SMS provider
            exists in this codebase and the bot notify path only reaches
            Telegram users by their Telegram id, so `otp_sender()` returns None
            and the phone method answers "coming soon" (503 OTP_UNAVAILABLE).
    domain  DNS TXT or meta tag, and
    handle  bio code, both checked by the Phase 2B ownership service
            (app/services/ownership.py), in the job runner only.
- Every change is written to directory_audit_events; admin actions also go
  to admin_audit_logs.
"""
import csv
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import select

from app.extensions import db
from app.models.directory import (BusinessListing, BusinessListingIdentifier, DirectoryAuditEvent,
                                  DirectoryClaim, DirectoryImportBatch, ListingSource, ListingStatus)
from app.services import foundations, jobs, ownership

logger = logging.getLogger(__name__)

COLUMNS = ("name", "category", "city", "state", "phone", "website", "social_network", "social_handle")
IMPORT_SOURCES = (ListingSource.ADMIN_IMPORT, ListingSource.PARTNER)
MAX_IMPORT_ROWS = 2000
MAX_IMPORT_BYTES = 1024 * 1024
MIN_SOURCE_NOTE = 5

OTP_TTL = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
OTP_SENDS_PER_LISTING_PER_HOUR = 3
OPEN_CLAIMS_PER_LISTING = 20

UNCLAIMED_MESSAGE = ("This business is listed in the Siiqo directory but hasn't claimed its listing. "
                     "It has no trust record on Siiqo yet, which doesn't mean anything good or bad about it. "
                     "Pay through a Siiqo Pay Link so your money is held until you receive your order.")

_PHONE_RE = re.compile(r"234[789][01]\d{8}")
_NETWORK_HOSTS = {"instagram.com": "instagram", "tiktok.com": "tiktok", "x.com": "x", "twitter.com": "x",
                  "facebook.com": "facebook", "fb.com": "facebook"}


class DirectoryError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status

    def body(self):
        return {"code": self.code, "message": self.message}


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def name_key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())[:200]


# ── normalising one row ────────────────────────────────────────────────────

def _clean_text(value, limit):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:limit] or None


def normalise_phone(value):
    digits = foundations._norm_phone(value)
    return digits if digits and _PHONE_RE.fullmatch(digits) else None


def normalise_social(network, handle):
    """(network, handle) or (None, None). Accepts a profile link in `handle`."""
    network = (network or "").strip().lower()
    handle = (handle or "").strip()
    if not handle:
        return None, None
    if not network and ("/" in handle or "." in handle.split("/")[0]):
        host = foundations._norm_domain(handle) or ""
        for h, n in _NETWORK_HOSTS.items():
            if host == h or host.endswith("." + h):
                network = n
    if network == "twitter":
        network = "x"
    if network not in ownership.NETWORKS:
        return None, None
    norm = ownership.normalise_handle(handle)
    return (network, norm) if norm else (None, None)


def parse_row(raw):
    """(clean, errors). `clean` holds only the allowed business fields."""
    errors = []
    name = _clean_text(raw.get("name"), 200)
    if not name or len(name) < 2:
        errors.append("name_missing")
    clean = {
        "name": name,
        "category": _clean_text(raw.get("category"), 100),
        "city": _clean_text(raw.get("city"), 100),
        "state": _clean_text(raw.get("state"), 100),
        "phone": None, "website": None, "social_network": None, "social_handle": None,
    }
    if (raw.get("phone") or "").strip():
        clean["phone"] = normalise_phone(raw.get("phone"))
        if not clean["phone"]:
            errors.append("phone_invalid")
    if (raw.get("website") or "").strip():
        clean["website"] = ownership.normalise_domain(raw.get("website"))
        if not clean["website"]:
            errors.append("website_invalid")
    if (raw.get("social_handle") or "").strip():
        clean["social_network"], clean["social_handle"] = normalise_social(raw.get("social_network"),
                                                                           raw.get("social_handle"))
        if not clean["social_handle"]:
            errors.append("social_invalid")
    if not (clean["phone"] or clean["website"] or clean["social_handle"]) and "phone_invalid" not in errors \
            and "website_invalid" not in errors and "social_invalid" not in errors:
        errors.append("no_contact")
    return clean, errors


def identifiers_of(fields):
    """[(kind, value)] in seller_identifiers format."""
    out = []
    if fields.get("phone"):
        out.append(("phone", fields["phone"]))
    if fields.get("website"):
        out.append(("domain", fields["website"]))
    if fields.get("social_handle") and fields.get("social_network"):
        out.append(("handle", f"{fields['social_network']}:{fields['social_handle']}"))
    return out


def _handle_variants(value):
    network, _, handle = value.partition(":")
    if network in ("x", "twitter"):
        return [f"x:{handle}", f"twitter:{handle}"]
    return [value]


def find_duplicates(idents, exclude_listing_id=None):
    """Existing stores and live listings sharing any identifier."""
    from app.models.foundations import SellerIdentifier
    from app.models.user import Storefront
    stores, listings = {}, {}
    for kind, value in idents:
        values = _handle_variants(value) if kind == "handle" else [value]
        for row in SellerIdentifier.query.filter(SellerIdentifier.kind == kind,
                                                 SellerIdentifier.value.in_(values)).limit(20).all():
            if row.vendor_id not in stores:
                sf = Storefront.query.filter_by(vendor_id=row.vendor_id).first()
                stores[row.vendor_id] = {"vendor_id": row.vendor_id,
                                         "store_slug": sf.store_slug if sf else None,
                                         "store_name": sf.store_name if sf else None,
                                         "matched_on": kind}
        q = db.session.query(BusinessListing).join(
            BusinessListingIdentifier, BusinessListingIdentifier.listing_id == BusinessListing.id
        ).filter(BusinessListingIdentifier.kind == kind, BusinessListingIdentifier.value.in_(values),
                 BusinessListing.status != ListingStatus.REMOVED)
        if exclude_listing_id:
            q = q.filter(BusinessListing.id != exclude_listing_id)
        for listing in q.limit(20).all():
            listings.setdefault(listing.id, {"id": listing.id, "name": listing.name,
                                             "status": listing.status, "matched_on": kind})
    return list(stores.values()), list(listings.values())


# ── audit ──────────────────────────────────────────────────────────────────

def _audit(event, *, actor_type, actor_id=None, listing_id=None, batch_id=None, claim_id=None,
           details=None, ip=None, admin=None):
    """Directory audit row (and an admin audit row for admin actions) in the
    caller's transaction."""
    from flask import has_request_context, request
    db.session.add(DirectoryAuditEvent(event=event, listing_id=listing_id, batch_id=batch_id, claim_id=claim_id,
                                       actor_type=actor_type, actor_id=actor_id, details=details,
                                       ip_address=(ip or None) and ip[:45]))
    if admin is not None:
        from app.models.audit import AdminAuditLog
        resource_type, resource_id = ("BusinessListing", listing_id) if listing_id else \
            ("DirectoryImportBatch", batch_id)
        db.session.add(AdminAuditLog(
            admin_id=admin.id, admin_email=admin.email, admin_role=admin.role, action=event,
            resource_type=resource_type, resource_id=str(resource_id), details=details,
            ip_address=(ip or "unknown")[:45],
            user_agent=request.headers.get("User-Agent", "unknown") if has_request_context() else "system",
        ))


# ── import ─────────────────────────────────────────────────────────────────

def _read_csv(text):
    if len(text.encode("utf-8", "ignore")) > MAX_IMPORT_BYTES:
        raise DirectoryError("FILE_TOO_LARGE", "The file is larger than 1 MB. Split it into smaller files.", 413)
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    headers = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    if "name" not in headers:
        raise DirectoryError("NO_NAME_COLUMN", "The file needs a header row with a 'name' column.")
    reader.fieldnames = headers
    rows = []
    for i, raw in enumerate(reader):
        if i >= MAX_IMPORT_ROWS:
            raise DirectoryError("TOO_MANY_ROWS", f"Import up to {MAX_IMPORT_ROWS} rows at a time.", 413)
        rows.append({k: (v if isinstance(v, str) else "") for k, v in raw.items() if k})
    ignored = [h for h in headers if h and h not in COLUMNS]
    return rows, ignored


def dry_run(admin, text, *, source, source_note, filename=None, ip=None):
    source = (source or ListingSource.ADMIN_IMPORT).strip().lower()
    if source not in IMPORT_SOURCES:
        raise DirectoryError("INVALID_SOURCE", "Source must be admin_import or partner.")
    source_note = _clean_text(source_note, 500)
    if not source_note or len(source_note) < MIN_SOURCE_NOTE:
        raise DirectoryError("SOURCE_NOTE_REQUIRED",
                             "Say where this list comes from and why Siiqo may use it (the source note).")
    raws, ignored = _read_csv(text or "")
    if not raws:
        raise DirectoryError("EMPTY_FILE", "The file has no rows.")

    rows, seen = [], {}
    counts = {"add": 0, "duplicate": 0, "invalid": 0}
    for line, raw in enumerate(raws, start=2):
        clean, errors = parse_row(raw)
        idents = identifiers_of(clean)
        row = {"line": line, "fields": clean, "identifiers": [list(i) for i in idents]}
        if errors:
            row.update(action="invalid", errors=errors)
        else:
            stores, listings = find_duplicates(idents)
            in_file = sorted({seen[i] for i in idents if i in seen})
            if stores or listings or in_file:
                row.update(action="duplicate", duplicate_stores=stores, duplicate_listings=listings,
                           duplicate_lines=in_file)
            else:
                row["action"] = "add"
            for i in idents:
                seen.setdefault(i, line)
        counts[row["action"]] += 1
        rows.append(row)

    summary = {**counts, "total": len(rows), "ignored_columns": ignored}
    batch = DirectoryImportBatch(admin_id=admin.id, source=source, source_note=source_note,
                                 filename=_clean_text(filename, 200), status="dry_run", rows=rows, summary=summary)
    db.session.add(batch)
    db.session.flush()
    _audit("DIRECTORY_IMPORT_DRY_RUN", actor_type="ADMIN", actor_id=admin.id, batch_id=batch.id, admin=admin,
           ip=ip, details={"source": source, "source_note": source_note, **counts,
                           "ignored_columns": ignored})
    db.session.commit()
    return batch


def batch_public(batch):
    return {"id": batch.id, "status": batch.status, "source": batch.source, "source_note": batch.source_note,
            "filename": batch.filename, "summary": batch.summary, "rows": batch.rows,
            "created_at": _aware(batch.created_at).isoformat() if batch.created_at else None,
            "applied_at": _aware(batch.applied_at).isoformat() if batch.applied_at else None}


def _add_listing_identifiers(listing, idents):
    now = _now()
    for kind, value in idents:
        db.session.add(BusinessListingIdentifier(listing_id=listing.id, kind=kind, value=value, source="directory",
                                                 first_seen=now, last_seen=now))


def apply(admin, batch_id, ip=None):
    batch = db.session.query(DirectoryImportBatch).filter_by(id=batch_id).with_for_update().first()
    if batch is None:
        raise DirectoryError("NOT_FOUND", "Import not found.", 404)
    if batch.status == "applied":
        raise DirectoryError("ALREADY_APPLIED", "This import was already applied.", 409)
    added, skipped = [], []
    for row in batch.rows or []:
        if row.get("action") != "add":
            continue
        fields = row["fields"]
        idents = [tuple(i) for i in row.get("identifiers") or []]
        stores, listings = find_duplicates(idents)
        if stores or listings:
            skipped.append(row["line"])
            continue
        listing = BusinessListing(name=fields["name"], name_key=name_key(fields["name"]),
                                  category=fields.get("category"), city=fields.get("city"),
                                  state=fields.get("state"), phone=fields.get("phone"),
                                  website=fields.get("website"), social_network=fields.get("social_network"),
                                  social_handle=fields.get("social_handle"), source=batch.source,
                                  source_note=batch.source_note, import_batch_id=batch.id,
                                  status=ListingStatus.UNCLAIMED)
        db.session.add(listing)
        db.session.flush()
        _add_listing_identifiers(listing, idents)
        db.session.flush()
        added.append(listing.id)
    batch.status = "applied"
    batch.applied_at = _now()
    batch.applied_by = admin.id
    batch.summary = {**(batch.summary or {}), "applied_added": len(added), "applied_skipped_lines": skipped}
    _audit("DIRECTORY_IMPORT_APPLIED", actor_type="ADMIN", actor_id=admin.id, batch_id=batch.id, admin=admin,
           ip=ip, details={"source": batch.source, "source_note": batch.source_note, "added": len(added),
                           "skipped_lines": skipped})
    db.session.commit()
    return batch, added


def admin_remove(admin, listing, reason, ip=None):
    reason = _clean_text(reason, 200)
    if not reason:
        raise DirectoryError("REASON_REQUIRED", "Give a reason for removing this listing.")
    if listing.status == ListingStatus.REMOVED:
        raise DirectoryError("ALREADY_REMOVED", "This listing is already removed.", 409)
    previous = listing.status
    _remove(listing, reason)
    _audit("DIRECTORY_LISTING_REMOVED", actor_type="ADMIN", actor_id=admin.id, listing_id=listing.id, admin=admin,
           ip=ip, details={"reason": reason, "previous_status": previous})
    db.session.commit()
    return listing


def _remove(listing, reason):
    listing.status = ListingStatus.REMOVED
    listing.removed_at = _now()
    listing.removed_reason = reason
    BusinessListingIdentifier.query.filter_by(listing_id=listing.id).delete()


# ── public views ───────────────────────────────────────────────────────────

def public_listing(listing):
    """Public fields only: never the phone number or the import source note."""
    return {
        "id": listing.id,
        "name": listing.name,
        "category": listing.category,
        "city": listing.city,
        "state": listing.state,
        "status": listing.status,
        "on_siiqo": False,
        "verified": False,
    }


def otp_sender():
    """A callable(phone, text) -> bool that sends a one-time code, or None.

    None today: there is no SMS provider in this codebase, and the bot notify
    path (Notification -> BOT_ORCHESTRATOR_URL/telegram-notification) only
    reaches Telegram users by Telegram id, not a phone number on WhatsApp."""
    return None


def claim_options(listing):
    methods = []
    if listing.phone:
        methods.append({"method": "phone", "hint": f"ending in {listing.phone[-2:]}",
                        "available": otp_sender() is not None})
    if listing.website:
        methods.append({"method": "domain", "value": listing.website, "available": True})
    if listing.social_handle and listing.social_network:
        methods.append({"method": "handle", "network": listing.social_network, "value": listing.social_handle,
                        "available": True})
    return {**public_listing(listing), "methods": methods}


def listing_for_lookups(lookups, query_type=None, raw=None):
    """The unclaimed listing matching a /check query, or None."""
    for kind, (op, value) in lookups or []:
        if kind not in ("phone", "domain", "handle"):
            continue
        q = db.session.query(BusinessListing).join(
            BusinessListingIdentifier, BusinessListingIdentifier.listing_id == BusinessListing.id
        ).filter(BusinessListingIdentifier.kind == kind, BusinessListing.status == ListingStatus.UNCLAIMED)
        if op == "eq":
            values = _handle_variants(value) if kind == "handle" else [value]
            q = q.filter(BusinessListingIdentifier.value.in_(values))
        else:
            q = q.filter(BusinessListingIdentifier.value.endswith(value, autoescape=True))
        listing = q.order_by(BusinessListing.id.asc()).first()
        if listing is not None:
            return listing
    if query_type in ("unknown", "store_slug") and raw:
        return listing_for_name(raw)
    return None


def listing_for_name(name):
    """The one unclaimed listing whose name matches exactly (letters and digits), else None."""
    key = name_key(name)
    if len(key) < 3:
        return None
    rows = BusinessListing.query.filter_by(name_key=key, status=ListingStatus.UNCLAIMED) \
        .order_by(BusinessListing.id.asc()).limit(2).all()
    return rows[0] if len(rows) == 1 else None


def name_match(name):
    """For /start's name check: {id, name, category, city, state} or None."""
    listing = listing_for_name(name)
    if listing is None:
        return None
    return {k: v for k, v in public_listing(listing).items() if k in ("id", "name", "category", "city", "state")}


# ── claims and removal requests ────────────────────────────────────────────

def _code_hash(claim, code):
    secret = (os.environ.get("SECRET_KEY") or "siiqo-secret-key-salt-2026").encode()
    return hmac.new(secret, f"directory-otp:{claim.public_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _has_verified_ownership(user_id, kind, network, value):
    from app.models.platform import OwnershipClaim
    if not user_id:
        return False
    return OwnershipClaim.query.filter_by(vendor_id=user_id, kind=kind, network=network or "", value=value,
                                         status="verified").first() is not None


def start(listing, *, purpose, method, user_id=None, reason=None, ip=None):
    if purpose not in ("claim", "removal"):
        raise DirectoryError("INVALID_PURPOSE", "purpose must be claim or removal.")
    if purpose == "claim" and not user_id:
        raise DirectoryError("SIGN_IN_REQUIRED", "Sign in to claim this business.", 401)
    if listing.status != ListingStatus.UNCLAIMED:
        raise DirectoryError("NOT_AVAILABLE", "This listing can't be claimed or removed right now.", 409)
    if DirectoryClaim.query.filter_by(listing_id=listing.id, status="pending").count() >= OPEN_CLAIMS_PER_LISTING:
        raise DirectoryError("TOO_MANY", "Too many open requests for this listing. Try again later.", 429)

    claim = DirectoryClaim(public_id=secrets.token_urlsafe(16)[:22], listing_id=listing.id, purpose=purpose,
                           method=method, user_id=user_id, token=secrets.token_hex(16), status="pending",
                           reason=_clean_text(reason, 300) if purpose == "removal" else None)
    code = None
    if method == "phone":
        if not listing.phone:
            raise DirectoryError("NO_PHONE", "This listing has no phone number.")
        sender = otp_sender()
        if sender is None:
            raise DirectoryError("OTP_UNAVAILABLE", "Codes by phone are coming soon. Use the website or social "
                                                    "handle option instead.", 503)
        since = _now() - timedelta(hours=1)
        recent = DirectoryClaim.query.filter(DirectoryClaim.listing_id == listing.id,
                                             DirectoryClaim.method == "phone",
                                             DirectoryClaim.created_at >= since).count()
        if recent >= OTP_SENDS_PER_LISTING_PER_HOUR:
            raise DirectoryError("TOO_MANY_CODES", "Too many codes sent to this number. Try again in an hour.", 429)
        code = f"{secrets.randbelow(10 ** 6):06d}"
        claim.code_hash = _code_hash(claim, code)
        claim.code_expires_at = _now() + OTP_TTL
    elif method == "domain":
        if not listing.website:
            raise DirectoryError("NO_WEBSITE", "This listing has no website.")
        claim.kind, claim.network, claim.value = "domain", "", listing.website
    elif method == "handle":
        if not (listing.social_handle and listing.social_network):
            raise DirectoryError("NO_HANDLE", "This listing has no social handle.")
        claim.kind, claim.network, claim.value = "handle", listing.social_network, listing.social_handle
    else:
        raise DirectoryError("INVALID_METHOD", "method must be phone, domain or handle.")

    db.session.add(claim)
    db.session.flush()
    _audit("DIRECTORY_CLAIM_STARTED" if purpose == "claim" else "DIRECTORY_REMOVAL_REQUESTED",
           actor_type="USER" if user_id else "PUBLIC", actor_id=user_id, listing_id=listing.id, claim_id=claim.id,
           ip=ip, details={"method": method, "reason": claim.reason} if claim.reason else {"method": method})

    if code is not None:
        text = f"Your Siiqo code is {code}. It confirms you own {listing.name[:60]} on Siiqo. Expires in 10 minutes."
        try:
            sent = bool(sender(listing.phone, text))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DIRECTORY] code not sent: {type(e).__name__}")
            sent = False
        if not sent:
            db.session.rollback()
            raise DirectoryError("CODE_NOT_SENT", "We couldn't send the code. Try another option.", 502)
    elif purpose == "claim" and _has_verified_ownership(user_id, claim.kind, claim.network, claim.value):
        # Already proved through the seller's Websites and handles (Phase 2B).
        _proven(claim, "ownership")
    db.session.commit()
    return claim


def verify_code(claim, code, ip=None):
    if claim.method != "phone":
        raise DirectoryError("WRONG_METHOD", "This request isn't using a phone code.")
    if claim.status != "pending":
        raise DirectoryError("NOT_PENDING", "This code can't be used any more. Start again.", 409)
    if _aware(claim.code_expires_at) is None or _aware(claim.code_expires_at) < _now():
        claim.status = "expired"
        db.session.commit()
        raise DirectoryError("CODE_EXPIRED", "The code has expired. Start again to get a new one.", 410)
    claim.attempts = (claim.attempts or 0) + 1
    code = re.sub(r"\D", "", str(code or ""))
    if not (len(code) == 6 and hmac.compare_digest(claim.code_hash or "", _code_hash(claim, code))):
        if claim.attempts >= OTP_MAX_ATTEMPTS:
            claim.status = "failed"
            claim.last_result = "too_many_attempts"
        db.session.commit()
        raise DirectoryError("CODE_WRONG", "That code isn't right.", 400)
    _proven(claim, "otp", ip=ip)
    db.session.commit()
    return claim


def request_check(claim):
    if claim.method not in ("domain", "handle"):
        raise DirectoryError("WRONG_METHOD", "This request uses a phone code.")
    if claim.status not in ("pending", "checking"):
        return claim
    claim.status = "checking"
    db.session.commit()
    jobs.enqueue("directory_claim_check", {"claim_id": claim.id}, user_id=claim.user_id)
    db.session.refresh(claim)
    return claim


def check_claim(claim):
    """Runs the Phase 2B ownership checks against this claim's token."""
    if claim.status not in ("pending", "checking"):
        return claim
    outcome, detail = ownership._run_methods(claim)
    claim.last_checked_at = _now()
    if outcome == "found":
        _proven(claim, detail)
    else:
        claim.status = "pending"
        claim.last_result = detail
    db.session.commit()
    return claim


@jobs.handler("directory_claim_check")
def _claim_check_job(payload, job):
    claim = db.session.get(DirectoryClaim, int(payload.get("claim_id") or 0))
    if claim is None:
        return {"skipped": "missing"}
    check_claim(claim)
    return {"claim_id": claim.id, "status": claim.status}


def _proven(claim, how, ip=None):
    listing = db.session.get(BusinessListing, claim.listing_id)
    if listing is None or listing.status != ListingStatus.UNCLAIMED:
        claim.status = "failed"
        claim.last_result = "listing_not_available"
        return
    claim.status = "verified"
    claim.proved_by = how
    claim.verified_at = _now()
    claim.last_result = "found"
    if claim.purpose == "claim":
        listing.status = ListingStatus.CLAIMED
        listing.claimed_user_id = claim.user_id
        listing.claimed_at = _now()
        _audit("DIRECTORY_LISTING_CLAIMED", actor_type="USER", actor_id=claim.user_id, listing_id=listing.id,
               claim_id=claim.id, ip=ip, details={"method": claim.method, "proved_by": how})
        link_storefront(listing, claim.user_id)
    else:
        _remove(listing, "owner_request")
        _audit("DIRECTORY_LISTING_REMOVED", actor_type="USER" if claim.user_id else "PUBLIC",
               actor_id=claim.user_id, listing_id=listing.id, claim_id=claim.id, ip=ip,
               details={"method": claim.method, "proved_by": how, "reason": claim.reason})


def link_storefront(listing, user_id):
    """Link a claimed listing to its owner's store, once the store exists, and
    add the listing's identifiers to the seller's identifier graph. Returns the
    storefront or None."""
    from app.models.foundations import SellerIdentifier
    from app.models.user import Storefront
    if listing.status != ListingStatus.CLAIMED or listing.claimed_user_id != user_id:
        return None
    sf = Storefront.query.filter_by(vendor_id=user_id).first()
    if sf is None or listing.claimed_storefront_id:
        return sf if sf is not None and listing.claimed_storefront_id == sf.id else None
    listing.claimed_storefront_id = sf.id
    t = SellerIdentifier.__table__
    now = _now()
    for row in BusinessListingIdentifier.query.filter_by(listing_id=listing.id).all():
        existing = db.session.execute(select(t.c.id).where(t.c.vendor_id == user_id, t.c.kind == row.kind,
                                                           t.c.value == row.value)).first()
        if existing:
            db.session.execute(t.update().where(t.c.id == existing.id).values(last_seen=now))
        else:
            db.session.execute(t.insert().values(vendor_id=user_id, kind=row.kind, value=row.value,
                                                 source="directory", first_seen=now, last_seen=now))
    _audit("DIRECTORY_LISTING_LINKED", actor_type="USER", actor_id=user_id, listing_id=listing.id,
           details={"storefront_id": sf.id})
    return sf


def next_step(claim, listing):
    if claim.purpose != "claim" or claim.status != "verified":
        return None
    if listing.claimed_storefront_id:
        return {"action": "verify", "href": "/vendor/trust/verify"}
    return {"action": "create_store", "href": f"/start?name={quote(listing.name)}"}


def claim_public(claim):
    listing = db.session.get(BusinessListing, claim.listing_id)
    body = {
        "id": claim.public_id,
        "purpose": claim.purpose,
        "method": claim.method,
        "status": claim.status,
        "last_result": claim.last_result,
        "listing": public_listing(listing) if listing else None,
        "instructions": ownership.instructions(claim) if claim.kind in ("domain", "handle") else None,
        "code_expires_at": _aware(claim.code_expires_at).isoformat() if claim.code_expires_at else None,
        "next": next_step(claim, listing) if listing else None,
    }
    return body
