"""
"Check a seller": a buyer pastes a phone number, @handle, social link, store
link, Pay Link, website or bank account and learns whether it belongs to a
seller on Siiqo, and how trustworthy that seller is.

Only public Trust Passport claims are returned, and only for sellers whose
Passport is visible. Matched identifier values, emails, phones and account
details are never echoed back.
"""
import re
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request

from app.extensions import limiter
from app.services import foundations
from app.services import passport as passport_svc

check_bp = Blueprint('check', __name__)

MAX_RESULTS = 5
MAX_QUERY_LEN = 300
RATE_LIMIT = "30 per minute"

NOT_FOUND_MESSAGE = ("We couldn't find a Siiqo seller linked to this. That doesn't mean they're a scam, "
                     "but they haven't built a trust record on Siiqo yet. Pay through a Siiqo Pay Link "
                     "so your money is held in escrow until you receive your order.")

# host → network keys used in Storefront.social_links
_SOCIAL_HOSTS = {
    "instagram.com": ("instagram",),
    "tiktok.com": ("tiktok",),
    "x.com": ("x", "twitter"),
    "twitter.com": ("twitter", "x"),
    "facebook.com": ("facebook",),
    "fb.com": ("facebook",),
    "m.facebook.com": ("facebook",),
}
_SIIQO_HOSTS = ("siiqo.com", "siiqo.app")
# First path segments on siiqo.com that prefix a store slug (siiqo.com/id/<slug>).
_STORE_PREFIXES = {"id", "store", "stores", "shop", "s", "vendor-store"}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,99}$")
_HANDLE_RE = re.compile(r"^@[a-z0-9._]{1,60}$")
_PHONE_CHARS_RE = re.compile(r"^\+?[\d\s().-]{10,20}$")


def _is_nigerian_phone(text):
    if not _PHONE_CHARS_RE.match(text):
        return None
    digits = re.sub(r"\D", "", text)
    if re.fullmatch(r"0[789][01]\d{8}", digits) or re.fullmatch(r"234[789][01]\d{8}", digits):
        return foundations._norm_phone(digits)
    return None


def _host_and_path(text):
    url = text if "://" in text else f"https://{text}"
    try:
        parsed = urlparse(url)
    except ValueError:
        return None, ""
    host = (parsed.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return host, parsed.path or ""


def _looks_like_url(text):
    if " " in text:
        return False
    if "://" in text or text.startswith("www."):
        return True
    host = text.split("/", 1)[0]
    return "." in host and bool(re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host))


def classify(raw):
    """Work out what `raw` is. Returns (query_type, [(kind, matcher)], extras)
    where matcher is ("eq", value) or ("endswith", value); extras holds direct
    fallbacks ({"store_slug": ..., "paylink_slug": ...})."""
    text = (raw or "").strip()
    lowered = text.lower()
    if not text:
        return "empty", [], {}

    phone = _is_nigerian_phone(text)
    if phone:
        return "phone", [("phone", ("eq", phone))], {}

    if _HANDLE_RE.match(lowered):
        handle = foundations._norm_handle(lowered)
        return "handle", [("handle", ("endswith", f":{handle}"))], {}

    if _looks_like_url(lowered):
        host, path = _host_and_path(lowered)
        segments = [s for s in path.split("/") if s]

        # Pay Link: anything with /pay/<slug>
        if "pay" in segments:
            idx = segments.index("pay")
            if idx + 1 < len(segments):
                slug = segments[idx + 1].lower()
                return "paylink", [("paylink_slug", ("eq", slug))], {"paylink_slug": slug}

        if host:
            for social_host, networks in _SOCIAL_HOSTS.items():
                if host == social_host or host.endswith("." + social_host):
                    handle = foundations._norm_handle(f"https://{social_host}{path}")
                    if not handle:
                        return "social_url", [], {}
                    return "social_url", [("handle", ("eq", f"{n}:{handle}")) for n in networks], {}

            for siiqo_host in _SIIQO_HOSTS:
                if host == siiqo_host or host.endswith("." + siiqo_host):
                    sub = host[: -len(siiqo_host) - 1] if host != siiqo_host else ""
                    if sub and sub not in ("api", "app", "www") and "." not in sub:
                        slug = sub
                    elif segments:
                        slug = segments[1] if segments[0] in _STORE_PREFIXES and len(segments) > 1 else segments[0]
                    else:
                        return "store_url", [], {}
                    slug = slug.lower()
                    return "store_url", [("store_slug", ("eq", slug))], {"store_slug": slug}

            domain = foundations._norm_domain(lowered)
            if domain:
                return "website", [("domain", ("eq", domain))], {}

    if _SLUG_RE.match(lowered):
        return "store_slug", [("store_slug", ("eq", lowered))], {"store_slug": lowered}

    return "unknown", [], {}


def _matches(lookups, extras):
    """[(vendor_id, matched_on)] in a stable order, one entry per seller."""
    from app.models.foundations import SellerIdentifier
    from app.models.payment_link import PaymentLink

    found = []
    seen = set()

    def add(vendor_id, kind):
        if vendor_id and vendor_id not in seen:
            seen.add(vendor_id)
            found.append((vendor_id, kind))

    for kind, (op, value) in lookups:
        q = SellerIdentifier.query.filter(SellerIdentifier.kind == kind)
        if op == "eq":
            q = q.filter(SellerIdentifier.value == value)
        else:
            q = q.filter(SellerIdentifier.value.endswith(value, autoescape=True))
        for row in q.order_by(SellerIdentifier.last_seen.desc()).limit(50).all():
            add(row.vendor_id, kind)

    # Fallbacks for sellers whose identifiers haven't been backfilled yet.
    if extras.get("store_slug"):
        sf = passport_svc.find_storefront(extras["store_slug"])
        if sf is not None:
            add(sf.vendor_id, "store_slug")
    if extras.get("paylink_slug"):
        link = PaymentLink.query.filter_by(slug=extras["paylink_slug"]).first()
        if link is not None:
            add(link.vendor_id, "paylink_slug")
    return found


def _results(matches):
    from app.models.user import Storefront
    results = []
    for vendor_id, kind in matches:
        sf = Storefront.query.filter_by(vendor_id=vendor_id).first()
        if not passport_svc.is_visible(sf):
            continue
        results.append({"passport": passport_svc.build_passport(sf), "matched_on": kind})
        if len(results) >= MAX_RESULTS:
            break
    return results


def _respond(query_type, results):
    found = bool(results)
    foundations.record_event("check", props={"query_type": query_type, "found": found})
    from app.services import who_checked
    who_checked.record_results(results, "check")   # "Who checked you": seller + identifier type only
    body ={"query_type": query_type, "found": found, "results": results}
    if not found:
        body["message"] = NOT_FOUND_MESSAGE
    return jsonify(body), 200


@check_bp.route('', methods=['GET'])
@check_bp.route('/', methods=['GET'])
@limiter.limit(RATE_LIMIT)
def check_seller():
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({"query_type": "empty", "found": False, "results": [],
                        "message": "Paste a phone number, @handle, store link, Pay Link or website to check."}), 400
    if len(q) > MAX_QUERY_LEN:
        return jsonify({"query_type": "unknown", "found": False, "results": [],
                        "message": "That's too long to check. Paste just the link, number or handle."}), 400
    query_type, lookups, extras = classify(q)
    results = _results(_matches(lookups, extras)) if lookups else []
    if not results:
        unclaimed = _directory_result(query_type, lookups, q)
        if unclaimed is not None:
            return unclaimed
    return _respond(query_type, results)


def _directory_result(query_type, lookups, q):
    """Phase 3 (3-5): no Siiqo seller matched, but an unclaimed directory listing
    did. Public listing fields only, neutral wording, no trust signals."""
    from app.services import directory
    listing = directory.listing_for_lookups(lookups, query_type=query_type, raw=q)
    if listing is None:
        return None
    foundations.record_event("check", props={"query_type": query_type, "found": False, "directory": True})
    return jsonify({"query_type": query_type, "found": False, "results": [], "status": "unclaimed",
                    "listing": directory.public_listing(listing), "message": directory.UNCLAIMED_MESSAGE}), 200


@check_bp.route('/account', methods=['GET'])
@limiter.limit(RATE_LIMIT)
def check_account():
    bank_code = (request.args.get('bank_code') or '').strip()
    account_number = re.sub(r"\D", "", request.args.get('account_number') or '')
    if not bank_code or not re.fullmatch(r"[0-9A-Za-z]{2,10}", bank_code) or len(account_number) != 10:
        return jsonify({"query_type": "account", "found": False, "results": [],
                        "message": "Enter the bank and the 10-digit account number you're paying into."}), 400
    value = foundations.payout_account_hash(bank_code, account_number)
    results = _results(_matches([("payout_account_hash", ("eq", value))], {}))
    return _respond("account", results)
