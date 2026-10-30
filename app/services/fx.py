"""
fx.py — naira exchange rate captured at payment time.

Vendors are paid in naira. An order charged in another currency (Pay Links
can be priced in USD/GBP/EUR/CAD/...) is paid out at the rate that applied
when the buyer paid, stored on ``Order.fx_rate_to_ngn`` (NGN per 1 unit of
the order currency).

Where the rate comes from, in order of preference:

1. The gateway itself.
   * Daya (bank transfer / crypto Pay Links): the buyer is asked for a fixed
     naira amount, stored as ``DayaPayment.amount_ngn``. Naira collected
     divided by the order's face amount is the rate the gateway applied.
   * Flutterwave v3 "verify transaction" returns ``amount``,
     ``charged_amount``, ``app_fee``, ``merchant_fee``, ``amount_settled``
     and ``currency``, all in the transaction currency. It returns no
     settlement currency and no exchange rate
     (https://developer.flutterwave.com/v3.0.0/reference/verify-transaction,
     https://developer.flutterwave.com/v3.0.0/docs/webhooks), so no gateway
     rate can be read from it and we fall through to a quote.
2. A quote from a rate source already used in the code: Daya ``get_rate``
   (NGN per USDT, side SELL = stablecoin to naira). USDT tracks USD, so this
   only quotes USD. Other currencies get no quote.
3. Nothing: the rate stays NULL and the payout is held for support.

A rate is never invented. When the gateway rate and the quote both exist and
differ by more than ``FX_MAX_DEVIATION`` (default 0.10 = 10%), the rate is
treated as wrong: nothing is stored, so the payout is held.

Each capture (or rejection) is recorded as a ``PlatformEvent`` with the source
used, so support can see where a payout's rate came from.
"""
import logging
import os
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from app.extensions import db

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEVIATION = Decimal("0.10")
_RATE_PLACES = Decimal("0.000001")
_KOBO = Decimal("0.01")

SOURCE_DAYA_AMOUNT = "gateway:daya_amount_ngn"
SOURCE_DAYA_QUOTE = "quote:daya_usdt_sell"


def _dec(value):
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def order_currency(order, escrow) -> str:
    return ((getattr(escrow, "currency", None) or getattr(order, "currency", None) or "NGN")
            .strip().upper())


def max_deviation() -> Decimal:
    d = _dec(os.environ.get("FX_MAX_DEVIATION"))
    return d if d is not None and d > 0 else DEFAULT_MAX_DEVIATION


def gateway_rate(escrow, gateway_payload):
    """The NGN rate the gateway applied, or None. Only documented data is used."""
    if gateway_payload is None or isinstance(gateway_payload, dict):
        # Flutterwave verify / webhook payloads carry no settlement rate.
        return None
    amount_ngn = _dec(getattr(gateway_payload, "amount_ngn", None))
    face = _dec(getattr(escrow, "amount", None))
    if amount_ngn and face and amount_ngn > 0 and face > 0:
        return amount_ngn / face, SOURCE_DAYA_AMOUNT
    return None


def quoted_rate(currency: str):
    """A live quote from Daya for USD, or None. Never raises."""
    if (currency or "").upper() != "USD" or not os.environ.get("DAYA_API_KEY"):
        return None
    try:
        from app.services import daya_service
        rate = _dec((daya_service.get_rate(asset="USDT", side="SELL") or {}).get("rate"))
    except Exception as exc:
        logger.info("[FX] Daya quote unavailable: %s", exc)
        return None
    if rate is None or rate <= 0:
        return None
    return rate, SOURCE_DAYA_QUOTE


def _record_event(order, name: str, source: str, properties: dict):
    try:
        from app.models.telemetry import PlatformEvent
        db.session.add(PlatformEvent(
            event_name=name,
            order_id=order.id,
            business_id=getattr(order, "vendor_id", None),
            source=(source or "")[:50],
            properties=properties,
        ))
    except Exception as exc:  # the audit row must never block a payment
        logger.warning("[FX] Could not record %s for Order #%s: %s", name, order.id, exc)


def capture_rate(order, escrow, gateway_payload=None):
    """Record the NGN rate on ``order.fx_rate_to_ngn`` when payment is confirmed.

    Idempotent: a rate already stored is kept. Returns ``{"rate", "source"}``
    or None when no trustworthy rate is available (the payout will be held).
    """
    if order is None or escrow is None:
        return None
    currency = order_currency(order, escrow)
    if currency == "NGN":
        if order.fx_rate_to_ngn is None:
            order.fx_rate_to_ngn = 1
        return {"rate": Decimal("1"), "source": "ngn"}

    existing = _dec(order.fx_rate_to_ngn)
    same_currency = (order.currency or "").strip().upper() == currency
    if same_currency and existing is not None and existing > 0:
        return {"rate": existing, "source": "existing"}

    if not same_currency:
        # The order row still carries another currency's rate (e.g. NGN/1 set
        # before the escrow's currency reached it): never reuse that rate.
        order.currency = currency
        order.fx_rate_to_ngn = None

    gw = gateway_rate(escrow, gateway_payload)
    quote = quoted_rate(currency)
    props = {
        "currency": currency,
        "gateway_rate": str(gw[0]) if gw else None,
        "gateway_source": gw[1] if gw else None,
        "quoted_rate": str(quote[0]) if quote else None,
        "quote_source": quote[1] if quote else None,
    }

    if gw and quote:
        deviation = abs(gw[0] - quote[0]) / quote[0]
        props["deviation"] = str(deviation.quantize(_RATE_PLACES))
        if deviation > max_deviation():
            logger.warning(
                "[FX] Order #%s %s rate rejected: gateway %s vs quote %s (deviation %s > %s) — payout will be held",
                order.id, currency, gw[0], quote[0], props["deviation"], max_deviation(),
            )
            _record_event(order, "fx_rate_rejected", gw[1], props)
            return None

    chosen = gw or quote
    if not chosen:
        logger.warning("[FX] Order #%s paid in %s — no rate available, payout will be held", order.id, currency)
        return None

    rate = chosen[0].quantize(_RATE_PLACES, rounding=ROUND_HALF_UP)
    if rate <= 0:
        return None
    order.fx_rate_to_ngn = rate
    props["rate"] = str(rate)
    _record_event(order, "fx_rate_captured", chosen[1], props)
    logger.info("[FX] Order #%s %s rate captured: %s NGN (%s)", order.id, currency, rate, chosen[1])
    return {"rate": rate, "source": chosen[1]}


def naira_net(order, escrow):
    """Vendor's net share (amount - fee) in naira for a foreign-currency order
    whose payment-time rate is known, rounded half-up to 2 dp. None when the
    order is in naira or the rate is unknown/unusable."""
    currency = order_currency(order, escrow)
    if currency == "NGN":
        return None
    if (getattr(order, "currency", None) or currency).strip().upper() != currency:
        return None  # rate on the order belongs to another currency
    rate = _dec(getattr(order, "fx_rate_to_ngn", None))
    if rate is None or rate <= 0:
        return None
    amount = _dec(escrow.amount) or Decimal("0")
    fee = _dec(escrow.fee_amount) or Decimal("0")
    return ((amount - fee) * rate).quantize(_KOBO, rounding=ROUND_HALF_UP)
