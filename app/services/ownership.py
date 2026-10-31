"""
Domain and handle ownership (docs/PLATFORM_API_V1.md, 2B-3).

A seller proves a website with a DNS TXT record `siiqo-verify=<token>` or a
`<meta name="siiqo-verify" content="<token>">` tag on the home page, and a
social handle with the code `siiqo-<short token>` in the profile bio.

Checks run only in the job runner (`ownership_check`), never inline in a
request. Pages are fetched with the scraper's SSRF guard (public addresses
only, every redirect re-checked). Verified claims are re-checked daily; when
the proof is gone the claim lapses and the identifier it added is removed.
"""
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.extensions import db
from app.services import foundations, jobs

logger = logging.getLogger(__name__)

NETWORKS = {
    "instagram": "https://www.instagram.com/{handle}/",
    "tiktok": "https://www.tiktok.com/@{handle}",
    "x": "https://x.com/{handle}",
    "facebook": "https://www.facebook.com/{handle}",
}
MAX_CLAIMS_PER_SELLER = 20
MAX_PAGE_BYTES = 512 * 1024
FETCH_TIMEOUT = 10
RECHECK_EVERY = timedelta(hours=24)
PENDING_RECHECK_EVERY = timedelta(hours=1)
PENDING_WINDOW = timedelta(days=14)
LAPSE_AFTER_ERRORS = 3
USER_AGENT = "SiiqoVerify/1.0 (+https://siiqo.com/developers)"

_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_HANDLE_RE = re.compile(r"^[a-z0-9._]{1,60}$")
_OWN_DOMAINS = ("siiqo.com", "siiqo.app")


class OwnershipError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


# ── input ──────────────────────────────────────────────────────────────────

def normalise_domain(value):
    domain = foundations._norm_domain(value)
    if not domain:
        return None
    domain = domain.rstrip(".")
    if not _DOMAIN_RE.match(domain):
        return None
    if any(domain == d or domain.endswith("." + d) for d in _OWN_DOMAINS):
        return None
    return domain


def normalise_handle(value):
    handle = foundations._norm_handle(value)
    if not handle:
        return None
    handle = handle.lstrip("@")
    return handle if _HANDLE_RE.match(handle) else None


def bio_code(claim):
    return f"siiqo-{claim.token[:8]}"


def instructions(claim):
    if claim.kind == "domain":
        return {
            "dns": {"type": "TXT", "host": claim.value, "value": f"siiqo-verify={claim.token}"},
            "meta": {"page": f"https://{claim.value}/",
                     "tag": f'<meta name="siiqo-verify" content="{claim.token}">'},
        }
    return {"bio": {"code": bio_code(claim),
                    "profile_url": NETWORKS[claim.network].format(handle=claim.value)}}


def claim_public(claim):
    def iso(dt):
        return _aware(dt).isoformat() if dt else None
    return {
        "id": claim.id, "kind": claim.kind, "network": claim.network or None, "value": claim.value,
        "status": claim.status, "method": claim.method, "last_result": claim.last_result,
        "verified_at": iso(claim.verified_at), "last_checked_at": iso(claim.last_checked_at),
        "created_at": iso(claim.created_at), "instructions": instructions(claim),
    }


def create_claim(vendor_id, kind, value, network=None):
    from app.models.platform import OwnershipClaim
    if kind == "domain":
        norm, network = normalise_domain(value), ""
        if not norm:
            raise OwnershipError("INVALID_DOMAIN", "Enter a website address like mystore.com.")
    elif kind == "handle":
        network = (network or "").strip().lower()
        if network == "twitter":
            network = "x"
        if network not in NETWORKS:
            raise OwnershipError("INVALID_NETWORK", "Choose Instagram, TikTok, X or Facebook.")
        norm = normalise_handle(value)
        if not norm:
            raise OwnershipError("INVALID_HANDLE", "Enter your handle, like @mystore.")
    else:
        raise OwnershipError("INVALID_KIND", "kind must be 'domain' or 'handle'.")

    existing = OwnershipClaim.query.filter_by(vendor_id=vendor_id, kind=kind, network=network, value=norm).first()
    if existing:
        return existing, False
    if OwnershipClaim.query.filter_by(vendor_id=vendor_id).count() >= MAX_CLAIMS_PER_SELLER:
        raise OwnershipError("TOO_MANY", f"You can add up to {MAX_CLAIMS_PER_SELLER} websites and handles.", 409)
    claim = OwnershipClaim(vendor_id=vendor_id, kind=kind, network=network, value=norm,
                           token=secrets.token_hex(16), status="pending")
    db.session.add(claim)
    db.session.commit()
    return claim, True


def remove_claim(claim):
    _remove_identifier(claim)
    db.session.delete(claim)
    db.session.commit()


def request_check(claim):
    """Queue a check (job runner). Returns the job."""
    if claim.status != "verified":
        claim.status = "checking"
        db.session.commit()
    return jobs.enqueue("ownership_check", {"claim_id": claim.id}, user_id=claim.vendor_id)


def verified_domains(vendor_id):
    from app.models.platform import OwnershipClaim
    return OwnershipClaim.query.filter_by(vendor_id=vendor_id, kind="domain", status="verified").all()


def origin_allowed(vendor_id, origin):
    """True when `origin` (scheme://host[:port]) is on one of the seller's
    verified domains: the domain itself or www.<domain>; any subdomain when
    the domain was proved by DNS."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse((origin or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        return False
    if parsed.path not in ("", "/") or parsed.query or parsed.username or parsed.password:
        return False
    host = parsed.hostname.lower().rstrip(".")
    for claim in verified_domains(vendor_id):
        d = claim.value
        if host == d or host == f"www.{d}":
            return True
        if claim.method == "dns" and host.endswith("." + d):
            return True
    return False


# ── identifier graph ───────────────────────────────────────────────────────

def _identifier(claim):
    if claim.kind == "domain":
        return "domain", claim.value
    return "handle", f"{claim.network}:{claim.value}"


def _add_identifier(claim):
    from app.models.foundations import SellerIdentifier
    kind, value = _identifier(claim)
    t = SellerIdentifier.__table__
    now = _now()
    existing = db.session.execute(select(t.c.id).where(t.c.vendor_id == claim.vendor_id, t.c.kind == kind,
                                                       t.c.value == value)).first()
    if existing:
        db.session.execute(t.update().where(t.c.id == existing.id).values(last_seen=now))
        return
    db.session.execute(t.insert().values(vendor_id=claim.vendor_id, kind=kind, value=value, source="verified",
                                         first_seen=now, last_seen=now))
    claim.identifier_added = True


def _remove_identifier(claim):
    """Remove only the identifier row this claim added (never one the seller's
    store or account already had)."""
    from app.models.foundations import SellerIdentifier
    if not claim.identifier_added:
        return
    kind, value = _identifier(claim)
    t = SellerIdentifier.__table__
    db.session.execute(t.delete().where(t.c.vendor_id == claim.vendor_id, t.c.kind == kind,
                                        t.c.value == value, t.c.source == "verified"))
    claim.identifier_added = False


# ── fetching (SSRF-safe) ───────────────────────────────────────────────────

class _Absent(Exception):
    """The proof is definitely not there (page loaded, record answered)."""


def _fetch(url):
    from app.utils import scraper
    response = scraper._safe_get(url, timeout=FETCH_TIMEOUT, stream=True,
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        if response.status_code >= 400:
            raise _Absent(f"http_{response.status_code}")
        if hasattr(response, "iter_content"):
            data = b""
            for chunk in response.iter_content(chunk_size=65536):
                data += chunk
                if len(data) >= MAX_PAGE_BYTES:
                    break
            return data[:MAX_PAGE_BYTES].decode(getattr(response, "encoding", None) or "utf-8", "replace")
        return (response.text or "")[:MAX_PAGE_BYTES]
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _check_dns(claim):
    from app.services import dns_txt
    try:
        records = dns_txt.txt_records(claim.value)
    except dns_txt.NXDomain:
        raise _Absent("nxdomain")
    expected = f"siiqo-verify={claim.token}"
    return any(r.strip() == expected for r in records)


def _check_meta(claim):
    from bs4 import BeautifulSoup
    html = None
    last_error = None
    for scheme in ("https", "http"):
        try:
            html = _fetch(f"{scheme}://{claim.value}/")
            break
        except _Absent:
            raise
        except Exception as e:  # noqa: BLE001
            last_error = e
    if html is None:
        raise last_error or RuntimeError("fetch failed")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("meta"):
        if (tag.get("name") or "").strip().lower() == "siiqo-verify" and \
                (tag.get("content") or "").strip() == claim.token:
            return True
    return False


def _check_bio(claim):
    page = _fetch(NETWORKS[claim.network].format(handle=claim.value))
    return bio_code(claim).lower() in page.lower()


def _run_methods(claim):
    """('found', method) | ('absent', reason) | ('error', reason)."""
    from app.utils.scraper import UnsafeURLError
    methods = [("dns", _check_dns), ("meta", _check_meta)] if claim.kind == "domain" else [("bio", _check_bio)]
    absent, errors = [], []
    for name, fn in methods:
        try:
            if fn(claim):
                return "found", name
            absent.append(f"{name}:not_found")
        except _Absent as e:
            absent.append(f"{name}:{e}")
        except UnsafeURLError:
            # The address resolves somewhere private: never fetched, never verifiable.
            absent.append(f"{name}:unsafe_address")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}:{type(e).__name__}")
    if errors:
        # Any method we couldn't complete: not proof that the record is gone.
        return "error", ",".join(errors + absent)[:40]
    return "absent", ",".join(absent + errors)[:40]


def check_claim(claim, now=None):
    now = now or _now()
    outcome, detail = _run_methods(claim)
    claim.last_checked_at = now
    was_verified = claim.status == "verified"
    if outcome == "found":
        claim.status = "verified"
        claim.method = detail
        claim.last_result = "found"
        claim.error_count = 0
        claim.lapsed_at = None
        if not was_verified:
            claim.verified_at = now
        _add_identifier(claim)
    elif outcome == "absent":
        claim.last_result = detail
        claim.error_count = 0
        if was_verified:
            _lapse(claim, now)
        else:
            claim.status = "pending"
    else:
        claim.last_result = detail
        claim.error_count = (claim.error_count or 0) + 1
        if was_verified and claim.error_count >= LAPSE_AFTER_ERRORS:
            _lapse(claim, now)
        elif not was_verified:
            claim.status = "pending"
    db.session.commit()
    return claim


def _lapse(claim, now):
    claim.status = "lapsed"
    claim.lapsed_at = now
    _remove_identifier(claim)
    logger.info(f"[OWNERSHIP] claim {claim.id} ({claim.kind}) lapsed")


@jobs.handler("ownership_check")
def _ownership_job(payload, job):
    from app.models.platform import OwnershipClaim
    claim = db.session.get(OwnershipClaim, int(payload.get("claim_id") or 0))
    if claim is None:
        return {"skipped": "missing"}
    check_claim(claim)
    return {"claim_id": claim.id, "status": claim.status}


def run_ownership_rechecks(now=None, limit=200):
    """Scheduled: re-check verified claims daily and recent pending ones hourly."""
    from app.models.platform import OwnershipClaim
    now = now or _now()
    due = []
    for claim in OwnershipClaim.query.filter(OwnershipClaim.status.in_(("verified", "pending", "checking"))) \
            .order_by(OwnershipClaim.id.asc()).all():
        last = _aware(claim.last_checked_at)
        if claim.status == "verified":
            if last is None or last <= now - RECHECK_EVERY:
                due.append(claim.id)
        elif _aware(claim.created_at) and _aware(claim.created_at) > now - PENDING_WINDOW:
            if last is None or last <= now - PENDING_RECHECK_EVERY:
                due.append(claim.id)
        if len(due) >= limit:
            break
    for claim_id in due:
        try:
            jobs.enqueue("ownership_check", {"claim_id": claim_id})
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            logger.warning(f"[OWNERSHIP] could not queue claim {claim_id}: {e}")
    return len(due)
