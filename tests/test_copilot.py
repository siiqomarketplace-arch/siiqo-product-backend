"""
Phase 4 (8b): seller copilot ("Ask Siiqo").

The model is a scripted fake (tests/copilot_fakes.py); no network, no key.
Covers: tools return only the signed-in seller's data (including a
cross-seller attempt through the loop), the 6-tool-call cap, that no write
tool exists and nothing is written, suggested actions are buttons that need
a tap, AI off, the monthly cap, the model override and PII stripping.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db
from app.models.ai import AIUsage
from app.models.assistant import CopilotMessage
from app.models.order import Order
from app.services import copilot as svc
from app.services import copilot_tools as tools
from app.services.ai import gateway
from copilot_fakes import ScriptedModel, answer

_AI_ENV = ["ANTHROPIC_API_KEY", "AI_ENABLED", "AI_ENABLED_ACTIONS", "AI_SELLER_DAILY_ACTION_CAP",
           "AI_DAILY_COST_CAP_USD", "COPILOT_MONTHLY_CAP", "AI_COPILOT_MODEL"]
THREAD = "thread-0001"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in _AI_ENV:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def model(monkeypatch):
    def _install(script):
        monkeypatch.setenv("AI_ENABLED", "1")
        fake = ScriptedModel(script)
        monkeypatch.setattr(svc, "get_client", lambda: fake)
        return fake
    return _install


@pytest.fixture
def two_sellers(make_vendor, make_user, make_order, make_product):
    a, a_store = make_vendor()
    b, b_store = make_vendor()
    buyer = make_user()
    a_prod = make_product(a_store, price=15000, stock=3)
    a_prod.name = "Ankara fabric"
    b_prod = make_product(b_store, price=99000, stock=1)
    b_prod.name = "Bola phone"
    a_unpaid = make_order(a, buyer=buyer, total=15000)
    a_paid = make_order(a, buyer=buyer, total=30000)
    a_paid.status = "PAID"
    b_unpaid = make_order(b, buyer=buyer, total=99000)
    b_paid = make_order(b, total=50000)
    b_paid.status = "PAID"
    db.session.commit()
    return {"a": a, "b": b, "a_unpaid": a_unpaid, "a_paid": a_paid, "b_unpaid": b_unpaid,
            "b_paid": b_paid, "a_prod": a_prod, "b_prod": b_prod, "buyer": buyer}


def ask(client, auth_headers, user, question, thread=THREAD):
    return client.post("/api/copilot/ask", json={"question": question, "thread": thread},
                       headers=auth_headers(user))


# ── Tools: scope ────────────────────────────────────────────────────────────
def test_every_tool_is_scoped_to_the_seller(two_sellers):
    a, b = two_sellers["a"], two_sellers["b"]
    orders = tools.run_tool(a, "list_orders", {})
    assert {o["order_id"] for o in orders["orders"]} == {two_sellers["a_unpaid"].id, two_sellers["a_paid"].id}
    assert sum(orders["counts_by_status"].values()) == 2
    assert tools.run_tool(a, "get_order", {"order_id": two_sellers["b_paid"].id}) == \
        {"found": False, "order_id": two_sellers["b_paid"].id}
    products = tools.run_tool(a, "list_products", {})
    assert [p["name"] for p in products["products"]] == ["Ankara fabric"]
    for name in ("payout_status", "analytics_summary", "list_disputes", "list_pay_links", "trust_drivers"):
        blob = json.dumps(tools.run_tool(a, name, {}))
        assert "Bola" not in blob and "99000" not in blob
    assert tools.run_tool(a, "analytics_summary", {})["orders"] == 2


def test_tool_results_hold_no_buyer_contact(two_sellers):
    a = two_sellers["a"]
    order = two_sellers["a_paid"]
    order.buyer_phone, order.delivery_address, order.delivery_phone = "08031112222", "1 Secret St", "08033334444"
    db.session.commit()
    blob = json.dumps([tools.run_tool(a, "list_orders", {}), tools.run_tool(a, "get_order", {"order_id": order.id})])
    for secret in ("08031112222", "1 Secret St", "08033334444", two_sellers["buyer"].email, "Test Buyer"):
        assert secret not in blob


def test_no_write_tools_exist():
    names = {t["name"] for t in tools.TOOLS}
    assert names == {"list_orders", "get_order", "payout_status", "list_products", "analytics_summary",
                     "trust_drivers", "list_disputes", "list_pay_links"}
    assert 6 <= len(names) <= 10
    for name in names:
        assert not name.startswith(("create", "update", "delete", "mark", "send", "approve", "set", "refund",
                                     "release", "withdraw", "cancel", "publish"))


def test_bad_tool_arguments_are_errors(two_sellers):
    with pytest.raises(tools.ToolError):
        tools.run_tool(two_sellers["a"], "list_orders", {"status": "BOGUS"})
    with pytest.raises(tools.ToolError):
        tools.run_tool(two_sellers["a"], "list_orders", {"vendor_id": two_sellers["b"].id})
    with pytest.raises(tools.ToolError):
        tools.run_tool(two_sellers["a"], "refund_order", {})


# ── The loop ───────────────────────────────────────────────────────────────
def test_answers_with_tools_and_records_usage(client, auth_headers, two_sellers, model):
    fake = model([("tools", [("list_orders", {"status": "UNPAID"})]),
                  ("final", lambda res: answer(f"You have {res[0][1]['total_matching']} unpaid order."))])
    body = ask(client, auth_headers, two_sellers["a"], "Which orders are unpaid?").get_json()
    assert body["outcome"] == "answered" and body["answer"] == "You have 1 unpaid order."
    assert body["tools_used"] == ["list_orders"] and fake.calls == 2
    assert fake.requests[0]["tool_choice"] == {"type": "auto"}
    assert {t["name"] for t in fake.requests[0]["tools"]} == {t["name"] for t in tools.TOOLS}
    rows = AIUsage.query.filter_by(action="seller_copilot").all()
    assert len(rows) == 2 and all(r.seller_id == two_sellers["a"].id for r in rows)


def test_cross_seller_attempt_through_the_loop(client, auth_headers, two_sellers, model):
    other = two_sellers["b_paid"].id
    fake = model([
        ("tools", [("get_order", {"order_id": other}), ("list_orders", {"status": "TO_SHIP"})]),
        ("final", answer("I can't find that order.", actions=[
            {"type": "mark_shipped", "order_id": other},
            {"type": "remind_buyer", "order_id": two_sellers["b_unpaid"].id},
        ])),
    ])
    body = ask(client, auth_headers, two_sellers["a"], f"Ship order {other}").get_json()
    results = fake.results(fake.requests[-1]["messages"])
    assert results[0] == ("get_order", {"found": False, "order_id": other})
    assert all(o["order_id"] != other for o in results[1][1]["orders"])
    assert body["actions"] == []            # other seller's ids never become buttons
    assert Order.query.get(other).status == "PAID"


def test_step_cap_six_tool_calls(client, auth_headers, two_sellers, model):
    script = [("tools", [("list_orders", {})])] * 8
    fake = model(script)
    body = ask(client, auth_headers, two_sellers["a"], "Tell me everything").get_json()
    assert body["outcome"] == "step_cap" and body["fallback"] is True
    assert len(body["tools_used"]) == svc.MAX_TOOL_CALLS == 6
    assert fake.calls == 8                  # 6 tool turns, the "limit reached" turn, then stop
    last_results = fake.results(fake.requests[-1]["messages"])
    assert last_results[-1][1]["is_error"] is True
    assert body["actions"][0]["href"] == "/vendor/orders"


def test_step_cap_parallel_calls(client, auth_headers, two_sellers, model):
    fake = model([
        ("tools", [("list_orders", {})] * 7),
        ("final", answer("Done.")),
    ])
    body = ask(client, auth_headers, two_sellers["a"], "Orders?").get_json()
    assert body["outcome"] == "answered" and len(body["tools_used"]) == 6
    errors = [r for _, r in fake.results(fake.requests[-1]["messages"]) if isinstance(r, dict) and r.get("is_error")]
    assert len(errors) == 1


def test_nothing_is_written_by_the_loop(client, auth_headers, two_sellers, model):
    before = {o.id: o.status for o in Order.query.all()}
    model([("tools", [(t["name"], {}) for t in tools.TOOLS if t["name"] != "get_order"][:6]),
           ("final", answer("Summary."))])
    body = ask(client, auth_headers, two_sellers["a"], "Give me a summary").get_json()
    assert body["outcome"] == "answered"
    assert {o.id: o.status for o in Order.query.all()} == before
    # Only the question and answer text are stored.
    assert CopilotMessage.query.count() == 2


# ── Actions need a tap ─────────────────────────────────────────────────────
def test_suggested_actions_are_buttons_not_writes(client, auth_headers, two_sellers, model):
    a = two_sellers["a"]
    paid, unpaid = two_sellers["a_paid"], two_sellers["a_unpaid"]
    model([("tools", [("list_orders", {"status": "TO_SHIP"})]),
           ("final", answer("Order to ship.", actions=[
               {"type": "mark_shipped", "order_id": paid.id},
               {"type": "mark_shipped", "order_id": unpaid.id},       # not paid: dropped
               {"type": "remind_buyer", "order_id": unpaid.id},
               {"type": "create_pay_link_draft", "product_id": two_sellers["a_prod"].id},
           ]))])
    body = ask(client, auth_headers, a, "What should I ship?").get_json()
    kinds = [x["type"] for x in body["actions"]]
    assert kinds == ["mark_shipped", "remind_buyer", "create_pay_link_draft"]
    ship = body["actions"][0]
    assert ship["method"] == "PATCH" and ship["endpoint"] == f"/vendor/orders/{paid.id}/status"
    assert ship["body"] == {"status": "SHIPPED"} and ship["confirm"]
    assert "create=true" in body["actions"][2]["href"] and "amount=15000.00" in body["actions"][2]["href"]
    # Nothing happened yet.
    assert db.session.get(Order, paid.id).status == "PAID"
    from app.models.communication import Message
    assert Message.query.count() == 0

    # The seller taps: the existing endpoint runs it.
    r = client.patch(f"/api{ship['endpoint']}", json=ship["body"], headers=auth_headers(a))
    assert r.status_code == 200 and db.session.get(Order, paid.id).status == "SHIPPED"
    remind = body["actions"][1]
    r = client.post(f"/api{remind['endpoint']}", json=remind["body"], headers=auth_headers(a))
    assert r.status_code == 201 and Message.query.count() == 1


def test_remind_buyer_dropped_for_guest_order(two_sellers, make_order):
    guest = make_order(two_sellers["a"], total=5000)
    assert svc.build_action(two_sellers["a"], {"type": "remind_buyer", "order_id": guest.id}) is None
    assert svc.build_action(two_sellers["a"], {"type": "open_page", "page": "nowhere"}) is None
    assert svc.build_action(two_sellers["a"], {"type": "refund", "order_id": guest.id}) is None


# ── Off, caps, model, PII ──────────────────────────────────────────────────
def test_ai_off_falls_back_to_orders_page(client, auth_headers, two_sellers, monkeypatch):
    monkeypatch.setenv("AI_ENABLED", "0")
    body = ask(client, auth_headers, two_sellers["a"], "Which orders are unpaid?").get_json()
    assert body["outcome"] == "disabled" and body["answer"] == svc.FALLBACK_TEXT
    assert body["actions"] == [{"type": "open_page", "label": "Open Orders", "kind": "navigate",
                                "href": "/vendor/orders"}]
    assert AIUsage.query.count() == 0


def test_model_says_not_answered_falls_back(client, auth_headers, two_sellers, model):
    model([("final", answer("No idea.", answered=False))])
    body = ask(client, auth_headers, two_sellers["a"], "What will I sell next year?").get_json()
    assert body["outcome"] == "fallback" and body["answer"] == svc.FALLBACK_TEXT


def test_monthly_cap(client, auth_headers, two_sellers, model, monkeypatch):
    monkeypatch.setenv("COPILOT_MONTHLY_CAP", "2")
    fake = model([("final", answer("One.")), ("final", answer("Two."))])
    a = two_sellers["a"]
    assert ask(client, auth_headers, a, "q1").get_json()["outcome"] == "answered"
    assert ask(client, auth_headers, a, "q2").get_json()["outcome"] == "answered"
    body = ask(client, auth_headers, a, "q3").get_json()
    assert body["outcome"] == "cap" and fake.calls == 2
    status = client.get("/api/copilot/status", headers=auth_headers(a)).get_json()
    assert status == {"questions_this_month": 2, "monthly_cap": 2, "max_tool_calls": 6}
    # The other seller has their own cap.
    model([("final", answer("Hi."))])
    assert ask(client, auth_headers, two_sellers["b"], "q1").get_json()["outcome"] == "answered"


def test_copilot_does_not_use_daily_seller_cap(client, auth_headers, two_sellers, model, monkeypatch):
    monkeypatch.setenv("AI_SELLER_DAILY_ACTION_CAP", "1")
    model([("tools", [("list_orders", {})]), ("final", answer("Ok.")), ("final", answer("Ok again."))])
    a = two_sellers["a"]
    assert ask(client, auth_headers, a, "q1").get_json()["outcome"] == "answered"
    assert ask(client, auth_headers, a, "q2").get_json()["outcome"] == "answered"


def test_model_override(monkeypatch, client, auth_headers, two_sellers, model):
    spec = gateway.prompts.get("seller_copilot")
    assert gateway.model_for(spec) == "claude-haiku-5-5"
    monkeypatch.setenv("AI_COPILOT_MODEL", "gpt-4")
    assert gateway.model_for(spec) == "claude-haiku-5-5"
    monkeypatch.setenv("AI_COPILOT_MODEL", "claude-sonnet-5-5")
    assert gateway.model_for(spec) == "claude-sonnet-5-5"
    fake = model([("final", answer("Hi."))])
    assert ask(client, auth_headers, two_sellers["a"], "hello").get_json()["outcome"] == "answered"
    assert fake.requests[0]["model"] == "claude-sonnet-5-5"
    assert AIUsage.query.one().model == "claude-sonnet-5-5"


def test_question_pii_is_stripped_before_the_model(client, auth_headers, two_sellers, model):
    fake = model([("final", answer("Ok."))])
    ask(client, auth_headers, two_sellers["a"], "Did 08031234567 or ada@example.com pay?")
    sent = json.dumps(fake.requests[0]["messages"])
    assert "08031234567" not in sent and "ada@example.com" not in sent and "[phone]" in sent


def test_auth_and_validation(client, auth_headers, make_user, two_sellers, model):
    buyer = make_user()
    assert ask(client, auth_headers, buyer, "hi").status_code == 403
    model([])
    assert ask(client, auth_headers, two_sellers["a"], "   ").status_code == 400
    assert ask(client, auth_headers, two_sellers["a"], "hi", thread="x").status_code == 400


def test_history_is_per_seller(client, auth_headers, two_sellers, model):
    model([("final", answer("Hello A."))])
    ask(client, auth_headers, two_sellers["a"], "hi")
    mine = client.get(f"/api/copilot/history?thread={THREAD}", headers=auth_headers(two_sellers["a"])).get_json()
    theirs = client.get(f"/api/copilot/history?thread={THREAD}", headers=auth_headers(two_sellers["b"])).get_json()
    assert [m["text"] for m in mine["messages"]] == ["hi", "Hello A."] and theirs["messages"] == []
