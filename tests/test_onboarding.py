"""
AI seller onboarding (Phase 1, slice 1): /api/onboarding.

No network: the AI gateway gets a fake Anthropic client, Paystack's
subaccount call is replaced, uploads go to a temporary folder.
"""
import io
import json
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from PIL import Image

from app.extensions import db
from app.models.ai import AIUsage
from app.models.jobs import Job
from app.models.onboarding import OnboardingSession
from app.models.payment_link import PaymentLink
from app.models.product import Product
from app.models.user import Storefront, UserRole
from app.models.withdrawal import VendorBankAccount
from app.services import jobs
from app.services import onboarding_build as build
from app.services.onboarding_critic import banned_claims, check_listing, check_spec, numbers_in
from app.services.store_spec import (
    KNOWN_LAYOUT_STYLES,
    PRESET_IDS,
    PRESET_LAYOUT_STYLE,
    parse_store_spec,
    resolve_preset_id,
    spec_from_template_options,
    template_options_from_spec,
)

API = "/api/onboarding"

ANSWERS = {
    "business_name": "Ada Fabrics",
    "what_you_sell": "Ankara dresses and tops made to measure.",
    "who_buys": "Women who want outfits for weddings and church",
    "city": "Enugu",
    "state": "Enugu",
    "delivery": "Pickup in Enugu or delivery by courier",
    "price_range": "15k - 40k",
    "contact_phone": "08031234567",
    "product_type": "physical",
}

GOOD_SPEC = {
    "preset": "fashion",
    "sections": ["hero", "products", "about", "contact"],
    "hero": {"heading": "Ankara dresses made to measure", "subtext": "Dresses and tops sewn to your size.",
             "cta": "See the dresses"},
    "copy": {"tagline": "Ankara made to measure", "aboutTitle": "About Ada Fabrics",
             "aboutDescription": "We sew Ankara dresses and tops in Enugu.", "productsHeading": "Our dresses",
             "productsSubheading": "Pick a style and send your size.", "contactHeading": "Get in touch",
             "contactDescription": "Send us a message to order."},
}
GOOD_LISTING = {"title": "Blue Ankara maxi dress", "description": "A long dress in a blue Ankara print.",
                "category": None, "attributes": [{"name": "Colour", "value": "Blue"}]}
BANK = {"bank_code": "058", "account_number": "0123456789", "account_name": "ADA OKAFOR", "bank_name": "GTBank"}


# ── Fakes ───────────────────────────────────────────────────────────────────
def _response(obj):
    usage = SimpleNamespace(input_tokens=500, output_tokens=200, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, iterations=None)
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return SimpleNamespace(id="msg_test", model="claude-opus-5-5", stop_reason="end_turn", stop_details=None,
                           content=[SimpleNamespace(type="text", text=text)], usage=usage)


def _action_of(request):
    system = request["system"][0]["text"]
    if system.startswith("You draft the first version"):
        return "store_spec"
    if system.startswith("You draft a product listing"):
        return "listing_from_photo"
    if system.startswith("You apply one edit request"):
        return "store_edit"
    raise AssertionError("unknown prompt")


class FakeAI:
    """Anthropic-shaped client. ``script[action]`` is a list of answers used in
    order (the last one repeats); an Exception instance is raised instead."""

    def __init__(self, **script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []
        self._lock = threading.Lock()
        endpoint = SimpleNamespace(create=self._create)
        self.messages = endpoint
        self.beta = SimpleNamespace(messages=endpoint)

    def _create(self, **request):
        request.pop("betas", None)
        request.pop("extra_body", None)
        action = _action_of(request)
        with self._lock:
            self.calls.append((action, request))
            answers = self.script.get(action) or [RuntimeError(f"no script for {action}")]
            item = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(item, Exception):
            raise item
        return _response(item)

    def calls_for(self, action):
        return [r for a, r in self.calls if a == action]


def _inputs_of(request):
    content = request["messages"][0]["content"]
    text = content if isinstance(content, str) else content[-1]["text"]
    return json.loads(text.split("<inputs>\n", 1)[1].rsplit("\n</inputs>", 1)[0])


# ── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _no_ai_env(monkeypatch):
    for key in ("AI_ENABLED", "AI_ENABLED_ACTIONS", "ANTHROPIC_API_KEY", "AI_SELLER_DAILY_ACTION_CAP",
                "ONBOARDING_DAILY_BUILDS", "ONBOARDING_DAILY_REBUILDS"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def upload_root(app, tmp_path, monkeypatch):
    """Local uploads (and the build's photo reads) go to a temp folder."""
    monkeypatch.setattr(app, "root_path", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_ai(monkeypatch):
    def _install(**script):
        monkeypatch.setenv("AI_ENABLED", "1")
        client = FakeAI(**script)
        monkeypatch.setattr(build, "get_ai_client", lambda: client)
        return client
    return _install


@pytest.fixture
def paystack(monkeypatch):
    calls = []

    def _fake(business_name, bank_code, account_number, description=""):
        calls.append((business_name, bank_code, account_number))
        return {"success": True, "subaccount_code": "ACCT_test123"}

    import app.services.escrow.paystack_provider as provider
    monkeypatch.setattr(provider, "create_paystack_subaccount", _fake)
    return calls


@pytest.fixture
def seller(make_user):
    user = make_user()
    user.is_verified = True
    db.session.commit()
    return user


def _jpeg(colour=(30, 90, 200), size=(48, 48), fmt="JPEG"):
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, fmt)
    return buf.getvalue()


def _session(client, headers, answers=None):
    res = client.post(f"{API}/sessions", headers=headers, json={"answers": answers} if answers else {})
    assert res.status_code in (200, 201), res.get_json()
    return res.get_json()["session"]


def _upload(client, headers, sid, data=None, name="p.jpg", **form):
    payload = {"photo": (io.BytesIO(data if data is not None else _jpeg()), name), **form}
    return client.post(f"{API}/sessions/{sid}/photos", headers=headers, data=payload,
                       content_type="multipart/form-data")


def _built(client, headers, answers=ANSWERS, photos=2, prices=(15000, None)):
    s = _session(client, headers, answers)
    for i in range(photos):
        form = {"caption": f"Style {i + 1}"}
        if i < len(prices) and prices[i] is not None:
            form["price"] = str(prices[i])
        assert _upload(client, headers, s["id"], **form).status_code == 201
    res = client.post(f"{API}/sessions/{s['id']}/build", headers=headers)
    assert res.status_code == 202, res.get_json()
    return client.get(f"{API}/sessions/{s['id']}", headers=headers).get_json()["session"]


# ── Store Spec (Python mirror of spec.ts / presets.ts) ──────────────────────
def _old_chain(t):
    c = "tech"
    for preset, names in (
        ("fashion", ["Fashion Chic", "Luxe Serif", "TemplateFashion"]), ("beauty", ["Beauty & Wellness", "TemplateBeauty"]),
        ("luxe-serif", ["Luxe Serif", "TemplateLuxeSerif"]), ("food", ["Street Food", "TemplateFood"]),
        ("restaurant-dark", ["Restaurant Dark", "TemplateRestaurantDark"]), ("real-estate", ["Real Estate", "TemplateRealEstate"]),
        ("minimal", ["Dark Portfolio", "Freelancer Pro", "TemplateMinimal"]), ("photography", ["Photography"]),
        ("bold-fitness", ["Bold Fitness", "Bold Energetic", "TemplateBoldFitness"]), ("education-hub", ["Education Hub"]),
        ("corporate", ["Corporate Pro"]), ("playful-dynamic", ["Playful Dynamic"]), ("market", ["African Market", "TemplateMarket"]),
    ):
        if t in names:
            c = preset
    return c


def test_preset_resolution_matches_the_website():
    samples = list(KNOWN_LAYOUT_STYLES) + ["Modern Tech", "TemplateTech", "", "Unknown", "luxe serif", " Photography"]
    for s in samples:
        assert resolve_preset_id(s) == _old_chain(s), s
    assert resolve_preset_id(None) == "tech"
    assert len(PRESET_IDS) == 14 and set(PRESET_LAYOUT_STYLE) == set(PRESET_IDS)
    for preset in PRESET_IDS:   # the saved name maps back to its preset
        assert resolve_preset_id(PRESET_LAYOUT_STYLE[preset]) == preset


def test_template_options_round_trip():
    from app.routes.vendor import _build_default_template_options
    for category in ("fashion", "phones", "skincare", "food", "general"):
        tpl = _build_default_template_options(category)
        spec = spec_from_template_options(tpl)
        assert parse_store_spec({**spec})[1] == []
        back = template_options_from_spec(spec)
        assert back == tpl, category
    spec = spec_from_template_options({"layout_style": "Luxe Serif", "primary_color": "#111111", "custom": 1,
                                       "sections": ["hero", "bogus", "faq"]})
    assert spec["preset"] == "luxe-serif" and spec["theme"]["primaryColor"] == "#111111"
    assert spec["sections"] == ["hero", "faq"] and spec["extra"] == {"custom": 1}


def test_parse_store_spec_rules():
    ok = {"version": 1, "preset": "food", "theme": {"primaryColor": "#AABBCC"}, "hero": {"heading": "  Hi  "},
          "sections": ["hero"], "unknown": 1}
    spec, errors = parse_store_spec(ok)
    assert errors == [] and spec["hero"]["heading"] == "Hi" and "unknown" not in spec
    assert spec["copy"] == {} and spec["extra"] == {}
    for bad in (
        {**ok, "version": 2}, {**ok, "preset": "nope"}, {**ok, "theme": {"primaryColor": "red"}},
        {**ok, "hero": {"heading": "x" * 121}}, {**ok, "sections": ["hero", "footer"]},
        {**ok, "copy": {"tagline": 5}}, {**ok, "theme": None},
    ):
        assert parse_store_spec(bad)[1], bad


# ── Critic ──────────────────────────────────────────────────────────────────
def test_banned_claims():
    assert "verified" in banned_claims("A verified seller")
    assert "escrow" in banned_claims("Backed by escrow")
    assert "guaranteed" in banned_claims("Satisfaction guaranteed")
    assert "genuine" in banned_claims("100% genuine phones")
    assert "warranty" in banned_claims("Comes with warranty")
    assert "fees" in banned_claims("No fees")
    assert "same-day" in banned_claims("Same-day delivery in Lagos")
    # A claim word is fine when the seller's own answers use it.
    assert banned_claims("Organic shea butter", "We sell organic shea butter") == []
    assert "organic" in banned_claims("Organic shea butter", "We sell shea butter")
    # Never-allowed claims stay banned even if the seller wrote them.
    assert "verified" in banned_claims("Verified gadgets", "verified gadgets")


def test_fact_trace_numbers_and_places():
    assert numbers_in("from 15k to 1.2m, since 2019") >= {15.0, 15000.0, 1.2, 1200000.0, 2019.0}
    spec = {"version": 1, "preset": "fashion", "sections": ["hero", "products", "contact"],
            "hero": {"heading": "Dresses from 15,000"}, "copy": {}, "theme": {}, "nav": {}, "extra": {}}
    assert check_spec(spec, ANSWERS) == []            # 15k is in the answers
    spec["hero"]["heading"] = "Since 2015 in Lagos"
    problems = check_spec(spec, ANSWERS)
    assert any("2015" in p for p in problems) and any("lagos" in p for p in problems)
    spec["hero"]["heading"] = "Made in Enugu"
    assert check_spec(spec, ANSWERS) == []


def test_required_sections_and_listing_checks():
    spec = {"version": 1, "preset": "fashion", "sections": ["hero", "about"], "hero": {"heading": "Hi"}}
    assert any("required" in p for p in check_spec(spec, ANSWERS))
    assert check_listing(GOOD_LISTING, ANSWERS) == []
    assert check_listing({**GOOD_LISTING, "title": "Dress for 20000 naira"}, ANSWERS)
    assert check_listing({**GOOD_LISTING, "category": "Cars"}, ANSWERS, categories=["Fashion"])


def test_old_preset_defaults_are_claim_free():
    from app.routes.vendor import _build_default_template_options
    for category in ("fashion", "phones", "skincare", "food", "general"):
        tpl = _build_default_template_options(category)
        text = " ".join(str(v) for v in tpl.values())
        assert banned_claims(text) == [], (category, banned_claims(text))
        assert set(tpl) >= {"layout_style", "hero_heading", "about_description", "store_tagline", "sections"}


# ── Name check ──────────────────────────────────────────────────────────────
def test_name_check_available(client):
    body = client.get(f"{API}/name-check?name=Mama Put Kitchen").get_json()
    assert body["available"] is True and body["slug"] == "mama-put-kitchen"
    assert body["suggestions"] == [] and body["store_url"].endswith("/mama-put-kitchen")


def test_name_check_taken_suggests_free_slugs(client, make_vendor):
    _, store = make_vendor()
    store.store_slug = "ada-fabrics"
    _, store2 = make_vendor()
    store2.store_slug = "ada-fabrics-store"
    db.session.commit()
    body = client.get(f"{API}/name-check?name=Ada Fabrics").get_json()
    assert body["available"] is False and body["reason"] == "taken"
    assert body["suggestions"] and "ada-fabrics" not in body["suggestions"]
    for s in body["suggestions"]:
        assert Storefront.query.filter_by(store_slug=s).first() is None


def test_name_check_reserved_and_invalid(client):
    body = client.get(f"{API}/name-check?name=Market").get_json()
    assert body["available"] is False and body["reason"] == "reserved" and body["suggestions"]
    assert "market" not in body["suggestions"]
    body = client.get(f"{API}/name-check?name=!!!").get_json()
    assert body["available"] is False and body["reason"] == "invalid"
    assert client.get(f"{API}/name-check").status_code == 400


# ── Sessions ────────────────────────────────────────────────────────────────
def test_session_lifecycle_and_one_open_session(client, seller, auth_headers):
    h = auth_headers(seller)
    assert client.get(f"{API}/sessions/current", headers=h).status_code == 404
    res = client.post(f"{API}/sessions", headers=h, json={"answers": {"business_name": "Ada Fabrics"}})
    assert res.status_code == 201
    s = res.get_json()["session"]
    assert s["status"] == "interview" and s["step"] == 1 and s["answers"] == {"business_name": "Ada Fabrics"}
    assert [st["key"] for st in s["stages"]] == ["copy", "colours", "products", "assemble"]
    again = client.post(f"{API}/sessions", headers=h)
    assert again.status_code == 200 and again.get_json()["session"]["id"] == s["id"]
    assert client.get(f"{API}/sessions/current", headers=h).get_json()["session"]["id"] == s["id"]
    assert OnboardingSession.query.count() == 1

    # The database refuses a second open session for the same user.
    from sqlalchemy.exc import IntegrityError
    db.session.add(OnboardingSession(user_id=seller.id, open_user_id=seller.id, answers={}))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_other_users_session_is_404(client, seller, make_user, auth_headers):
    s = _session(client, auth_headers(seller), ANSWERS)
    other = auth_headers(make_user())
    sid = s["id"]
    assert client.get(f"{API}/sessions/{sid}", headers=other).status_code == 404
    assert client.patch(f"{API}/sessions/{sid}", headers=other, json={"step": 2}).status_code == 404
    assert _upload(client, other, sid).status_code == 404
    assert client.post(f"{API}/sessions/{sid}/build", headers=other).status_code == 404
    assert client.patch(f"{API}/sessions/{sid}/draft", headers=other, json={"draft_listings": []}).status_code == 404
    assert client.post(f"{API}/sessions/{sid}/publish", headers=other, json=BANK).status_code == 404
    assert client.get(f"{API}/sessions/{sid}").status_code == 401


def test_existing_seller_cannot_start(client, make_vendor, auth_headers):
    vendor, store = make_vendor()
    res = client.post(f"{API}/sessions", headers=auth_headers(vendor))
    assert res.status_code == 409 and res.get_json()["code"] == "ALREADY_HAS_STORE"


def test_answer_validation_and_merge(client, seller, auth_headers):
    h = auth_headers(seller)
    sid = _session(client, h)["id"]
    patch = lambda body: client.patch(f"{API}/sessions/{sid}", headers=h, json=body)  # noqa: E731
    for bad in (
        {"answers": {"favourite_colour": "blue"}},
        {"answers": {"business_name": "x" * 101}},
        {"answers": {"business_name": "A"}},
        {"answers": {"contact_phone": "call me"}},
        {"answers": {"store_slug": "Bad Slug!"}},
        {"answers": {"store_slug": "admin"}},
        {"answers": {"product_type": "car"}},
        {"answers": {"price_range": {"min": 50, "max": 10}}},
        {"answers": "nope"},
        {"step": 9},
        {},
    ):
        res = patch(bad)
        assert res.status_code == 400, bad
    res = patch({"answers": {"business_name": " Ada Fabrics ", "what_you_sell": "Dresses",
                             "price_range": {"min": 5000, "max": 40000}}, "step": 3})
    assert res.status_code == 200
    s = res.get_json()["session"]
    assert s["answers"]["business_name"] == "Ada Fabrics" and s["step"] == 3
    s = patch({"answers": {"what_you_sell": None, "city": "Enugu"}}).get_json()["session"]
    assert "what_you_sell" not in s["answers"] and s["answers"]["city"] == "Enugu"
    assert s["answers"]["business_name"] == "Ada Fabrics"


def test_photo_upload_rules(client, seller, auth_headers, upload_root, monkeypatch):
    h = auth_headers(seller)
    sid = _session(client, h)["id"]
    res = _upload(client, h, sid, caption="Blue dress", price="15000")
    assert res.status_code == 201
    photo = res.get_json()["photo"]
    assert photo["kind"] == "product" and photo["caption"] == "Blue dress" and photo["price"] == 15000
    assert photo["url"].startswith("/static/uploads/onboarding/")
    assert (upload_root / photo["url"].lstrip("/")).is_file()

    assert _upload(client, h, sid, data=b"not an image at all", name="x.jpg").get_json()["code"] == "NOT_AN_IMAGE"
    assert _upload(client, h, sid, name="x.gif").status_code == 400          # helper: extension
    assert _upload(client, h, sid, price="-5").get_json()["code"] == "INVALID_PRICE"
    res = client.post(f"{API}/sessions/{sid}/photos", headers=h, data={}, content_type="multipart/form-data")
    assert res.get_json()["code"] == "PHOTO_REQUIRED"

    import app.routes.onboarding as routes
    monkeypatch.setattr(routes, "MAX_PHOTO_BYTES", 100)
    assert _upload(client, h, sid).get_json()["code"] == "PHOTO_TOO_LARGE"
    monkeypatch.undo()
    monkeypatch.setattr(client.application, "root_path", str(upload_root))

    for _ in range(4):
        assert _upload(client, h, sid).status_code == 201
    res = _upload(client, h, sid)
    assert res.status_code == 400 and res.get_json()["code"] == "PHOTO_LIMIT"

    # One logo: a second upload replaces the first.
    assert _upload(client, h, sid, kind="logo").status_code == 201
    assert _upload(client, h, sid, kind="logo", data=_jpeg(fmt="PNG"), name="l.png").status_code == 201
    photos = client.get(f"{API}/sessions/{sid}", headers=h).get_json()["session"]["photos"]
    assert sum(p["kind"] == "logo" for p in photos) == 1 and sum(p["kind"] == "product" for p in photos) == 5

    # Delete frees a slot.
    first = photos[0]["id"]
    assert client.delete(f"{API}/sessions/{sid}/photos/{first}", headers=h).status_code == 200
    assert _upload(client, h, sid).status_code == 201


# ── Build: fallback (AI off) ────────────────────────────────────────────────
def test_build_requires_answers(client, seller, auth_headers):
    h = auth_headers(seller)
    sid = _session(client, h, {"business_name": "Ada Fabrics"})["id"]
    res = client.post(f"{API}/sessions/{sid}/build", headers=h)
    assert res.status_code == 400 and res.get_json()["missing"] == ["what_you_sell"]


def test_fallback_build_with_ai_off(client, seller, auth_headers):
    h = auth_headers(seller)
    answers = {**ANSWERS, "about": "We have sewn Ankara for years. 100% genuine fabric, guaranteed."}
    s = _session(client, h, answers)
    _upload(client, h, s["id"], caption="Blue dress", price="15000")
    _upload(client, h, s["id"])
    res = client.post(f"{API}/sessions/{s['id']}/build", headers=h)
    assert res.status_code == 202
    body = res.get_json()
    assert body["job"]["status"] == "done" and body["job"]["result"]["ai_used"] is False

    s = client.get(f"{API}/sessions/{s['id']}", headers=h).get_json()["session"]
    assert s["status"] == "ready_fallback"
    assert all(st["status"] == "done" for st in s["stages"])
    spec = s["draft_spec"]
    assert parse_store_spec(spec)[1] == []
    assert spec["preset"] == "fashion"                       # nearest preset from "Ankara dresses"
    assert spec["hero"]["heading"] == "Ada Fabrics"
    assert spec["hero"]["subtext"] == "Ankara dresses and tops made to measure."
    assert "Based in Enugu, Enugu." in spec["copy"]["aboutDescription"]
    assert "genuine" not in spec["copy"]["aboutDescription"]     # claim sentence dropped
    assert spec["copy"]["contactDescription"] == ANSWERS["delivery"]
    assert spec["extra"]["sections_control"] == "spec"
    text = " ".join(str(v) for g in ("hero", "copy") for v in spec[g].values())
    assert banned_claims(text, json.dumps(ANSWERS)) == []
    assert s["preview_template_options"]["layout_style"] == "Fashion Chic"

    listings = s["draft_listings"]
    assert len(listings) == 2 and all(item["source"] == "fallback" for item in listings)
    assert listings[0]["title"] == "Blue dress" and listings[0]["price"] == 15000
    assert listings[1]["title"] == "" and listings[1]["price"] is None and listings[1]["image_url"]
    assert s["build"]["copy_source"] == "fallback" and "AI_ENABLED" in s["build"]["copy_fallback_reason"]
    assert AIUsage.query.count() == 0


def test_fallback_uses_logo_colour(client, seller, auth_headers):
    h = auth_headers(seller)
    s = _session(client, h, ANSWERS)
    _upload(client, h, s["id"], kind="logo", data=_jpeg(colour=(200, 30, 30)))
    client.post(f"{API}/sessions/{s['id']}/build", headers=h)
    s = client.get(f"{API}/sessions/{s['id']}", headers=h).get_json()["session"]
    theme = s["draft_spec"]["theme"]
    assert theme["paletteId"] == "custom" and theme["primaryColor"].startswith("#c")
    assert next(st for st in s["stages"] if st["key"] == "colours")["source"] == "logo"


def test_nearest_preset():
    assert build.nearest_preset({"what_you_sell": "Small chops and cakes"}) == "food"
    assert build.nearest_preset({"what_you_sell": "Wigs and skincare"}) == "beauty"
    assert build.nearest_preset({"what_you_sell": "Used iPhones and laptops"}) == "tech"
    assert build.nearest_preset({"what_you_sell": "Things"}) == "market"


# ── Build: AI path ──────────────────────────────────────────────────────────
def test_ai_build_success(client, seller, auth_headers, fake_ai):
    ai = fake_ai(store_spec=[GOOD_SPEC], listing_from_photo=[GOOD_LISTING])
    h = auth_headers(seller)
    s = _built(client, h)
    assert s["status"] == "ready"
    assert s["build"]["copy_source"] == "ai"
    assert s["draft_spec"]["hero"]["heading"] == GOOD_SPEC["hero"]["heading"]
    assert s["draft_spec"]["theme"]["paletteId"] == "Monochrome Mono"
    assert [item["source"] for item in s["draft_listings"]] == ["ai", "ai"]
    assert s["draft_listings"][0]["title"] == "Blue Ankara maxi dress"
    assert s["draft_listings"][0]["price"] == 15000        # the seller's price, never the AI's
    assert len(ai.calls_for("store_spec")) == 1 and len(ai.calls_for("listing_from_photo")) == 2

    # Business data only: no phone, and the photo goes as an image block.
    spec_req = ai.calls_for("store_spec")[0]
    assert "08031234567" not in json.dumps(spec_req)
    assert "contact_phone" not in _inputs_of(spec_req)["answers"]
    vision = ai.calls_for("listing_from_photo")[0]["messages"][0]["content"]
    assert vision[0]["type"] == "image" and vision[0]["source"]["type"] == "base64"
    assert AIUsage.query.filter_by(outcome="ok").count() == 3


def test_ai_copy_rejected_by_critic_then_retried(client, seller, auth_headers, fake_ai):
    bad = json.loads(json.dumps(GOOD_SPEC))
    bad["hero"]["subtext"] = "Verified seller with guaranteed quality."
    ai = fake_ai(store_spec=[bad, GOOD_SPEC], listing_from_photo=[GOOD_LISTING])
    s = _built(client, auth_headers(seller), photos=0)
    assert s["status"] == "ready" and s["build"]["copy_source"] == "ai"
    calls = ai.calls_for("store_spec")
    assert len(calls) == 2
    feedback = _inputs_of(calls[1])["critic_feedback"]
    assert any("verified" in p for p in feedback) and any("guaranteed" in p for p in feedback)
    assert "Verified" not in json.dumps(s["draft_spec"])


def test_ai_copy_falls_back_after_two_retries(client, seller, auth_headers, fake_ai):
    bad = json.loads(json.dumps(GOOD_SPEC))
    bad["copy"]["aboutDescription"] = "Serving Lagos since 2010."     # facts not in the answers
    ai = fake_ai(store_spec=[bad], listing_from_photo=[GOOD_LISTING])
    s = _built(client, auth_headers(seller), photos=1)
    assert len(ai.calls_for("store_spec")) == 3
    assert s["status"] == "ready_fallback" and s["build"]["copy_source"] == "fallback"
    assert s["build"]["copy_critic_problems"]
    assert s["draft_spec"]["hero"]["heading"] == "Ada Fabrics"
    assert s["draft_listings"][0]["source"] == "ai"


def test_ai_error_falls_back_without_retry(client, seller, auth_headers, fake_ai):
    ai = fake_ai(store_spec=[RuntimeError("overloaded")], listing_from_photo=[RuntimeError("down")])
    s = _built(client, auth_headers(seller), photos=1)
    assert len(ai.calls_for("store_spec")) == 1 and len(ai.calls_for("listing_from_photo")) == 1
    assert s["status"] == "ready_fallback"
    assert s["draft_listings"][0]["source"] == "fallback"


def test_ai_listing_with_invented_price_falls_back(client, seller, auth_headers, fake_ai):
    priced = {**GOOD_LISTING, "description": "Yours for 9500 naira, original quality."}
    ai = fake_ai(store_spec=[GOOD_SPEC], listing_from_photo=[priced])
    s = _built(client, auth_headers(seller), photos=1, prices=(None,))
    assert len(ai.calls_for("listing_from_photo")) == 3
    assert s["draft_listings"][0]["source"] == "fallback" and s["status"] == "ready_fallback"


def test_daily_build_cap_counts_only_ai_builds(client, seller, auth_headers, fake_ai):
    h = auth_headers(seller)
    s = _built(client, h, photos=0)                       # AI off: free
    assert client.post(f"{API}/sessions/{s['id']}/build", headers=h).status_code == 202
    fake_ai(store_spec=[GOOD_SPEC])
    assert client.post(f"{API}/sessions/{s['id']}/build", headers=h).status_code == 202
    res = client.post(f"{API}/sessions/{s['id']}/build", headers=h)
    assert res.status_code == 429 and res.get_json()["code"] == "BUILD_LIMIT_REACHED"
    # Section rebuilds have their own allowance.
    res = client.post(f"{API}/sessions/{s['id']}/build", headers=h, json={"part": "copy"})
    assert res.status_code == 202


def test_section_rebuild_keeps_listings(client, seller, auth_headers, fake_ai):
    h = auth_headers(seller)
    s = _built(client, h, photos=1)
    listings = s["draft_listings"]
    fake_ai(store_spec=[GOOD_SPEC])
    assert client.post(f"{API}/sessions/{s['id']}/build", headers=h, json={"part": "copy"}).status_code == 202
    s = client.get(f"{API}/sessions/{s['id']}", headers=h).get_json()["session"]
    assert s["build"]["copy_source"] == "ai" and s["draft_listings"] == listings
    assert s["status"] == "ready_fallback"                # listings are still fallback drafts
    assert client.post(f"{API}/sessions/{s['id']}/build", headers=h, json={"part": "logo"}).status_code == 400


def test_part_rebuild_needs_a_draft(client, seller, auth_headers):
    h = auth_headers(seller)
    sid = _session(client, h, ANSWERS)["id"]
    res = client.post(f"{API}/sessions/{sid}/build", headers=h, json={"part": "copy"})
    assert res.status_code == 400 and res.get_json()["code"] == "NO_DRAFT_YET"


def test_stale_build_is_recovered_on_poll(client, seller, auth_headers):
    h = auth_headers(seller)
    sid = _session(client, h, ANSWERS)["id"]
    old = jobs.utcnow() - timedelta(minutes=12)
    job = Job(type=build.JOB_TYPE_BUILD, payload={"session_id": sid, "prev_status": "interview"},
              status="running", attempts=1, started_at=old, created_at=old, user_id=seller.id)
    db.session.add(job)
    db.session.commit()
    session = db.session.get(OnboardingSession, sid)
    session.status, session.job_id = "building", job.id
    db.session.commit()
    s = client.get(f"{API}/sessions/{sid}", headers=h).get_json()["session"]
    assert s["status"] == "interview" and s["job"]["status"] == "failed"
    assert client.post(f"{API}/sessions/{sid}/build", headers=h).status_code == 202


def test_build_already_running_is_409(client, seller, auth_headers):
    h = auth_headers(seller)
    sid = _session(client, h, ANSWERS)["id"]
    job = Job(type=build.JOB_TYPE_BUILD, payload={"session_id": sid}, status="running", attempts=1,
              started_at=jobs.utcnow(), user_id=seller.id)
    db.session.add(job)
    db.session.commit()
    session = db.session.get(OnboardingSession, sid)
    session.status, session.job_id = "building", job.id
    db.session.commit()
    res = client.post(f"{API}/sessions/{sid}/build", headers=h)
    assert res.status_code == 409 and res.get_json()["code"] == "BUILD_IN_PROGRESS"
    assert client.patch(f"{API}/sessions/{sid}", headers=h, json={"step": 2}).status_code == 409


# ── Draft edits ─────────────────────────────────────────────────────────────
def test_draft_edits(client, seller, auth_headers):
    h = auth_headers(seller)
    s = _built(client, h, photos=2)
    sid = s["id"]
    url = f"{API}/sessions/{sid}/draft"
    res = client.patch(url, headers=h, json={"draft_spec": {"hero": {"heading": "Sewn in Enugu"},
                                                            "preset": "luxe-serif"}})
    assert res.status_code == 200
    spec = res.get_json()["session"]["draft_spec"]
    assert spec["hero"]["heading"] == "Sewn in Enugu" and spec["preset"] == "luxe-serif"
    assert spec["copy"]["contactDescription"] == ANSWERS["delivery"]      # merged, not replaced
    bad = client.patch(url, headers=h, json={"draft_spec": {"theme": {"primaryColor": "red"}}})
    assert bad.status_code == 400 and bad.get_json()["code"] == "INVALID_SPEC"

    listings = s["draft_listings"]
    listings[0].update(title="Blue dress", price=15000)
    listings[1].update(title="Red dress", price=20000.5, quantity=3)
    res = client.patch(url, headers=h, json={"draft_listings": listings})
    assert res.status_code == 200
    assert res.get_json()["session"]["draft_listings"][1]["price"] == 20000.5
    for bad_listing in ({"image_url": "https://evil.example/x.jpg"}, {"price": -1}, {"title": "x" * 121},
                        {"photo_id": 99999}, {"attributes": [{"name": "a"}]}):
        res = client.patch(url, headers=h, json={"draft_listings": [{**listings[0], **bad_listing}]})
        assert res.status_code == 400, bad_listing
    assert client.patch(url, headers=h, json={"draft_listings": [listings[0]] * 6}).status_code == 400


def test_draft_edit_needs_ready_build(client, seller, auth_headers):
    h = auth_headers(seller)
    sid = _session(client, h, ANSWERS)["id"]
    res = client.patch(f"{API}/sessions/{sid}/draft", headers=h, json={"draft_listings": []})
    assert res.status_code == 409


def test_chat_edit(client, seller, auth_headers, fake_ai):
    h = auth_headers(seller)
    s = _built(client, h, photos=0)
    url = f"{API}/sessions/{s['id']}/draft"
    res = client.patch(url, headers=h, json={"instruction": "change the headline to Ada's Ankara"})
    assert res.status_code == 422 and res.get_json()["code"] == "AI_EDIT_UNAVAILABLE"   # AI off

    fake_ai(store_edit=[
        {"changes": [{"field": "hero.heading", "value": "Ada's Ankara"}, {"field": "theme.primaryColor",
                                                                           "value": "#123456"}],
         "preset": None, "summary": "Changed the headline."},
        {"changes": [{"field": "hero.subtext", "value": "Verified Ankara, guaranteed."}],
         "preset": None, "summary": "x"},
    ])
    res = client.patch(url, headers=h, json={"instruction": "change the headline to Ada's Ankara"})
    assert res.status_code == 200
    body = res.get_json()
    assert body["session"]["draft_spec"]["hero"]["heading"] == "Ada's Ankara"
    assert body["edit_summary"] == "Changed the headline."
    assert body["session"]["draft_spec"]["theme"] == s["draft_spec"]["theme"]   # theme is not editable here
    res = client.patch(url, headers=h, json={"instruction": "make it sound trustworthy"})
    assert res.status_code == 422 and res.get_json()["code"] == "EDIT_REJECTED"


# ── Publish ─────────────────────────────────────────────────────────────────
def test_publish_refused_before_build(client, seller, auth_headers):
    h = auth_headers(seller)
    sid = _session(client, h, ANSWERS)["id"]
    res = client.post(f"{API}/sessions/{sid}/publish", headers=h, json=BANK)
    assert res.status_code == 409 and res.get_json()["code"] == "BUILD_NOT_READY"
    assert Storefront.query.count() == 0


def test_publish_creates_everything_once(client, seller, auth_headers, paystack):
    h = auth_headers(seller)
    s = _built(client, h, photos=3, prices=(15000, 20000, None))
    listings = s["draft_listings"]
    listings[1]["title"] = "Red dress"
    client.patch(f"{API}/sessions/{s['id']}/draft", headers=h, json={"draft_listings": listings})

    res = client.post(f"{API}/sessions/{s['id']}/publish", headers=h, json=BANK)
    assert res.status_code == 201, res.get_json()
    body = res.get_json()
    assert body["store_url"] == "https://siiqo.com/ada-fabrics"
    assert body["pay_link_url"].startswith("https://siiqo.com/pay/style-1-")
    assert body["share"]["url"] == body["store_url"] and body["share"]["pay_link_url"] == body["pay_link_url"]
    assert body["store_url"] in body["share"]["whatsapp_text"]
    assert body["share"]["whatsapp_url"].startswith("https://wa.me/?text=")
    share_text = body["share"]["whatsapp_text"].replace(body["store_url"], "").replace(body["pay_link_url"], "")
    assert banned_claims(share_text) == []

    store = Storefront.query.one()
    assert store.vendor_id == seller.id and store.store_slug == "ada-fabrics" and store.is_published
    assert store.template_options["layout_style"] == "Fashion Chic"
    assert store.template_options["sections_control"] == "spec"
    assert store.template_options["hero_heading"] == "Ada Fabrics"
    assert (store.bank_code, store.account_number, store.account_name) == ("058", "0123456789", "ADA OKAFOR")
    assert store.city == "Enugu" and store.phone == "08031234567"
    assert store.paystack_subaccount_code == "ACCT_test123"
    assert paystack == [("Ada Fabrics", "058", "0123456789")]

    bank = VendorBankAccount.query.one()
    assert bank.vendor_id == seller.id and bank.account_number == "0123456789" and bank.is_default
    assert bank.paystack_subaccount_code == "ACCT_test123"

    products = Product.query.order_by(Product.id).all()
    assert [p.name for p in products] == ["Style 1", "Red dress"]        # the unpriced listing is skipped
    assert float(products[0].price) == 15000 and products[0].images and products[0].storefront_id == store.id

    link = PaymentLink.query.one()
    assert link.title == "Style 1" and float(link.amount) == 15000 and link.status == "ACTIVE"
    assert link.link_type == "PAY_LINK" and link.product_type == "physical" and link.vendor_id == seller.id
    db.session.refresh(seller)
    assert seller.role == UserRole.VENDOR

    # Again: same answer, nothing new.
    again = client.post(f"{API}/sessions/{s['id']}/publish", headers=h, json=BANK)
    assert again.status_code == 200 and again.get_json() == body
    assert (Storefront.query.count(), Product.query.count(), PaymentLink.query.count(),
            VendorBankAccount.query.count()) == (1, 2, 1, 1)
    assert len(paystack) == 1

    s = client.get(f"{API}/sessions/{s['id']}", headers=h).get_json()["session"]
    assert s["status"] == "published" and s["published_storefront_id"] == store.id
    assert client.get(f"{API}/sessions/current", headers=h).status_code == 404
    assert client.post(f"{API}/sessions", headers=h).get_json()["code"] == "ALREADY_HAS_STORE"
    assert client.patch(f"{API}/sessions/{s['id']}", headers=h, json={"step": 2}).status_code == 409


def test_publish_without_prices_makes_an_open_pay_link(client, seller, auth_headers, paystack):
    h = auth_headers(seller)
    s = _built(client, h, photos=1, prices=(None,))
    res = client.post(f"{API}/sessions/{s['id']}/publish", headers=h, json=BANK)
    assert res.status_code == 201
    link = PaymentLink.query.one()
    assert link.amount is None and link.title == "Pay Ada Fabrics"
    assert Product.query.count() == 0


def test_publish_validation(client, seller, auth_headers, paystack, make_vendor):
    h = auth_headers(seller)
    s = _built(client, h, photos=0)
    url = f"{API}/sessions/{s['id']}/publish"
    res = client.post(url, headers=h, json={**BANK, "account_number": "123"})
    assert res.status_code == 400 and "account_number" in res.get_json()["errors"]

    vendor, store = make_vendor()
    store.store_slug = "ada-fabrics"
    db.session.commit()
    res = client.post(url, headers=h, json=BANK)
    assert res.status_code == 409 and res.get_json()["code"] == "SLUG_UNAVAILABLE"
    assert res.get_json()["suggestions"]

    client.patch(f"{API}/sessions/{s['id']}", headers=h, json={"answers": {"store_slug": "ada-fabrics-enugu"}})
    seller.is_verified = False
    db.session.commit()
    res = client.post(url, headers=h, json=BANK)
    assert res.status_code == 403 and res.get_json()["code"] == "EMAIL_NOT_VERIFIED"
    seller.is_verified = True
    db.session.commit()
    res = client.post(url, headers=h, json=BANK)
    assert res.status_code == 201 and res.get_json()["store_slug"] == "ada-fabrics-enugu"


def test_publish_survives_paystack_failure(client, seller, auth_headers, monkeypatch):
    import app.services.escrow.paystack_provider as provider

    def _down(**kwargs):
        raise RuntimeError("paystack down")
    monkeypatch.setattr(provider, "create_paystack_subaccount", _down)
    h = auth_headers(seller)
    s = _built(client, h, photos=0)
    res = client.post(f"{API}/sessions/{s['id']}/publish", headers=h, json=BANK)
    assert res.status_code == 201
    assert Storefront.query.one().paystack_subaccount_code is None


# ── The old route still works ───────────────────────────────────────────────
def test_vendor_onboard_still_works(client, seller, auth_headers, paystack, monkeypatch):
    import app.routes.vendor as vendor_routes
    monkeypatch.setattr(vendor_routes, "send_siiqo_email", lambda **kw: None)
    res = client.post("/api/vendor/onboard", headers=auth_headers(seller),
                      json={"business_name": "Tunde Gadgets", "category": "phones",
                            "bank_code": "058", "account_number": "0123456789", "account_name": "TUNDE"})
    assert res.status_code == 201, res.get_json()
    assert res.get_json()["store_slug"] == "tunde-gadgets"
    store = Storefront.query.one()
    assert store.template_options["layout_style"] == "Modern Tech" and store.is_published
    assert store.template_options["primary_color"] == "#0A0F1D"
    assert store.paystack_subaccount_code == "ACCT_test123"
    again = client.post("/api/vendor/onboard", headers=auth_headers(seller), json={"business_name": "X"})
    assert again.status_code == 200


# ── Extras ──────────────────────────────────────────────────────────────────
def test_fallback_strips_trust_claims_only():
    text = "Fresh bread daily. Delivery fee depends on your area. 100% genuine, verified baker."
    assert build.claim_free(text) == "Fresh bread daily. Delivery fee depends on your area."


def test_name_check_is_rate_limited(client):
    from app.extensions import limiter
    limiter.enabled = True
    try:
        limiter.reset()
        codes = [client.get(f"{API}/name-check?name=Shop {i}").status_code for i in range(31)]
    finally:
        limiter.reset()
        limiter.enabled = False
    assert codes[:30] == [200] * 30 and codes[30] == 429


def test_build_on_a_background_thread(client, seller, auth_headers, monkeypatch):
    monkeypatch.setenv("JOBS_MODE", "thread")
    h = auth_headers(seller)
    sid = _session(client, h, ANSWERS)["id"]
    _upload(client, h, sid)
    res = client.post(f"{API}/sessions/{sid}/build", headers=h)
    assert res.status_code == 202
    job_id = res.get_json()["job"]["id"]
    for t in threading.enumerate():
        if t.name == f"job-{job_id}":
            t.join(timeout=30)
    db.session.expire_all()
    s = client.get(f"{API}/sessions/{sid}", headers=h).get_json()["session"]
    assert s["status"] == "ready_fallback" and s["job"]["status"] == "done"
    assert len(s["draft_listings"]) == 1


# ── Review fixes: bank name from the bank, digital Pay Link, email, build timeout ──
@pytest.fixture
def bank_resolve(monkeypatch):
    state = {"status": "resolved", "name": "ADAEZE OKAFOR", "calls": []}

    def fake(bank_code, account_number, timeout=10):
        from app.services.bank_resolve import BankResolveResult
        state["calls"].append((bank_code, account_number))
        return BankResolveResult(status=state["status"],
                                 account_name=state["name"] if state["status"] == "resolved" else None,
                                 message="test")
    import app.services.bank_resolve as br
    monkeypatch.setattr(br, "resolve_account", fake)
    return state


def test_publish_saves_the_banks_name_not_the_browsers(client, seller, auth_headers, paystack, bank_resolve):
    h = auth_headers(seller)
    s = _built(client, h)
    res = client.post(f"{API}/sessions/{s['id']}/publish", headers=h,
                      json={**BANK, "account_name": "SOMEONE ELSE"})
    assert res.status_code == 201, res.get_json()
    row = VendorBankAccount.query.filter_by(vendor_id=seller.id).one()
    assert row.account_name == "ADAEZE OKAFOR" and row.is_verified is True
    assert Storefront.query.filter_by(vendor_id=seller.id).one().account_name == "ADAEZE OKAFOR"


def test_publish_refused_when_bank_rejects_account(client, seller, auth_headers, paystack, bank_resolve):
    bank_resolve["status"] = "rejected"
    h = auth_headers(seller)
    s = _built(client, h)
    res = client.post(f"{API}/sessions/{s['id']}/publish", headers=h, json=BANK)
    assert res.status_code == 400 and res.get_json()["code"] == "BANK_NOT_RESOLVED"
    assert Storefront.query.filter_by(vendor_id=seller.id).count() == 0
    assert db.session.get(OnboardingSession, s["id"]).status in OnboardingSession.READY_STATUSES


def test_publish_when_bank_check_unavailable_saves_unverified(client, seller, auth_headers, paystack, bank_resolve):
    bank_resolve["status"] = "unavailable"
    h = auth_headers(seller)
    s = _built(client, h)
    assert client.post(f"{API}/sessions/{s['id']}/publish", headers=h, json=BANK).status_code == 201
    row = VendorBankAccount.query.filter_by(vendor_id=seller.id).one()
    assert row.account_name == BANK["account_name"] and row.is_verified is False


def test_digital_seller_first_pay_link_is_a_service(client, seller, auth_headers, paystack, bank_resolve):
    h = auth_headers(seller)
    s = _built(client, h, answers={**ANSWERS, "product_type": "digital"})
    res = client.post(f"{API}/sessions/{s['id']}/publish", headers=h, json=BANK)
    assert res.status_code == 201, res.get_json()
    link = db.session.get(PaymentLink, res.get_json()["payment_link_id"])
    assert link.product_type == "service" and link.file_url is None


def test_publish_sends_store_live_email(client, seller, auth_headers, paystack, bank_resolve, monkeypatch):
    sent = []
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda **k: sent.append(k) or True)
    h = auth_headers(seller)
    s = _built(client, h)
    body = client.post(f"{API}/sessions/{s['id']}/publish", headers=h, json=BANK).get_json()
    assert len(sent) == 1 and body["store_url"] in sent[0]["message"]
    for word in ("verified", "guarantee", "escrow"):
        assert word not in sent[0]["message"].lower()


def test_onboarding_build_times_out_after_three_minutes():
    job = Job(type="onboarding_build", payload={}, status="running", user_id=None, max_attempts=1)
    db.session.add(job)
    db.session.commit()
    job.started_at = jobs.utcnow() - timedelta(minutes=4)
    db.session.commit()
    other = Job(type="something_else", payload={}, status="running", user_id=None, max_attempts=1)
    db.session.add(other)
    db.session.commit()
    other.started_at = jobs.utcnow() - timedelta(minutes=4)
    db.session.commit()
    assert jobs.is_stale(job) and not jobs.is_stale(other)
    assert jobs.expire_stale() == 1
    db.session.refresh(job)
    db.session.refresh(other)
    assert job.status == "failed" and other.status == "running"


def test_start_is_reserved(client):
    assert client.get(f"{API}/name-check?name=start").get_json()["reason"] == "reserved"


@pytest.mark.parametrize("slug", ["trust.js", "my.store", "Bad Slug", "-dash", "x/y"])
def test_vendor_onboard_rejects_unsafe_store_urls(client, seller, auth_headers, paystack, monkeypatch, slug):
    import app.routes.vendor as vendor_routes
    monkeypatch.setattr(vendor_routes, "send_siiqo_email", lambda **kw: None)
    res = client.post("/api/vendor/onboard", headers=auth_headers(seller),
                      json={"business_name": "Tunde Gadgets", "store_slug": slug})
    assert res.status_code == 400
    assert Storefront.query.count() == 0
