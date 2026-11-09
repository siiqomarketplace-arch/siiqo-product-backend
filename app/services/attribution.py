"""Where a buyer came from (BUYING_FIX_PLAN A2).

The website saves utm_source / utm_campaign / ref / the landing page for 30
days and sends them with checkout as ``attribution``. The bot sends the header
``X-Siiqo-Client: telegram|whatsapp``. This module cleans those values and
picks ``orders.source`` / ``orders.campaign``. Values are ids and labels only,
never personal data.
"""
from __future__ import annotations

import re

SOURCE_MAX = 40
CAMPAIGN_MAX = 100
_FIELD_MAX = {"source": SOURCE_MAX, "medium": 40, "campaign": CAMPAIGN_MAX, "ref": 100, "landing": 200}
BOT_CLIENTS = ("telegram", "whatsapp")


def clean_token(value, max_len: int) -> str | None:
    """Lowercase, only [a-z0-9_.-] (spaces become '_'), cut to max_len."""
    if not isinstance(value, str):
        return None
    v = re.sub(r"\s+", "_", value.strip().lower())
    v = re.sub(r"[^a-z0-9_.\-]", "", v)[:max_len].strip("_.-")
    return v or None


def clean_attribution(raw) -> dict:
    """The optional checkout ``attribution`` object, cleaned. Unknown keys are dropped."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, max_len in _FIELD_MAX.items():
        val = raw.get(key)
        if key == "landing":
            # Keep the path shape for the market/store hint; never stored as-is.
            if isinstance(val, str) and val.strip():
                out[key] = val.strip().lower()[:max_len]
            continue
        val = clean_token(val, max_len)
        if val:
            out[key] = val
    return out


def _landing_hint(landing: str | None) -> str | None:
    if not landing:
        return None
    path = re.sub(r"^[a-z]+://[^/]+", "", landing).split("?")[0].strip("/")
    first = path.split("/")[0] if path else ""
    if first in ("market", "marketplace", "search"):
        return "market"
    if first in ("store", "stores", "s", "shop") or landing in ("store", "storefront"):
        return "store"
    if first in ("product", "products"):
        return "product"
    return None


def client_header_source(headers) -> str | None:
    """'telegram' / 'whatsapp' from the bot's X-Siiqo-Client header."""
    try:
        val = clean_token(headers.get("X-Siiqo-Client"), SOURCE_MAX)
    except Exception:
        return None
    return val if val in BOT_CLIENTS else None


def order_source(attribution: dict, headers=None, *, pay_link: bool = False) -> str:
    """orders.source: attribution.source, else pay_link, else the bot client,
    else the landing page hint (market/store), else 'direct'."""
    if attribution.get("source"):
        return attribution["source"]
    if pay_link:
        return "pay_link"
    bot = client_header_source(headers) if headers is not None else None
    if bot:
        return bot
    return _landing_hint(attribution.get("landing")) or "direct"


def apply_to_order(order, payload, headers=None, *, pay_link: bool = False) -> dict:
    """Set order.source / order.campaign from a request payload. Returns the
    cleaned attribution (without the landing path) for event properties."""
    attribution = clean_attribution((payload or {}).get("attribution") if isinstance(payload, dict) else None)
    order.source = order_source(attribution, headers, pay_link=pay_link)
    order.campaign = attribution.get("campaign")
    return {k: v for k, v in attribution.items() if k != "landing"}
