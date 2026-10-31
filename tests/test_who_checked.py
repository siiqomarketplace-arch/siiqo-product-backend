"""'Who checked you' (Phase 3, 3-6a): sellers see how often their business was
checked, by week, channel and identifier type. Nothing about the searcher is
ever stored or shown, and only the owner sees the counts."""
from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db
from app.models.foundations import SellerIdentifier
from app.models.telemetry import PlatformEvent
from app.services import who_checked
from app.services.passport import create_api_client

PHONE = "08031234567"


@pytest.fixture
def seller(make_vendor):
    vendor, store = make_vendor()
    store.store_slug = "mama-ade"
    db.session.add(SellerIdentifier(vendor_id=vendor.id, kind="phone", value="2348031234567", source="test"))
    db.session.add(SellerIdentifier(vendor_id=vendor.id, kind="handle", value="instagram:mamaade", source="test"))
    db.session.commit()
    return vendor, store


ALL_EVENTS = (who_checked.EVENT_NAME, who_checked.PASSPORT_VIEW_EVENT, who_checked.BADGE_VIEW_EVENT)
HUMAN_UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"
VIEW = {"X-Siiqo-Passport-View": "1", "User-Agent": HUMAN_UA}


def _events(vendor_id=None, names=(who_checked.EVENT_NAME,)):
    q = PlatformEvent.query.filter(PlatformEvent.event_name.in_(names))
    if vendor_id is not None:
        q = q.filter_by(business_id=vendor_id)
    return q.all()


def test_check_records_type_only_without_searcher_data(client, seller):
    vendor, _ = seller
    client.get("/api/check", query_string={"q": PHONE},
               headers={"X-Forwarded-For": "203.0.113.9", "CF-Connecting-IP": "203.0.113.9"})
    client.get("/api/check", query_string={"q": "@mamaade"})
    client.get("/api/check", query_string={"q": "nobody-at-all"})
    events = _events(vendor.id)
    assert len(events) == 2
    for e in events:
        assert e.source == "check"
        assert e.user_id is None and e.session_id is None and e.order_id is None
        assert set(e.properties) == {"channel", "matched_on"}
        stored = repr(e.to_dict())
        for private in ("203.0.113.9", PHONE, "2348031234567", "mamaade", "@"):
            assert private not in stored
    assert sorted(e.properties["matched_on"] for e in events) == ["handle", "phone"]


def test_checks_views_and_badges_are_recorded_apart(client, seller, monkeypatch):
    vendor, store = seller
    _, raw = create_api_client("Partner", mode="live")
    db.session.commit()
    auth = {"Authorization": f"Bearer {raw}"}
    assert client.post("/v1/lookup", json={"identifier": PHONE}, headers=auth).status_code == 200
    assert client.get(f"/v1/businesses/{store.store_slug}/trust", headers=auth).status_code == 200
    assert client.get(f"/api/passport/{store.store_slug}", headers=VIEW).status_code == 200
    from app.services import ownership
    monkeypatch.setattr(ownership, "origin_allowed", lambda vendor_id, origin: True)
    res = client.get(f"/v1/widget/{store.store_slug}", query_string={"origin": "https://mamaade.ng"})
    assert res.get_json()["allowed"] is True

    assert sorted(e.source for e in _events(vendor.id)) == ["api", "api"]
    views = _events(vendor.id, (who_checked.PASSPORT_VIEW_EVENT,))
    badges = _events(vendor.id, (who_checked.BADGE_VIEW_EVENT,))
    assert len(views) == 1 and len(badges) == 1
    assert views[0].properties == {"channel": "passport", "matched_on": None}
    assert badges[0].properties == {"channel": "widget", "matched_on": None}


@pytest.mark.parametrize("user_agent", [
    "WhatsApp/2.23.20.0 A",
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Twitterbot/1.0",
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "TelegramBot (like TwitterBot)",
    "LinkedInBot/1.0 (compatible; Mozilla/5.0; Apache-HttpClient +http://www.linkedin.com)",
    "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
])
def test_preview_bots_are_not_passport_views(client, seller, user_agent):
    _, store = seller
    res = client.get(f"/api/passport/{store.store_slug}",
                     headers={"X-Siiqo-Passport-View": "1", "User-Agent": user_agent})
    assert res.status_code == 200
    assert _events(names=ALL_EVENTS) == []


def test_unmarked_passport_fetches_are_not_views(client, seller):
    """Metadata and share-image fetches don't send the view header."""
    _, store = seller
    assert client.get(f"/api/passport/{store.store_slug}", headers={"User-Agent": HUMAN_UA}).status_code == 200
    assert client.get(f"/api/passport/{store.store_slug}/badge.svg").status_code == 200
    assert _events(names=ALL_EVENTS) == []


def test_user_agent_is_never_stored(client, seller):
    _, store = seller
    client.get(f"/api/passport/{store.store_slug}", headers=VIEW)
    (event,) = _events(names=ALL_EVENTS)
    stored = repr(event.to_dict())
    assert "Mozilla" not in stored and "Chrome" not in stored and "Android" not in stored


def test_counts_by_week_and_figure(seller, make_vendor):
    vendor, store = seller
    other, _ = make_vendor()
    now = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)    # a Thursday
    rows = []

    def add(days_ago, channel, kind=None, vid=vendor.id):
        rows.append(who_checked._row(vid, channel, kind, None, now - timedelta(days=days_ago)))

    add(0, "check", "phone"); add(1, "check", "phone"); add(2, "api", "store_slug")    # this week
    add(0, "passport"); add(1, "passport"); add(0, "widget")                           # this week
    add(7, "widget"); add(8, "passport"); add(9, "check", "handle")                    # last week
    add(30, "check", "handle")                                                          # 5 weeks ago
    add(60, "check", "phone"); add(60, "passport")                                      # too old
    add(0, "check", "phone", vid=other.id); add(0, "widget", vid=other.id)              # someone else
    who_checked._insert(rows)

    data = who_checked.for_vendor(vendor.id, now=now)
    assert data["since"] == "2026-09-07" and data["weeks_shown"] == 8
    checks = data["checks"]
    assert len(checks["weeks"]) == 8
    assert checks["weeks"][-1] == {"week_start": "2026-10-26", "count": 3}
    assert checks["weeks"][-2] == {"week_start": "2026-10-19", "count": 1}
    assert checks["weeks"][-5] == {"week_start": "2026-09-28", "count": 1}
    assert checks["total"] == 5 and checks["this_week"] == 3
    assert checks["by_channel"] == [{"channel": "check", "count": 4}, {"channel": "api", "count": 1}]
    assert data["passport_views"]["total"] == 3 and data["passport_views"]["this_week"] == 2
    assert data["badge_views"]["total"] == 2 and data["badge_views"]["this_week"] == 1
    assert data["badge_views"]["weeks"][-2] == {"week_start": "2026-10-19", "count": 1}
    assert data["top_identifiers"] == [{"type": "handle", "count": 2}, {"type": "phone", "count": 2},
                                       {"type": "store_slug", "count": 1}]


def test_insights_endpoint_is_owner_only(client, seller, make_vendor, make_user, auth_headers):
    vendor, _ = seller
    other, _ = make_vendor()
    client.get("/api/check", query_string={"q": PHONE})

    mine = client.get("/api/check/insights", headers=auth_headers(vendor))
    assert mine.status_code == 200
    assert mine.get_json()["checks"]["total"] == 1
    assert mine.get_json()["top_identifiers"] == [{"type": "phone", "count": 1}]

    theirs = client.get("/api/check/insights", headers=auth_headers(other)).get_json()
    assert theirs["checks"]["total"] == 0 and theirs["top_identifiers"] == []
    # No way to name another seller: query parameters are ignored.
    sneaky = client.get(f"/api/check/insights?vendor_id={vendor.id}", headers=auth_headers(other)).get_json()
    assert sneaky["checks"]["total"] == 0

    buyer = make_user()
    assert client.get("/api/check/insights", headers=auth_headers(buyer)).status_code == 404
    assert client.get("/api/check/insights").status_code == 401


def test_insights_body_has_no_searcher_fields(client, seller, auth_headers):
    vendor, _ = seller
    client.get("/api/check", query_string={"q": PHONE}, headers={"X-Forwarded-For": "198.51.100.7"})
    body = client.get("/api/check/insights", headers=auth_headers(vendor)).get_json()
    assert set(body) == {"since", "weeks_shown", "checks", "passport_views", "badge_views", "top_identifiers"}
    text = repr(body)
    for private in ("198.51.100.7", PHONE, "2348031234567", "ip", "user", "session"):
        assert private not in text


def test_recording_never_breaks_a_check(client, seller, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(who_checked, "_row", boom)
    res = client.get("/api/check", query_string={"q": PHONE})
    assert res.status_code == 200 and res.get_json()["found"] is True
    assert client.get("/api/passport/mama-ade", headers=VIEW).status_code == 200
    assert _events(names=ALL_EVENTS) == []
