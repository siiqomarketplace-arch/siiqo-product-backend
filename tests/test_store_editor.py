"""
Store editor (Phase 1 items 1-6, 1-7, 1-8): revisions, revert, AI edit and
"Refresh my store with AI". No network: the AI gateway gets a fake client.
"""
import json
import threading
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models.product import Category
from app.models.store_revision import StoreRevision
from app.models.user import Storefront
from app.services import onboarding_build as build
from app.services import store_editor
from app.services.onboarding_critic import check_spec
from app.services.store_spec import (
    AI_SECTION_IDS,
    SECTION_IDS,
    parse_store_spec,
    spec_from_template_options,
    template_options_from_spec,
)

API = "/api/vendor"

OLD_TPL = {
    "layout_style": "Fashion Chic",
    "primaryColor": "#000000",
    "primary_color": "#000000",
    "hero_heading": "Ada Fabrics",
    "hero_subtext": "Ankara dresses made to measure.",
    "about_description": "We sew Ankara dresses in Enugu.",
    "sections": ["hero", "products", "about"],
    "template_specific_copy": {"x": "kept"},
}

AI_SPEC = {
    "preset": "fashion",
    "sections": ["hero", "products", "about", "contact"],
    "hero": {"heading": "Ankara dresses made to measure", "subtext": "Dresses sewn to your size.",
             "cta": "See the dresses"},
    "copy": {"tagline": "Ankara made to measure", "aboutTitle": "About Ada Fabrics",
             "aboutDescription": "We sew Ankara dresses in Enugu.", "productsHeading": "Our dresses",
             "productsSubheading": "Pick a style.", "contactHeading": "Get in touch",
             "contactDescription": "Send us a message to order."},
}
BAD_SPEC = {**AI_SPEC, "hero": {**AI_SPEC["hero"], "subtext": "Verified Ankara, guaranteed."}}


# ── Fake Anthropic client (same shape as tests/test_onboarding.py) ──────────
def _response(obj):
    usage = SimpleNamespace(input_tokens=500, output_tokens=200, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, iterations=None)
    return SimpleNamespace(id="msg_test", model="claude-opus-5-5", stop_reason="end_turn", stop_details=None,
                           content=[SimpleNamespace(type="text", text=json.dumps(obj))], usage=usage)


def _action_of(request):
    system = request["system"][0]["text"]
    if system.startswith("You draft the first version"):
        return "store_spec"
    if system.startswith("You apply one edit request"):
        return "store_edit"
    raise AssertionError("unknown prompt")


class FakeAI:
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

    def inputs(self, action):
        out = []
        for a, r in self.calls:
            if a == action:
                content = r["messages"][0]["content"]
                text = content if isinstance(content, str) else content[-1]["text"]
                out.append(json.loads(text.split("<inputs>\n", 1)[1].rsplit("\n</inputs>", 1)[0]))
        return out


@pytest.fixture(autouse=True)
def _no_ai_env(monkeypatch):
    for key in ("AI_ENABLED", "AI_ENABLED_ACTIONS", "ANTHROPIC_API_KEY", "AI_SELLER_DAILY_ACTION_CAP"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_ai(monkeypatch):
    def _install(**script):
        monkeypatch.setenv("AI_ENABLED", "1")
        client = FakeAI(**script)
        monkeypatch.setattr(build, "get_ai_client", lambda: client)
        return client
    return _install


@pytest.fixture
def seller(make_vendor, make_product):
    vendor, store = make_vendor()
    store.store_name = "Ada Fabrics"
    store.store_description = "Ankara dresses and tops made to measure in Enugu."
    store.city = "Enugu"
    store.template_options = dict(OLD_TPL)
    cat = Category(name="Fashion", slug="fashion")
    db.session.add(cat)
    db.session.commit()
    product = make_product(store, category_id=cat.id)
    product.name = "Blue Ankara maxi dress"
    db.session.commit()
    return vendor, store


def _save(client, h, tpl, source="editor"):
    body = {"template_options": json.dumps(tpl)}
    if source:
        body["revision_source"] = source
    return client.patch(f"{API}/update-settings", headers=h, json=body)


def _tpl(store_id):
    db.session.expire_all()
    return db.session.get(Storefront, store_id).template_options


# ── Store Spec: the passport section ────────────────────────────────────────
def test_passport_is_a_section_id_but_not_offered_to_the_ai():
    assert "passport" in SECTION_IDS and "passport" not in AI_SECTION_IDS
    spec, errors = parse_store_spec({"version": 1, "preset": "tech",
                                     "sections": ["hero", "passport", "products", "contact"]})
    assert errors == [] and spec["sections"] == ["hero", "passport", "products", "contact"]
    _, errors = parse_store_spec({"version": 1, "preset": "tech", "sections": list(SECTION_IDS)})
    assert errors == []
    _, errors = parse_store_spec({"version": 1, "preset": "tech", "sections": list(SECTION_IDS) + ["hero"]})
    assert errors and "at most 9" in errors[0]
    tpl = {"layout_style": "Modern Tech", "sections": ["hero", "passport"], "sections_control": "spec"}
    spec = spec_from_template_options(tpl)
    assert spec["sections"] == ["hero", "passport"] and spec["extra"] == {"sections_control": "spec"}
    assert template_options_from_spec(spec)["sections"] == ["hero", "passport"]


# ── Revisions and revert ────────────────────────────────────────────────────
def test_editor_save_keeps_a_revision_first(client, seller, auth_headers):
    vendor, store = seller
    h = auth_headers(vendor)
    new = {**OLD_TPL, "hero_heading": "New heading", "sections_control": "spec"}
    assert _save(client, h, new).status_code == 200
    assert _tpl(store.id)["hero_heading"] == "New heading"
    revs = client.get(f"{API}/storefront/revisions", headers=h).get_json()["revisions"]
    assert len(revs) == 1
    assert revs[0]["source"] == "editor" and revs[0]["created_by"] == vendor.id
    assert revs[0]["template_options"] == OLD_TPL


def test_plain_settings_save_keeps_no_revision(client, seller, auth_headers):
    vendor, store = seller
    h = auth_headers(vendor)
    assert _save(client, h, {**OLD_TPL, "primaryColor": "#123456"}, source=None).status_code == 200
    assert StoreRevision.query.count() == 0


def test_revision_source_is_checked(client, seller, auth_headers):
    vendor, store = seller
    h = auth_headers(vendor)
    assert _save(client, h, OLD_TPL, source="revert").status_code == 400
    res = client.patch(f"{API}/update-settings", headers=h, json={"revision_source": "editor"})
    assert res.status_code == 400
    assert StoreRevision.query.count() == 0


def test_revisions_list_the_last_ten_newest_first(client, seller, auth_headers):
    vendor, store = seller
    h = auth_headers(vendor)
    for i in range(12):
        assert _save(client, h, {**OLD_TPL, "hero_heading": f"Heading {i}"}).status_code == 200
    revs = client.get(f"{API}/storefront/revisions", headers=h).get_json()["revisions"]
    assert len(revs) == 10
    assert revs[0]["template_options"]["hero_heading"] == "Heading 10"   # state before the 12th save
    assert revs[-1]["template_options"]["hero_heading"] == "Heading 1"
    assert StoreRevision.query.filter_by(storefront_id=store.id).count() == 10


def test_revert_saves_current_state_then_restores(client, seller, auth_headers):
    vendor, store = seller
    h = auth_headers(vendor)
    new = {**OLD_TPL, "hero_heading": "New heading", "sections_control": "spec"}
    _save(client, h, new)
    rev_id = client.get(f"{API}/storefront/revisions", headers=h).get_json()["revisions"][0]["id"]
    res = client.post(f"{API}/storefront/revert/{rev_id}", headers=h)
    assert res.status_code == 200 and res.get_json()["template_options"] == OLD_TPL
    assert _tpl(store.id) == OLD_TPL
    revs = client.get(f"{API}/storefront/revisions", headers=h).get_json()["revisions"]
    assert [r["source"] for r in revs] == ["revert", "editor"]
    assert revs[0]["template_options"] == new          # the revert can itself be undone
    res = client.post(f"{API}/storefront/revert/{revs[0]['id']}", headers=h)
    assert res.status_code == 200 and _tpl(store.id) == new


def test_other_sellers_revision_is_404(client, seller, make_vendor, auth_headers):
    vendor, store = seller
    other, other_store = make_vendor()
    other_store.template_options = {"layout_style": "Modern Tech"}
    db.session.commit()
    _save(client, auth_headers(other), {"layout_style": "Photography"})
    other_rev = StoreRevision.query.filter_by(storefront_id=other_store.id).one()
    h = auth_headers(vendor)
    assert client.post(f"{API}/storefront/revert/{other_rev.id}", headers=h).status_code == 404
    assert client.post(f"{API}/storefront/revert/999999", headers=h).status_code == 404
    assert client.get(f"{API}/storefront/revisions", headers=h).get_json()["revisions"] == []
    assert _tpl(other_store.id) == {"layout_style": "Photography"}
    assert _tpl(store.id) == OLD_TPL


def test_buyers_cannot_use_the_editor_endpoints(client, make_user, auth_headers):
    h = auth_headers(make_user())
    assert client.get(f"{API}/storefront/revisions", headers=h).status_code == 403
    assert client.post(f"{API}/storefront/ai-refresh", headers=h).status_code == 403
    assert client.post(f"{API}/storefront/revert/1", headers=h).status_code == 403


# ── AI edit ─────────────────────────────────────────────────────────────────
def _spec_body(instruction="change the headline to Ada's Ankara"):
    spec = spec_from_template_options({**OLD_TPL, "sections_control": "spec"})
    return {"spec": spec, "instruction": instruction}


def test_ai_edit_with_ai_off_returns_no_proposal(client, seller, auth_headers):
    vendor, store = seller
    res = client.post(f"{API}/storefront/ai-edit", headers=auth_headers(vendor), json=_spec_body())
    assert res.status_code == 200
    body = res.get_json()
    assert body["proposal"] is None and body["reason"] == "ai_unavailable"


def test_ai_edit_validates_input(client, seller, auth_headers):
    vendor, store = seller
    h = auth_headers(vendor)
    res = client.post(f"{API}/storefront/ai-edit", headers=h, json={"spec": {"version": 2}, "instruction": "x"})
    assert res.status_code == 400 and res.get_json()["code"] == "INVALID_SPEC"
    res = client.post(f"{API}/storefront/ai-edit", headers=h, json={**_spec_body(), "instruction": " "})
    assert res.status_code == 400 and res.get_json()["code"] == "INVALID_INSTRUCTION"


def test_ai_edit_proposes_without_saving(client, seller, auth_headers, fake_ai):
    vendor, store = seller
    ai = fake_ai(store_edit=[{"changes": [{"field": "hero.heading", "value": "Ada's Ankara"},
                                          {"field": "theme.primaryColor", "value": "#123456"}],
                              "preset": None, "summary": "Changed the headline."}])
    res = client.post(f"{API}/storefront/ai-edit", headers=auth_headers(vendor), json=_spec_body())
    assert res.status_code == 200
    body = res.get_json()
    assert body["proposal"]["hero"]["heading"] == "Ada's Ankara"
    assert body["proposal"]["theme"]["primaryColor"] == "#000000"        # theme is not AI-editable
    assert body["proposal"]["extra"]["sections_control"] == "spec"
    assert body["summary"] == "Changed the headline."
    assert _tpl(store.id) == OLD_TPL and StoreRevision.query.count() == 0
    sent = ai.inputs("store_edit")[0]
    assert sent["answers"]["business_name"] == "Ada Fabrics"
    assert "Blue Ankara maxi dress" in sent["answers"]["product_names"]


def test_ai_edit_rejected_by_critic(client, seller, auth_headers, fake_ai):
    vendor, store = seller
    fake_ai(store_edit=[{"changes": [{"field": "hero.subtext", "value": "Verified Ankara, guaranteed."}],
                         "preset": None, "summary": "x"}])
    res = client.post(f"{API}/storefront/ai-edit", headers=auth_headers(vendor),
                      json=_spec_body("make it sound trustworthy"))
    body = res.get_json()
    assert res.status_code == 200 and body["proposal"] is None and body["reason"] == "rejected"
    assert any("verified" in p for p in body["problems"])
    assert _tpl(store.id) == OLD_TPL


def test_ai_edit_ignores_problems_in_untouched_seller_text(client, seller, auth_headers, fake_ai):
    """The seller's store has no contact section and old text with a claim;
    an AI change to another field is still allowed."""
    vendor, store = seller
    body = _spec_body("shorten the about text")
    body["spec"]["copy"]["tagline"] = "100% genuine fabrics"
    fake_ai(store_edit=[{"changes": [{"field": "copy.aboutDescription", "value": "Ankara dresses from Enugu."}],
                         "preset": None, "summary": "Shortened."}])
    res = client.post(f"{API}/storefront/ai-edit", headers=auth_headers(vendor), json=body)
    out = res.get_json()
    assert out["proposal"]["copy"]["aboutDescription"] == "Ankara dresses from Enugu."
    assert out["proposal"]["sections"] == ["hero", "products", "about"]


# ── AI refresh ──────────────────────────────────────────────────────────────
def test_refresh_with_ai_off_uses_the_sellers_words(client, seller, auth_headers):
    vendor, store = seller
    res = client.post(f"{API}/storefront/ai-refresh", headers=auth_headers(vendor))
    assert res.status_code == 200
    body = res.get_json()
    assert body["source"] == "fallback" and body["reason"].startswith("AIDisabled")
    spec = body["proposal"]
    assert parse_store_spec(spec)[1] == []
    assert spec["preset"] == "fashion"                                   # nearest preset to Ankara/fashion
    assert spec["hero"]["heading"] == "Ada Fabrics"
    assert "Ankara dresses and tops" in spec["hero"]["subtext"]
    assert spec["extra"]["sections_control"] == "spec"
    assert spec["extra"]["template_specific_copy"] == {"x": "kept"}      # unmodelled options kept
    assert spec["sections"][-2:] == ["passport", "contact"]
    assert _tpl(store.id) == OLD_TPL and StoreRevision.query.count() == 0


def test_refresh_with_ai(client, seller, auth_headers, fake_ai):
    vendor, store = seller
    ai = fake_ai(store_spec=[AI_SPEC])
    body = client.post(f"{API}/storefront/ai-refresh", headers=auth_headers(vendor)).get_json()
    assert body["source"] == "ai" and body["reason"] is None
    spec = body["proposal"]
    assert spec["hero"]["heading"] == "Ankara dresses made to measure"
    assert spec["sections"] == ["hero", "products", "about", "passport", "contact"]
    assert check_spec(spec, store_editor.facts_as_answers(store_editor.store_facts(store))) == []
    sent = ai.inputs("store_spec")[0]
    assert "passport" not in sent["allowed_sections"]
    assert sent["answers"]["business_name"] == "Ada Fabrics"
    assert _tpl(store.id) == OLD_TPL


def test_refresh_critic_rejection_falls_back(client, seller, auth_headers, fake_ai):
    vendor, store = seller
    ai = fake_ai(store_spec=[BAD_SPEC])
    body = client.post(f"{API}/storefront/ai-refresh", headers=auth_headers(vendor)).get_json()
    assert body["source"] == "fallback" and body["reason"] == "critic rejected the draft"
    assert any("verified" in p for p in body["problems"])
    assert len(ai.inputs("store_spec")) == 3                              # 1 + 2 retries
    assert "Verified" not in json.dumps(body["proposal"])


def test_use_this_saves_with_an_ai_refresh_revision(client, seller, auth_headers, fake_ai):
    vendor, store = seller
    h = auth_headers(vendor)
    fake_ai(store_spec=[AI_SPEC])
    spec = client.post(f"{API}/storefront/ai-refresh", headers=h).get_json()["proposal"]
    tpl = template_options_from_spec(spec)
    assert _save(client, h, tpl, source="ai_refresh").status_code == 200
    assert _tpl(store.id)["sections_control"] == "spec"
    rev = client.get(f"{API}/storefront/revisions", headers=h).get_json()["revisions"][0]
    assert rev["source"] == "ai_refresh" and rev["template_options"] == OLD_TPL
    # Undo
    assert client.post(f"{API}/storefront/revert/{rev['id']}", headers=h).status_code == 200
    assert _tpl(store.id) == OLD_TPL
