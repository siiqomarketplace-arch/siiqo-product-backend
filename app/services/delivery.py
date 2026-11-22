"""Delivery fees the buyer pays at checkout (BUYING_FIX_PLAN D1/D2).

The seller sets ``storefronts.delivery_settings``::

    {"self_delivery": {"enabled": bool, "fee_same_city": number,
                       "fee_same_state": number,
                       "fee_other_states": number | null,   # null = not outside the state
                       "ships_within_days": int},
     "pickup": {"enabled": bool, "area": "Wuse 2, Abuja"}}  # an area, never a full address

At checkout the server prices the chosen option itself; the fee the browser
sends is never trusted. Siiqo partners are priced from their coverage list
(app/services/logistics_partners.py).
"""
from __future__ import annotations

import logging
import math
import re

log = logging.getLogger(__name__)

MAX_FEE = 500_000
MAX_DAYS = 30
MAX_AREA = 120
PARTNER_MARKUP = 1.10           # buyer pays partner price x 1.10; the partner earns its price
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


def price_option(option: dict, sf, delivery_city, delivery_state, delivery_lga=None):
    """(fee, option id, provider quote or None) for one vendor's chosen
    delivery option, priced by the server. The fee the browser sends is never
    used. Raises DeliveryError when the option isn't available."""
    option = option if isinstance(option, dict) else {}
    opt_id = option.get("id")
    opt_id = str(opt_id).strip() if opt_id is not None else None

    if opt_id == VENDOR_SELF_ID:
        return vendor_self_fee(sf, delivery_city, delivery_state), opt_id, None

    if opt_id in PICKUP_IDS:
        ds = getattr(sf, "delivery_settings", None)
        if isinstance(ds, dict) and not ((ds.get("pickup") or {}).get("enabled")):
            raise DeliveryError("PICKUP_NOT_AVAILABLE", "This seller doesn't offer pickup.")
        return 0.0, opt_id, None

    if not opt_id:
        return 0.0, None, None

    # Siiqo partners (and, later, API couriers): app/services/logistics_partners.py
    from app.services.logistics_partners import quote_selected
    quote = quote_selected(opt_id, sf, delivery_state, delivery_city, delivery_lga)
    if quote is not None:
        return quote.fee, quote.id, quote

    log.warning("checkout: unknown delivery option %r refused", opt_id)
    raise DeliveryError("DELIVERY_OPTION_INVALID",
                        "That delivery option is not available. Please choose another one.")
