"""
Product form (Phase 1, 1-3 and 1-9):
  * POST /api/vendor/products/draft-from-photo ("Fill from photo");
  * "Save draft" saves a hidden product (is_active=False) and publishing it
    later runs the payout-account check.

No network: the AI gateway gets a fake Anthropic client.
"""
import io
import json
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from app.extensions import db
from app.models.ai import AIUsage
from app.models.product import Category, Product
from app.models.social import Post
from app.models.user import UserRole
from app.models.withdrawal import VendorBankAccount
from app.services import listing_draft
from app.services import onboarding_build as build

URL = "/api/vendor/products/draft-from-photo"

GOOD = {"title": "Blue Ankara maxi dress", "description": "A long dress in a blue Ankara print.",
        "category": "Fashion", "attributes": [{"name": "Colour", "value": "Blue"}]}


class FakeAI:
    """Anthropic-shaped client answering the listing_from_photo prompt from a script."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self._lock = threading.Lock()
        endpoint = SimpleNamespace(create=self._create)
        self.messages = endpoint
        self.beta = SimpleNamespace(messages=endpoint)

    def _create(self, **request):
        request.pop("betas", None)
        request.pop("extra_body", None)
        assert request["system"][0]["text"].startswith("You draft a product listing")
        with self._lock:
            self.calls.append(request)
            item = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(item, Exception):
            raise item
        usage = SimpleNamespace(input_tokens=500, output_tokens=200, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0, iterations=None)
        return SimpleNamespace(id="msg_test", model="claude-opus-5-5", stop_reason="end_turn",
                               stop_details=None, usage=usage,
                               content=[SimpleNamespace(type="text", text=json.dumps(item))])


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in ("AI_ENABLED", "AI_ENABLED_ACTIONS", "ANTHROPIC_API_KEY", "AI_SELLER_DAILY_ACTION_CAP"):
        monkeypatch.delenv(key, raising=False)
    listing_draft.clear_cache()
    yield
    listing_draft.clear_cache()


@pytest.fixture
def fake_ai(monkeypatch):
    def _install(*answers):
        monkeypatch.setenv("AI_ENABLED", "1")
        client = FakeAI(*answers)
        monkeypatch.setattr(build, "get_ai_client", lambda: client)
        return client
    return _install


@pytest.fixture
def vendor(make_vendor):
    user, store = make_vendor()
    for i, name in enumerate(("Electronics", "Fashion", "Beauty")):
        db.session.add(Category(name=name, slug=name.lower()))
    db.session.commit()
    return user, store


def _jpeg(colour=(30, 90, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (48, 48), colour).save(buf, "JPEG")
    return buf.getvalue()


def _post(client, headers, data=None, name="dress.jpg", **form):
    payload = {"photo": (io.BytesIO(_jpeg() if data is None else data), name), **form}
    return client.post(URL, headers=headers, data=payload, content_type="multipart/form-data")


def _inputs(request):
    text = request["messages"][0]["content"][-1]["text"]
    return json.loads(text.split("<inputs>\n", 1)[1].rsplit("\n</inputs>", 1)[0])


# ── Fill from photo ─────────────────────────────────────────────────────────
def test_ai_off_returns_no_draft(client, auth_headers, vendor):
    res = _post(client, auth_headers(vendor[0]))
    assert res.status_code == 200
    assert res.get_json() == {"draft": None, "reason": "ai_off", "cached": False}
    assert AIUsage.query.count() == 0


def test_ai_success_prefills_without_price(client, auth_headers, vendor, fake_ai):
    ai = fake_ai(GOOD)
    res = _post(client, auth_headers(vendor[0]), note="blue dress")
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["reason"] is None and body["cached"] is False
    assert body["draft"] == {**GOOD, "source": "ai"}
    assert "price" not in body["draft"]

    assert len(ai.calls) == 1
    content = ai.calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    sent = _inputs(ai.calls[0])
    assert sent["categories"] == ["Beauty", "Electronics", "Fashion"]
    assert sent["seller_note"] == "blue dress"
    assert sent["business_name"] == vendor[1].store_name
    assert AIUsage.query.filter_by(action="listing_from_photo", outcome="ok").count() == 1


def test_category_not_ours_is_dropped_by_critic_retry(client, auth_headers, vendor, fake_ai):
    ai = fake_ai({**GOOD, "category": "Ankara"}, GOOD)
    body = _post(client, auth_headers(vendor[0])).get_json()
    assert len(ai.calls) == 2   # the critic's notes went back once
    assert body["draft"]["category"] == "Fashion"


def test_critic_rejection_gives_no_draft(client, auth_headers, vendor, fake_ai):
    claim = {**GOOD, "description": "A genuine designer dress, guaranteed original."}
    ai = fake_ai(claim)
    body = _post(client, auth_headers(vendor[0])).get_json()
    assert body == {"draft": None, "reason": "rejected", "cached": False}
    assert len(ai.calls) == 1 + listing_draft.RETRIES


def test_provider_failure_gives_no_draft(client, auth_headers, vendor, fake_ai):
    fake_ai(RuntimeError("down"))
    body = _post(client, auth_headers(vendor[0])).get_json()
    assert body == {"draft": None, "reason": "unavailable", "cached": False}


def test_seller_cap_gives_no_draft(client, auth_headers, vendor, fake_ai, monkeypatch):
    ai = fake_ai(GOOD)
    monkeypatch.setenv("AI_SELLER_DAILY_ACTION_CAP", "0")
    body = _post(client, auth_headers(vendor[0])).get_json()
    assert body == {"draft": None, "reason": "capped", "cached": False}
    assert ai.calls == []


def test_same_photo_is_served_from_cache(client, auth_headers, vendor, fake_ai):
    ai = fake_ai(GOOD)
    headers = auth_headers(vendor[0])
    first = _post(client, headers).get_json()
    second = _post(client, headers, name="again.jpg").get_json()
    assert len(ai.calls) == 1
    assert second["cached"] is True and second["draft"] == first["draft"]
    # another photo is a new call
    _post(client, headers, data=_jpeg((200, 30, 30)))
    assert len(ai.calls) == 2


def test_failures_are_not_cached(client, auth_headers, vendor, fake_ai):
    headers = auth_headers(vendor[0])
    assert _post(client, headers).get_json()["reason"] == "ai_off"
    ai = fake_ai(GOOD)
    body = _post(client, headers).get_json()
    assert body["draft"] is not None and body["cached"] is False and len(ai.calls) == 1


def test_non_vendor_gets_403(client, auth_headers, make_user):
    buyer = make_user(role=UserRole.BUYER)
    assert _post(client, auth_headers(buyer)).status_code == 403


def test_bad_files_are_refused(client, auth_headers, vendor, fake_ai, monkeypatch):
    ai = fake_ai(GOOD)
    headers = auth_headers(vendor[0])
    res = _post(client, headers, data=b"<?php echo 1; ?>" * 4, name="x.jpg")
    assert res.status_code == 400 and res.get_json()["code"] == "NOT_AN_IMAGE"
    res = _post(client, headers, name="dress.exe")
    assert res.status_code == 400 and res.get_json()["code"] == "NOT_AN_IMAGE"
    monkeypatch.setattr("app.utils.upload.PHOTO_MAX_BYTES", 100)
    res = _post(client, headers)
    assert res.status_code == 400 and res.get_json()["code"] == "PHOTO_TOO_LARGE"
    res = client.post(URL, headers=headers, data={}, content_type="multipart/form-data")
    assert res.status_code == 400 and res.get_json()["code"] == "PHOTO_REQUIRED"
    assert ai.calls == []


# ── Save draft ──────────────────────────────────────────────────────────────
def _add(client, headers, **form):
    data = {"name": "Blue dress", "description": "", "category": "Fashion", **form}
    return client.post("/api/vendor/products/add", headers=headers, data=data,
                       content_type="multipart/form-data")


def test_save_draft_saves_a_hidden_product_without_bank(client, auth_headers, vendor):
    user, store = vendor
    res = _add(client, auth_headers(user), status="draft", is_published="false")
    assert res.status_code == 201, res.get_json()
    body = res.get_json()
    assert body["is_draft"] is True and body["is_active"] is False
    product = db.session.get(Product, body["id"])
    assert product.is_active is False and product.is_free is False
    assert product.category.name == "Fashion"
    assert Post.query.count() == 0   # no community launch post for a draft


def test_publish_still_needs_a_bank_account(client, auth_headers, vendor):
    res = _add(client, auth_headers(vendor[0]), price="5000")
    assert res.status_code == 400 and res.get_json()["code"] == "BANK_ACCOUNT_REQUIRED"


def test_publishing_a_draft_checks_the_bank_account(client, auth_headers, vendor):
    user, store = vendor
    headers = auth_headers(user)
    pid = _add(client, headers, status="draft").get_json()["id"]

    res = client.patch(f"/api/vendor/products/update/{pid}", headers=headers,
                       data={"status": "active", "price": "5000", "description": "Long dress"},
                       content_type="multipart/form-data")
    assert res.status_code == 400 and res.get_json()["code"] == "BANK_ACCOUNT_REQUIRED"
    db.session.expire_all()
    assert db.session.get(Product, pid).is_active is False

    db.session.add(VendorBankAccount(vendor_id=user.id, bank_name="Bank", bank_code="058",
                                     account_number="0123456789", account_name="TEST USER"))
    db.session.commit()
    res = client.patch(f"/api/vendor/products/update/{pid}", headers=headers,
                       data={"status": "active", "price": "5000", "description": "Long dress",
                             "category": "Beauty", "is_free": "false"},
                       content_type="multipart/form-data")
    assert res.status_code == 200, res.get_json()
    db.session.expire_all()
    product = db.session.get(Product, pid)
    assert product.is_active is True and float(product.price) == 5000 and product.is_free is False
    assert product.category.name == "Beauty"


def test_digital_draft_can_wait_for_its_file(client, auth_headers, vendor):
    user, store = vendor
    headers = auth_headers(user)
    res = _add(client, headers, status="draft", product_type="digital")
    assert res.status_code == 201, res.get_json()
    db.session.add(VendorBankAccount(vendor_id=user.id, bank_name="Bank", bank_code="058",
                                     account_number="0123456789", account_name="TEST USER"))
    db.session.commit()
    pid = res.get_json()["id"]
    res = client.patch(f"/api/vendor/products/update/{pid}", headers=headers,
                       data={"status": "active", "price": "2000"}, content_type="multipart/form-data")
    assert res.status_code == 400   # going live still needs the download link
    res = _add(client, headers, name="Ebook", price="2000", product_type="digital")
    assert res.status_code == 400
