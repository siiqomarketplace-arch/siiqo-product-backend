"""
Store concierge (Phase 4, item 8a; AI_ORCHESTRATION_OPTIONS.md section 2, row 8a).

A buyer chat on one seller's store. A constrained workflow, not an agent:

  1. limits (code): store opted in, per-IP limit, per-store monthly cap;
  2. classify the buyer's message (Haiku, prompt ``concierge_classify``);
  3. retrieve ONLY this store's catalogue and policies (code);
  4. negotiation and "buy" go through the code rule engine
     (app/services/negotiation_rules.py, ``is_negotiable`` / ``floor_price``);
     an accepted price becomes a ConciergeOffer DRAFT the seller approves, or,
     when the seller turned on "auto-accept within my floor price", a Pay Link
     created here by code at the rule price;
  5. word the reply (Haiku, prompt ``concierge_answer``) from the retrieved
     facts only; a code critic checks every product, price and stock claim;
     1 retry, then a templated reply built from the same facts;
  6. anything the facts cannot answer, AI off, a cap or an error: "Ask the
     seller directly" with the store's contact.

No buyer personal data goes to the AI: the buyer is never asked for any, the
inputs hold only the message text and catalogue facts, and the gateway strips
phone numbers, emails and account numbers from the text before any call.
The floor price is never sent to the AI and never shown to the buyer.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.assistant import (
    ConciergeConversation,
    ConciergeMessage,
    ConciergeOffer,
    ConciergeSettings,
)
from app.models.product import Product
from app.models.user import Storefront
from app.services import ai
from app.services import negotiation_rules as rules

log = logging.getLogger(__name__)

ACTION_CLASSIFY = "concierge_classify"
ACTION_ANSWER = "concierge_answer"
MAX_MESSAGE_LEN = 500
MAX_PRODUCTS = 8
MAX_TURNS_CONTEXT = 4
AI_TIMEOUT_SECONDS = 8.0
ANSWER_ATTEMPTS = 2          # 1 retry per answer
SESSION_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")

INTENTS = ("product_info", "price", "availability", "negotiate", "buy", "delivery",
           "store_policy", "contact_seller", "greeting", "other")
OFFER_INTENTS = ("negotiate", "buy")

BANNED_PHRASES = ("floor price", "minimum price", "lowest price is", "verified", "guarantee",
                  "escrow", "100% safe")

FALLBACK_TEXT = "I can't answer that here. Please ask the seller directly."
CAP_TEXT = "The store assistant is resting for now. Please ask the seller directly."


class ConciergeError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def get_client():
    """Anthropic-compatible client for the gateway; None builds the default.
    Tests replace this with a fake."""
    return None


# ── Configuration ──────────────────────────────────────────────────────────
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def monthly_cap() -> int:
    """AI-answered buyer questions per store per calendar month (UTC)."""
    return _env_int("CONCIERGE_MONTHLY_CAP", 300)


def ip_limits() -> list[tuple[str, int, int]]:
    return [
        ("minute", 60, _env_int("CONCIERGE_IP_PER_MINUTE", 8)),
        ("day", 86400, _env_int("CONCIERGE_IP_PER_DAY", 80)),
    ]


def _month_start(now=None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def _hash(value: str, purpose: str) -> str:
    key = (current_app.config.get("SECRET_KEY") or "siiqo").encode("utf-8")
    return hmac.new(key, f"{purpose}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


# ── Store, settings, contact ───────────────────────────────────────────────
def store_for_slug(slug: str) -> Storefront | None:
    store = Storefront.query.filter_by(store_slug=(slug or "").strip().lower()).first()
    if store is None or not store.is_published:
        return None
    if store.vendor is not None and not store.vendor.is_active:
        return None
    return store


def settings_for(store: Storefront, create: bool = False) -> ConciergeSettings | None:
    row = ConciergeSettings.query.filter_by(storefront_id=store.id).first()
    if row is None and create:
        row = ConciergeSettings(storefront_id=store.id, vendor_id=store.vendor_id,
                                enabled=False, auto_accept_within_floor=False)
        db.session.add(row)
        db.session.flush()
    return row


def is_enabled(store: Storefront) -> bool:
    row = settings_for(store)
    return bool(row and row.enabled)


def contact_for(store: Storefront) -> dict:
    """The store's public contact (what the store page already shows)."""
    phone = (store.phone or "").strip() or None
    digits = re.sub(r"\D", "", phone or "")
    return {
        "store_name": store.store_name,
        "store_slug": store.store_slug,
        "phone": phone,
        "whatsapp_link": f"https://wa.me/{digits}" if digits else None,
        "vendor_id": store.vendor_id,   # for the in-app "Message seller" chat
    }


def public_config(store: Storefront) -> dict:
    enabled = is_enabled(store)
    return {
        "enabled": enabled,
        "store_name": store.store_name,
        "contact": contact_for(store) if enabled else None,
    }


# ── Retrieval: only this store (code) ──────────────────────────────────────
_STOP = {"the", "and", "for", "you", "your", "have", "has", "how", "much", "what", "this", "that",
         "with", "can", "get", "abeg", "pls", "please", "want", "need", "any", "dey", "una", "una's",
         "is", "it", "do", "does", "are", "there", "price", "cost", "last", "sell", "buy", "one",
         "naira", "available", "stock", "still", "left", "many"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) >= 3 and t not in _STOP}


def _store_products(store: Storefront):
    return (Product.query
            .filter(Product.storefront_id == store.id,
                    Product.is_active.is_(True),
                    db.or_(Product.is_deleted.is_(False), Product.is_deleted.is_(None)))
            .order_by(Product.created_at.desc(), Product.id.desc())
            .limit(500).all())


def retrieve(store: Storefront, query: str) -> tuple[list, bool]:
    """(products, matched). Products are ALWAYS filtered by this store's id.
    ``matched`` is False when nothing matched the words and the newest items
    are returned instead (so "what do you sell?" still has facts)."""
    wanted = _tokens(query)
    products = _store_products(store)
    scored = []
    for p in products:
        name = _tokens(p.name)
        attrs = _tokens(" ".join(str(v) for v in (p.attributes or {}).values()) if isinstance(p.attributes, dict) else "")
        cat = _tokens(p.category.name if p.category else "")
        desc = _tokens((p.description or "")[:600])
        score = 3 * len(wanted & name) + 2 * len(wanted & (attrs | cat)) + len(wanted & desc)
        if score:
            scored.append((score, p))
    if scored:
        scored.sort(key=lambda sp: (-sp[0], -sp[1].id))
        return [p for _, p in scored[:MAX_PRODUCTS]], True
    return products[:MAX_PRODUCTS], False


def _in_stock(p) -> bool:
    if (p.product_type or "physical") != "physical":
        return True
    return int(p.stock_quantity or 0) > 0


def _price_str(value) -> str:
    d = Decimal(str(value or 0))
    return f"{d:.2f}".rstrip("0").rstrip(".") if d == d.to_integral_value() else f"{d:.2f}"


def product_fact(p) -> dict:
    """What the AI may say about a product. Never the floor price."""
    fact = {
        "id": p.id,
        "name": p.name,
        "price": _price_str(p.price),
        "currency": p.currency or "NGN",
        "in_stock": _in_stock(p),
        "product_type": p.product_type or "physical",
        "condition": p.condition,
        "negotiable": bool(p.is_negotiable),
        "category": p.category.name if p.category else None,
        "description": (p.description or "")[:300],
    }
    if (p.product_type or "physical") == "physical":
        fact["stock"] = int(p.stock_quantity or 0)
    if isinstance(p.attributes, dict) and p.attributes:
        fact["details"] = {str(k)[:40]: str(v)[:80] for k, v in list(p.attributes.items())[:10]}
    return fact


def product_card(p) -> dict:
    images = p.images or []
    return {
        "id": p.id,
        "name": p.name,
        "price": _price_str(p.price),
        "currency": p.currency or "NGN",
        "image": images[0] if images else None,
        "in_stock": _in_stock(p),
    }


def store_facts(store: Storefront) -> dict:
    delivery = []
    for item in (store.logistics_settings or []):
        if isinstance(item, str):
            delivery.append(item[:60])
        elif isinstance(item, dict):
            label = item.get("name") or item.get("label") or item.get("id")
            if label:
                delivery.append(str(label)[:60])
    hours = store.working_hours if isinstance(store.working_hours, (dict, list)) else None
    return {
        "name": store.store_name,
        "city": store.city,
        "state": store.state,
        "description": (store.store_description or "")[:400],
        "delivery_options": delivery[:8],
        "opening_hours": hours,
    }


# ── The critic (code) ──────────────────────────────────────────────────────
_MONEY_RE = re.compile(
    r"(?:₦|\bngn\s?|\bn(?=\d))\s?(?P<a>\d[\d,]*(?:\.\d+)?)\s*(?P<as>k\b|m\b)?"
    r"|(?P<b>\d[\d,]*(?:\.\d+)?)\s?(?P<bs>k\b|naira\b|ngn\b)"
    r"|(?P<c>\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b)",
    re.IGNORECASE,
)
_STOCK_RE = re.compile(r"\b(\d+)\s*(?:pieces?\s+|units?\s+|items?\s+)?(?:left|in stock|available|remaining)\b",
                       re.IGNORECASE)
_IN_STOCK_RE = re.compile(r"(?<!out of )(?<!not )\bin stock\b|\bavailable\b", re.IGNORECASE)


def _money_values(text: str) -> list[Decimal]:
    out = []
    for m in _MONEY_RE.finditer(text or ""):
        raw = m.group("a") or m.group("b") or m.group("c")
        suf = (m.group("as") or m.group("bs") or "").lower()
        try:
            v = Decimal(raw.replace(",", ""))
        except (InvalidOperation, AttributeError):
            continue
        if suf == "k":
            v *= 1000
        elif suf == "m":
            v *= 1_000_000
        out.append(v.quantize(Decimal("0.01")))
    return out


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").replace("₦", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def critic(data: dict, facts: list[dict], offer: dict | None) -> list[str]:
    """Every product, price and stock claim must match the retrieved facts.
    Returns the problems found (empty = the answer may be shown)."""
    problems: list[str] = []
    by_id = {f["id"]: f for f in facts}
    answer = (data.get("answer") or "").strip()
    if not answer:
        return ["empty answer"]
    if len(answer) > 700:
        problems.append("answer too long")

    for pid in data.get("product_ids") or []:
        if pid not in by_id:
            problems.append(f"product {pid} is not in this store's facts")

    allowed_money = {_dec(f["price"]) for f in facts}
    if offer:
        for key in ("unit_price", "total", "list_price"):
            if offer.get(key) is not None:
                allowed_money.add(_dec(offer[key]))
    allowed_money.discard(None)

    for claim in data.get("claims") or []:
        f = by_id.get(claim.get("product_id"))
        if f is None:
            problems.append(f"claim about product {claim.get('product_id')} not in facts")
            continue
        value = str(claim.get("value") or "").strip().lower()
        if claim.get("field") == "price":
            ok = _dec(value) is not None and (
                _dec(value) == _dec(f["price"])
                or (offer and offer.get("product_id") == f["id"] and _dec(value) in allowed_money)
            )
            if not ok:
                problems.append(f"price claim {value!r} does not match product {f['id']}")
        elif claim.get("field") == "stock":
            if value in ("in_stock", "in stock"):
                ok = f["in_stock"]
            elif value in ("out_of_stock", "out of stock"):
                ok = not f["in_stock"]
            elif value.isdigit():
                ok = "stock" in f and int(value) == f["stock"]
            else:
                ok = False
            if not ok:
                problems.append(f"stock claim {value!r} does not match product {f['id']}")

    for amount in _money_values(answer):
        if amount not in allowed_money:
            problems.append(f"amount {amount} is not a catalogue price")

    stocks = {f["stock"] for f in facts if "stock" in f}
    for m in _STOCK_RE.finditer(answer):
        if int(m.group(1)) not in stocks:
            problems.append(f"stock count {m.group(1)} is not in the facts")

    cited = [by_id[p] for p in (data.get("product_ids") or []) if p in by_id]
    if cited and _IN_STOCK_RE.search(answer) and not any(f["in_stock"] for f in cited) \
            and "out of stock" not in answer.lower():
        problems.append("says in stock for an out-of-stock product")

    low = answer.lower()
    for phrase in BANNED_PHRASES:
        if phrase in low:
            problems.append(f"banned phrase {phrase!r}")
    return problems


# ── Templates (no AI) ──────────────────────────────────────────────────────
def _naira(currency: str, amount) -> str:
    d = Decimal(str(amount))
    num = f"{d:,.0f}" if d == d.to_integral_value() else f"{d:,.2f}"
    return f"₦{num}" if (currency or "NGN") == "NGN" else f"{currency} {num}"


def template_reply(intent: str, facts: list[dict], matched: bool, offer: dict | None) -> tuple[str, bool]:
    """(text, handoff) built only from facts."""
    if offer:
        status = offer["status"]
        name = offer.get("product_name") or "this item"
        if status == "pay_link_ready":
            return (f"Done. Your payment link for {name} is ready below: "
                    f"{_naira(offer['currency'], offer['unit_price'])} each, total "
                    f"{_naira(offer['currency'], offer['total'])}."), False
        if status == "sent_to_seller":
            return (f"I've sent your request for {name} to the seller to approve. "
                    "A payment link will show here once they approve it."), False
        if status == "below_minimum":
            return f"Sorry, the store can't go that low on {name}. You can offer a bit more.", False
        if status == "not_negotiable":
            return (f"The price of {name} is fixed at "
                    f"{_naira(offer['currency'], offer['list_price'])}."), False
        if status == "out_of_stock":
            return f"Sorry, {name} is out of stock for that quantity.", True
        return FALLBACK_TEXT, True
    if intent in ("contact_seller",):
        return "You can reach the seller directly below.", True
    if intent == "greeting":
        return "Hello! Ask me about anything in this store.", False
    if matched and facts and intent in ("product_info", "price", "availability", "negotiate", "buy", "other"):
        f = facts[0]
        stock = "in stock" if f["in_stock"] else "out of stock"
        return f"{f['name']}: {_naira(f['currency'], f['price'])}, {stock}.", False
    return FALLBACK_TEXT, True


# ── Offers and Pay Links (code only) ───────────────────────────────────────
def _offer_facts(offer: ConciergeOffer | None, decision, product) -> dict | None:
    if decision is None:
        return None
    if offer is not None:
        status = "pay_link_ready" if offer.status == "link_created" else "sent_to_seller"
    else:
        status = decision.status if decision.status != rules.ACCEPT else "sent_to_seller"
    return {
        "status": status,
        "product_id": product.id if product else None,
        "product_name": product.name if product else None,
        "currency": (product.currency or "NGN") if product else "NGN",
        "quantity": decision.quantity,
        "list_price": _price_str(decision.list_price) if decision.list_price is not None else None,
        "unit_price": _price_str(decision.unit_price) if (offer is not None and decision.unit_price is not None) else None,
        "total": _price_str(decision.total) if (offer is not None and decision.total is not None) else None,
    }


def create_pay_link(offer: ConciergeOffer):
    """Create the Pay Link for an approved offer, by code, at the offer's
    agreed price. Same rules as POST /vendor/payment-links: the seller must
    have a payout account. Returns the PaymentLink, or None without one."""
    from app.models.payment_link import PaymentLink
    from app.models.user import User
    from app.services.onboarding_publish import _pay_link_slug
    from app.services.seller_home import has_payout_account

    vendor = db.session.get(User, offer.vendor_id)
    if vendor is None or not has_payout_account(vendor):
        return None
    product = offer.product
    title = (product.name if offer.quantity == 1 else f"{offer.quantity} x {product.name}")[:100]
    ptype = product.product_type or "physical"
    if ptype == "physical":
        link_type, file_url = "physical", None
    elif ptype == "digital" and product.file_url:
        link_type, file_url = "digital", product.file_url
    else:
        link_type, file_url = "service", None
    link = PaymentLink(
        vendor_id=offer.vendor_id,
        link_type="PAY_LINK",
        title=title,
        description="Price agreed in the store chat.",
        amount=Decimal(str(offer.agreed_price)) * int(offer.quantity),
        currency=offer.currency or "NGN",
        buyer_email=None,
        status="ACTIVE",
        slug=_pay_link_slug(title),
        product_type=link_type,
        file_url=file_url,
    )
    db.session.add(link)
    db.session.flush()
    offer.payment_link_id = link.id
    offer.status = "link_created"
    offer.decided_at = datetime.now(timezone.utc)
    return link


def _draft_offer(store, settings, conv, product, decision, offered) -> ConciergeOffer:
    # One open draft per product per conversation: a newer request replaces it.
    for old in ConciergeOffer.query.filter_by(conversation_id=conv.id, product_id=product.id,
                                              status="pending_seller").all():
        old.status = "expired"
        old.decided_at = datetime.now(timezone.utc)
    offer = ConciergeOffer(
        storefront_id=store.id, vendor_id=store.vendor_id, conversation_id=conv.id,
        product_id=product.id, quantity=decision.quantity, currency=product.currency or "NGN",
        list_price=decision.list_price,
        offered_price=offered if offered is not None else decision.list_price,
        agreed_price=decision.unit_price, rule_decision=decision.status, status="pending_seller",
    )
    db.session.add(offer)
    db.session.flush()
    if decision.status == rules.ACCEPT and settings is not None and settings.auto_accept_within_floor:
        if create_pay_link(offer) is not None:
            offer.auto_accepted = True
    return offer


# ── Conversation ───────────────────────────────────────────────────────────
def _conversation(store, session_id: str, ip: str) -> ConciergeConversation:
    sh = _hash(session_id, "concierge-session")
    conv = ConciergeConversation.query.filter_by(storefront_id=store.id, session_hash=sh).first()
    if conv is None:
        conv = ConciergeConversation(storefront_id=store.id, session_hash=sh,
                                     ip_hash=_hash(ip or "?", "concierge-ip"))
        db.session.add(conv)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            conv = ConciergeConversation.query.filter_by(storefront_id=store.id, session_hash=sh).first()
    return conv


def conversation_for(store, session_id: str) -> ConciergeConversation | None:
    if not SESSION_RE.match(session_id or ""):
        return None
    return ConciergeConversation.query.filter_by(
        storefront_id=store.id, session_hash=_hash(session_id, "concierge-session")).first()


def history(store, session_id: str) -> dict:
    conv = conversation_for(store, session_id)
    if conv is None:
        return {"messages": [], "offers": []}
    msgs = (ConciergeMessage.query.filter_by(conversation_id=conv.id)
            .order_by(ConciergeMessage.id.asc()).limit(100).all())
    offers = (ConciergeOffer.query.filter_by(conversation_id=conv.id)
              .order_by(ConciergeOffer.id.desc()).limit(20).all())
    return {"messages": [m.to_dict() for m in msgs], "offers": [o.to_dict() for o in offers]}


def ai_questions_this_month(store_id: int) -> int:
    return ConciergeMessage.query.filter(
        ConciergeMessage.storefront_id == store_id,
        ConciergeMessage.role == "buyer",
        ConciergeMessage.ai_counted.is_(True),
        ConciergeMessage.created_at >= _month_start(),
    ).count()


def _recent_turns(conv_id: int, exclude_id: int) -> list[dict]:
    rows = (ConciergeMessage.query.filter(ConciergeMessage.conversation_id == conv_id,
                                          ConciergeMessage.id != exclude_id)
            .order_by(ConciergeMessage.id.desc()).limit(MAX_TURNS_CONTEXT).all())
    return [{"role": r.role, "text": r.text[:300]} for r in reversed(rows)]


def _reply(conv, store, text, *, source, intent=None, products=(), offer=None,
           handoff=False, fallback=False, reason=None) -> dict:
    msg = ConciergeMessage(conversation_id=conv.id, storefront_id=store.id, role="assistant",
                           text=text, intent=intent, source=source,
                           product_ids=[p.id for p in products] or None,
                           offer_id=offer.id if offer is not None else None)
    db.session.add(msg)
    db.session.commit()
    return {
        "reply": msg.to_dict(),
        "products": [product_card(p) for p in products],
        "offer": offer.to_dict() if offer is not None else None,
        "handoff": bool(handoff or fallback),
        "contact": contact_for(store) if (handoff or fallback) else None,
        "fallback": bool(fallback),
        "reason": reason,
    }


def handle_message(store: Storefront, session_id: str, text: str, ip: str) -> dict:
    """One buyer message in, one assistant reply out. Raises ConciergeError
    for a closed concierge (404), bad input (400) or the IP limit (429)."""
    from app.services.bot_intent import hit_rate_limit

    settings = settings_for(store)
    if settings is None or not settings.enabled:
        raise ConciergeError("CONCIERGE_OFF", "This store's assistant is not on.", 404)
    if not SESSION_RE.match(session_id or ""):
        raise ConciergeError("BAD_SESSION", "A chat session id is required.")
    text = " ".join((text or "").split())[:MAX_MESSAGE_LEN]
    if not text:
        raise ConciergeError("EMPTY_MESSAGE", "Type a message first.")

    exceeded = hit_rate_limit("concierge", "web", _hash(ip or "?", "concierge-ip")[:40], ip_limits())
    if exceeded:
        raise ConciergeError("RATE_LIMITED", "Too many messages. Please wait a moment, "
                             "or ask the seller directly.", 429)

    conv = _conversation(store, session_id, ip)
    buyer_msg = ConciergeMessage(conversation_id=conv.id, storefront_id=store.id, role="buyer", text=text)
    db.session.add(buyer_msg)
    conv.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    if ai_questions_this_month(store.id) >= monthly_cap():
        return _reply(conv, store, CAP_TEXT, source="fallback", fallback=True, reason="store_monthly_cap")

    # 1. Classify (AI). Off or failing -> "ask the seller directly".
    client = get_client()
    try:
        result = ai.run(ACTION_CLASSIFY, seller_id=store.vendor_id, inputs={"message": text},
                        client=client, timeout=AI_TIMEOUT_SECONDS)
        cls = result.data or {}
    except ai.AIError as exc:
        if not isinstance(exc, (ai.AIDisabled, ai.AICapExceeded)):
            buyer_msg.ai_counted = True
            db.session.commit()
        reason = "ai_off" if isinstance(exc, ai.AIDisabled) else ("ai_cap" if isinstance(exc, ai.AICapExceeded) else "ai_error")
        return _reply(conv, store, FALLBACK_TEXT, source="fallback", fallback=True, reason=reason)
    buyer_msg.ai_counted = True
    intent = cls.get("intent") if cls.get("intent") in INTENTS else "other"
    buyer_msg.intent = intent
    db.session.commit()

    if intent == "contact_seller":
        text_out, _ = template_reply(intent, [], False, None)
        return _reply(conv, store, text_out, source="template", intent=intent, handoff=True)

    # 2. Retrieve (code, this store only).
    hint = (cls.get("product_hint") or "").strip()
    products, matched = retrieve(store, f"{hint} {text}" if hint else text)
    if hint and not matched:
        products, matched = retrieve(store, text)
    facts = [product_fact(p) for p in products]

    # 3. Negotiation / buy (code rule engine). AI only words the outcome.
    offer = None
    offer_facts = None
    if intent in OFFER_INTENTS and matched and products:
        product = products[0]
        # The amount comes from the buyer's own words, parsed by code, never from the AI.
        offered = rules.parse_offer_amount(text)
        qty = cls.get("quantity") if isinstance(cls.get("quantity"), int) else 1
        if intent == "negotiate" and offered is None:
            decision = None     # "last price?" with no number: answer from facts only
        else:
            decision = rules.evaluate(product, offered, qty)
        if decision is not None:
            if decision.can_draft:
                offer = _draft_offer(store, settings, conv, product, decision, offered)
                db.session.commit()
            offer_facts = _offer_facts(offer, decision, product)

    # 4. Answer (AI) + critic, 1 retry, then template.
    inputs = {
        "store": store_facts(store),
        "products": facts,
        "products_match_question": matched,
        "question": text,
        "intent": intent,
        "recent_turns": _recent_turns(conv.id, buyer_msg.id),
        "offer": offer_facts,
    }
    answer, handoff, source = None, False, "template"
    for _attempt in range(ANSWER_ATTEMPTS):
        try:
            res = ai.run(ACTION_ANSWER, seller_id=store.vendor_id, inputs=inputs,
                         client=client, timeout=AI_TIMEOUT_SECONDS)
        except ai.AIError:
            break
        data = res.data or {}
        problems = critic(data, facts, offer_facts)
        if not problems:
            answer, handoff, source = data["answer"].strip(), bool(data.get("handoff")), "ai"
            cited = [p for p in products if p.id in set(data.get("product_ids") or [])]
            break
        log.info("[CONCIERGE] critic rejected answer for store %s: %s", store.id, problems[:3])
        inputs = {**inputs, "critic_feedback": problems[:5]}
    if answer is None:
        answer, handoff = template_reply(intent, facts, matched, offer_facts)
        cited = products[:1] if (matched and products and not offer_facts) else []
    if offer is not None:
        cited = [offer.product]
    return _reply(conv, store, answer, source=source, intent=intent, products=cited,
                  offer=offer, handoff=handoff, fallback=(answer == FALLBACK_TEXT))


# ── Seller side ────────────────────────────────────────────────────────────
def approve_offer(offer: ConciergeOffer) -> dict:
    """The seller approved: code re-checks the product and creates the Pay Link
    at the agreed price. Raises ConciergeError when it cannot."""
    if offer.status != "pending_seller":
        raise ConciergeError("OFFER_CLOSED", "This offer is no longer waiting for you.", 409)
    product = offer.product
    decision = rules.evaluate(product, None, offer.quantity)   # active, in stock, priced
    if decision.status not in (rules.ACCEPT,):
        raise ConciergeError("PRODUCT_UNAVAILABLE", "The product is no longer available in that quantity.", 409)
    link = create_pay_link(offer)
    if link is None:
        raise ConciergeError("BANK_ACCOUNT_REQUIRED",
                             "Add your bank account before creating a Pay Link.", 400)
    db.session.commit()
    return offer.to_dict(for_seller=True)


def decline_offer(offer: ConciergeOffer) -> dict:
    if offer.status != "pending_seller":
        raise ConciergeError("OFFER_CLOSED", "This offer is no longer waiting for you.", 409)
    offer.status = "declined"
    offer.decided_at = datetime.now(timezone.utc)
    db.session.commit()
    return offer.to_dict(for_seller=True)


def seller_summary(store: Storefront) -> dict:
    row = settings_for(store)
    return {
        "settings": row.to_dict() if row else {"enabled": False, "auto_accept_within_floor": False,
                                                "updated_at": None},
        "usage": {"ai_questions_this_month": ai_questions_this_month(store.id),
                  "monthly_cap": monthly_cap()},
        "pending_offers": ConciergeOffer.query.filter_by(storefront_id=store.id,
                                                         status="pending_seller").count(),
    }
