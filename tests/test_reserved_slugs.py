"""Phase 0B routing: planned siiqo.com/<word> pages can't be claimed as store URLs."""
import pytest

from app.routes.vendor import RESERVED_STORE_SLUGS


@pytest.mark.parametrize("slug", ["market", "search", "ask", "check", "t", "sell", "pay", "developers", "help", "id"])
def test_planned_pages_are_reserved(slug):
    assert slug in RESERVED_STORE_SLUGS


def test_check_slug_reports_reserved(client, make_user, auth_headers):
    user = make_user()
    res = client.get("/api/vendor/check-slug?slug=search", headers=auth_headers(user))
    assert res.get_json()["available"] is False
