"""
Phase 4 (8a): store concierge.

The AI gateway gets a scripted fake Anthropic client; no network, no key.
Covers: opt-in, retrieval limited to the store, the code critic, negotiation
rules (floor, auto-accept on/off), Pay Link drafts vs code-created links,
caps, the per-IP limit, AI off -> "ask the seller directly", and that no
floor price or buyer contact data reaches the AI.
"""
import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models.ai import AIUsage
from app.models.assistant import ConciergeMessage, ConciergeOffer, ConciergeSettings
from app.models.payment_link import PaymentLink
from app.models.withdrawal import VendorBankAccount
from app.services import concierge as svc
from app.services import negotiation_rules as rules

_AI_ENV = ["ANTHROPIC_API_KEY", "AI_ENABLED", "AI_ENABLED_ACTIONS", "AI_SELLER_DAILY_ACTION_CAP",
           "AI_DAILY_COST_CAP_USD", "CONCIERGE_MONTHLY_CAP", "CONCIERGE_IP_PER_MINUTE",
           "CONCIERGE_IP_PER_DAY"]


def _response(obj):
    usage = SimpleNamespace(input_tokens=120, output_tokens=40, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, iterations=None)
    return SimpleNamespace(id="msg_test", model="claude-haiku-5-5", stop_reason="end_turn",
                           stop_details=None, usage=usage,
                           content=[SimpleNamespace(type="text", text=json.dumps(obj))])


class FakeAI:
    """classify / answer are callables(inputs) -> dict, or lists consumed in order."""

    def __init__(self, classify=None, answer=None):
        self.classify, self.answer = classify, answer
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        system = kwargs["system"][0]["text"]
        kind = "classify" if system.startswith("You sort one message") else "answer"
        raw = kwargs["messages"][0]["content"]
        inputs = json.loads(raw.split("<inputs>")[1].split("</inputs>")[0])
        self.calls.append((kind, inputs, raw))
        fn = self.classify if kind == "classify" else self.answer
        if isinstance(fn, list):
            fn = fn.pop(0)
        if fn is None:
            raise AssertionError(f"unexpected {kind} call")
        return _response(fn(inputs) if callable(fn) else fn)

    def count(self, kind):
        return sum(1 for k, _, _ in self.calls if k == kind)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in _AI_ENV:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def fake_ai(monkeypatch):
    def _install(**kw):
        monkeypatch.setenv("AI_ENABLED", "1")
        client = FakeAI(**kw)
        monkeypatch.setattr(svc, "get_client", lambda: client)
        return client
    return _install


@pytest.fixture
def shop(make_vendor):
    def _make(enabled=True, auto=False, bank=True, phone="08031234567"):
        vendor, store = make_vendor()
        store.is_published = True
        store.phone = phone
        store.city = "Lagos"
        db.session.add(ConciergeSettings(storefront_id=store.id, vendor_id=vendor.id,
                                         enabled=enabled, auto_accept_within_floor=auto))
        if bank:
            db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Test Bank", bank_code="000",
                                             account_number="0000000000", account_name="Test"))
        db.session.commit()
        return vendor, store
    return _make


@pytest.fixture
def item(make_product):
    def _make(store, name, price=20000, stock=5, negotiable=False, floor=None, **extra):
        p = make_product(store, price=price, stock=stock, **extra)
        p.name = name
        p.description = f"{name} from our shop"
        p.is_negotiable = negotiable
        p.floor_price = floor
        db.session.commit()
        return p
    return _make


SESSION = "sess-" + "a" * 20


def send(client, store, message, session=SESSION, ip="10.0.0.1"):
    return client.post(f"/api/concierge/{store.store_slug}/messages",
                       json={"session_id": session, "message": message},
                       headers={"X-Forwarded-For": ip})


def classify(intent, hint=None, qty=None):
    return {"intent": intent, "product_hint": hint, "quantity": qty}


# ── Rule engine (unit) ──────────────────────────────────────────────────────
def _p(price=20000, stock=5, negotiable=True, floor=15000, ptype="physical", active=True):
    return SimpleNamespace(price=Decimal(price), stock_quantity=stock, is_negotiable=negotiable,
                           floor_price=None if floor is None else Decimal(floor), product_type=ptype,
                           is_active=active, is_deleted=False, is_free=False)


def test_rules_below_floor_is_refused():
    d = rules.evaluate(_p(), 14000)
    assert d.status == rules.BELOW_MINIMUM and d.unit_price is None and not d.can_draft


def test_rules_within_floor_accepts_at_offer():
    d = rules.evaluate(_p(), 16000, 2)
    assert d.status == rules.ACCEPT and d.unit_price == Decimal("16000.00") and d.total == Decimal("32000.00")


def test_rules_at_floor_accepts():
    assert rules.evaluate(_p(), 15000).status == rules.ACCEPT


def test_rules_never_above_list_price():
    d = rules.evaluate(_p(), 25000)
    assert d.status == rules.ACCEPT and d.unit_price == Decimal("20000.00")


def test_rules_not_negotiable():
    assert rules.evaluate(_p(negotiable=False), 18000).status == rules.NOT_NEGOTIABLE


def test_rules_negotiable_without_floor_needs_seller():
    d = rules.evaluate(_p(floor=None), 10000)
    assert d.status == rules.NEEDS_SELLER and d.unit_price == Decimal("10000.00")


def test_rules_out_of_stock_and_invalid():
    assert rules.evaluate(_p(stock=1), 16000, 2).status == rules.OUT_OF_STOCK
    assert rules.evaluate(_p(), 16000, 0).status == rules.INVALID
    assert rules.evaluate(_p(active=False), 16000).status == rules.INVALID
    assert rules.evaluate(_p(), -5).status == rules.INVALID
    # services are not stock-limited
    assert rules.evaluate(_p(stock=0, ptype="service"), 16000).status == rules.ACCEPT


def test_rules_floor_above_list_is_capped_at_list():
    assert rules.evaluate(_p(price=20000, floor=30000), 20000).status == rules.ACCEPT


@pytest.mark.parametrize("text,amount", [
    ("can I get it for 15k", "15000"), ("₦15,000 last", "15000"), ("I go pay 15000 naira", "15000"),
    ("2 pieces for 30k", "30000"), ("make it N12500", "12500"), ("1.5m abeg", "1500000"),
    ("how much is the iphone 13", None),
])
def test_parse_offer_amount(text, amount):
    got = rules.parse_offer_amount(text)
    assert (got is None and amount is None) or got == Decimal(amount)


# ── Opt-in ─────────────────────────────────────────────────────────────────
def test_off_by_default_and_public_config(client, make_vendor, fake_ai):
    vendor, store = make_vendor()
    store.is_published = True
    db.session.commit()
    ai_client = fake_ai()
    r = client.get(f"/api/concierge/{store.store_slug}")
    assert r.status_code == 200 and r.get_json()["enabled"] is False and r.get_json()["contact"] is None
    r = send(client, store, "hello")
    assert r.status_code == 404 and r.get_json()["code"] == "CONCIERGE_OFF"
    assert ai_client.calls == []


def test_seller_turns_it_on_in_settings(client, make_vendor, auth_headers):
    vendor, store = make_vendor()
    store.is_published = True
    db.session.commit()
    r = client.get("/api/concierge/seller/settings", headers=auth_headers(vendor))
    assert r.get_json()["settings"] == {"enabled": False, "auto_accept_within_floor": False, "updated_at": None}
    r = client.patch("/api/concierge/seller/settings", json={"enabled": True}, headers=auth_headers(vendor))
    assert r.status_code == 200 and r.get_json()["settings"]["enabled"] is True
    assert r.get_json()["settings"]["auto_accept_within_floor"] is False
    assert client.get(f"/api/concierge/{store.store_slug}").get_json()["enabled"] is True
    r = client.patch("/api/concierge/seller/settings", json={"enabled": "yes"}, headers=auth_headers(vendor))
    assert r.status_code == 400


# ── Retrieval ──────────────────────────────────────────────────────────────
def test_retrieval_only_returns_this_store(client, shop, item, fake_ai):
    _, mine = shop()
    _, other = shop()
    mine_bag = item(mine, "Leather bag", price=20000)
    item(other, "Leather bag deluxe", price=99000)
    item(other, "Leather belt", price=5000)
    products, matched = svc.retrieve(mine, "leather bag")
    assert matched and [p.id for p in products] == [mine_bag.id]

    ai_client = fake_ai(classify=classify("price", "leather bag"),
                        answer=lambda inp: {"answer": "The Leather bag is ₦20,000.",
                                            "product_ids": [inp["products"][0]["id"]],
                                            "claims": [{"product_id": inp["products"][0]["id"],
                                                        "field": "price", "value": "20000"}],
                                            "handoff": False})
    r = send(client, mine, "how much is the leather bag")
    body = r.get_json()
    assert r.status_code == 200 and body["reply"]["source"] == "ai"
    _, inputs, _ = [c for c in ai_client.calls if c[0] == "answer"][0]
    assert {p["id"] for p in inputs["products"]} == {mine_bag.id}
    assert "99000" not in json.dumps(inputs)


def test_floor_price_and_contacts_never_reach_the_ai(client, shop, item, fake_ai):
    _, store = shop()
    item(store, "Ankara gown", price=30000, negotiable=True, floor=24321)
    ai_client = fake_ai(classify=classify("price", "ankara gown"),
                        answer={"answer": "It is ₦30,000.", "product_ids": [], "claims": [], "handoff": False})
    send(client, store, "price of ankara gown? call me 08031234567 or me@buyer.com")
    blob = json.dumps([c[2] for c in ai_client.calls])
    assert "24321" not in blob and "floor" not in json.dumps([c[1] for c in ai_client.calls]).lower()
    assert "08031234567" not in blob and "me@buyer.com" not in blob


# ── Critic ─────────────────────────────────────────────────────────────────
def test_critic_rejects_invented_price_then_template(client, shop, item, fake_ai):
    _, store = shop()
    bag = item(store, "Leather bag", price=20000, stock=3)
    bad = {"answer": "The Leather bag is ₦18,000 today.", "product_ids": [bag.id],
           "claims": [{"product_id": bag.id, "field": "price", "value": "18000"}], "handoff": False}
    ai_client = fake_ai(classify=classify("price", "leather bag"), answer=[bad, bad])
    body = send(client, store, "how much is the leather bag").get_json()
    assert ai_client.count("answer") == 2          # 1 retry
    assert body["reply"]["source"] == "template"
    assert "₦20,000" in body["reply"]["text"] and "18,000" not in body["reply"]["text"]


def test_critic_retry_succeeds(client, shop, item, fake_ai):
    _, store = shop()
    bag = item(store, "Leather bag", price=20000)
    bad = {"answer": "It costs ₦18,000.", "product_ids": [bag.id], "claims": [], "handoff": False}
    good = {"answer": "It costs ₦20,000.", "product_ids": [bag.id],
            "claims": [{"product_id": bag.id, "field": "price", "value": "20000"}], "handoff": False}
    ai_client = fake_ai(classify=classify("price", "leather bag"), answer=[bad, good])
    body = send(client, store, "how much is the leather bag").get_json()
    assert body["reply"]["source"] == "ai" and body["reply"]["text"] == "It costs ₦20,000."
    second = [c for c in ai_client.calls if c[0] == "answer"][1][1]
    assert second["critic_feedback"]


def test_critic_rejects_invented_stock():
    facts = [{"id": 1, "name": "Bag", "price": "20000", "currency": "NGN", "in_stock": True, "stock": 3}]
    assert svc.critic({"answer": "Only 7 left!", "product_ids": [1], "claims": [], "handoff": False}, facts, None)
    assert svc.critic({"answer": "It's in stock.", "product_ids": [1],
                       "claims": [{"product_id": 1, "field": "stock", "value": "9"}]}, facts, None)
    assert not svc.critic({"answer": "Only 3 left at ₦20,000.", "product_ids": [1],
                           "claims": [{"product_id": 1, "field": "stock", "value": "3"}]}, facts, None)


def test_critic_rejects_in_stock_for_sold_out_and_foreign_product():
    facts = [{"id": 1, "name": "Bag", "price": "20000", "currency": "NGN", "in_stock": False, "stock": 0}]
    assert svc.critic({"answer": "Yes, it is in stock.", "product_ids": [1], "claims": []}, facts, None)
    assert svc.critic({"answer": "Try our shoes.", "product_ids": [99], "claims": []}, facts, None)
    assert svc.critic({"answer": "It is in stock.", "product_ids": [1],
                       "claims": [{"product_id": 1, "field": "stock", "value": "in_stock"}]}, facts, None)


def test_critic_rejects_banned_phrases():
    facts = [{"id": 1, "name": "Bag", "price": "20000", "currency": "NGN", "in_stock": True, "stock": 3}]
    assert svc.critic({"answer": "This seller is verified.", "product_ids": [], "claims": []}, facts, None)


# ── Negotiation and Pay Links ──────────────────────────────────────────────
def _negotiate_answer(inp):
    offer = inp["offer"]
    text = {"sent_to_seller": "I've sent it to the seller.",
            "pay_link_ready": "Your link is ready below.",
            "below_minimum": "Sorry, we can't go that low.",
            "not_negotiable": "The price is fixed."}.get(offer["status"], "Sorry.")
    return {"answer": text, "product_ids": [offer["product_id"]], "claims": [], "handoff": False}


def test_below_floor_is_refused_without_offer(client, shop, item, fake_ai):
    _, store = shop(auto=True)
    item(store, "Ankara gown", price=30000, negotiable=True, floor=25000)
    ai_client = fake_ai(classify=classify("negotiate", "ankara gown"), answer=_negotiate_answer)
    body = send(client, store, "ankara gown for 20k?").get_json()
    assert body["offer"] is None and ConciergeOffer.query.count() == 0 and PaymentLink.query.count() == 0
    offer_in = [c for c in ai_client.calls if c[0] == "answer"][0][1]["offer"]
    assert offer_in["status"] == "below_minimum" and offer_in["unit_price"] is None
    assert "25000" not in json.dumps(offer_in)


def test_within_floor_is_a_draft_by_default(client, shop, item, fake_ai):
    _, store = shop(auto=False)
    gown = item(store, "Ankara gown", price=30000, negotiable=True, floor=25000)
    fake_ai(classify=classify("negotiate", "ankara gown"), answer=_negotiate_answer)
    body = send(client, store, "I fit pay 26k for the ankara gown").get_json()
    assert body["offer"]["status"] == "pending_seller" and body["offer"]["pay_link_slug"] is None
    offer = ConciergeOffer.query.one()
    assert offer.product_id == gown.id and offer.agreed_price == Decimal("26000.00")
    assert offer.auto_accepted is False and PaymentLink.query.count() == 0


def test_auto_accept_creates_pay_link_by_code_at_rule_price(client, shop, item, fake_ai):
    vendor, store = shop(auto=True)
    item(store, "Ankara gown", price=30000, negotiable=True, floor=25000)
    fake_ai(classify=classify("negotiate", "ankara gown", 2), answer=_negotiate_answer)
    body = send(client, store, "2 ankara gown for 26k each").get_json()
    link = PaymentLink.query.one()
    assert link.vendor_id == vendor.id and link.amount == Decimal("52000.00") and link.status == "ACTIVE"
    assert link.product_type == "physical" and link.buyer_email is None
    assert body["offer"]["status"] == "link_created" and body["offer"]["pay_link_slug"] == link.slug
    assert ConciergeOffer.query.one().auto_accepted is True


def test_auto_accept_never_applies_without_floor(client, shop, item, fake_ai):
    _, store = shop(auto=True)
    item(store, "Ankara gown", price=30000, negotiable=True, floor=None)
    fake_ai(classify=classify("negotiate", "ankara gown"), answer=_negotiate_answer)
    body = send(client, store, "ankara gown 10k").get_json()
    assert body["offer"]["status"] == "pending_seller" and PaymentLink.query.count() == 0


def test_auto_accept_without_bank_stays_a_draft(client, shop, item, fake_ai):
    _, store = shop(auto=True, bank=False)
    item(store, "Ankara gown", price=30000, negotiable=True, floor=25000)
    fake_ai(classify=classify("negotiate", "ankara gown"), answer=_negotiate_answer)
    body = send(client, store, "ankara gown 26k").get_json()
    assert body["offer"]["status"] == "pending_seller" and PaymentLink.query.count() == 0


def test_not_negotiable_refused(client, shop, item, fake_ai):
    _, store = shop(auto=True)
    item(store, "Ankara gown", price=30000, negotiable=False)
    fake_ai(classify=classify("negotiate", "ankara gown"), answer=_negotiate_answer)
    body = send(client, store, "ankara gown 26k").get_json()
    assert body["offer"] is None and PaymentLink.query.count() == 0


def test_buy_at_list_price_is_draft_then_seller_approves(client, shop, item, fake_ai, auth_headers):
    vendor, store = shop(auto=False)
    item(store, "Leather bag", price=20000)
    fake_ai(classify=classify("buy", "leather bag"), answer=_negotiate_answer)
    body = send(client, store, "I want to buy the leather bag, send link").get_json()
    offer_id = body["offer"]["id"]
    assert PaymentLink.query.count() == 0

    # Buyer polls: still pending.
    conv = client.get(f"/api/concierge/{store.store_slug}/conversation?session_id={SESSION}").get_json()
    assert conv["offers"][0]["status"] == "pending_seller"

    r = client.post(f"/api/concierge/seller/offers/{offer_id}/approve", headers=auth_headers(vendor))
    assert r.status_code == 200 and r.get_json()["offer"]["status"] == "link_created"
    link = PaymentLink.query.one()
    assert link.amount == Decimal("20000.00")
    conv = client.get(f"/api/concierge/{store.store_slug}/conversation?session_id={SESSION}").get_json()
    assert conv["offers"][0]["pay_link_slug"] == link.slug
    # Approving twice is refused.
    assert client.post(f"/api/concierge/seller/offers/{offer_id}/approve",
                       headers=auth_headers(vendor)).status_code == 409


def test_other_seller_cannot_approve_or_see_offer(client, shop, item, fake_ai, auth_headers):
    _, store = shop()
    intruder, _ = shop()
    item(store, "Leather bag", price=20000)
    fake_ai(classify=classify("buy", "leather bag"), answer=_negotiate_answer)
    offer_id = send(client, store, "buy leather bag").get_json()["offer"]["id"]
    r = client.post(f"/api/concierge/seller/offers/{offer_id}/approve", headers=auth_headers(intruder))
    assert r.status_code == 404 and PaymentLink.query.count() == 0
    assert client.get("/api/concierge/seller/offers", headers=auth_headers(intruder)).get_json()["offers"] == []


def test_seller_declines(client, shop, item, fake_ai, auth_headers):
    vendor, store = shop()
    item(store, "Leather bag", price=20000)
    fake_ai(classify=classify("buy", "leather bag"), answer=_negotiate_answer)
    offer_id = send(client, store, "buy leather bag").get_json()["offer"]["id"]
    r = client.post(f"/api/concierge/seller/offers/{offer_id}/decline", headers=auth_headers(vendor))
    assert r.get_json()["offer"]["status"] == "declined" and PaymentLink.query.count() == 0


# ── Fallbacks, caps and limits ─────────────────────────────────────────────
def test_ai_off_gives_ask_the_seller(client, shop, item, monkeypatch):
    _, store = shop(phone="08031234567")
    item(store, "Leather bag")
    monkeypatch.setenv("AI_ENABLED", "0")
    body = send(client, store, "how much is the leather bag").get_json()
    assert body["fallback"] is True and body["reason"] == "ai_off"
    assert body["reply"]["text"] == svc.FALLBACK_TEXT
    assert body["contact"]["phone"] == "08031234567" and body["contact"]["whatsapp_link"].startswith("https://wa.me/")
    assert AIUsage.query.count() == 0


def test_contact_seller_intent_hands_off(client, shop, fake_ai):
    _, store = shop()
    ai_client = fake_ai(classify=classify("contact_seller"))
    body = send(client, store, "I want to talk to the owner").get_json()
    assert body["handoff"] is True and body["contact"]["vendor_id"] == store.vendor_id
    assert ai_client.count("answer") == 0


def test_store_monthly_cap(client, shop, item, fake_ai, monkeypatch):
    _, store = shop()
    item(store, "Leather bag")
    monkeypatch.setenv("CONCIERGE_MONTHLY_CAP", "2")
    ai_client = fake_ai(classify=classify("greeting"),
                        answer={"answer": "Hello!", "product_ids": [], "claims": [], "handoff": False})
    for i in range(2):
        assert send(client, store, f"hi {i}", session=f"sess-{uuid.uuid4().hex}").get_json()["fallback"] is False
    body = send(client, store, "hi again").get_json()
    assert body["fallback"] is True and body["reason"] == "store_monthly_cap"
    assert ai_client.count("classify") == 2


def test_concierge_does_not_use_sellers_daily_cap(client, shop, item, fake_ai, monkeypatch):
    vendor, store = shop()
    monkeypatch.setenv("AI_SELLER_DAILY_ACTION_CAP", "1")
    fake_ai(classify=classify("greeting"),
            answer={"answer": "Hello!", "product_ids": [], "claims": [], "handoff": False})
    for i in range(3):
        assert send(client, store, f"hello {i}").get_json()["fallback"] is False
    from app.services.ai import gateway
    assert gateway.seller_actions_today(vendor.id) == 0          # own monthly cap, not the daily one
    assert AIUsage.query.filter_by(seller_id=vendor.id).count() == 6


def test_per_ip_limit(client, shop, fake_ai, monkeypatch):
    _, store = shop()
    monkeypatch.setenv("CONCIERGE_IP_PER_MINUTE", "2")
    fake_ai(classify=classify("greeting"),
            answer={"answer": "Hello!", "product_ids": [], "claims": [], "handoff": False})
    assert send(client, store, "a", ip="10.9.9.9").status_code == 200
    assert send(client, store, "b", ip="10.9.9.9").status_code == 200
    r = send(client, store, "c", ip="10.9.9.9")
    assert r.status_code == 429 and r.get_json()["contact"]["store_slug"] == store.store_slug
    assert send(client, store, "d", ip="10.1.1.1").status_code == 200


def test_conversation_is_per_store_per_session(client, shop, fake_ai):
    _, store = shop()
    _, other = shop()
    fake_ai(classify=classify("greeting"),
            answer={"answer": "Hello!", "product_ids": [], "claims": [], "handoff": False})
    send(client, store, "hi", session="sess-" + "b" * 20)
    send(client, other, "hi", session="sess-" + "b" * 20)
    send(client, store, "hi", session="sess-" + "c" * 20)
    h = client.get(f"/api/concierge/{store.store_slug}/conversation?session_id=sess-{'b' * 20}").get_json()
    assert [m["role"] for m in h["messages"]] == ["buyer", "assistant"]
    # The session id itself is never stored.
    assert ConciergeMessage.query.count() == 6
    from app.models.assistant import ConciergeConversation
    assert all("b" * 20 not in c.session_hash for c in ConciergeConversation.query.all())


def test_bad_session_and_empty_message(client, shop, fake_ai):
    _, store = shop()
    fake_ai()
    assert send(client, store, "hi", session="short").status_code == 400
    assert send(client, store, "   ").status_code == 400
