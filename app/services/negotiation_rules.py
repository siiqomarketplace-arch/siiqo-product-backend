"""
Price rules for the store concierge (Phase 4, 8a). Code decides; AI only words it.

Uses the same product fields as the buyer negotiation route
(app/routes/negotiation.py): ``Product.is_negotiable`` and the hidden
``Product.floor_price``. The floor never leaves this module except as a yes/no.

    evaluate(product, offered_unit_price, quantity) -> Decision

Decision.status:
  accept          the rule allows this unit price (Decision.unit_price)
  needs_seller    negotiable but no floor is set, so only the seller can say yes
  below_minimum   under the floor: refused (the buyer is not told the floor)
  not_negotiable  the product's price is fixed and the offer is below it
  out_of_stock    not enough stock for the quantity
  invalid         inactive product, free item, bad amount or quantity

Only ``accept`` may ever become a Pay Link without the seller (and only when
the seller turned on "auto-accept within my floor price"). A buyer never pays
more than the list price: an offer at or above it is accepted at the list price.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_QUANTITY = 20

ACCEPT = "accept"
NEEDS_SELLER = "needs_seller"
BELOW_MINIMUM = "below_minimum"
NOT_NEGOTIABLE = "not_negotiable"
OUT_OF_STOCK = "out_of_stock"
INVALID = "invalid"

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Decision:
    status: str
    list_price: Decimal | None
    unit_price: Decimal | None      # the rule-approved unit price (accept / needs_seller)
    quantity: int
    reason: str = ""

    @property
    def total(self) -> Decimal | None:
        if self.unit_price is None:
            return None
        return (self.unit_price * self.quantity).quantize(CENT)

    @property
    def can_draft(self) -> bool:
        return self.status in (ACCEPT, NEEDS_SELLER)


def _money(value) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    return d


def evaluate(product, offered_unit_price=None, quantity: int | None = 1) -> Decision:
    """``offered_unit_price`` None means the buyer wants it at the list price."""
    try:
        qty = 1 if quantity is None else int(quantity)
    except (TypeError, ValueError):
        qty = 1
    list_price = _money(getattr(product, "price", None)) if product is not None else None

    if product is None or not getattr(product, "is_active", False) or getattr(product, "is_deleted", False):
        return Decision(INVALID, list_price, None, qty, "product unavailable")
    if qty < 1 or qty > MAX_QUANTITY:
        return Decision(INVALID, list_price, None, qty, "quantity out of range")
    if list_price is None or list_price <= 0 or getattr(product, "is_free", False):
        return Decision(INVALID, list_price, None, qty, "no price to pay")
    if (product.product_type or "physical") == "physical" and int(product.stock_quantity or 0) < qty:
        return Decision(OUT_OF_STOCK, list_price, None, qty, "not enough stock")

    if offered_unit_price is None:
        return Decision(ACCEPT, list_price, list_price, qty, "list price")
    offered = _money(offered_unit_price)
    if offered is None or offered <= 0:
        return Decision(INVALID, list_price, None, qty, "bad amount")
    if offered >= list_price:
        return Decision(ACCEPT, list_price, list_price, qty, "at or above list price")
    if not product.is_negotiable:
        return Decision(NOT_NEGOTIABLE, list_price, None, qty, "fixed price")

    floor = _money(product.floor_price)
    if floor is None or floor <= 0:
        return Decision(NEEDS_SELLER, list_price, offered, qty, "no floor set")
    floor = min(floor, list_price)
    if offered < floor:
        return Decision(BELOW_MINIMUM, list_price, None, qty, "below floor")
    return Decision(ACCEPT, list_price, offered, qty, "within floor")


# ── Reading an amount from a buyer's message (code, not AI) ─────────────────
_AMOUNT_RE = re.compile(
    r"(?P<cur>₦|ngn|naira|\bn(?=\s?\d))?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<suf>k\b|m\b|thousand\b|naira\b|ngn\b)?",
    re.IGNORECASE,
)


def parse_amounts(text: str) -> list[Decimal]:
    """Money amounts written in a message: ₦15,000 / 15k / 15000 naira / 1.5m.
    Bare numbers under 100 with no money marker are skipped (they are usually
    quantities: "2 pieces")."""
    out = []
    for m in _AMOUNT_RE.finditer(text or ""):
        try:
            value = Decimal(m.group("num").replace(",", ""))
        except InvalidOperation:
            continue
        suf = (m.group("suf") or "").lower()
        if suf in ("k", "thousand"):
            value *= 1000
        elif suf == "m":
            value *= 1_000_000
        marked = bool(m.group("cur") or suf)
        if not marked and value < 100:
            continue
        out.append(value.quantize(CENT))
    return out


def parse_offer_amount(text: str) -> Decimal | None:
    """The buyer's offered amount: the last money amount in the message, or None."""
    amounts = parse_amounts(text)
    return amounts[-1] if amounts else None
