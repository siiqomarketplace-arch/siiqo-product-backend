"""
Trust Platform API v1 (docs/PLATFORM_API_V1.md), mounted at /v1.

Keyed endpoints take `Authorization: Bearer <key>` (or `X-Siiqo-Api-Key`),
answer with `X-Siiqo-Quota-*` headers, and return errors as
`{"code": ..., "message": ...}`. Test keys answer only from fixtures.
Only public Passport claims are ever returned.
"""
import re
from urllib.parse import urlparse

from flask import Blueprint, current_app, g, jsonify, request

from app.extensions import limiter
from app.services import foundations
from app.services import passport as passport_svc
from app.services import platform as platform_svc
from app.services import platform_webhooks as webhooks
from app.services import who_checked

v1_bp = Blueprint('v1', __name__)

MAX_IDENTIFIER_LEN = 300
_ACCOUNT_RE = re.compile(r"^([0-9a-z]{2,10})\s*[:/ ]\s*(\d{10})$", re.IGNORECASE)


def _error(code, message, status, headers=None):
    res = jsonify({"code": code, "message": message})
    res.status_code = status
    for k, v in (headers or {}).items():
        res.headers[k] = v
    return res


def _authenticate(count=True):
    """(client, None) or (None, error response). Applies the client's rate
    limit, and its monthly quota when `count`."""
    raw = platform_svc.raw_key_from_request(request)
    if not raw:
        return None, _error("UNAUTHORIZED", "Send your API key as 'Authorization: Bearer <key>'.", 401)
    client = passport_svc.client_for_key(raw)
    if client is None:
        return None, _error("INVALID_KEY", "This API key is not valid or has been revoked.", 401)
    code, headers = platform_svc.admit(client, count=count)
    if code == "RATE_LIMITED":
        return None, _error("RATE_LIMITED", "Too many requests this minute. Slow down and retry.", 429, headers)
    if code == "QUOTA_EXCEEDED":
        return None, _error("QUOTA_EXCEEDED", "This key has used its requests for this month.", 429, headers)
    g.platform_headers = headers
    g.platform_client = client
    return client, None


@v1_bp.after_request
def _headers(response):
    for k, v in (getattr(g, "platform_headers", None) or {}).items():
        response.headers.setdefault(k, v)
    if request.endpoint != "v1.widget":
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _signed(claims, client):
    if platform_svc.is_test(client):
        return platform_svc.sign_test(claims)
    return passport_svc.sign(claims)


# ── business trust ─────────────────────────────────────────────────────────

@v1_bp.route('/businesses/<slug>/trust', methods=['GET'])
def business_trust(slug):
    client, err = _authenticate()
    if err:
        return err
    if platform_svc.is_test(client):
        claims = platform_svc.fixture(slug)
    else:
        sf = passport_svc.find_storefront(slug)
        claims = passport_svc.build_passport(sf) if passport_svc.is_visible(sf) else None
        if claims is not None:
            who_checked.record(sf.vendor_id, "api", "store_slug", sf.id)
    if claims is None:
        return _error("NOT_FOUND", "No public Trust Passport for this business.", 404)
    statement, alg = _signed(claims, client)
    return jsonify({"business": claims, "statement": statement, "alg": alg}), 200


# ── lookup ─────────────────────────────────────────────────────────────────

def _live_lookup(identifier, bank_code, account_number):
    from app.routes import check
    match = _ACCOUNT_RE.match(identifier) if identifier else None
    if match and not (bank_code or account_number):
        bank_code, account_number = match.group(1), match.group(2)
    if bank_code or account_number:
        query_type = "account"
        value = foundations.payout_account_hash(str(bank_code), str(account_number))
        lookups, extras = [("payout_account_hash", ("eq", value))], {}
    else:
        query_type, lookups, extras = check.classify(identifier)
    results = check._results(check._matches(lookups, extras)) if lookups else []
    foundations.record_event("check", props={"query_type": query_type, "found": bool(results), "source": "api_v1"})
    who_checked.record_results(results[:1], "api")   # only the first match is returned
    return results[0]["passport"] if results else None


@v1_bp.route('/lookup', methods=['POST'])
def lookup():
    body = request.get_json(silent=True) or {}
    identifier = str(body.get('identifier') or '').strip()
    bank_code = re.sub(r"[^0-9A-Za-z]", "", str(body.get('bank_code') or ''))
    account_number = re.sub(r"\D", "", str(body.get('account_number') or ''))
    problem = None
    if not identifier and not (bank_code and account_number):
        problem = "Send {\"identifier\": \"...\"}: a phone number, @handle, store link, Pay Link or bank account."
    elif len(identifier) > MAX_IDENTIFIER_LEN:
        problem = "identifier is too long."
    elif (bank_code or account_number) and not (re.fullmatch(r"[0-9A-Za-z]{2,10}", bank_code)
                                                and len(account_number) == 10):
        problem = "bank_code and a 10-digit account_number are both needed."

    client, err = _authenticate(count=problem is None)
    if err:
        return err
    if problem:
        return _error("BAD_REQUEST", problem, 400)

    if platform_svc.is_test(client):
        claims = platform_svc.fixture_for_identifier(identifier or f"{bank_code}:{account_number}")
    else:
        claims = _live_lookup(identifier, bank_code, account_number)
    if claims is None:
        return jsonify({"match": "not_on_siiqo"}), 200
    statement, alg = _signed(claims, client)
    return jsonify({"match": "found", "business": claims, "statement": statement, "alg": alg}), 200


# ── widget (no key) ────────────────────────────────────────────────────────

def _clean_origin(value):
    value = (value or "").strip().rstrip("/")
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return value.lower()


def _local_origin(origin):
    host = urlparse(origin).hostname or ""
    return host in ("localhost", "127.0.0.1", "[::1]", "::1") or host.endswith(".localhost")


_WIDGET_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
}


@v1_bp.route('/widget/<slug>', methods=['GET', 'OPTIONS'], provide_automatic_options=False)
@limiter.limit("300 per minute")
def widget(slug):
    """For trust.js: allowed only when `origin` is one of the seller's verified
    domains. A browser's Origin header, when sent, must agree with it.
    CORS: any site may call it (GET only, no credentials). The keyed /v1
    endpoints send no CORS headers: they are for servers, keeping keys off pages."""
    from app.services import ownership
    if request.method == 'OPTIONS':
        res = current_app.response_class(status=204)
        res.headers.update(_WIDGET_CORS)
        return res
    origin = _clean_origin(request.args.get('origin'))
    header_origin = request.headers.get('Origin')
    body = {"allowed": False}
    if origin and (not header_origin or _clean_origin(header_origin) == origin):
        fixture = platform_svc.fixture(slug)
        if fixture is not None:
            # Development only: fixture badges render on localhost pages.
            if _local_origin(origin):
                statement, alg = platform_svc.sign_test(fixture)
                body = {"allowed": True, "business": fixture, "statement": statement, "alg": alg}
        else:
            sf = passport_svc.find_storefront(slug)
            if passport_svc.is_visible(sf) and ownership.origin_allowed(sf.vendor_id, origin):
                claims = passport_svc.build_passport(sf)
                statement, alg = passport_svc.sign(claims)
                body = {"allowed": True, "business": claims, "statement": statement, "alg": alg}
                who_checked.record(sf.vendor_id, "widget", None, sf.id)
    res = jsonify(body)
    res.headers.update(_WIDGET_CORS)
    res.headers["Cache-Control"] = "public, max-age=300"
    res.headers["Vary"] = "Origin"
    return res, 200


# ── webhooks ───────────────────────────────────────────────────────────────

def _own_endpoint(client, endpoint_id):
    from app.models.platform import WebhookEndpoint
    from app.extensions import db
    endpoint = db.session.get(WebhookEndpoint, endpoint_id)
    if endpoint is None or endpoint.client_id != client.id or endpoint.deleted_at is not None:
        return None
    return endpoint


@v1_bp.route('/webhooks', methods=['GET', 'POST'])
def webhooks_collection():
    client, err = _authenticate(count=False)
    if err:
        return err
    if request.method == 'GET':
        return jsonify({"webhooks": [webhooks.endpoint_public(e) for e in webhooks.endpoints_for(client)],
                        "events": list(webhooks.EVENTS)}), 200
    data = request.get_json(silent=True) or {}
    try:
        endpoint, secret = webhooks.create_endpoint(client, data.get('url'), data.get('events'))
    except webhooks.WebhookError as e:
        return _error(e.code, e.message, e.status)
    return jsonify({"webhook": webhooks.endpoint_public(endpoint), "secret": secret,
                    "message": "Store this secret now; it won't be shown again."}), 201


@v1_bp.route('/webhooks/<int:endpoint_id>', methods=['DELETE'])
def webhooks_delete(endpoint_id):
    client, err = _authenticate(count=False)
    if err:
        return err
    endpoint = _own_endpoint(client, endpoint_id)
    if endpoint is None:
        return _error("NOT_FOUND", "Webhook not found.", 404)
    webhooks.delete_endpoint(endpoint)
    return jsonify({"id": endpoint_id, "deleted": True}), 200


@v1_bp.route('/webhooks/<int:endpoint_id>/deliveries', methods=['GET'])
def webhooks_deliveries(endpoint_id):
    client, err = _authenticate(count=False)
    if err:
        return err
    endpoint = _own_endpoint(client, endpoint_id)
    if endpoint is None:
        return _error("NOT_FOUND", "Webhook not found.", 404)
    return jsonify({"deliveries": [webhooks.delivery_public(d) for d in webhooks.deliveries_for(endpoint)]}), 200


@v1_bp.route('/webhooks/<int:endpoint_id>/test', methods=['POST'])
def webhooks_test(endpoint_id):
    client, err = _authenticate(count=False)
    if err:
        return err
    endpoint = _own_endpoint(client, endpoint_id)
    if endpoint is None:
        return _error("NOT_FOUND", "Webhook not found.", 404)
    if not endpoint.is_active:
        return _error("DISABLED", "This webhook is disabled. Delete it and add it again.", 409)
    d = webhooks.send_test_event(endpoint)
    from app.extensions import db
    db.session.refresh(d)
    return jsonify({"delivery": webhooks.delivery_public(d)}), 202


# ── Evidence API (Phase 4) ─────────────────────────────────────────────────

@v1_bp.route('/evidence/secret', methods=['GET', 'POST'])
def evidence_secret():
    """GET: the current evidence signing secret's prefix. POST: a new secret
    (shown once; replaces the old one at once)."""
    from app.services import partner_evidence as evidence
    client, err = _authenticate(count=False)
    if err:
        return err
    if request.method == 'GET':
        return jsonify({"secret": evidence.secret_public(evidence.secret_row(client)),
                        "mode": "test" if platform_svc.is_test(client) else "live"}), 200
    row, secret = evidence.create_secret(client)
    return jsonify({"secret": secret, **evidence.secret_public(row),
                    "message": "Store this secret now; it won't be shown again. Any older secret stops working."}), 201


@v1_bp.route('/evidence', methods=['POST'])
def evidence_post():
    """A signed sales outcome about a Siiqo seller (docs/PLATFORM_API_V1.md).
    Live keys: stored once per event id; counts toward the seller's Trust
    Score v1 only after the seller accepts this platform. Test keys: checked
    in full, never stored."""
    from app.services import partner_evidence as evidence
    raw = request.get_data(cache=True) or b""
    too_big = len(raw) > evidence.MAX_BODY_BYTES
    client, err = _authenticate(count=not too_big)
    if err:
        return err
    if too_big:
        return _error("PAYLOAD_TOO_LARGE", f"Send at most {evidence.MAX_BODY_BYTES} bytes.", 413)
    try:
        body, status = evidence.ingest(client, raw, request.headers.get(evidence.SIGNATURE_HEADER))
    except evidence.EvidenceError as e:
        res = jsonify(e.body())
        res.status_code = e.status
        return res
    return jsonify(body), status
