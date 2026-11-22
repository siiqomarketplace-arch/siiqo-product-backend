"""Delivery providers: who may deliver an order, where, and for how much.

Today there is one kind of provider: an approved Siiqo logistics partner
(a ``PartnerApplication`` with status APPROVED). A partner lists the areas it
serves and its price per area in ``pricing_settings["coverage"]``::

    [{"state": "Lagos", "lgas": ["Ikeja", "Yaba"], "price": 2500},
     {"state": "Ogun",  "lgas": [],                "price": 4000}]   # [] = whole state

The server prices every partner delivery from that list; the fee the browser
sends is never used. The buyer pays the partner's price plus Siiqo's 10%
(PARTNER_MARKUP); the partner earns its own price.

Seam for API couriers (not built): ``quote_options`` asks every provider in
``_PROVIDERS`` for ``DeliveryQuote``s, and ``quote_selected`` resolves the
option id the buyer picked. A courier provider (e.g. an aggregator such as
Terminal Africa) would add a class with the same two methods, option ids
prefixed ``courier_``, and ``provider_type = "courier"`` on its quotes and on
``LogisticsAssignment.provider_type``. Nothing else in checkout needs to change.
"""
from __future__ import annotations

import functools
import logging
import math
import re
from dataclasses import asdict, dataclass

from app.services.delivery import (DeliveryError, PARTNER_MARKUP, PARTNER_MAX_FEE, PARTNER_PREFIX,
                                   _norm_place, _same_state)

log = logging.getLogger(__name__)

PROVIDER_SIIQO_PARTNER = "siiqo_partner"
PROVIDER_COURIER = "courier"            # reserved for API couriers; no provider yet
COURIER_PREFIX = "courier_"             # reserved option-id prefix for API couriers

MAX_COVERAGE_AREAS = 60
MAX_LGAS_PER_AREA = 60
MAX_NAME = 80
LEGACY_DEFAULT_PRICE = 1500.0           # what older partners without prices were shown at


# ── who is an approved partner ─────────────────────────────────────────────

@dataclass
class PartnerAccess:
    ok: bool
    status: int = 200
    code: str | None = None
    message: str | None = None
    application: object = None

    def response(self):
        from flask import jsonify
        body = {"message": self.message, "code": self.code}
        if self.application is not None:
            body["application_status"] = self.application.status
            if self.code == "PARTNER_REJECTED" and getattr(self.application, "review_note", None):
                body["review_note"] = self.application.review_note
        return jsonify(body), self.status


PENDING_MESSAGE = ("Your application is being reviewed. We'll email you as soon as it's approved, "
                   "usually within 48 hours.")


def latest_application(user_id):
    from app.models.partnerships import PartnerApplication
    return (PartnerApplication.query.filter_by(user_id=user_id)
            .order_by(PartnerApplication.id.desc()).first())


def partner_access(user) -> PartnerAccess:
    """Only an active PARTNER account with an APPROVED application (not
    suspended) may use the partner portal, its routes and withdrawals."""
    from app.models.user import UserRole
    if user is None:
        return PartnerAccess(False, 401, "AUTH_REQUIRED", "Please log in again.")
    if user.role != UserRole.PARTNER:
        return PartnerAccess(False, 403, "NOT_PARTNER",
                             "This account is not a Siiqo partner. Apply at siiqo.com/partners/apply")
    app_row = latest_application(user.id)
    if app_row is None:
        return PartnerAccess(False, 403, "NOT_PARTNER",
                             "We can't find your partner application. Apply at siiqo.com/partners/apply")
    status = (app_row.status or "PENDING").upper()
    if status == "PENDING":
        return PartnerAccess(False, 403, "PARTNER_PENDING", PENDING_MESSAGE, app_row)
    if status == "REJECTED":
        return PartnerAccess(False, 403, "PARTNER_REJECTED",
                             "Your partner application was not approved. Check your email for the reason.",
                             app_row)
    if status == "SUSPENDED" or not user.is_active or not user.is_verified:
        return PartnerAccess(False, 403, "PARTNER_SUSPENDED",
                             "Your partner account is suspended. Contact support@siiqo.com.", app_row)
    if status != "APPROVED":
        return PartnerAccess(False, 403, "NOT_PARTNER", "Your partner account is not active.", app_row)
    return PartnerAccess(True, 200, None, None, app_row)


def partner_required(fn):
    """Route guard (use under @jwt_required()). Sets flask.g.partner and
    g.partner_app, or answers 401/403 with a clear code."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from flask import g
        from flask_jwt_extended import get_jwt_identity
        from app.extensions import db
        from app.models.user import User
        try:
            user = db.session.get(User, int(get_jwt_identity()))
        except (TypeError, ValueError):
            user = None
        access = partner_access(user)
        if not access.ok:
            return access.response()
        g.partner = user
        g.partner_app = access.application
        return fn(*args, **kwargs)
    return wrapper


# ── coverage and prices ────────────────────────────────────────────────────

def _clean_name(value) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    s = " ".join(str(value).split())[:MAX_NAME]
    return s or None


def _clean_price(value, where) -> float:
    if isinstance(value, bool) or value is None or value == "":
        raise DeliveryError("INVALID_COVERAGE", f"Set a delivery price for {where}.")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise DeliveryError("INVALID_COVERAGE", f"The price for {where} must be a number.")
    if not math.isfinite(num) or num <= 0 or num > PARTNER_MAX_FEE:
        raise DeliveryError("INVALID_COVERAGE",
                            f"The price for {where} must be between 1 and {PARTNER_MAX_FEE:,}.")
    return round(num, 2)


def clean_coverage(raw) -> list[dict]:
    """Validate the areas a partner serves. Raises DeliveryError."""
    if not isinstance(raw, list) or not raw:
        raise DeliveryError("INVALID_COVERAGE", "Add at least one state you deliver to, with a price.")
    if len(raw) > MAX_COVERAGE_AREAS:
        raise DeliveryError("INVALID_COVERAGE", f"You can list up to {MAX_COVERAGE_AREAS} areas.")
    out, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise DeliveryError("INVALID_COVERAGE", "Each area needs a state and a price.")
        state = _clean_name(entry.get("state"))
        if not state:
            raise DeliveryError("INVALID_COVERAGE", "Each area needs a state.")
        lgas_raw = entry.get("lgas") or []
        if isinstance(lgas_raw, str):
            lgas_raw = [x for x in lgas_raw.split(",")]
        if not isinstance(lgas_raw, list) or len(lgas_raw) > MAX_LGAS_PER_AREA:
            raise DeliveryError("INVALID_COVERAGE", f"Up to {MAX_LGAS_PER_AREA} LGAs per state.")
        lgas = []
        for lga in lgas_raw:
            name = _clean_name(lga)
            if name and name.lower() not in {x.lower() for x in lgas}:
                lgas.append(name)
        where = f"{state}" + (f" ({', '.join(lgas[:3])}{'...' if len(lgas) > 3 else ''})" if lgas else "")
        price = _clean_price(entry.get("price"), where)
        key = (_norm_place(state), tuple(sorted(x.lower() for x in lgas)))
        if key in seen:
            raise DeliveryError("INVALID_COVERAGE", f"{where} is listed twice.")
        seen.add(key)
        out.append({"state": state, "lgas": lgas, "price": price})
    return out


def _legacy_price(pricing: dict) -> float:
    model = (pricing.get("pricing_model") or "FLAT").upper()
    raw = pricing.get("flat_rate") if model == "FLAT" else pricing.get("base_fee")
    try:
        num = float(raw)
        if math.isfinite(num) and num > 0:
            return round(min(num, PARTNER_MAX_FEE), 2)
    except (TypeError, ValueError):
        pass
    return LEGACY_DEFAULT_PRICE


def partner_coverage(app_row) -> list[dict]:
    """The partner's coverage list. Partners who joined before coverage
    existed are read from state_of_operation and their old flat rate."""
    pricing = app_row.pricing_settings if isinstance(getattr(app_row, "pricing_settings", None), dict) else {}
    cov = pricing.get("coverage")
    if isinstance(cov, list) and cov:
        try:
            return clean_coverage(cov)
        except DeliveryError:
            log.warning("logistics: partner app %s has invalid coverage; ignoring", getattr(app_row, "id", None))
            return []
    states = [s.strip() for s in re.split(r"[,;/]", app_row.state_of_operation or "") if s.strip()]
    price = _legacy_price(pricing)
    return [{"state": s, "lgas": [], "price": price} for s in states]


def partner_price_for(app_row, state, city=None, lga=None) -> float | None:
    """The partner's own price to deliver to this address, or None when it
    doesn't serve it. A matching LGA entry wins over a whole-state entry."""
    if not state:
        return None
    places = {p for p in (_norm_place(city), _norm_place(lga)) if p}
    whole_state = None
    for entry in partner_coverage(app_row):
        if not _same_state(entry["state"], state):
            continue
        if entry["lgas"]:
            if places & {_norm_place(x) for x in entry["lgas"]}:
                return float(entry["price"])
        elif whole_state is None:
            whole_state = float(entry["price"])
    return whole_state


def buyer_fee(partner_price: float) -> float:
    return round(max(0.0, float(partner_price)) * PARTNER_MARKUP, 2)


def vendor_partner_ids(sf) -> set[int] | None:
    """Partner user ids the seller picked in storefronts.logistics_settings,
    or None when the seller hasn't picked any (then any approved partner)."""
    chosen = getattr(sf, "logistics_settings", None)
    if not isinstance(chosen, list):
        return None
    ids = set()
    for item in chosen:
        s = str(item or "")
        if s.startswith(PARTNER_PREFIX) and s[len(PARTNER_PREFIX):].isdigit():
            ids.add(int(s[len(PARTNER_PREFIX):]))
    return ids or None


def approved_logistics_partners():
    """[(user, application)] for every approved, active logistics partner."""
    from app.models.partnerships import PartnerApplication
    from app.models.user import User, UserRole
    rows = (PartnerApplication.query.join(User, User.id == PartnerApplication.user_id)
            .filter(PartnerApplication.status == "APPROVED",
                    User.role == UserRole.PARTNER, User.is_active.is_(True), User.is_verified.is_(True))
            .order_by(PartnerApplication.id).all())
    out, seen = [], set()
    for app_row in rows:
        if (app_row.service_type or "LOGISTICS").upper() != "LOGISTICS" or app_row.user_id in seen:
            continue
        # Only the newest application counts (a later SUSPENDED row wins).
        latest = latest_application(app_row.user_id)
        if latest is None or latest.id != app_row.id:
            continue
        seen.add(app_row.user_id)
        out.append((app_row.user, app_row))
    return out


# ── quotes (the provider seam) ─────────────────────────────────────────────

@dataclass
class DeliveryQuote:
    id: str                     # option id the buyer sends back at checkout
    provider_type: str          # "siiqo_partner" (later "courier")
    provider_id: int | None     # partner user id (None for couriers)
    name: str
    fee: float                  # what the buyer pays
    provider_fee: float         # what the provider earns (never sent to buyers)
    currency: str = "NGN"
    estimated_delivery: str = "1-3 days"

    def public(self) -> dict:
        d = asdict(self)
        d.pop("provider_fee", None)
        d["type"] = "local_dispatch"
        return d


class SiiqoPartnerProvider:
    provider_type = PROVIDER_SIIQO_PARTNER

    def quotes(self, sf, state, city=None, lga=None) -> list[DeliveryQuote]:
        allowed = vendor_partner_ids(sf)
        out = []
        for user, app_row in approved_logistics_partners():
            if allowed is not None and user.id not in allowed:
                continue
            price = partner_price_for(app_row, state, city, lga)
            if price is None:
                continue
            out.append(DeliveryQuote(
                id=f"{PARTNER_PREFIX}{user.id}", provider_type=self.provider_type, provider_id=user.id,
                name=app_row.business_name or user.full_name or "Siiqo delivery partner",
                fee=buyer_fee(price), provider_fee=price))
        return out

    def quote(self, option_id, sf, state, city=None, lga=None) -> DeliveryQuote | None:
        if not str(option_id).startswith(PARTNER_PREFIX):
            return None
        pid = str(option_id)[len(PARTNER_PREFIX):]
        if not pid.isdigit() or int(pid) <= 0:
            raise DeliveryError("DELIVERY_OPTION_INVALID", "That delivery option is not available.")
        for q in self.quotes(sf, state, city, lga):
            if q.provider_id == int(pid):
                return q
        raise DeliveryError("DELIVERY_NOT_AVAILABLE",
                            "This delivery partner doesn't deliver to your area. Choose another option.")


# Add an API courier provider here later (same quotes()/quote() methods).
_PROVIDERS = [SiiqoPartnerProvider()]


def quote_options(sf, state, city=None, lga=None) -> list[DeliveryQuote]:
    quotes = []
    for provider in _PROVIDERS:
        try:
            quotes.extend(provider.quotes(sf, state, city, lga))
        except Exception as e:  # one provider failing never hides the others
            log.warning("logistics: provider %s quote failed: %s", provider.provider_type, e)
    return sorted(quotes, key=lambda q: q.fee)


def quote_selected(option_id, sf, state, city=None, lga=None) -> DeliveryQuote | None:
    """The server's quote for the option the buyer picked, None when the id
    isn't a provider option. Raises DeliveryError when it isn't allowed."""
    for provider in _PROVIDERS:
        q = provider.quote(option_id, sf, state, city, lga)
        if q is not None:
            return q
    return None
