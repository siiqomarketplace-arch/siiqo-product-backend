"""
Phase 3: Ask (AI search, 3-3) and Market (paginated browsing, 3-4).

SQLite only: the Postgres full-text path is not exercised here; the code
falls back to ILIKE (pg_features() reports no FTS on SQLite). The AI gateway
gets a fake Anthropic client; no network, no API key.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models.product import Category, Product
from app.models.trust import VendorTrustProfile
from app.models.verification import VerificationStatus
from app.services import search as svc

_AI_ENV = ["ANTHROPIC_API_KEY", "AI_ENABLED", "AI_ENABLED_ACTIONS", "AI_SELLER_DAILY_ACTION_CAP",
           "AI_DAILY_COST_CAP_USD", "TRUST_SCORE_V1_LIVE"]


# ── Fakes ───────────────────────────────────────────────────────────────────
def _response(obj):
    usage = SimpleNamespace(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, iterations=None)
    return SimpleNamespace(id="msg_test", model="claude-haiku-5-5", stop_reason="end_turn",
                           stop_details=None, usage=usage,
                           content=[SimpleNamespace(type="text", text=json.dumps(obj))])


class FakeAI:
    """Answers search_parse and search_reasons from scripted callables."""

    def __init__(self, parse=None, reasons=None, error=None):
        self.parse, self.reasons, self.error = parse, reasons, error
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        system = kwargs["system"][0]["text"]
        kind = "reasons" if "reason line" in system else "parse"
        inputs = json.loads(kwargs["messages"][0]["content"].split("<inputs>")[1].split("</inputs>")[0])
        self.calls.append((kind, inputs))
        if self.error is not None:
            raise self.error
        fn = self.reasons if kind == "reasons" else self.parse
        if fn is None:
            raise AssertionError(f"unexpected {kind} call")
        return _response(fn(inputs))

    def count(self, kind):
        return sum(1 for k, _ in self.calls if k == kind)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in _AI_ENV:
        monkeypatch.delenv(key, raising=False)
    svc.clear_cache()
    svc.reset_features()
    yield
    svc.clear_cache()


@pytest.fixture
def fake_ai(monkeypatch):
    def _install(**kw):
        monkeypatch.setenv("AI_ENABLED", "1")
        client = FakeAI(**kw)
        monkeypatch.setattr(svc, "get_ai_client", lambda: client)
        return client
    return _install


@pytest.fixture
def shop(make_vendor):
    """A published store in a city."""
    def _make(name=None, city="Abuja", state="FCT", description=None):
        vendor, store = make_vendor()
        store.is_published = True
        store.city, store.state = city, state
        if name:
            store.store_name = name
        if description:
            store.store_description = description
        db.session.commit()
        return vendor, store
    return _make


@pytest.fixture
def item(make_product):
    def _make(store, name, description="Plain item", price=10000, is_active=True, **extra):
        p = make_product(store, price=price, **extra)
        p.name, p.description, p.is_active = name, description, is_active
        db.session.commit()
        return p
    return _make


def _earn_trust(vendor, score, tier, make_order):
    db.session.add(VendorTrustProfile(vendor_id=vendor.id, total_trust_score=score, trust_tier=tier))
    order = make_order(vendor)
    order.status = "COMPLETED"
    db.session.commit()


def _verify(vendor, subscription=True, days=365):
    now = datetime.now(timezone.utc)
    db.session.add(VerificationStatus(user_id=vendor.id, identity_verified_at=now,
                                      valid_until=now + timedelta(days=days),
                                      source_provider="youverify", subscription_active=subscription))
    db.session.commit()


def _ids(body):
    return [r["id"] for r in body["results"]]


# ── Parser ──────────────────────────────────────────────────────────────────
def test_regex_parse_extracts_fields_with_high_confidence():
    intent, conf = svc.regex_parse("birthday cake in Abuja under 40k")
    assert intent["keyword"] == "birthday cake"
    assert intent["city"] == "Abuja"
    assert intent["max_price"] == 40000
    assert intent["category"] == "Food & Drinks"
    assert conf >= svc.LOW_CONFIDENCE


def test_regex_parse_store_intent():
    intent, _ = svc.regex_parse("tailor shops in lagos")
    assert intent["looking_for"] == "store" and intent["city"] == "Lagos"


@pytest.mark.parametrize("q", [
    "abeg who dey sell better akara for my daughter party",
    "I need someone that can bake a big wedding cake fifty thousand naira",
    "who sells the nicest native wears for owambe in my area",
])
def test_regex_parse_low_confidence(q):
    _, conf = svc.regex_parse(q)
    assert conf < svc.LOW_CONFIDENCE


def test_parse_endpoint_is_instant_and_regex_only(client, fake_ai):
    ai = fake_ai()
    res = client.get("/api/search/parse?q=shoes+in+lagos+under+20k")
    body = res.get_json()
    assert res.status_code == 200
    assert body["intent"]["city"] == "Lagos" and body["intent"]["max_price"] == 20000
    assert ai.calls == []


def test_short_query_is_rejected(client):
    assert client.get("/api/search?q=a").status_code == 400


# ── Ranking ─────────────────────────────────────────────────────────────────
def test_name_match_ranks_above_description_match(client, shop, item):
    _, store = shop()
    desc_only = item(store, "Wrap skirt", description="Pairs well with an ankara gown")
    name_hit = item(store, "Ankara gown", description="Made to measure")
    body = client.get("/api/search?q=ankara gown").get_json()
    assert _ids(body)[:2] == [f"p{name_hit.id}", f"p{desc_only.id}"]


def test_unrelated_products_are_not_returned(client, shop, item):
    _, store = shop()
    item(store, "Restart button", description="Spare part")
    body = client.get("/api/search?q=art").get_json()
    assert body["results"] == [] and body["zero_results"] is True


def test_city_fit_ranks_local_first(client, shop, item):
    _, lagos = shop(city="Lagos", state="Lagos")
    _, abuja = shop(city="Abuja", state="FCT")
    far = item(lagos, "Chocolate cake")
    near = item(abuja, "Chocolate cake")
    body = client.get("/api/search?q=chocolate cake in abuja").get_json()
    assert _ids(body)[:2] == [f"p{near.id}", f"p{far.id}"]


def test_price_over_budget_is_excluded(client, shop, item):
    _, store = shop()
    cheap = item(store, "Leather shoes", price=15000)
    item(store, "Leather shoes", price=90000)
    body = client.get("/api/search?q=leather shoes under 20k").get_json()
    assert _ids(body) == [f"p{cheap.id}"]


def test_trust_boost_breaks_equal_relevance_and_fit(client, shop, item, make_order):
    v_new, s_new = shop()
    v_trusted, s_trusted = shop()
    new_p = item(s_new, "Ankara fabric")
    trusted_p = item(s_trusted, "Ankara fabric")
    _earn_trust(v_trusted, 900, "PLATINUM", make_order)
    body = client.get("/api/search?q=ankara fabric").get_json()
    assert _ids(body)[:2] == [f"p{trusted_p.id}", f"p{new_p.id}"]
    trusted = body["results"][0]["labels"]["trust"]
    assert trusted == {"score": 900, "score_max": 1000, "tier": "PLATINUM"}
    assert body["results"][1]["labels"]["trust"]["tier"] == "NEW"
    assert body["results"][1]["labels"]["trust"]["score"] is None


def test_trust_never_beats_much_better_relevance(client, shop, item, make_order):
    v_trusted, s_trusted = shop()
    _, s_plain = shop()
    weak = item(s_trusted, "Wrap skirt", description="Goes with an ankara gown")
    strong = item(s_plain, "Ankara gown")
    _earn_trust(v_trusted, 1000, "PLATINUM", make_order)
    body = client.get("/api/search?q=ankara gown").get_json()
    assert _ids(body)[0] == f"p{strong.id}" and f"p{weak.id}" in _ids(body)


def test_paid_plan_and_verified_badge_do_not_boost(app, shop, item):
    v_plain, s_plain = shop()
    v_paid, s_paid = shop()
    item(s_plain, "Ankara fabric")
    item(s_paid, "Ankara fabric")
    _verify(v_paid, subscription=True)                    # Verified Pro plan, identity passed
    s_paid.is_pro_verified = True                         # legacy paid badge too
    s_paid.pro_verified_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    db.session.commit()
    intent, _ = svc.regex_parse("ankara fabric")
    items = svc.build_results(intent, {})
    by_vendor = {it["store"]["slug"]: it for it in items}
    paid, plain = by_vendor[s_paid.store_slug], by_vendor[s_plain.store_slug]
    assert paid["_score"] == plain["_score"]
    assert paid["_trust"] == plain["_trust"] == 0.0
    assert paid["labels"]["verified"] is True and plain["labels"]["verified"] is False


def test_trust_boost_uses_only_score_and_tier():
    assert svc.trust_boost(None) == 0.0
    assert svc.trust_boost({"score": None, "tier": "NEW"}) == 0.0
    high = svc.trust_boost({"score": 90, "score_max": 100, "tier": "PLATINUM"})
    low = svc.trust_boost({"score": 40, "score_max": 100, "tier": "BRONZE"})
    assert 0 < low < high <= 1


def test_stores_are_searchable_and_link_to_store_page(client, shop, item):
    _, store = shop(name="Mama Put Kitchen", description="Home-cooked jollof and soups")
    item(store, "Plate of rice")  # a store with nothing to buy is left out (C1)
    body = client.get("/api/search?q=jollof").get_json()
    assert body["results"][0]["kind"] == "store"
    assert body["results"][0]["href"] == f"/{store.store_slug}"


def test_product_result_links_to_existing_pages(client, shop, item):
    _, store = shop()
    p = item(store, "Ankara gown")
    r = client.get("/api/search?q=ankara").get_json()["results"][0]
    assert r["href"] == f"/products/{p.id}" and r["store_href"] == f"/{store.store_slug}"
    assert r["labels"]["on_siiqo"] is True


# ── Labels: Verified only when true ─────────────────────────────────────────
def test_verified_label_only_when_claim_is_true(client, shop, item):
    v_ok, s_ok = shop()
    v_lapsed, s_lapsed = shop()
    v_expired, s_expired = shop()
    for s in (s_ok, s_lapsed, s_expired):
        item(s, "Ankara gown")
    _verify(v_ok, subscription=True)
    _verify(v_lapsed, subscription=False)                 # plan lapsed: no mark
    s_expired.is_pro_verified = True                      # legacy badge, expired
    s_expired.pro_verified_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.session.commit()
    body = client.get("/api/search?q=ankara gown").get_json()
    flags = {r["store"]["slug"]: r["labels"]["verified"] for r in body["results"]}
    assert flags == {s_ok.store_slug: True, s_lapsed.store_slug: False, s_expired.store_slug: False}


def test_verified_only_filter(client, shop, item):
    v_ok, s_ok = shop()
    _, s_plain = shop()
    ok = item(s_ok, "Ankara gown")
    item(s_plain, "Ankara gown")
    _verify(v_ok)
    body = client.get("/api/search?q=ankara gown&verified=1").get_json()
    assert _ids(body) == [f"p{ok.id}"]


# ── Visibility ──────────────────────────────────────────────────────────────
def test_drafts_inactive_stores_and_accounts_are_excluded(client, shop, item):
    _, live = shop()
    _, unpublished = shop()
    v_suspended, suspended = shop()
    visible = item(live, "Ankara gown")
    item(live, "Ankara gown", is_active=False)                       # draft
    item(live, "Ankara gown", is_deleted=True)                       # deleted
    item(live, "Ankara gown", stock=0)                               # sold out (physical)
    digital = item(live, "Ankara gown pattern", product_type="digital", stock=0)
    item(unpublished, "Ankara gown")
    item(suspended, "Ankara gown")
    unpublished.is_published = False
    v_suspended.is_active = False
    db.session.commit()

    ids = set(_ids(client.get("/api/search?q=ankara gown").get_json()))
    assert ids == {f"p{visible.id}", f"p{digital.id}"}

    market = client.get("/api/market/products?q=ankara").get_json()
    assert {p["product_id"] for p in market["products"]} == {visible.id, digital.id}
    stores = client.get("/api/market/stores").get_json()
    assert [s["store_id"] for s in stores["stores"]] == [live.id]


# ── Market: pagination, filters, sort ───────────────────────────────────────
def test_market_pagination(client, shop, item):
    _, store = shop()
    for i in range(30):
        item(store, f"Item {i}")
    p1 = client.get("/api/market/products?per_page=12").get_json()
    p3 = client.get("/api/market/products?per_page=12&page=3").get_json()
    assert (p1["total"], p1["pages"], len(p1["products"]), p1["has_next"]) == (30, 3, 12, True)
    assert (len(p3["products"]), p3["has_next"], p3["has_prev"]) == (6, False, True)
    seen = {p["product_id"] for page in (1, 2, 3)
            for p in client.get(f"/api/market/products?per_page=12&page={page}").get_json()["products"]}
    assert len(seen) == 30


def test_market_per_page_is_capped(client, shop, item):
    _, store = shop()
    item(store, "Thing")
    body = client.get("/api/market/products?per_page=5000").get_json()
    assert body["per_page"] == svc.MARKET_PER_PAGE_MAX


def test_market_filters(client, shop, item):
    v_ok, abuja = shop(city="Abuja")
    _, lagos = shop(city="Lagos", state="Lagos")
    fashion = Category(name="Fashion", slug="fashion")
    food = Category(name="Food & Drinks", slug="food-drinks")
    db.session.add_all([fashion, food])
    db.session.commit()
    gown = item(abuja, "Gown", price=20000, category_id=fashion.id)
    cake = item(lagos, "Cake", price=8000, category_id=food.id)
    ebook = item(lagos, "Recipe ebook", price=2000, product_type="digital", stock=0, category_id=food.id)
    _verify(v_ok)

    def ids(qs):
        return {p["product_id"] for p in client.get(f"/api/market/products?{qs}").get_json()["products"]}

    assert ids("category=fashion") == {gown.id}
    assert ids("category=Food%20%26%20Drinks") == {cake.id, ebook.id}      # old ?category=<name> links
    assert ids("city=lagos") == {cake.id, ebook.id}
    assert ids("min_price=5000&max_price=10000") == {cake.id}
    assert ids("type=digital") == {ebook.id}
    assert ids("type=physical") == {gown.id, cake.id}
    assert ids("verified=1") == {gown.id}
    assert ids("q=cake") == {cake.id}
    body = client.get("/api/market/products?category=nope").get_json()
    assert body["ignored"] == ["category"] and body["total"] == 3


def test_market_sort(client, shop, item):
    _, store = shop()
    a = item(store, "A", price=500)
    b = item(store, "B", price=1500)
    c = item(store, "C", price=1000)

    def order(sort):
        return [p["product_id"] for p in client.get(f"/api/market/products?sort={sort}").get_json()["products"]]

    assert order("price_asc") == [a.id, c.id, b.id]
    assert order("price_desc") == [b.id, c.id, a.id]
    assert order("newest") == [c.id, b.id, a.id]


def test_market_categories_counts_visible_products_only(client, shop, item):
    _, store = shop()
    cat = Category(name="Fashion", slug="fashion")
    db.session.add(cat)
    db.session.commit()
    item(store, "Gown", category_id=cat.id)
    item(store, "Draft gown", category_id=cat.id, is_active=False)
    body = client.get("/api/market/categories").get_json()
    assert body["categories"] == [{"id": cat.id, "name": "Fashion", "slug": "fashion",
                                   "icon": None, "count": 1}]


# ── Reason-line fact check ──────────────────────────────────────────────────
def _fact_item(shop, item):
    _, store = shop(name="Ada Fabrics", city="Wuse", state="Abuja")
    p = item(store, "Ankara maxi dress", description="Hand-sewn cotton dress, size 12", price=15000)
    intent, _ = svc.regex_parse("ankara dress")
    return next(it for it in svc.build_results(intent, {}) if it["id"] == f"p{p.id}")


def test_fact_check_accepts_lines_built_from_own_fields(shop, item):
    it = _fact_item(shop, item)
    assert svc.fact_check("Ankara maxi dress · ₦15,000 · Wuse, Abuja", it)
    assert svc.fact_check("Hand-sewn cotton dress in size 12 from Ada Fabrics", it)
    assert svc.fact_check("Cotton ankara dress for 15k", it)


@pytest.mark.parametrize("line", [
    "Ankara maxi dress · ₦12,000",                 # number not in the fields
    "Ankara dress · delivers in 2 days",           # claim not in the fields
    "Silk ankara dress",                           # word not in the fields
    "Verified seller of ankara dresses",           # banned claim word
    "Best ankara dress in Abuja",                  # superlative
    "",
    "x" * 200,
])
def test_fact_check_rejects(shop, item, line):
    it = _fact_item(shop, item)
    assert not svc.fact_check(line, it)


def test_fact_check_rejects_claim_words_even_if_in_description(shop, item):
    _, store = shop()
    p = item(store, "Ankara dress", description="Trusted and verified quality")
    intent, _ = svc.regex_parse("ankara dress")
    it = next(i for i in svc.build_results(intent, {}) if i["id"] == f"p{p.id}")
    assert not svc.fact_check("Verified ankara dress", it)


def test_fact_check_uses_only_that_results_fields(shop, item):
    _, s1 = shop(city="Lagos", state="Lagos")
    _, s2 = shop(city="Kano", state="Kano")
    item(s1, "Ankara dress")
    p2 = item(s2, "Ankara dress")
    intent, _ = svc.regex_parse("ankara dress")
    it2 = next(i for i in svc.build_results(intent, {}) if i["id"] == f"p{p2.id}")
    assert svc.fact_check("Ankara dress · Kano", it2)
    assert not svc.fact_check("Ankara dress · Lagos", it2)


# ── AI off / mocked / failing ───────────────────────────────────────────────
def test_ai_off_gives_keyword_results_without_reasons(client, shop, item):
    _, store = shop()
    p = item(store, "Ankara gown")
    body = client.get("/api/search?q=abeg who dey sell ankara gown for my sister wedding").get_json()
    assert _ids(body)[0] == f"p{p.id}"
    assert body["parse"] == {"source": "regex", "confidence": body["parse"]["confidence"], "ai": "off"}
    assert body["reasons_status"] == "off" and body["results"][0]["reason"] is None
    r = client.get("/api/search/reasons?q=abeg who dey sell ankara gown for my sister wedding").get_json()
    assert r == {"query": "abeg who dey sell ankara gown for my sister wedding", "reasons": {}, "status": "off", "dropped": 0}


def test_ai_mocked_parse_and_reasons_with_fact_check(client, shop, item, fake_ai):
    _, store = shop(name="Ada Fabrics", city="Ibadan", state="Oyo")
    good = item(store, "Akara balls", description="Fried bean cakes for parties", price=5000)
    bad = item(store, "Akara platter", description="Party platter", price=9000)

    def reasons(inputs):
        out = []
        for r in inputs["results"]:
            if r["id"] == f"p{good.id}":
                out.append({"id": r["id"], "text": "Fried bean cakes for parties · ₦5,000 · Ibadan"})
            elif r["id"] == f"p{bad.id}":
                out.append({"id": r["id"], "text": "Freshest akara in Ibadan, delivered in 1 hour"})
            else:
                out.append({"id": r["id"], "text": ""})
        return {"reasons": out}

    ai = fake_ai(
        parse=lambda inputs: {"keyword": "akara", "category": "Food & Drinks", "city": "Ibadan",
                              "min_price_ngn": None, "max_price_ngn": 10000, "looking_for": "product"},
        reasons=reasons,
    )
    q = "abeg who dey sell akara for party for Ibadan, nothing pass 10 thousand"
    body = client.get("/api/search", query_string={"q": q}).get_json()
    assert body["parse"]["source"] == "ai" and body["parse"]["ai"] == "ok"
    assert body["intent"]["keyword"] == "akara" and body["intent"]["max_price"] == 10000
    assert set(_ids(body)) == {f"p{good.id}", f"p{bad.id}"}
    assert body["reasons_status"] == "pending"
    assert ai.calls[0] == ("parse", {"query": q})

    r = client.get("/api/search/reasons", query_string={"q": q}).get_json()
    assert r["status"] == "ok" and r["dropped"] == 1
    assert r["reasons"] == {f"p{good.id}": "Fried bean cakes for parties · ₦5,000 · Ibadan"}

    # The answer now carries the line; nothing is called again (cache).
    again = client.get("/api/search", query_string={"q": q}).get_json()
    assert again["cached"] is True and again["reasons_status"] == "ok"
    line = {x["id"]: x["reason"] for x in again["results"]}
    assert line[f"p{good.id}"].startswith("Fried bean") and line[f"p{bad.id}"] is None
    assert ai.count("parse") == 1 and ai.count("reasons") == 1


def test_ai_reasons_in_one_request(client, shop, item, fake_ai):
    _, store = shop()
    p = item(store, "Ankara gown", price=20000)
    fake_ai(reasons=lambda inputs: {"reasons": [{"id": r["id"], "text": "Ankara gown · ₦20,000"}
                                                for r in inputs["results"]]})
    body = client.get("/api/search?q=ankara gown&reasons=1").get_json()
    assert body["reasons_status"] == "ok"
    assert {r["id"]: r["reason"] for r in body["results"]}[f"p{p.id}"] == "Ankara gown · ₦20,000"


def test_reasons_cover_top_ten_in_one_call(client, shop, item, fake_ai):
    _, store = shop()
    for i in range(14):
        item(store, f"Ankara gown {i}")
    ai = fake_ai(reasons=lambda inputs: {"reasons": []})
    client.get("/api/search/reasons?q=ankara gown")
    assert ai.count("reasons") == 1
    assert len(ai.calls[0][1]["results"]) == svc.REASON_COUNT


def test_ai_failure_falls_back_to_keyword_search(client, shop, item, fake_ai):
    _, store = shop()
    p = item(store, "Ankara gown")
    ai = fake_ai(error=RuntimeError("provider down"))
    body = client.get("/api/search?q=abeg who dey sell ankara gown for my sister wedding").get_json()
    assert _ids(body)[0] == f"p{p.id}"
    assert body["parse"]["source"] == "regex" and body["parse"]["ai"] == "failed"
    r = client.get("/api/search/reasons?q=abeg who dey sell ankara gown for my sister wedding").get_json()
    assert r["status"] == "failed" and r["reasons"] == {}
    assert ai.count("reasons") == 1                         # 0 retries for reasons


def test_ai_parse_invalid_output_retries_once_then_falls_back(client, shop, item, fake_ai):
    _, store = shop()
    item(store, "Ankara gown")
    ai = fake_ai(parse=lambda inputs: {"keyword": "x"})       # schema mismatch
    body = client.get("/api/search?q=abeg who dey sell ankara gown for my sister wedding").get_json()
    assert body["parse"]["ai"] == "failed" and ai.count("parse") == 2
    assert body["results"]


def test_ai_parse_is_skipped_when_regex_is_confident(client, shop, item, fake_ai):
    _, store = shop()
    item(store, "Ankara gown")
    ai = fake_ai(reasons=lambda inputs: {"reasons": []})
    body = client.get("/api/search?q=ankara gown in abuja").get_json()
    assert body["parse"] == {"source": "regex", "confidence": 1.0, "ai": "not_needed"}
    assert ai.count("parse") == 0


def test_ai_parse_daily_cap_per_ip(client, shop, item, fake_ai, monkeypatch):
    _, store = shop()
    item(store, "Ankara gown")
    monkeypatch.setattr(svc, "AI_PARSE_DAILY_CAP_PER_IP", 2)
    ai = fake_ai(parse=lambda inputs: {"keyword": "ankara gown", "category": None, "city": None,
                                       "min_price_ngn": None, "max_price_ngn": None, "looking_for": "any"})
    statuses = [client.get(f"/api/search?q=abeg who dey sell ankara gown for my sister wedding {i}").get_json()["parse"]["ai"]
                for i in range(3)]
    assert statuses == ["ok", "ok", "capped"] and ai.count("parse") == 2


def test_ai_parse_critic_drops_bad_fields():
    fallback = {"city": "Lagos", "max_price": None, "category": None}
    cleaned = svc._clean_ai_intent({"keyword": "akara", "category": None, "city": "<script>",
                                    "min_price_ngn": 9000, "max_price_ngn": 1000,
                                    "looking_for": "store"}, fallback)
    assert cleaned["city"] == "Lagos" and cleaned["min_price"] is None and cleaned["max_price"] is None
    assert svc._clean_ai_intent({"keyword": "the a for"}, fallback) is None


# ── Cache and rate limit ────────────────────────────────────────────────────
def test_answers_are_cached_by_normalised_query(client, shop, item):
    _, store = shop()
    item(store, "Ankara gown")
    first = client.get("/api/search?q=Ankara  Gown").get_json()
    second = client.get("/api/search?q=ankara gown!").get_json()
    assert first["cached"] is False and second["cached"] is True
    assert _ids(first) == _ids(second)
    other_filter = client.get("/api/search?q=ankara gown&verified=1").get_json()
    assert other_filter["cached"] is False


def test_cache_expires_after_ttl(client, shop, item, monkeypatch):
    _, store = shop()
    item(store, "Ankara gown")
    client.get("/api/search?q=ankara gown")
    real = svc.time.monotonic
    monkeypatch.setattr(svc.time, "monotonic", lambda: real() + svc.CACHE_TTL_SECONDS + 1)
    assert client.get("/api/search?q=ankara gown").get_json()["cached"] is False


def test_search_rate_limit_per_ip(client):
    from app.extensions import limiter
    limiter.enabled = True
    try:
        limiter.reset()
        codes = [client.get(f"/api/search?q=ankara {i}").status_code for i in range(31)]
    finally:
        limiter.reset()
        limiter.enabled = False
    assert codes[:30] == [200] * 30 and codes[30] == 429


# ── Postgres feature detection ──────────────────────────────────────────────
def test_sqlite_uses_ilike_fallback(app):
    with app.app_context():
        assert svc.pg_features() == {"fts": False, "trgm": False}


def test_tsquery_is_built_from_safe_terms():
    assert svc._tsquery(svc._terms("Ankara's gowns & shoes; DROP TABLE")) == \
        "ankara:* | gowns:* | shoes:* | drop:* | table:*"
