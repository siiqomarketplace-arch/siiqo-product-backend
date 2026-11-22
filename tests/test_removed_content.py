"""Siiqo doesn't give grants, loans or funding: the Grants Hub, grant blog
articles and financing partners are removed (410 / 400), while normal blog
articles and other partner types keep working. Data is never deleted."""
import pytest

from app.extensions import db
from app.models.community import Article, ArticleSlugRedirect
from app.models.partnerships import PartnerApplication
from app.models.user import User, UserRole
from app.services.removed_content import is_removed_article, is_removed_slug


def _article(slug, category="Vendor Tips", sub_category=None, published=True, title=None):
    a = Article(title=title or slug.replace("-", " ").title(), slug=slug, content="Body text.",
                category=category, sub_category=sub_category, is_published=published)
    db.session.add(a)
    db.session.commit()
    return a


# ── Grants Hub API: 410 REMOVED everywhere ───────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("get", "/api/grants"),
    ("get", "/api/grants/open"),
    ("get", "/api/grants/featured"),
    ("get", "/api/grants/some-grant-slug"),
    ("get", "/api/grants/category/Women"),
    ("get", "/api/grants/stats"),
    ("post", "/api/grants"),
    ("put", "/api/grants/1"),
    ("delete", "/api/grants/1"),
    ("post", "/api/admin/init-grants-orm"),
    ("post", "/api/admin/fix-published-status"),
    ("get", "/api/admin/debug-grants"),
])
def test_grants_endpoints_answer_410(client, method, path):
    res = getattr(client, method)(path, json={})
    assert res.status_code == 410, (path, res.status_code)
    assert res.get_json()["code"] == "REMOVED"


# ── Grant blog articles ──────────────────────────────────────────────────────

def test_rule_catches_known_slugs():
    for slug in (
        "niya-x-cascador-founders-programme-how-to-apply",
        "how-to-apply-for-the-offchart-nxt-startup-challenge-2026",
        "ecowas-65000-startup-awards-2026-how-to-apply",
        "niya-cascador-founders-programme-how-to-apply",
        "startup-south-pledges-30m-for-startup-challenge-at-offchart-nxt",
        "breaking-nigeria-opens-5-million-grant-portal-for-young-founders-as-niya-cascador-programme-goes-live",
        "niya-cascador-founders-programme-how-to-apply-for-the-fg-s-5m-youth-funding-2026",
    ):
        assert is_removed_slug(slug), slug
    # Whole words only: these are normal articles.
    for slug in ("how-to-get-a-refund", "fragrant-oils-business", "immigrant-traders-in-lagos",
                 "fund-your-wallet", "10-things-every-nigerian-vendor-should-set-up-before-selling-online"):
        assert not is_removed_slug(slug), slug


def test_grant_articles_excluded_from_list_and_normal_ones_kept(client):
    _article("10-things-every-vendor-should-set-up")
    _article("how-to-apply-for-this-programme", category="Grants & Funding")
    _article("ten-open-programmes", category="News", sub_category="open")
    _article("lagos-youth-grant-2026", category="News")

    res = client.get("/api/marketplace/blog?per_page=50")
    assert res.status_code == 200
    slugs = [a["slug"] for a in res.get_json()["articles"]]
    assert slugs == ["10-things-every-vendor-should-set-up"]
    assert res.get_json()["total"] == 1

    # Asking for the old category directly gives nothing either.
    res = client.get("/api/marketplace/blog?category=Grants%20%26%20Funding")
    assert res.get_json()["articles"] == []


def test_grant_article_detail_is_410_and_normal_article_works(client):
    _article("10-things-every-vendor-should-set-up")
    _article("how-to-apply-for-this-programme", category="Grants & Funding")

    ok = client.get("/api/marketplace/blog/10-things-every-vendor-should-set-up")
    assert ok.status_code == 200
    assert ok.get_json()["slug"] == "10-things-every-vendor-should-set-up"

    gone = client.get("/api/marketplace/blog/how-to-apply-for-this-programme")
    assert gone.status_code == 410
    assert gone.get_json()["code"] == "REMOVED"

    # Slug rule alone (no row needed) and the known list.
    assert client.get("/api/marketplace/blog/best-small-business-grants-2026").status_code == 410
    assert client.get("/api/marketplace/blog/niya-x-cascador-founders-programme-how-to-apply").status_code == 410


def test_old_slug_pointing_at_grant_article_is_410_not_redirect(client):
    target = _article("how-to-apply-for-this-programme", category="Grants & Funding")
    db.session.add(ArticleSlugRedirect(article_id=target.id, old_slug="old-programme-guide"))
    db.session.commit()

    res = client.get("/api/marketplace/blog/old-programme-guide")
    assert res.status_code == 410
    assert res.get_json()["code"] == "REMOVED"


def test_removed_slugs_list_for_the_website(client):
    target = _article("how-to-apply-for-this-programme", category="Grants & Funding")
    _article("10-things-every-vendor-should-set-up")
    db.session.add(ArticleSlugRedirect(article_id=target.id, old_slug="old-programme-guide"))
    db.session.commit()

    slugs = client.get("/api/marketplace/blog-removed").get_json()["slugs"]
    assert "how-to-apply-for-this-programme" in slugs
    assert "old-programme-guide" in slugs
    assert "niya-x-cascador-founders-programme-how-to-apply" in slugs
    assert "10-things-every-vendor-should-set-up" not in slugs


def test_grant_rows_are_kept_not_deleted(client):
    a = _article("how-to-apply-for-this-programme", category="Grants & Funding")
    client.get("/api/marketplace/blog/how-to-apply-for-this-programme")
    client.get("/api/marketplace/blog?per_page=50")
    assert db.session.get(Article, a.id) is not None
    assert is_removed_article(a)


# ── Financing partners ───────────────────────────────────────────────────────

def _apply(client, role, email):
    return client.post("/api/partners/apply", json={
        "email": email, "password": "a-test-password-123", "fullname": "Ada Partner",
        "business_name": "Ada Ltd", "partner_role": role,
        "coverage": [{"state": "Lagos", "price": 2000}],
    })


@pytest.mark.parametrize("role", ["FINANCING", "financing"])
def test_new_financing_partner_application_refused(client, role):
    res = _apply(client, role, "fin@example.com")
    assert res.status_code == 400
    assert "financing" in res.get_json()["message"].lower()
    assert User.query.filter_by(email="fin@example.com").first() is None
    assert PartnerApplication.query.count() == 0


def test_logistics_partner_application_still_works(client):
    res = _apply(client, "LOGISTICS", "logi@example.com")
    assert res.status_code == 201
    app_row = PartnerApplication.query.one()
    assert app_row.service_type == "LOGISTICS"


def test_existing_financing_partner_kept_but_not_listed(client, make_user):
    partner = make_user(role=UserRole.PARTNER)
    partner.is_verified = True
    db.session.add(PartnerApplication(user_id=partner.id, business_name="Old Lender Ltd",
                                      status="APPROVED", service_type="FINANCING"))
    db.session.commit()

    res = client.get("/api/partners/active?role=FINANCING")
    assert res.status_code == 200
    assert res.get_json()["data"] == []
    # The record itself is untouched.
    assert PartnerApplication.query.filter_by(service_type="FINANCING").count() == 1
