"""Seller source, activation numbers, the weekly dashboard and batched
broadcasts with unsubscribe (app/services/attribution.py, activation.py,
broadcasts.py). No email leaves the machine: sending is mocked."""
from datetime import datetime, timedelta, timezone

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.finance import Ledger
from app.models.growth import BroadcastJob, BroadcastRecipient, UserAttribution
from app.models.marketing import MarketingCampaign, MarketingCode
from app.models.withdrawal import DayaPayment
from app.services import activation, attribution, broadcasts

NOW = datetime(2027, 1, 20, 12, 0, tzinfo=timezone.utc)   # a Wednesday


@pytest.fixture(autouse=True)
def _campaign_start(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_START_DATE", "2026-12-05")


def _superadmin():
    admin = AdminUser(name="Ops", email="super@example.com", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _seller(make_vendor, created_at, channel=None):
    vendor, store = make_vendor()
    store.created_at = created_at
    if channel:
        db.session.add(UserAttribution(user_id=vendor.id, channel=channel, store_created_at=created_at))
    db.session.commit()
    return vendor, store


def _paid(make_order, vendor, buyer, paid_at, **extra):
    status = extra.pop("status", "PAID")
    order = make_order(vendor, buyer=buyer, payment_method=extra.pop("payment_method", "CRYPTO"), **extra)
    order.status = status
    order.paid_at = paid_at
    db.session.commit()
    return order


# ── channel resolution ──────────────────────────────────────────────────────

@pytest.mark.parametrize("source,medium,campaign,expected", [
    (None, None, None, "organic"),
    ("instagram.com", "referral", None, "organic"),          # untagged visit from a social site
    ("facebook", "cpc", "dec_launch", "ads"),
    ("tiktok", "paid_social", None, "ads"),
    ("techcabal", "press", None, "pr"),
    ("blog", None, None, "pr"),
    ("instagram", "social", "staff_posts", "content"),
    ("facebook", "social", "facebook_sellers", "facebook_sellers"),
    ("influencer", "story", None, "influencer"),
    ("market", "event", None, "market_activation"),
])
def test_channel_from_utm(source, medium, campaign, expected):
    assert attribution.channel_from_utm(source, medium, campaign) == expected


def test_code_kind_channels(app):
    camp = MarketingCampaign(name="Radio", slug="radio-dec", channel="radio")
    db.session.add(camp)
    db.session.flush()
    codes = {
        "agent": MarketingCode(code="AG1", kind="agent"),
        "influencer": MarketingCode(code="INF1", kind="influencer"),
        "pr": MarketingCode(code="MED1", kind="media"),
        "referral": MarketingCode(code="INV1", kind="seller_invite"),
        "market_activation": MarketingCode(code="FLY1", kind="flyer"),
    }
    radio = MarketingCode(code="RAD1", kind="campaign", campaign_id=camp.id)
    db.session.add_all([*codes.values(), radio])
    db.session.commit()
    for expected, code in codes.items():
        assert attribution.channel_for_code(code) == expected
    assert attribution.channel_for_code(radio) == "pr"


def test_every_signup_gets_a_source(app, make_user):
    db.session.add(MarketingCode(code="INF22", kind="influencer"))
    db.session.commit()
    a, b, c = make_user(), make_user(), make_user()
    attribution.record_signup(a, {"ref_code": "INF22", "utm": {"source": "x", "medium": "social"}})
    attribution.record_signup(b, {"utm": {"source": "Google", "medium": "CPC", "campaign": "Launch"}})
    attribution.record_signup(c, {})
    db.session.commit()
    rows = {r.user_id: r for r in UserAttribution.query.all()}
    assert rows[a.id].channel == "influencer" and rows[a.id].ref_code == "INF22"
    assert rows[b.id].channel == "ads" and rows[b.id].utm_source == "google" and rows[b.id].utm_campaign == "launch"
    assert rows[c.id].channel == "organic"

    # First touch wins; an organic row is resolved by the store setup's code.
    attribution.record_signup(b, {"utm": {"source": "blog"}})
    attribution.record_store_created(c, {"ref_code": "INF22"})
    db.session.commit()
    db.session.refresh(rows[b.id])
    db.session.refresh(rows[c.id])
    assert rows[b.id].channel == "ads" and rows[b.id].utm_source == "google"
    assert rows[c.id].channel == "influencer"


def test_register_records_source(client, monkeypatch):
    import app.routes.auth as auth_routes
    monkeypatch.setattr(auth_routes, "send_siiqo_email", lambda *a, **k: None)
    res = client.post("/api/auth/register", json={
        "email": "newseller@siiqo-test.ng", "password": "a-long-password",
        "utm": {"source": "techcabal", "medium": "press", "campaign": "launch"}})
    assert res.status_code == 201
    row = UserAttribution.query.one()
    assert (row.channel, row.utm_source, row.utm_medium) == ("pr", "techcabal", "press")


# ── existing vs new, activation ─────────────────────────────────────────────

def test_existing_before_campaign_start(make_vendor):
    start = attribution.campaign_start_date()
    _seller(make_vendor, start - timedelta(days=1), channel="agent")
    _seller(make_vendor, start + timedelta(days=1), channel="agent")
    _seller(make_vendor, start + timedelta(days=2))
    chans = sorted(s["channel"] for s in activation.cohort(start - timedelta(days=10), start + timedelta(days=10)))
    assert chans == ["agent", "existing", "organic"]


def test_activation_within_30_days_only(make_vendor, make_order, make_user):
    buyer = make_user()
    created = datetime(2026, 12, 14, 10, tzinfo=timezone.utc)
    fast, _ = _seller(make_vendor, created, channel="agent")
    late, _ = _seller(make_vendor, created, channel="agent")
    free, _ = _seller(make_vendor, created, channel="agent")
    _paid(make_order, fast, buyer, created + timedelta(days=10))
    _paid(make_order, late, buyer, created + timedelta(days=31))
    _paid(make_order, free, buyer, created + timedelta(days=2), payment_method="FREE")
    res = activation.activation(created - timedelta(days=1), created + timedelta(days=1), now=NOW + timedelta(days=60))
    assert res["registered"] == 3 and res["activated"] == 1 and res["activation_pct"] == 33.3
    assert res["by_channel"]["agent"]["activated"] == 1


def test_self_purchase_and_cancelled_do_not_activate(make_vendor, make_order, make_user):
    created = datetime(2026, 12, 14, 10, tzinfo=timezone.utc)
    seller, _ = _seller(make_vendor, created)
    _paid(make_order, seller, seller, created + timedelta(days=1))            # bought from themselves
    _paid(make_order, seller, make_user(), created + timedelta(days=2), status="CANCELLED")
    assert activation.activation(created - timedelta(days=1), created + timedelta(days=1),
                                 now=NOW + timedelta(days=60))["activated"] == 0
    _paid(make_order, seller, make_user(), created + timedelta(days=3))
    assert activation.activation(created - timedelta(days=1), created + timedelta(days=1),
                                 now=NOW + timedelta(days=60))["activated"] == 1


def test_cost_per_activated_seller(make_vendor, make_order, make_user):
    db.session.add(MarketingCampaign(name="Ads", slug="ads-dec", channel="other", spend_ngn=50000))
    db.session.commit()
    created = datetime(2026, 12, 14, 10, tzinfo=timezone.utc)
    for _ in range(2):
        seller, _ = _seller(make_vendor, created)
        _paid(make_order, seller, make_user(), created + timedelta(days=1))
    cost = activation.cost_per_activated_seller(now=NOW)
    assert cost["activated_sellers"] == 2 and cost["cost_per_activated_seller_ngn"] == 25000.0


# ── weekly maths ────────────────────────────────────────────────────────────

def test_wow_and_weeks():
    assert activation.wow(10, 5) == 100.0
    assert activation.wow(3, 4) == -25.0
    assert activation.wow(5, 0) is None
    weeks = activation.last_weeks(8, NOW)
    assert len(weeks) == 8 and weeks[-1][1] == activation.week_start(NOW)
    assert weeks[-1][1].astimezone(activation.LAGOS).weekday() == 0   # Monday, Lagos


def test_weekly_dashboard_counts(make_vendor, make_order, make_user):
    weeks = activation.last_weeks(8, NOW)
    (w6s, _), (w7s, _) = weeks[-2], weeks[-1]
    vendor, _ = _seller(make_vendor, w6s + timedelta(hours=2))
    buyer = make_user()
    o1 = _paid(make_order, vendor, buyer, w6s + timedelta(days=1))
    o2 = _paid(make_order, vendor, buyer, w7s + timedelta(days=1))
    o3 = _paid(make_order, vendor, make_user(), w7s + timedelta(days=2))
    for i, (o, when) in enumerate(((o1, w6s + timedelta(days=2)), (o2, w7s + timedelta(days=2)),
                                   (o3, w7s + timedelta(days=3)))):
        db.session.add(EscrowTransaction(order_id=o.id, transaction_number=f"W-{i}", status=EscrowStatus.RELEASED,
                                         amount=10000, released_at=when))
        db.session.add(Ledger(vendor_id=vendor.id, transaction_type="DEBIT", amount=9700,
                              reference_id=f"PAYOUT-W-{i}"))
    db.session.commit()
    data = activation.weekly_dashboard(8, now=NOW)
    last, prev = data["weeks"][-1], data["weeks"][-2]
    assert prev["registered_sellers"] == 1 and prev["activated_sellers"] == 1
    assert (prev["completed_protected_orders"], last["completed_protected_orders"]) == (1, 2)
    assert last["wow_pct"]["completed_protected_orders"] == 100.0
    assert (prev["new_buyers"], prev["repeat_buyers"]) == (1, 0)
    assert (last["new_buyers"], last["repeat_buyers"]) == (1, 1)
    assert data["payout_failures"] == []
    csv_text = activation.dashboard_csv(data)
    assert "Sellers by channel" in csv_text and last["week_start"] in csv_text


# ── payout failures ─────────────────────────────────────────────────────────

def test_payout_failures(make_vendor, make_order, make_user):
    vendor, _ = make_vendor()
    now = datetime.now(timezone.utc)
    stuck = _paid(make_order, vendor, make_user(), now - timedelta(days=3))
    paid_out = _paid(make_order, vendor, make_user(), now - timedelta(days=3))
    recent = _paid(make_order, vendor, make_user(), now - timedelta(hours=2))
    for o in (stuck, paid_out, recent):
        db.session.add(EscrowTransaction(order_id=o.id, transaction_number=f"PF-{o.id}", status=EscrowStatus.RELEASED,
                                         amount=5000, released_at=now - (timedelta(hours=1) if o is recent
                                                                         else timedelta(days=2))))
    db.session.add(Ledger(vendor_id=vendor.id, transaction_type="DEBIT", amount=4850,
                          reference_id=f"PAYOUT-PF-{paid_out.id}"))
    db.session.add(DayaPayment(order_id=recent.id, payment_type="ngn_onramp", amount_ngn=5000, status="UNDERPAID"))
    db.session.commit()
    items = activation.payout_failures(now=now)
    kinds = sorted((i["kind"], i["order_id"]) for i in items)
    assert kinds == sorted([("released_not_paid", stuck.id), ("daya_payment", recent.id)])


# ── segments ────────────────────────────────────────────────────────────────

def _segment_world(make_vendor, make_product, make_order, make_user):
    no_sale, s1 = make_vendor()
    make_product(s1)
    sold, s2 = make_vendor()
    make_product(s2)
    _paid(make_order, sold, make_user(), datetime.now(timezone.utc))
    empty, _ = make_vendor()
    unsub, s4 = make_vendor()
    make_product(s4)
    unsub.is_subscribed_to_broadcasts = False
    for u in (no_sale, sold, empty, unsub):   # example.com is treated as a placeholder address
        u.email = u.email.replace("example.com", "siiqo-test.ng")
    db.session.commit()
    return no_sale, sold, empty, unsub


def test_segment_counts(make_vendor, make_product, make_order, make_user):
    no_sale, sold, empty, unsub = _segment_world(make_vendor, make_product, make_order, make_user)
    c = broadcasts.audience_counts("SELLERS_NO_SALE")
    assert (c["matching"], c["unsubscribed"], c["will_receive"]) == (2, 1, 1)
    assert broadcasts.audience_counts("SELLERS_NO_PRODUCTS")["will_receive"] == 1
    assert broadcasts.audience_counts("SELLERS_ACTIVATED")["will_receive"] == 1
    assert broadcasts.audience_counts("SELLERS_ALL")["will_receive"] == 3
    ids = {uid for uid, *_ in broadcasts.audience_query("SELLERS_NO_SALE").filter(broadcasts._subscribed())}
    assert ids == {no_sale.id}


# ── batching, resume, unsubscribe ───────────────────────────────────────────

@pytest.fixture
def outbox(monkeypatch):
    import app.utils.email as email
    sent = []

    def fake(**kw):
        sent.append(kw)
        return not kw["to_email"].startswith("bounce")
    monkeypatch.setattr(email, "send_siiqo_email", fake)
    return sent


def test_batches_and_resume_after_restart(make_vendor, make_product, outbox, monkeypatch):
    monkeypatch.setenv("BROADCAST_BATCH_SIZE", "2")
    for i in range(5):
        v, s = make_vendor()
        v.email = f"{'bounce' if i == 4 else 'seller'}{i}@siiqo-test.ng"
        v.first_name = f"Ada{i}"
    db.session.commit()
    job = broadcasts.create_job("SELLERS_ALL", "Hi {first_name}", "<p>{store_name} needs a sale</p>",
                                api_base="https://api.siiqo-test.ng")
    assert job.total == 5 and job.status == "queued"

    # One batch, then the process "dies": the lease runs out.
    broadcasts.run_job(job.id, max_batches=1, pause=0)
    db.session.refresh(job)
    assert (job.sent, job.status) == (2, "sending")
    assert BroadcastRecipient.query.filter_by(job_id=job.id, status="pending").count() == 3

    assert broadcasts.resume_stalled() == 1
    db.session.refresh(job)
    assert (job.status, job.sent, job.failed) == ("done", 4, 1)
    assert len(outbox) == 5                       # nobody twice
    first = outbox[0]
    assert first["subject"] == "Hi Ada0" and "Store" in first["body_content"] and first["wait"] is True
    assert "t=" in first["unsubscribe_link"] and "@" not in first["unsubscribe_link"]
    assert first["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert first["unsubscribe_link"] in first["headers"]["List-Unsubscribe"]


def test_admin_broadcast_queues_and_reports(client, make_vendor, make_product, make_order, make_user, outbox):
    _segment_world(make_vendor, make_product, make_order, make_user)
    headers = _superadmin()
    count = client.get("/api/admin/broadcast/audience?audience=SELLERS_NO_SALE", headers=headers).get_json()
    assert count["will_receive"] == 1
    res = client.post("/api/admin/broadcast", headers=headers, json={
        "audience": "SELLERS_NO_SALE", "subject": "Your first sale", "body": "Hi {first_name}"})
    assert res.status_code == 202
    job = res.get_json()["job"]
    assert (job["total"], job["sent"], job["status"]) == (1, 1, "done")   # runs inline in tests
    assert client.get(f"/api/admin/broadcast/jobs/{job['id']}", headers=headers).get_json()["job"]["sent"] == 1


def test_unsubscribe_token_and_old_links(client, app, make_user):
    import hashlib
    user = make_user(email="reader@siiqo-test.ng")
    with app.test_request_context():
        token = broadcasts.unsubscribe_token(user.id)
    assert "reader" not in token
    assert client.get("/api/auth/unsubscribe?t=bad").status_code == 400
    assert client.get(f"/api/auth/unsubscribe?t={token}").status_code == 200
    db.session.refresh(user)
    assert user.is_subscribed_to_broadcasts is False

    user.is_subscribed_to_broadcasts = True
    db.session.commit()
    assert client.post(f"/api/auth/unsubscribe?t={token}").status_code == 200   # one-click POST
    db.session.refresh(user)
    assert user.is_subscribed_to_broadcasts is False

    user.is_subscribed_to_broadcasts = True
    db.session.commit()
    old = hashlib.sha256(f"{user.email}{app.config['SECRET_KEY']}".encode()).hexdigest()[:16]
    assert client.get(f"/api/auth/unsubscribe?email={user.email}&token={old}").status_code == 200
    db.session.refresh(user)
    assert user.is_subscribed_to_broadcasts is False
    assert client.get(f"/api/auth/unsubscribe?email={user.email}&token=0000").status_code == 400


def test_list_unsubscribe_header_on_the_message(app, monkeypatch):
    import smtplib
    import app.utils.email as email
    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, **k):
            pass

        def login(self, *a):
            pass

        def send_message(self, msg):
            captured["msg"] = msg

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    headers = email.unsubscribe_headers("https://api.siiqo-test.ng/api/auth/unsubscribe?t=abc", "hello@siiqo-test.ng")
    ok = email._send_email_async("smtp.invalid", 587, "user", "not-a-real-password", "hello@siiqo-test.ng",
                                 "reader@siiqo-test.ng", "Hi", "<p>x</p>", {}, headers)
    assert ok is True
    msg = captured["msg"]
    assert msg["List-Unsubscribe"] == ("<mailto:hello@siiqo-test.ng?subject=unsubscribe>, "
                                       "<https://api.siiqo-test.ng/api/auth/unsubscribe?t=abc>")
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    # Header injection is blocked.
    assert "\n" not in email.unsubscribe_headers("https://x.ng/u?t=a\r\nBcc: evil@x.ng")["List-Unsubscribe"]
