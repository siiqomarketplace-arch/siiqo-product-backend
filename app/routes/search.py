"""
Phase 3 public browsing and search routes (no auth).

Ask (item 3-3), website /search and /ask:
  GET /api/search/parse?q=        instant regex parse for the intent chips
  GET /api/search?q=&...          parsed intent + ranked results (no reason lines)
  GET /api/search/reasons?q=&...  fact-checked reason lines for the same answer

  Refinement filters on /search and /search/reasons:
    city, min_price, max_price, verified=1, type=physical|digital|service|event
  `reasons=1` on /search writes the reason lines in the same request.

Market (item 3-4), website /market:
  GET /api/market/products   page, per_page (<=48), q, category (slug or name),
                             city, min_price, max_price, type, verified=1,
                             sort=newest|price_asc|price_desc
  GET /api/market/stores     page, per_page, q, city, verified=1
  GET /api/market/categories categories that have visible products, with counts

Only active, non-draft products from published stores of active accounts are
returned. Rankings never use a paid plan.
"""
from flask import Blueprint, jsonify, request

from app.extensions import get_real_client_ip, limiter
from app.services import search as svc

search_bp = Blueprint("search", __name__)
market_bp = Blueprint("market", __name__)

MIN_QUERY_LEN = 2
MAX_QUERY_LEN = 200
SEARCH_RATE_LIMIT = "30 per minute"
REASONS_RATE_LIMIT = "30 per minute"
PARSE_RATE_LIMIT = "120 per minute"
MARKET_RATE_LIMIT = "120 per minute"

_TRUE = ("1", "true", "yes", "on")


def _query():
    q = " ".join((request.args.get("q") or "").split())
    if len(q) < MIN_QUERY_LEN:
        return None, (jsonify({"code": "QUERY_TOO_SHORT",
                               "message": "Type at least 2 letters to search."}), 400)
    return q[:MAX_QUERY_LEN], None


def _filters() -> dict:
    out = {}
    city = (request.args.get("city") or "").strip()[:60]
    if city:
        out["city"] = city
    for name in ("min_price", "max_price"):
        raw = request.args.get(name)
        if raw not in (None, ""):
            try:
                val = float(raw)
            except ValueError:
                continue
            if val >= 0:
                out[name] = val
    if (request.args.get("verified") or "").lower() in _TRUE:
        out["verified"] = True
    ptype = (request.args.get("type") or "").strip().lower()
    if ptype in svc.PRODUCT_TYPES:
        out["type"] = ptype
    return out


def _log_search(q: str, body: dict) -> None:
    try:
        from app.services.event_logger import log_platform_event
        log_platform_event(
            event_name="search_performed",
            session_id=request.headers.get("X-Session-ID"),
            source="ask",
            properties={
                "query": q[:120],
                "parse_source": body["parse"]["source"],
                "results_count": body["total"],
                "zero_results": body["zero_results"],
                "cached": body["cached"],
            },
        )
    except Exception:  # noqa: BLE001 - telemetry never breaks search
        pass


@search_bp.route("/parse", methods=["GET"])
@limiter.limit(PARSE_RATE_LIMIT)
def parse():
    q, err = _query()
    if err:
        return err
    return jsonify(svc.parse_only(q)), 200


@search_bp.route("", methods=["GET"])
@limiter.limit(SEARCH_RATE_LIMIT)
def ask():
    q, err = _query()
    if err:
        return err
    with_reasons = (request.args.get("reasons") or "").lower() in _TRUE
    body = svc.ask(q, ip=get_real_client_ip(), filters=_filters(), with_reasons=with_reasons)
    _log_search(q, body)
    return jsonify(body), 200


@search_bp.route("/reasons", methods=["GET"])
@limiter.limit(REASONS_RATE_LIMIT)
def reasons():
    q, err = _query()
    if err:
        return err
    return jsonify(svc.reasons(q, ip=get_real_client_ip(), filters=_filters())), 200


@market_bp.route("/products", methods=["GET"])
@limiter.limit(MARKET_RATE_LIMIT)
def market_products():
    return jsonify(svc.market_products(request.args.to_dict())), 200


@market_bp.route("/stores", methods=["GET"])
@limiter.limit(MARKET_RATE_LIMIT)
def market_stores():
    return jsonify(svc.market_stores(request.args.to_dict())), 200


@market_bp.route("/categories", methods=["GET"])
@limiter.limit(MARKET_RATE_LIMIT)
def market_categories():
    res = jsonify({"categories": svc.market_categories()})
    res.headers["Cache-Control"] = "public, max-age=300"
    return res, 200
