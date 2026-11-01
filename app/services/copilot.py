"""
Seller copilot, "Ask Siiqo" (Phase 4, item 8b; AI_ORCHESTRATION_OPTIONS.md section 2, row 8b).

The only real agent in Siiqo: the model chooses among READ tools
(app/services/copilot_tools.py) to answer a seller's open question. It is a
manual tool-use loop where every model call goes through the AI gateway
(``ai.converse``: kill switch, PII stripping, caps, metering).

Limits and safety:
  * at most MAX_TOOL_CALLS (6) tool calls per question; past that the model
    is told to answer, and if it still asks for tools the question falls back;
  * READ tools only, scoped to the signed-in seller (ids come from the session);
  * any action the model suggests is validated by code against the seller's
    own data and returned as a button. Nothing runs until the seller taps it,
    and then the website calls the existing endpoint (mark shipped, chat, Pay
    Link form, offer approval). This module never writes business data; it
    only stores the question and answer text;
  * per-seller monthly cap (COPILOT_MONTHLY_CAP); AI off, a cap or any error:
    "I couldn't answer that - here's the Orders page".
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

from app.extensions import db
from app.models.assistant import CopilotMessage
from app.services import ai
from app.services import copilot_tools as tools

log = logging.getLogger(__name__)

ACTION = "seller_copilot"
MAX_TOOL_CALLS = 6
MAX_MODEL_CALLS = MAX_TOOL_CALLS + 2
MAX_QUESTION_LEN = 500
MAX_TOOL_RESULT_CHARS = 8000
MAX_ACTIONS = 3
PRIOR_TURNS = 4
THREAD_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")

FALLBACK_TEXT = "I couldn't answer that — here's the Orders page."
CAP_TEXT = "You've used this month's Ask Siiqo questions. Here's the Orders page."

PAGES = {
    "orders": "/vendor/orders",
    "payouts": "/vendor/escrow",
    "products": "/vendor/products",
    "disputes": "/vendor/orders",          # disputes are opened from the order
    "pay_links": "/vendor/quick-links",
    "trust": "/vendor/trust",
    "analytics": "/vendor/analytics",
}
PAGE_LABELS = {
    "orders": "Open Orders", "payouts": "Open Payouts", "products": "Open Products",
    "disputes": "Open Disputes", "pay_links": "Open Pay Links", "trust": "Open Trust",
    "analytics": "Open Analytics",
}


def get_client():
    """Anthropic-compatible client for the gateway; None builds the default.
    Tests and the offline scenario evals replace this with a scripted fake."""
    return None


def monthly_cap() -> int:
    try:
        return int(os.environ.get("COPILOT_MONTHLY_CAP") or 200)
    except ValueError:
        return 200


def _month_start(now=None):
    now = now or datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def questions_this_month(vendor_id: int) -> int:
    return CopilotMessage.query.filter(
        CopilotMessage.vendor_id == vendor_id,
        CopilotMessage.role == "seller",
        CopilotMessage.ai_counted.is_(True),
        CopilotMessage.created_at >= _month_start(),
    ).count()


# ── Actions: suggested by the model, validated and built by code ───────────
def _page_action(page: str) -> dict:
    return {"type": "open_page", "label": PAGE_LABELS[page], "kind": "navigate", "href": PAGES[page]}


def build_action(vendor, suggestion: dict) -> dict | None:
    """Turn one model suggestion into a button, or None when it does not fit
    the seller's own data. The button only describes the existing endpoint;
    the website calls it after the seller taps (and confirms)."""
    from app.models.assistant import ConciergeOffer
    from app.models.order import Order
    from app.models.product import Product

    kind = suggestion.get("type")
    if kind == "open_page":
        page = suggestion.get("page")
        return _page_action(page) if page in PAGES else None

    if kind in ("mark_shipped", "remind_buyer"):
        oid = suggestion.get("order_id")
        if not isinstance(oid, int) or isinstance(oid, bool):
            return None
        order = Order.query.filter(Order.id == oid, Order.vendor_id == vendor.id).first()
        if order is None:
            return None
        if kind == "mark_shipped":
            from app.services.disputes import payout_blocked
            if order.status not in tools.TO_SHIP_STATUSES or payout_blocked(order, order.escrow):
                return None
            return {
                "type": "mark_shipped", "label": f"Mark order #{order.id} shipped", "kind": "request",
                "method": "PATCH", "endpoint": f"/vendor/orders/{order.id}/status",
                "body": {"status": "SHIPPED"},
                "confirm": f"Mark order #{order.id} as shipped? The buyer will be told.",
                "order_id": order.id,
            }
        if order.status != "PENDING" or order.buyer_id is None:
            return None
        store = vendor.storefront.store_name if vendor.storefront else "the store"
        return {
            "type": "remind_buyer", "label": f"Remind buyer (order #{order.id})", "kind": "request",
            "method": "POST", "endpoint": "/chat/send",
            "body": {"receiver_id": order.buyer_id, "order_id": order.id,
                     "content": (f"Hello! A quick reminder about your order #{order.id} from {store}. "
                                 "It is still waiting for payment. Reply here if you need help.")},
            "confirm": f"Send a payment reminder message to the buyer of order #{order.id}?",
            "order_id": order.id,
        }

    if kind == "create_pay_link_draft":
        pid = suggestion.get("product_id")
        params = {"create": "true"}
        if isinstance(pid, int) and not isinstance(pid, bool):
            sf = vendor.storefront
            product = (Product.query.filter(Product.id == pid, Product.storefront_id == sf.id).first()
                       if sf else None)
            if product is None:
                return None
            params.update({"title": product.name[:100], "amount": f"{float(product.price):.2f}",
                           "product_type": product.product_type or "physical"})
        from urllib.parse import urlencode
        return {"type": "create_pay_link_draft", "label": "Create Pay Link draft", "kind": "navigate",
                "href": f"{PAGES['pay_links']}?{urlencode(params)}"}

    if kind == "approve_offer":
        oid = suggestion.get("offer_id")
        if not isinstance(oid, int) or isinstance(oid, bool):
            return None
        offer = ConciergeOffer.query.filter_by(id=oid, vendor_id=vendor.id, status="pending_seller").first()
        if offer is None:
            return None
        name = offer.product.name if offer.product else "item"
        return {
            "type": "approve_offer", "label": f"Approve offer for {name}"[:60], "kind": "request",
            "method": "POST", "endpoint": f"/concierge/seller/offers/{offer.id}/approve", "body": {},
            "confirm": (f"Create a Pay Link for {offer.quantity} x {name} at "
                        f"{offer.currency} {float(offer.agreed_price):,.2f} each?"),
            "offer_id": offer.id,
        }
    return None


def build_actions(vendor, suggestions) -> list[dict]:
    out, seen = [], set()
    for s in suggestions or []:
        if not isinstance(s, dict):
            continue
        action = build_action(vendor, s)
        if action is None:
            log.info("[COPILOT] dropped suggested action %s for seller %s", s.get("type"), vendor.id)
            continue
        key = (action["type"], action.get("order_id"), action.get("offer_id"), action.get("href"))
        if key in seen:
            continue
        seen.add(key)
        out.append(action)
        if len(out) >= MAX_ACTIONS:
            break
    return out


# ── The loop ───────────────────────────────────────────────────────────────
def _prior_turns(vendor_id: int, thread: str) -> list[dict]:
    rows = (CopilotMessage.query.filter_by(vendor_id=vendor_id, thread=thread)
            .order_by(CopilotMessage.id.desc()).limit(PRIOR_TURNS * 2).all())
    return [{"role": r.role, "text": r.text[:400]} for r in reversed(rows)]


def _tool_result_text(result) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str, sort_keys=True)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + '..."[truncated]"'
    return text


def _save(vendor, thread, question, answer, outcome, *, counted, tool_calls=None, actions=None) -> dict:
    db.session.rollback()   # tools never write; drop anything a read may have loaded as pending
    q = CopilotMessage(vendor_id=vendor.id, thread=thread, role="seller", text=question,
                       ai_counted=counted)
    a = CopilotMessage(vendor_id=vendor.id, thread=thread, role="assistant", text=answer,
                       outcome=outcome, tool_calls=tool_calls or None, actions=actions or None)
    db.session.add_all([q, a])
    db.session.commit()
    return {
        "answer": answer,
        "outcome": outcome,
        "fallback": outcome != "answered",
        "actions": actions or [],
        "tools_used": [c["name"] for c in (tool_calls or [])],
        "message_id": a.id,
    }


def _fallback(vendor, thread, question, outcome, *, counted, tool_calls=None) -> dict:
    text = CAP_TEXT if outcome == "cap" else FALLBACK_TEXT
    return _save(vendor, thread, question, text, outcome, counted=counted,
                 tool_calls=tool_calls, actions=[_page_action("orders")])


def ask(vendor, question: str, thread: str, *, now: datetime | None = None) -> dict:
    """Answer one seller question. ``vendor`` is the signed-in seller (a User
    with a storefront); it is the only scope any tool sees."""
    question = " ".join((question or "").split())[:MAX_QUESTION_LEN]
    if not question:
        raise ValueError("question is empty")
    if not THREAD_RE.match(thread or ""):
        raise ValueError("bad thread id")

    if questions_this_month(vendor.id) >= monthly_cap():
        return _fallback(vendor, thread, question, "cap", counted=False)

    now = now or datetime.now(timezone.utc)
    first = ai.gateway.render_user(ACTION, {
        "question": question,
        "today": now.date().isoformat(),
        "weekday": now.strftime("%A"),
        "store_name": vendor.storefront.store_name if vendor.storefront else None,
        "earlier_in_this_chat": _prior_turns(vendor.id, thread),
    })
    messages: list[dict] = [{"role": "user", "content": first}]
    client = get_client()
    calls: list[dict] = []
    counted = False
    cap_hit = False

    for _step in range(MAX_MODEL_CALLS):
        try:
            result = ai.converse(ACTION, vendor.id, messages, tools=tools.TOOLS, client=client)
        except ai.AIError as exc:
            if not isinstance(exc, (ai.AIDisabled, ai.AICapExceeded)):
                counted = True
            outcome = "disabled" if isinstance(exc, ai.AIDisabled) else (
                "cap" if isinstance(exc, ai.AICapExceeded) else "fallback")
            if outcome == "cap":
                return _save(vendor, thread, question, CAP_TEXT, "cap", counted=counted,
                             tool_calls=calls, actions=[_page_action("orders")])
            return _fallback(vendor, thread, question, outcome, counted=counted, tool_calls=calls)
        counted = True

        if result.stop_reason != "tool_use":
            data = result.data or {}
            answer = (data.get("answer") or "").strip()
            if not answer or data.get("answered") is False:
                return _fallback(vendor, thread, question, "fallback", counted=True, tool_calls=calls)
            actions = build_actions(vendor, data.get("actions"))
            return _save(vendor, thread, question, answer, "answered", counted=True,
                         tool_calls=calls, actions=actions)

        if cap_hit:
            # Told to stop and still asking for tools: give up honestly.
            return _fallback(vendor, thread, question, "step_cap", counted=True, tool_calls=calls)

        messages.append({"role": "assistant", "content": result.meta["content"]})
        results = []
        for use in result.meta["tool_uses"]:
            name, args = use.get("name"), use.get("input") or {}
            if len(calls) >= MAX_TOOL_CALLS:
                cap_hit = True
                results.append({"type": "tool_result", "tool_use_id": use.get("id"), "is_error": True,
                                "content": "Tool call limit reached. Answer now with what you have, "
                                           "or set answered to false."})
                continue
            calls.append({"name": name, "input": args})
            try:
                output = tools.run_tool(vendor, name, args)
                results.append({"type": "tool_result", "tool_use_id": use.get("id"),
                                "content": _tool_result_text(output)})
            except tools.ToolError as exc:
                results.append({"type": "tool_result", "tool_use_id": use.get("id"), "is_error": True,
                                "content": str(exc)})
            except Exception:  # noqa: BLE001 - a broken read must not take the request down
                log.exception("[COPILOT] tool %s failed for seller %s", name, vendor.id)
                db.session.rollback()
                results.append({"type": "tool_result", "tool_use_id": use.get("id"), "is_error": True,
                                "content": "This tool failed. Answer without it or set answered to false."})
            finally:
                db.session.rollback()   # READ only: nothing a tool touched is ever kept
        messages.append({"role": "user", "content": results})

    return _fallback(vendor, thread, question, "step_cap", counted=counted, tool_calls=calls)


def history(vendor, thread: str) -> list[dict]:
    rows = (CopilotMessage.query.filter_by(vendor_id=vendor.id, thread=thread)
            .order_by(CopilotMessage.id.asc()).limit(60).all())
    return [{"id": r.id, "role": r.role, "text": r.text, "outcome": r.outcome,
             "actions": r.actions or [], "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows]
