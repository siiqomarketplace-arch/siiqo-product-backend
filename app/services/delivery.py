"""Delivery fees the buyer pays at checkout (BUYING_FIX_PLAN D1/D2).

The seller sets ``storefronts.delivery_settings``::

    {"self_delivery": {"enabled": bool, "fee_same_city": number,
                       "fee_same_state": number,
                       "fee_other_states": number | null,   # null = not outside the state
                       "ships_within_days": int},
     "pickup": {"enabled": bool, "area": "Wuse 2, Abuja"}}  # an area, never a full address

At checkout the server prices the chosen option itself; the fee the browser
sends is never trusted for the seller's own delivery or pickup, and is clamped
to the partner's minimum for Siiqo partners.
"""
from __future__ import annotations

import logging
import math
import re

log = logging.getLogger(__name__)

MAX_FEE = 500_000
MAX_DAYS = 30
MAX_AREA = 120
PARTNER_MARKUP = 1.10           # as cart.py: the partner is paid fee / 1.10
PARTNER_MAX_FEE = 200_000
PICKUP_IDS = ("pickup", "self_pickup")
VENDOR_SELF_ID = "vendor_self"
PARTNER_PREFIX = "siiqo_partner_"


class DeliveryError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# ── seller settings ────────────────────────────────────────────────────────

def _fee(value, field):
    """A fee in naira, or None when not set (for fee_other_states: not offered)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise DeliveryError("INVALID_DELIVERY_SETTINGS", f"{field} must be a number.")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise DeliveryError("INVALID_DELIVERY_SETTINGS", f"{field} must be a number.")
    if not math.isfinite(num) or num < 0 or num > MAX_FEE:
        raise DeliveryError("INVALID_DELIVERY_SETTINGS",
                            f"{field} must be between 0 and {MAX_FEE:,}.")
    return round(num, 2)


def clean_delivery_settings(raw) -> dict | None:
    """Validate and normalise what the seller sent. None clears the settings."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DeliveryError("INVALID_DELIVERY_SETTINGS", "delivery_settings must be an object.")
    sd = raw.get("self_delivery") or {}
    pk = raw.get("pickup") or {}
    if not isinstance(sd, dict) or not isinstance(pk, dict):
        raise DeliveryError("INVALID_DELIVERY_SETTINGS", "self_delivery and pickup must be objects.")

    days = sd.get("ships_within_days")
    if days is not None and days != "":
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise DeliveryError("INVALID_DELIVERY_SETTINGS", "ships_within_days must be a whole number.")
        if days < 0 or days > MAX_DAYS:
            raise DeliveryError("INVALID_DELIVERY_SETTINGS", f"ships_within_days must be 0 to {MAX_DAYS}.")
    else:
        days = None

    self_delivery = {
        "enabled": bool(sd.get("enabled")),
        "fee_same_city": _fee(sd.get("fee_same_city"), "fee_same_city"),
        "fee_same_state": _fee(sd.get("fee_same_state"), "fee_same_state"),
        "fee_other_states": _fee(sd.get("fee_other_states"), "fee_other_states"),
        "ships_within_days": days,
    }
    if self_delivery["enabled"] and self_delivery["fee_same_city"] is None and self_delivery["fee_same_state"] is None:
        raise DeliveryError("INVALID_DELIVERY_SETTINGS",
                            "Set your delivery fee for buyers in your city or state.")

    area = pk.get("area")
    area = " ".join(str(area).split())[:MAX_AREA] if isinstance(area, (str, int, float)) and str(area).strip() else None
    pickup = {"enabled": bool(pk.get("enabled")), "area": area}
    return {"self_delivery": self_delivery, "pickup": pickup}


def public_delivery_settings(sf) -> dict | None:
    """What buyers may see: the same shape (fees, days, pickup area only)."""
    ds = getattr(sf, "delivery_settings", None)
    if not isinstance(ds, dict):
        return None
    try:
        return clean_delivery_settings(ds)
    except DeliveryError:
        return None


# ── checkout pricing ───────────────────────────────────────────────────────

def _norm_place(value) -> str:
    s = re.sub(r"\bstate\b", "", str(value or "").lower())
    s = re.sub(r"[^a-z ]", " ", s)
    s = " ".join(s.split())
    if s in ("federal capital territory", "fct abuja", "abuja fct", "abuja"):
        s = "fct"
    return s


def _same_state(a, b) -> bool:
    x, y = _norm_place(a), _norm_place(b)
    return bool(x and y and x == y)


def vendor_self_fee(sf, delivery_city, delivery_state) -> float:
    """The seller's own delivery fee for this address (same rule as the
    website): same city -> fee_same_city, same state -> fee_same_state,
    elsewhere -> fee_other_states. Raises DELIVERY_NOT_AVAILABLE."""
    ds = public_delivery_settings(sf)
    sd = (ds or {}).get("self_delivery") or {}
    if not sd.get("enabled"):
        raise DeliveryError("DELIVERY_NOT_AVAILABLE", "This seller doesn't offer their own delivery.")
    buyer_city = _norm_place(delivery_city)
    city_match = bool(buyer_city) and buyer_city == _norm_place(getattr(sf, "city", None))
    state_match = _same_state(delivery_state, getattr(sf, "state", None))
    if city_match and (state_match or not getattr(sf, "state", None)):
        fee = sd.get("fee_same_city") if sd.get("fee_same_city") is not None else sd.get("fee_same_state")
    elif state_match:
        fee = sd.get("fee_same_state")
    else:
        fee = sd.get("fee_other_states")
    if fee is None:
        raise DeliveryError("DELIVERY_NOT_AVAILABLE",
                            "This seller doesn't deliver to your area. Choose pickup or another option.")
    return float(fee)


def partner_min_fee(partner_id: int) -> float:
    """Lowest fee a buyer can pay for a Siiqo partner: its flat rate (FLAT) or
    base fee (other models), with the 10% markup. Same defaults as
    GET /api/logistics/active."""
    pricing = {}
    if partner_id:
        from app.models.partnerships import PartnerApplication
        app_row = PartnerApplication.query.filter_by(user_id=partner_id, status="APPROVED").first()
        pricing = (app_row.pricing_settings if app_row and isinstance(app_row.pricing_settings, dict) else {}) or {}
        model = (pricing.get("pricing_model") or "FLAT").upper()
        try:
            base = float(pricing.get("flat_rate") or 1500) if model == "FLAT" else float(pricing.get("base_fee") or 1000)
        except (TypeError, ValueError):
            base = 1500.0
    else:
        base = 1500.0   # "Siiqo Standard Delivery" fallback option
    return round(max(0.0, base) * PARTNER_MARKUP, 2)


def price_option(option: dict, sf, delivery_city, delivery_state) -> tuple[float, str | None]:
    """(fee, provider id) for one vendor's chosen logistics option."""
    option = option if isinstance(option, dict) else {}
    opt_id = option.get("id")
    opt_id = str(opt_id).strip() if opt_id is not None else None
    try:
        client_fee = max(0.0, float(option.get("fee", 0.0)))
        if not math.isfinite(client_fee):
            client_fee = 0.0
    except (TypeError, ValueError):
        client_fee = 0.0

    if opt_id == VENDOR_SELF_ID:
        return vendor_self_fee(sf, delivery_city, delivery_state), opt_id

    if opt_id in PICKUP_IDS:
        ds = getattr(sf, "delivery_settings", None)
        if isinstance(ds, dict) and not ((ds.get("pickup") or {}).get("enabled")):
            raise DeliveryError("PICKUP_NOT_AVAILABLE", "This seller doesn't offer pickup.")
        return 0.0, opt_id

    if opt_id and opt_id.startswith(PARTNER_PREFIX):
        pid = opt_id[len(PARTNER_PREFIX):]
        minimum = partner_min_fee(int(pid)) if pid.isdigit() else partner_min_fee(0)
        fee = min(max(client_fee, minimum), PARTNER_MAX_FEE)
        if fee != client_fee:
            log.warning("checkout: partner fee %.2f for %s adjusted to %.2f", client_fee, opt_id, fee)
        return round(fee, 2), opt_id

    if client_fee > 0:
        log.warning("checkout: unknown logistics option %r with fee %.2f kept as sent", opt_id, client_fee)
    return client_fee, opt_id
