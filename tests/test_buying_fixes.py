"""Make buying easy, and measure it (docs/BUYING_FIX_PLAN.md): store places,
buying funnel events and attribution, honest trust on product pages, empty
stores out of the Market, server-priced delivery and guest questions."""
from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db, limiter
from app.models.communication import Notification
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.onboarding import OnboardingSession
from app.models.order import Order
from app.models.partnerships import PartnerApplication
from app.models.product import Product
from app.models.telemetry import PlatformEvent
from app.models.user import UserRole
from app.services import places
from app.services.attribution import clean_attribution, order_source
from app.services.metrics import buying_metrics


def _events(name, **filters):
    return PlatformEvent.query.filter_by(event_name=f"funnel.{name}", **filters).all()


def _month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ── 1. Places ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("Wuse 2, Abuja", ("Abuja", "FCT")),
    ("Abuja", ("Abuja", "FCT")),
    ("FCT", ("Abuja", "FCT")),
    ("Ikeja Lagos", ("Ikeja", "Lagos")),
    ("Ikeja", ("Ikeja", "Lagos")),
    ("lagos", (None, "Lagos")),
    ("Enugu State", (None, "Enugu")),
    ("Benin City, Edo State, Nigeria", ("Benin City", "Edo")),
    ("Uyo, Akwa Ibom", ("Uyo", "Akwa Ibom")),
    ("Port Harcourt", ("Port Harcourt", "Rivers")),
    ("Somewhere Nice", ("Somewhere Nice", None)),
    ("", (None, None)),
    (None, (None, None)),
])
def test_split_place(text, expected):
    assert places.split_place(text) == expected


def test_split_place_trims_long_text():
    city, state = places.split_place("x" * 300)
    assert state is None and len(city) == 100


def test_place_label():
    assert places.place_label("Ikeja", "Lagos") == "Ikeja, Lagos"
    assert places.place_label(None, "Lagos") == "Lagos"
    assert places.place_label(None, None) is None


def _onboarding(user, answers, **kw):
    s = OnboardingSession(user_id=user.id, status="published", answers=answers, **kw)
    db.session.add(s)
    db.session.commit()
    return s


def test_backfill_fills_blank_places_only(make_vendor, make_product):
    vendor, store = make_vendor()
    _onboarding(vendor, {"location": "Wuse 2, Abuja"})
    phys = make_product(store)
    digital = make_product(store, product_type="digital")
    named = make_product(store, location="Garki")

    other, kept = make_vendor()                      # already has a place: untouched
    kept.city, kept.state = "Kano", "Kano"
    db.session.commit()
    _onboarding(other, {"location": "Ikeja Lagos"})
    kept_product = make_product(kept)

    stats = places.backfill_store_places()
    db.session.expire_all()
    assert (store.city, store.state) == ("Abuja", "FCT")
    assert (kept.city, kept.state) == ("Kano", "Kano")
    assert db.session.get(Product, phys.id).location == "Abuja, FCT"
    assert db.session.get(Product, digital.id).location is None
    assert db.session.get(Product, named.id).location == "Garki"
    assert db.session.get(Product, kept_product.id).location == "Kano"
    assert stats == {"stores": 1, "products": 2}
    assert places.backfill_store_places() == {"stores": 0, "products": 0}   # idempotent


def test_backfill_uses_latest_session_with_a_place(make_vendor):
    vendor, store = make_vendor()
    now = datetime.now(timezone.utc)
    _onboarding(vendor, {"location": "Ikeja"}, created_at=now - timedelta(days=2))
    _onboarding(vendor, {"city": "Owerri", "state": "Imo"}, created_at=now - timedelta(days=1))
    _onboarding(vendor, {"business_name": "No place here"}, created_at=now)
    places.backfill_store_places()
    db.session.expire_all()
    assert (store.city, store.state) == ("Owerri", "Imo")


def test_new_physical_product_gets_store_place(client, make_vendor, auth_headers):
    vendor, store = make_vendor()
    store.city, store.state = "Ikeja", "Lagos"
    db.session.commit()
    res = client.post("/api/vendor/products/add", headers=auth_headers(vendor),
                      data={"name": "Blue dress", "description": "", "status": "draft"},
                      content_type="multipart/form-data")
    assert res.status_code == 201, res.get_json()
    assert db.session.get(Product, res.get_json()["id"]).location == "Ikeja, Lagos"

    res = client.post("/api/vendor/products/add", headers=auth_headers(vendor),
                      data={"name": "Red dress", "description": "", "status": "draft", "location": "Yaba"},
                      content_type="multipart/form-data")
    assert db.session.get(Product, res.get_json()["id"]).location == "Yaba"


def test_edited_physical_product_without_place_gets_store_place(client, make_vendor, make_product, auth_headers):
    vendor, store = make_vendor()
    store.city, store.state = "Abuja", "FCT"
    db.session.commit()
    p = make_product(store)
    res = client.patch(f"/api/vendor/products/update/{p.id}", headers=auth_headers(vendor),
                       json={"description": "Now longer"})
    assert res.status_code == 200, res.get_json()
    db.session.expire_all()
    assert db.session.get(Product, p.id).location == "Abuja, FCT"


# ── 2. Attribution and funnel events ────────────────────────────────────────
def test_clean_attribution():
    a = clean_attribution({"source": " Instagram Ads! ", "campaign": "Abuja/Fashion-Nov",
                           "medium": "x" * 90, "ref": 5, "landing": "/market?utm_source=x", "evil": "y"})
    assert a["source"] == "instagram_ads" and a["campaign"] == "abujafashion-nov"
    assert len(a["medium"]) == 40 and "ref" not in a and "evil" not in a
    assert order_source({"landing": "/market/fashion"}) == "market"
    assert order_source({"landing": "https://siiqo.com/store/ada"}) == "store"
    assert order_source({}) == "direct"
    assert order_source({}, pay_link=True) == "pay_link"
    assert order_source({"source": "test"}, pay_link=True) == "test"
    assert order_source({}, {"X-Siiqo-Client": "WhatsApp"}) == "whatsapp"
    assert order_source({}, {"X-Siiqo-Client": "other"}) == "direct"


def _checkout(client, product, headers=None, **extra):
    body = {"items": [{"product_id": product.id, "quantity": 1}], "payment_method": "CRYPTO",
            "email": "buyer@example.com", "delivery_name": "Ada", "delivery_phone": "08031234567",
            "delivery_address": "1 Road", "delivery_city": "Ikeja", "delivery_state": "Lagos", **extra}
    return client.post("/api/cart/checkout", json=body, headers=headers or {})


def test_checkout_records_source_campaign_and_order_created(client, make_vendor, make_product):
    vendor, store = make_vendor()
    p = make_product(store, price=5000)
    res = _checkout(client, p, attribution={"source": "Test", "campaign": "Nov Launch", "landing": "/market"},
                    session_id="sess-1")
    assert res.status_code == 200, res.get_json()
    order = db.session.get(Order, res.get_json()["id"])
    assert (order.source, order.campaign) == ("test", "nov_launch")
    ev = _events("order_created", order_id=order.id)
    assert len(ev) == 1 and ev[0].session_id == "sess-1"
    assert ev[0].properties["kind"] == "cart" and ev[0].properties["source"] == "test"
    assert ev[0].properties["campaign"] == "nov_launch" and ev[0].properties["amount"] == 5000.0
    assert ev[0].properties["payment_method"] == "CRYPTO"


@pytest.mark.parametrize("headers,attribution,expected", [
    ({"X-Siiqo-Client": "telegram"}, None, "telegram"),
    ({"X-Siiqo-Client": "whatsapp"}, {"source": "whatsapp"}, "whatsapp"),
    ({}, {"landing": "/store/ada"}, "store"),
    ({}, None, "direct"),
])
def test_checkout_source_fallbacks(client, make_vendor, make_product, headers, attribution, expected):
    vendor, store = make_vendor()
    p = make_product(store)
    extra = {"attribution": attribution} if attribution else {}
    res = _checkout(client, p, headers=headers, **extra)
    assert db.session.get(Order, res.get_json()["id"]).source == expected


def test_orders_created_elsewhere_default_to_direct_or_pay_link(make_vendor, make_order):
    from app.models.payment_link import PaymentLink
    vendor, _ = make_vendor()
    link = PaymentLink(vendor_id=vendor.id, title="Cake", amount=5000, slug="cake-src-1")
    db.session.add(link)
    db.session.commit()
    assert make_order(vendor).source == "direct"
    o = make_order(vendor, payment_link_id=link.id)
    assert o.source == "pay_link"
    assert _events("order_created", order_id=o.id)[0].properties["kind"] == "pay_link"


def _escrow(order, status=EscrowStatus.PENDING_PAYMENT):
    e = EscrowTransaction(order_id=order.id, transaction_number=f"T-B{order.id}", status=status,
                          amount=order.total_amount, fee_amount=0, fee_percent=3)
    db.session.add(e)
    db.session.commit()
    return e


def test_paid_shipped_confirmed_once_per_order(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=8000, logistics_fee=1000, source="instagram", campaign="nov")
    escrow = _escrow(order)
    escrow.status = EscrowStatus.IN_ESCROW
    db.session.commit()
    escrow.status = EscrowStatus.IN_ESCROW       # a repeated webhook
    order.status = "PAID"
    db.session.commit()
    escrow.status = EscrowStatus.SHIPPED
    db.session.commit()
    escrow.status = EscrowStatus.DELIVERED
    order.status = "DELIVERED"
    db.session.commit()
    escrow.status = EscrowStatus.RELEASED
    order.status = "COMPLETED"
    db.session.commit()

    paid = _events("paid", order_id=order.id)
    assert len(paid) == 1
    assert paid[0].properties["amount"] == 9000.0 and paid[0].properties["source"] == "instagram"
    assert "buyer_email" not in paid[0].properties
    assert len(_events("shipped", order_id=order.id)) == 1
    assert len(_events("confirmed", order_id=order.id)) == 1


def test_dispute_event_once(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor)
    escrow = _escrow(order, EscrowStatus.IN_ESCROW)
    escrow.status = EscrowStatus.DISPUTED
    order.status = "DISPUTED"
    db.session.commit()
    assert len(_events("disputed", order_id=order.id)) == 1


def test_free_claims_are_not_buying_events(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = Order(vendor_id=vendor.id, buyer_email="g@example.com", total_amount=0, status="COMPLETED",
                  payment_method="FREE")
    db.session.add(order)
    db.session.commit()
    assert not _events("order_created", order_id=order.id) and not _events("paid", order_id=order.id)


def test_buying_metrics(make_vendor, make_order):
    vendor, _ = make_vendor()
    now = datetime.now(timezone.utc)
    for name in ("funnel.product_viewed", "product_viewed", "funnel.product_viewed", "funnel.add_to_cart"):
        db.session.add(PlatformEvent(event_name=name, timestamp=now))
    db.session.commit()

    a = make_order(vendor, total=10000, source="instagram", campaign="nov")
    ea = _escrow(a, EscrowStatus.IN_ESCROW)
    a.paid_at = now - timedelta(hours=10)
    db.session.commit()
    ea.status = EscrowStatus.SHIPPED
    db.session.commit()

    b = make_order(vendor, total=4000)                                   # paid 4 days ago, never shipped
    _escrow(b, EscrowStatus.IN_ESCROW)
    b.paid_at = now - timedelta(hours=80)
    db.session.commit()

    # An old cart event without a funnel.order_created still counts once.
    db.session.add(PlatformEvent(event_name="checkout_started", timestamp=now))
    db.session.add(PlatformEvent(event_name="checkout_started", timestamp=now, order_id=a.id))
    db.session.commit()

    m = buying_metrics(_month())
    s = m["steps"]
    assert s["product_viewed"] == 3 and s["add_to_cart"] == 1
    assert s["order_created"] == 3                 # a, b + the legacy one
    assert s["paid"] == 2 and s["shipped"] == 1
    assert m["conversion_pct"]["product_viewed_to_store_viewed"] == 0.0
    assert m["conversion_pct"]["store_viewed_to_add_to_cart"] is None
    assert m["paid_by_source"]["instagram"] == {"paid_orders": 1, "gmv_ngn": 10000.0}
    assert m["paid_by_source"]["direct"]["paid_orders"] == 1
    assert m["paid_by_campaign"]["nov"]["paid_orders"] == 1
    assert m["median_hours_paid_to_shipped"] == pytest.approx(10.0, abs=0.2)
    assert m["paid_not_shipped_after_72h"] == 1


def test_monthly_metrics_has_buying_section(make_vendor):
    from app.services.metrics import monthly_metrics
    assert "steps" in monthly_metrics(_month())["buying"]


def test_telemetry_accepts_funnel_names(client, monkeypatch):
    seen = []
    import app.services.event_logger as el
    monkeypatch.setattr(el, "log_platform_event", lambda **k: seen.append(k))
    res = client.post("/api/marketplace/telemetry", json={
        "event_name": "funnel.add_to_cart", "product_id": 1,
        "properties": {"source": "market", "campaign": "nov"}})
    assert res.status_code == 200 and seen[0]["event_name"] == "funnel.add_to_cart"


# ── 3. Trust on product pages ───────────────────────────────────────────────
def test_product_page_shows_new_seller_not_fake_silver(client, make_vendor, make_product):
    vendor, store = make_vendor()
    store.is_published = True
    store.city, store.state = "Ikeja", "Lagos"
    db.session.commit()
    p = make_product(store, location="Ikeja, Lagos")
    body = client.get(f"/api/marketplace/products/{p.id}").get_json()
    sf = body["storefront"]
    assert sf["trust_tier"] == "NEW" and sf["trust_score"] is None
    assert sf["trust_score_max"] and sf["trust_label"] == "New seller — no history yet"
    assert (sf["city"], sf["state"]) == ("Ikeja", "Lagos")
    assert body["location"] == "Ikeja, Lagos" and body["created_at"]

    card = next(s for s in client.get("/api/market/stores").get_json()["stores"] if s["store_id"] == store.id)
    assert card["labels"]["trust"]["tier"] == sf["trust_tier"]
    assert card["labels"]["trust"]["score"] == sf["trust_score"]
    assert card["labels"]["trust"]["score_max"] == sf["trust_score_max"]

    store_info = client.get(f"/api/marketplace/store/{store.store_slug}").get_json()["store_info"]
    assert store_info["trust_tier"] == "NEW" and store_info["trust_score"] is None
    assert store_info["trust_label"] == "New seller — no history yet"


# ── 4. Empty stores stay out of the Market ──────────────────────────────────
def _live_store(make_vendor, name):
    vendor, store = make_vendor()
    store.is_published = True
    store.store_name = name
    db.session.commit()
    return vendor, store


def test_empty_stores_hidden_from_market_but_reachable(client, make_vendor, make_product):
    _, full = _live_store(make_vendor, "Zobo Corner")
    make_product(full)
    _, empty = _live_store(make_vendor, "Zobo Empty")
    _, sold_out = _live_store(make_vendor, "Zobo Sold Out")
    make_product(sold_out, stock=0)
    _, digital = _live_store(make_vendor, "Zobo Guides")
    make_product(digital, stock=0, product_type="digital", file_url="https://files.example/a.pdf")

    market = {s["store_id"] for s in client.get("/api/market/stores").get_json()["stores"]}
    assert full.id in market and digital.id in market
    assert empty.id not in market and sold_out.id not in market

    search = client.get("/api/search?q=zobo").get_json()["results"]
    store_ids = {r["store_id"] for r in search if r["kind"] == "store"}
    assert full.id in store_ids
    assert empty.id not in store_ids and sold_out.id not in store_ids

    listing = {s["id"] for s in client.get("/api/marketplace/storefronts").get_json()["storefronts"]}
    assert full.id in listing and empty.id not in listing

    assert client.get(f"/api/marketplace/store/{empty.store_slug}").status_code == 200


# ── 5. Delivery settings and server-priced delivery ─────────────────────────
SETTINGS = {"self_delivery": {"enabled": True, "fee_same_city": 1500, "fee_same_state": 2500,
                              "fee_other_states": None, "ships_within_days": 2},
            "pickup": {"enabled": True, "area": "Wuse 2, Abuja"}}


def test_delivery_settings_round_trip_and_public(client, make_vendor, auth_headers):
    vendor, store = make_vendor()
    store.address = "Plot 5, Secret Close"
    db.session.commit()
    h = auth_headers(vendor)
    res = client.post("/api/logistics/settings", headers=h, json={"delivery_settings": SETTINGS})
    assert res.status_code == 200, res.get_json()
    assert client.get("/api/logistics/settings", headers=h).get_json()["delivery_settings"] == SETTINGS
    public = client.get(f"/api/logistics/settings?vendor_id={vendor.id}").get_json()
    assert public["delivery_settings"] == SETTINGS
    assert "Secret Close" not in str(public)
    assert client.get("/api/logistics/settings?vendor_id=999999").get_json()["delivery_settings"] is None


@pytest.mark.parametrize("bad", [
    {"self_delivery": {"enabled": True, "fee_same_city": -1}},
    {"self_delivery": {"enabled": True, "fee_same_city": 500001}},
    {"self_delivery": {"enabled": True, "fee_same_city": 100, "ships_within_days": 31}},
    {"self_delivery": {"enabled": True}},
    "nope",
])
def test_delivery_settings_validation(client, make_vendor, auth_headers, bad):
    vendor, _ = make_vendor()
    res = client.post("/api/logistics/settings", headers=auth_headers(vendor), json={"delivery_settings": bad})
    assert res.status_code == 400 and res.get_json()["code"] == "INVALID_DELIVERY_SETTINGS"


def test_delivery_settings_trims_area(client, make_vendor, auth_headers):
    vendor, store = make_vendor()
    client.post("/api/logistics/settings", headers=auth_headers(vendor),
                json={"delivery_settings": {"pickup": {"enabled": True, "area": "a" * 300}}})
    db.session.expire_all()
    assert len(store.delivery_settings["pickup"]["area"]) == 120


def _seller_with_delivery(make_vendor, make_product, settings=SETTINGS, city="Abuja", state="FCT"):
    vendor, store = make_vendor()
    store.city, store.state, store.delivery_settings = city, state, settings
    db.session.commit()
    return vendor, make_product(store, price=5000)


def _select(vendor, opt_id, fee):
    return [{"vendorId": vendor.id, "selectedOption": {"id": opt_id, "fee": fee}}]


@pytest.mark.parametrize("city,state,fee", [("Abuja", "FCT", 1500), ("Kubwa", "Abuja", 2500),
                                            ("Abuja", "Federal Capital Territory", 1500)])
def test_vendor_self_fee_is_priced_by_server(client, make_vendor, make_product, city, state, fee):
    vendor, p = _seller_with_delivery(make_vendor, make_product)
    res = _checkout(client, p, logistics_selections=_select(vendor, "vendor_self", 1),
                    delivery_city=city, delivery_state=state)
    assert res.status_code == 200, res.get_json()
    order = db.session.get(Order, res.get_json()["id"])
    assert float(order.logistics_fee) == fee and order.logistics_provider_id == "vendor_self"


def test_vendor_self_outside_area_is_refused(client, make_vendor, make_product):
    vendor, p = _seller_with_delivery(make_vendor, make_product)
    res = _checkout(client, p, logistics_selections=_select(vendor, "vendor_self", 0))   # Lagos buyer
    assert res.status_code == 400 and res.get_json()["code"] == "DELIVERY_NOT_AVAILABLE"
    assert Order.query.count() == 0


def test_vendor_self_without_settings_is_refused(client, make_vendor, make_product):
    vendor, p = _seller_with_delivery(make_vendor, make_product, settings=None)
    res = _checkout(client, p, logistics_selections=_select(vendor, "vendor_self", 0))
    assert res.status_code == 400 and res.get_json()["code"] == "DELIVERY_NOT_AVAILABLE"


def test_pickup_is_free_and_needs_pickup_enabled(client, make_vendor, make_product):
    vendor, p = _seller_with_delivery(make_vendor, make_product)
    res = _checkout(client, p, logistics_selections=_select(vendor, "pickup", 9999))
    assert float(db.session.get(Order, res.get_json()["id"]).logistics_fee) == 0

    off = {**SETTINGS, "pickup": {"enabled": False, "area": None}}
    vendor2, p2 = _seller_with_delivery(make_vendor, make_product, settings=off)
    res = _checkout(client, p2, logistics_selections=_select(vendor2, "pickup", 0))
    assert res.status_code == 400 and res.get_json()["code"] == "PICKUP_NOT_AVAILABLE"

    vendor3, p3 = _seller_with_delivery(make_vendor, make_product, settings=None)   # old sellers: as before
    res = _checkout(client, p3, logistics_selections=_select(vendor3, "pickup", 0))
    assert res.status_code == 200


def test_partner_fee_cannot_go_below_minimum(client, make_vendor, make_product, make_user):
    partner = make_user(role=UserRole.PARTNER)
    db.session.add(PartnerApplication(user_id=partner.id, business_name="Fast Riders", service_type="LOGISTICS",
                                      status="APPROVED", pricing_settings={"pricing_model": "FLAT",
                                                                           "flat_rate": 2000}))
    db.session.commit()
    vendor, p = _seller_with_delivery(make_vendor, make_product)
    res = _checkout(client, p, logistics_selections=_select(vendor, f"siiqo_partner_{partner.id}", 1))
    order = db.session.get(Order, res.get_json()["id"])
    assert float(order.logistics_fee) == 2200.0

    res = _checkout(client, p, logistics_selections=_select(vendor, f"siiqo_partner_{partner.id}", 10 ** 9))
    assert float(db.session.get(Order, res.get_json()["id"]).logistics_fee) == 200000.0

    res = _checkout(client, p, logistics_selections=_select(vendor, f"siiqo_partner_{partner.id}", 3300))
    assert float(db.session.get(Order, res.get_json()["id"]).logistics_fee) == 3300.0


def test_unknown_option_keeps_fee(client, make_vendor, make_product):
    vendor, p = _seller_with_delivery(make_vendor, make_product)
    res = _checkout(client, p, logistics_selections=_select(vendor, "mystery", 700))
    assert float(db.session.get(Order, res.get_json()["id"]).logistics_fee) == 700.0


# ── 6. Guest question to the seller ─────────────────────────────────────────
@pytest.fixture
def emails(monkeypatch):
    sent = []
    import app.utils.email as email
    monkeypatch.setattr(email, "send_siiqo_email", lambda **k: sent.append(k) or True)
    return sent


@pytest.fixture
def events(monkeypatch):
    seen = []
    import app.services.event_logger as el
    monkeypatch.setattr(el, "log_platform_event", lambda **k: seen.append(k))
    return seen


def _listed(make_vendor, make_product):
    vendor, store = make_vendor()
    store.is_published = True
    store.phone = "08099998888"
    db.session.commit()
    return vendor, make_product(store)


def _ask(client, product_id, **body):
    payload = {"name": "Chidi", "email": "chidi@example.com", "message": "Is this available in blue?", **body}
    return client.post(f"/api/public/products/{product_id}/question", json=payload)


def test_guest_question_reaches_seller(client, make_vendor, make_product, emails, events):
    vendor, p = _listed(make_vendor, make_product)
    res = _ask(client, p.id, whatsapp="+234 803 123 4567")
    assert res.status_code == 201 and res.get_json() == {"status": "sent"}
    assert vendor.email not in res.get_data(as_text=True) and "08099998888" not in res.get_data(as_text=True)
    note = Notification.query.filter_by(user_id=vendor.id).one()
    assert "Is this available in blue?" in note.message and "chidi@example.com" in note.message
    assert "+2348031234567" in note.message
    assert emails[0]["to_email"] == vendor.email and emails[0]["template_name"] == "base_notification"
    assert events[0]["event_name"] == "funnel.seller_contacted"
    assert events[0]["properties"] == {"channel": "guest_question"}


def test_guest_question_validation(client, make_vendor, make_product, emails, events):
    _, p = _listed(make_vendor, make_product)
    res = _ask(client, p.id, message="hey")
    assert res.status_code == 400 and res.get_json()["code"] == "MESSAGE_REQUIRED"
    res = _ask(client, p.id, message="x" * 1001)
    assert res.get_json()["code"] == "MESSAGE_REQUIRED"
    res = _ask(client, p.id, email="not-an-email")
    assert res.status_code == 400 and res.get_json()["code"] == "CONTACT_REQUIRED"
    res = _ask(client, p.id, email=None, whatsapp="12345")
    assert res.get_json()["code"] == "CONTACT_REQUIRED"
    assert _ask(client, p.id, email=None, whatsapp="08031234567").status_code == 201
    assert _ask(client, 999999).status_code == 404
    p.is_active = False
    db.session.commit()
    assert _ask(client, p.id).status_code == 404


def test_guest_question_honeypot(client, make_vendor, make_product, emails, events):
    vendor, p = _listed(make_vendor, make_product)
    res = _ask(client, p.id, website="http://spam.example")
    assert res.status_code == 201 and res.get_json() == {"status": "sent"}
    assert Notification.query.filter_by(user_id=vendor.id).count() == 0 and emails == [] and events == []


def test_guest_question_rate_limit(client, make_vendor, make_product, emails, events):
    _, p = _listed(make_vendor, make_product)
    limiter.enabled = True
    try:
        limiter.reset()
        codes = [_ask(client, p.id).status_code for _ in range(6)]
    finally:
        limiter.reset()
        limiter.enabled = False
    assert codes[:5] == [201] * 5 and codes[5] == 429
