"""Campaign rules (app/services/marketing.py):

1. Agent rewards: N500 first-sale bonus (sale >= N20,000), N200 for sales
   2-10 (each >= N10,000), max N2,300 per seller; counted only once delivery is
   confirmed (escrow released, or digital paid and completed); hold from the
   release; non-naira orders at the stored rate; below-minimum sales use no slot.
2. Buyer "First Order N500 Off": admin codes, checkout and Pay Link discount,
   first order / minimum / self / cap / race checks, use given back on refund,
   seller top-up of discount less 3% on release, idempotent.
3. Seller-to-seller referral reward: off by default, held then credited to the
   referring seller's ledger when set.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from app.extensions import db
from app.models.admin import AdminUser
from app.models.escrow import EscrowTransaction
from app.models.finance import Ledger
from app.models.marketing import AgentEarning, BuyerPromoCode, BuyerPromoUse, SellerAcquisition
from app.models.order import Order
from app.models.payment_link import PaymentLink
from app.models.withdrawal import VendorBankAccount
from app.services import daya_service
from app.services import marketing as svc
from tests.test_marketing_system import agent  # noqa: F401  (fixture)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def default_rules(monkeypatch):
    """The founder's defaults, whatever the machine's env says."""
    for key in ("AGENT_FIRST_SALE_BONUS_NGN", "AGENT_FIRST_SALE_MIN_NGN", "AGENT_SALE_REWARD_NGN",
                "AGENT_FOLLOW_ON_MIN_NGN", "AGENT_REWARDED_SALES", "AGENT_REWARD_HOLD_DAYS",
                "AGENT_SALES_DISTINCT_BUYERS", "SELLER_REFERRAL_REWARD_NGN", "SELLER_REFERRAL_MIN_NGN",
                "BUYER_OFFER_DISCOUNT_NGN", "BUYER_OFFER_MIN_ORDER_NGN", "BUYER_OFFER_CAP_PER_SELLER",
                "BUYER_OFFER_UNPAID_HOLD_HOURS"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def quiet_release(monkeypatch):
    """execute_order_escrow_release without emails or a real payout."""
    from app.routes import payments as payments_routes
    from app.utils import email
    monkeypatch.setattr(email, "send_siiqo_email", lambda *a, **k: True)
    monkeypatch.setattr(payments_routes, "_payout_vendor_via_daya", lambda *a, **k: False)
    from app.routes.escrow import execute_order_escrow_release

    def _release(order):
        escrow = EscrowTransaction.query.filter_by(order_id=order.id).one()
        assert execute_order_escrow_release(order, escrow, source="test") is True
        db.session.expire_all()
        return db.session.get(Order, order.id)
    return _release


def _admin():
    admin = AdminUser(name="Ops", email=f"ops-{uuid.uuid4().hex[:6]}@example.com", role="SUPERADMIN")
    admin.set_password("not-a-real-password")
    db.session.add(admin)
    db.session.commit()
    return {"Authorization": f"Bearer {create_access_token(identity=f'admin:{admin.id}')}"}


def _paid(order, escrow_status="IN_ESCROW"):
    """Payment confirmed: escrow holds the money (paid_at stamped by the flush hook)."""
    db.session.add(EscrowTransaction(order_id=order.id, amount=order.total_amount, status=escrow_status,
                                     currency=order.currency or "NGN",
                                     released_at=NOW() if escrow_status == "RELEASED" else None))
    order.status = "PAID"
    db.session.commit()
    return order


def _deliver(order, when=None):
    """Delivery confirmed: escrow released (at `when`)."""
    escrow = EscrowTransaction.query.filter_by(order_id=order.id).one()
    escrow.status = "RELEASED"
    escrow.released_at = when or NOW()
    db.session.commit()
    return order


def _sale(make_order, vendor, buyer, total, deliver=True, **kw):
    order = _paid(make_order(vendor, buyer=buyer, total=total, **kw))
    return _deliver(order) if deliver else order


def _credited(agent, make_vendor):
    agent_user, _, agent_code = agent()
    vendor, store = make_vendor()
    db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Test Bank", bank_code="058",
                                     account_number="9876543210", account_name="Seller"))
    db.session.commit()
    svc.credit_signup(vendor, {"ref_code": agent_code.code})
    db.session.commit()
    return agent_user, vendor, store


def _sales():
    db.session.expire_all()
    return AgentEarning.query.filter_by(kind="sale").order_by(AgentEarning.id).all()


# ── 1. agent rewards ─────────────────────────────────────────────────────────

def test_first_sale_bonus_needs_20000_and_follow_ons_need_10000(make_user, make_vendor, make_order, agent):
    _, vendor, _ = _credited(agent, make_vendor)
    assert svc.max_per_seller() == 2300

    # N15,000 before any first sale: below the first-sale minimum, no slot used.
    small = _sale(make_order, vendor, make_user(), 15000)
    assert _sales() == []
    first = _sale(make_order, vendor, make_user(), 20000)                 # exactly the minimum
    sales = _sales()
    assert [(e.order_id, e.sale_number, float(e.amount_ngn)) for e in sales] == [(first.id, 1, 500.0)]

    _sale(make_order, vendor, make_user(), 9999.99)                       # below the follow-on minimum
    follow = _sale(make_order, vendor, make_user(), 10000)
    assert [(e.order_id, e.sale_number, float(e.amount_ngn)) for e in _sales()] == [
        (first.id, 1, 500.0), (follow.id, 2, 200.0)]
    assert small.id not in {e.order_id for e in _sales()}

    # Up to 10 rewarded sales, N2,300 in all, never an 11th.
    for _ in range(10):
        _sale(make_order, vendor, make_user(), 12000)
    sales = _sales()
    assert [e.sale_number for e in sales] == list(range(1, 11))
    assert sum(float(e.amount_ngn) for e in sales) == 2300.0


def test_paid_is_not_enough_reward_waits_for_release_and_hold_starts_there(make_user, make_vendor, make_order,
                                                                           agent):
    _, vendor, _ = _credited(agent, make_vendor)
    order = _sale(make_order, vendor, make_user(), 25000, deliver=False)
    assert db.session.get(Order, order.id).paid_at is not None
    assert _sales() == []                                                 # paid, not delivered

    released = NOW() - timedelta(days=2)
    _deliver(order, when=released)
    (earning,) = _sales()
    assert earning.sale_number == 1 and earning.status == "HELD"
    assert svc._aware(earning.payable_at) == released + timedelta(days=7)

    svc.run_marketing_sweep(now=released + timedelta(days=6))
    assert _sales()[0].status == "HELD"
    svc.run_marketing_sweep(now=released + timedelta(days=7, minutes=1))
    assert _sales()[0].status == "PAYABLE"


def test_real_release_path_counts_the_sale(make_user, make_vendor, make_order, agent, quiet_release):
    _, vendor, _ = _credited(agent, make_vendor)
    order = _sale(make_order, vendor, make_user(), 30000, deliver=False)
    quiet_release(order)
    assert [(e.order_id, float(e.amount_ngn)) for e in _sales()] == [(order.id, 500.0)]


def test_seller_set_status_never_counts(make_user, make_vendor, make_order, agent):
    from app.services.foundations import mark_seller_status_edit
    _, vendor, _ = _credited(agent, make_vendor)
    order = make_order(vendor, buyer=make_user(), total=50000, payment_method="POD")
    with mark_seller_status_edit(order):
        order.status = "COMPLETED"
        db.session.commit()
    db.session.expire_all()
    assert db.session.get(Order, order.id).paid_at is None
    assert _sales() == []


def test_digital_order_counts_when_paid_and_completed(make_user, make_vendor, make_product, make_order, agent):
    from app.models.order import OrderItem
    _, vendor, store = _credited(agent, make_vendor)
    product = make_product(store, price=25000, product_type="digital")
    order = make_order(vendor, buyer=make_user(), total=25000, payment_method="ESCROW")
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, price_at_purchase=25000, quantity=1))
    order.status = "COMPLETED"           # server path: paid and delivered at once
    db.session.commit()
    assert [e.order_id for e in _sales()] == [order.id]

    # A physical order with no escrow completed the same way does not count.
    phys = make_order(vendor, buyer=make_user(), total=25000, payment_method="ESCROW")
    db.session.add(OrderItem(order_id=phys.id, product_id=make_product(store).id, price_at_purchase=25000,
                             quantity=1))
    phys.status = "COMPLETED"
    db.session.commit()
    assert phys.id not in {e.order_id for e in _sales()}


def test_foreign_currency_uses_stored_rate_and_no_rate_means_no_count(make_user, make_vendor, make_order, agent):
    _, vendor, _ = _credited(agent, make_vendor)
    no_rate = make_order(vendor, buyer=make_user(), total=100, currency="USD")
    _deliver(_paid(no_rate))
    assert _sales() == []                                                 # USD with no rate: doesn't count
    low = make_order(vendor, buyer=make_user(), total=10, currency="USD", fx_rate_to_ngn=1500)
    _deliver(_paid(low))                                                  # N15,000: below N20,000
    assert _sales() == []
    ok = make_order(vendor, buyer=make_user(), total=20, currency="USD", fx_rate_to_ngn=1500)
    _deliver(_paid(ok))                                                   # N30,000
    assert [e.order_id for e in _sales()] == [ok.id]
    assert svc.order_value_ngn(ok) == Decimal("30000")


def test_distinct_buyers_and_self_purchase_by_bank(monkeypatch, make_user, make_vendor, make_order, agent):
    _, vendor, _ = _credited(agent, make_vendor)
    buyer = make_user()
    _sale(make_order, vendor, buyer, 25000)
    _sale(make_order, vendor, buyer, 25000)                               # same buyer: not counted
    assert len(_sales()) == 1

    # A buyer who paid from the seller's own bank account is not a separate buyer.
    monkeypatch.setattr(svc, "_buyer_bank_accounts", lambda order: {"9876543210"})
    _sale(make_order, vendor, make_user(), 25000)
    assert len(_sales()) == 1


def test_refund_in_hold_reverses_and_frees_the_bonus_slot(make_user, make_vendor, make_order, agent):
    _, vendor, _ = _credited(agent, make_vendor)
    first = _sale(make_order, vendor, make_user(), 20000)
    second = _sale(make_order, vendor, make_user(), 12000)
    assert [e.sale_number for e in _sales()] == [1, 2]
    first.status = "REFUNDED"
    db.session.commit()
    rows = {e.order_id: e for e in _sales()}
    assert rows[first.id].status == "REVERSED"
    # The bonus slot is free again: the next N20,000+ sale gets the N500.
    later = _sale(make_order, vendor, make_user(), 21000)
    live = sorted((e.sale_number, e.order_id, float(e.amount_ngn)) for e in _sales() if e.status != "REVERSED")
    assert live == [(1, later.id, 500.0), (2, second.id, 200.0)]


def test_agent_portal_shows_new_rates(client, make_user, auth_headers, agent):
    agent_user, _, _ = agent()
    me = client.get("/api/agents/me", headers=auth_headers(agent_user)).get_json()
    assert me["rates"] == {"first_sale_bonus": 500, "first_sale_min": 20000, "per_sale": 200,
                           "follow_on_min": 10000, "rewarded_sales": 10, "max_per_seller": 2300,
                           "hold_days": 7, "min_products": 5}
    rules = client.get("/api/admin/marketing/rules", headers=_admin()).get_json()
    assert rules["agent"]["max_per_seller"] == 2300
    assert rules["seller_referral"] == {"reward_ngn": 0, "min_sale_ngn": 20000, "hold_days": 7, "on": False}
    assert rules["buyer_offer"]["seller_topup_ngn"] == 485.0


# ── 2. buyer offer ───────────────────────────────────────────────────────────

def _promo(client, vendor, **body):
    res = client.post("/api/admin/marketing/promos", json={"seller_id": vendor.id, **body}, headers=_admin())
    assert res.status_code == 201, res.get_json()
    return res.get_json()["promo"]


def _checkout(client, product, code, email="new@example.com", phone="+2348011112222", qty=1, headers=None,
              extra_items=()):
    items = [{"product_id": product.id, "quantity": qty}] + [{"product_id": p.id, "quantity": 1} for p in extra_items]
    return client.post("/api/cart/checkout", json={"items": items, "payment_method": "CRYPTO", "promo_code": code,
                                                   "email": email, "phone": phone, "delivery_name": "Ada"},
                       headers=headers or {})


def test_admin_creates_one_code_per_seller(client, make_vendor):
    vendor, store = make_vendor()
    vendor.first_name = "Ngozi"
    db.session.commit()
    promo = _promo(client, vendor)
    assert promo["code"] == "NGOZI500"
    assert (promo["discount_ngn"], promo["min_order_ngn"], promo["cap"], promo["uses"]) == (500.0, 5000.0, 10, 0)
    again = client.post("/api/admin/marketing/promos", json={"seller_id": vendor.id}, headers=_admin())
    assert again.status_code == 409 and again.get_json()["code"] == "PROMO_EXISTS"
    other, _ = make_vendor()
    other.first_name = "Ngozi"
    db.session.commit()
    assert _promo(client, other)["code"].startswith("NGOZI500")   # a free variant of the same name
    res = client.patch(f"/api/admin/marketing/promos/{promo['id']}", json={"active": False}, headers=_admin())
    assert res.status_code == 200 and res.get_json()["promo"]["active"] is False


def test_checkout_takes_500_off_and_release_tops_up_seller(client, make_vendor, make_product, quiet_release):
    vendor, store = make_vendor()
    product = make_product(store, price=8000)
    code = _promo(client, vendor)["code"]
    res = _checkout(client, product, code.lower())
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["promo"] == {"code": code, "discount_ngn": 500.0, "vendor_id": vendor.id, "paid_by": "siiqo"}
    order = db.session.get(Order, body["orders"][0]["order_id"])
    escrow = EscrowTransaction.query.filter_by(order_id=order.id).one()
    assert float(order.total_amount) == 7500.0 and float(escrow.amount) == 7500.0
    assert float(order.promo_discount_ngn) == 500.0
    assert float(escrow.fee_amount) == 225.0                              # 3% of what the buyer paid
    promo = BuyerPromoCode.query.filter_by(code=code).one()
    assert promo.uses_reserved == 1

    _paid_existing(order)
    order = quiet_release(order)
    db.session.expire_all()
    credits = Ledger.query.filter_by(vendor_id=vendor.id, transaction_type="CREDIT").all()
    topup = [c for c in credits if (c.reference_id or "").startswith("promo_topup:")]
    assert len(topup) == 1 and float(topup[0].amount) == 485.0
    assert topup[0].reference_id == f"promo_topup:{promo.id}:{order.id}"
    # The seller ends with full price less 3%: 7,275 + 485 = 7,760 = 8,000 - 240.
    assert sum(float(c.amount) for c in credits) == 7760.0
    use = BuyerPromoUse.query.filter_by(order_id=order.id).one()
    assert use.status == "TOPPED_UP" and float(use.topup_ngn) == 485.0

    # Idempotent: syncing, sweeping and committing again credit nothing more.
    svc.sync_promo_for_order(order.id)
    svc.run_marketing_sweep()
    db.session.commit()
    assert Ledger.query.filter(Ledger.vendor_id == vendor.id,
                               Ledger.reference_id.like("promo_topup:%")).count() == 1

    detail = client.get(f"/api/admin/marketing/promos/{promo.id}", headers=_admin()).get_json()["promo"]
    assert (detail["uses"], detail["delivered"], detail["siiqo_cost_ngn"], detail["topups_ngn"]) == (1, 1, 500.0, 485.0)
    assert detail["orders"][0]["order_id"] == order.id
    listed = client.get("/api/admin/marketing/promos", headers=_admin()).get_json()
    assert listed["totals"]["siiqo_cost_ngn"] == 500.0


def _paid_existing(order):
    escrow = EscrowTransaction.query.filter_by(order_id=order.id).one()
    escrow.status = "IN_ESCROW"
    order.status = "PAID"
    db.session.commit()


def test_promo_minimum_first_order_self_and_wrong_seller(client, make_user, make_vendor, make_product, make_order):
    vendor, store = make_vendor()
    code = _promo(client, vendor)["code"]
    cheap = make_product(store, price=4999)
    res = _checkout(client, cheap, code)
    assert res.status_code == 400 and res.get_json()["code"] == "PROMO_MIN_ORDER"
    assert Order.query.count() == 0                                        # nothing placed

    product = make_product(store, price=6000)
    # A guest who already has a paid order (by email) is not a first-order buyer.
    other_vendor, _ = make_vendor()
    old = make_order(other_vendor, buyer=None, total=1000)
    old.buyer_email = "repeat@example.com"
    db.session.commit()
    _paid(old)
    res = _checkout(client, product, code, email="REPEAT@example.com", phone="+2348099990000")
    assert res.status_code == 400 and res.get_json()["code"] == "PROMO_NOT_FIRST_ORDER"
    # ...nor by phone.
    old.buyer_phone = "0803 555 1234"
    db.session.commit()
    res = _checkout(client, product, code, email="fresh@example.com", phone="+234 803 555 1234")
    assert res.get_json()["code"] == "PROMO_NOT_FIRST_ORDER"

    # The seller can't use their own code.
    res = _checkout(client, product, code, email=vendor.email, phone="+2348070000001")
    assert res.status_code == 400 and res.get_json()["code"] == "PROMO_SELF"

    # A code for another seller.
    other_store_product = make_product(make_vendor()[1], price=9000)
    res = _checkout(client, other_store_product, code, email="x@example.com", phone="+2348070000002")
    assert res.get_json()["code"] == "PROMO_WRONG_SELLER"

    # Inactive and out-of-date codes.
    promo = BuyerPromoCode.query.filter_by(code=code).one()
    promo.ends_at = NOW() - timedelta(days=1)
    db.session.commit()
    assert _checkout(client, product, code, email="y@example.com",
                     phone="+2348070000003").get_json()["code"] == "PROMO_INACTIVE"


def test_multi_seller_cart_discounts_only_that_sellers_order(client, make_vendor, make_product):
    vendor, store = make_vendor()
    other, other_store = make_vendor()
    code = _promo(client, vendor)["code"]
    mine = make_product(store, price=6000)
    theirs = make_product(other_store, price=6000)
    res = _checkout(client, mine, code, extra_items=(theirs,))
    assert res.status_code == 200, res.get_json()
    totals = {o["vendor_id"]: float(o["total_amount"]) for o in res.get_json()["orders"]}
    assert totals == {vendor.id: 5500.0, other.id: 6000.0}


def test_one_use_per_buyer_and_cap_with_race(client, make_vendor, make_product, monkeypatch):
    vendor, store = make_vendor()
    promo_row = _promo(client, vendor, cap=2)
    code = promo_row["code"]
    product = make_product(store, price=6000, stock=50)
    assert _checkout(client, product, code, email="a@example.com", phone="+2348010000001").status_code == 200
    # Same buyer again while the first order is open: refused.
    res = _checkout(client, product, code, email="a@example.com", phone="+2348010000001")
    assert res.get_json()["code"] == "PROMO_NOT_FIRST_ORDER"
    assert _checkout(client, product, code, email="b@example.com", phone="+2348010000002").status_code == 200
    res = _checkout(client, product, code, email="c@example.com", phone="+2348010000003")
    assert res.status_code == 409 and res.get_json()["code"] == "PROMO_USED_UP"

    # Race: two checkouts both pass the check, only one can take the last use.
    promo = BuyerPromoCode.query.filter_by(code=code).one()
    promo.cap = 3
    db.session.commit()
    real_check = svc.check_promo
    monkeypatch.setattr(svc, "check_promo", lambda *a, **k: real_check(*a, **k))
    o1 = Order(vendor_id=vendor.id, total_amount=5500, status="PENDING", payment_method="CRYPTO")
    o2 = Order(vendor_id=vendor.id, total_amount=5500, status="PENDING", payment_method="CRYPTO")
    db.session.add_all([o1, o2])
    db.session.flush()
    checked = [real_check(code, vendor_id=vendor.id, subtotal_ngn=6000, email=f"{n}@example.com",
                          phone=f"+23480200000{n}") for n in (1, 2)]
    svc.reserve_promo(checked[0], o1, email="1@example.com")
    with pytest.raises(svc.MarketingError) as err:
        svc.reserve_promo(checked[1], o2, email="2@example.com")
    assert err.value.code == "PROMO_USED_UP"
    db.session.rollback()


def test_cancel_or_refund_before_release_gives_the_use_back_and_no_topup(client, make_vendor, make_product):
    vendor, store = make_vendor()
    code = _promo(client, vendor, cap=1)["code"]
    product = make_product(store, price=6000)
    res = _checkout(client, product, code)
    order = db.session.get(Order, res.get_json()["orders"][0]["order_id"])
    _paid_existing(order)
    escrow = EscrowTransaction.query.filter_by(order_id=order.id).one()
    escrow.status = "REFUNDED"
    order.status = "REFUNDED"
    db.session.commit()
    db.session.expire_all()
    use = BuyerPromoUse.query.filter_by(order_id=order.id).one()
    assert use.status == "RELEASED" and use.released_reason == "Order refunded"
    assert BuyerPromoCode.query.filter_by(code=code).one().uses_reserved == 0
    assert Ledger.query.filter(Ledger.reference_id.like("promo_topup:%")).count() == 0
    # The use is back: a new buyer can take it.
    assert _checkout(client, product, code, email="next@example.com",
                     phone="+2348010000009").status_code == 200


def test_unpaid_order_gives_use_back_but_late_payment_still_tops_up(client, make_vendor, make_product):
    vendor, store = make_vendor()
    code = _promo(client, vendor, cap=1)["code"]
    product = make_product(store, price=6000)
    order_id = _checkout(client, product, code).get_json()["orders"][0]["order_id"]
    result = svc.run_marketing_sweep(now=NOW() + timedelta(hours=49))
    assert result["promo_released"] == 1
    assert BuyerPromoCode.query.filter_by(code=code).one().uses_reserved == 0
    # The buyer pays later anyway and it is delivered: Siiqo still honours it.
    order = db.session.get(Order, order_id)
    _paid_existing(order)
    _deliver(order)
    db.session.expire_all()
    assert BuyerPromoUse.query.filter_by(order_id=order_id).one().status == "TOPPED_UP"
    assert Ledger.query.filter(Ledger.reference_id.like("promo_topup:%")).count() == 1


def test_promo_on_pay_link_and_preview(client, make_vendor, monkeypatch):
    monkeypatch.setattr(daya_service, "get_or_create_customer", lambda **k: "cus_test")
    monkeypatch.setattr(daya_service, "get_rate", lambda **k: {"rate": 1500, "rate_id": "rate_test"})
    funded = []

    def fund(**k):
        funded.append(k["amount_ngn"])
        return {"id": "fa_test", "amount": k["amount_ngn"],
                "instructions": [{"bank_name": "Test Bank", "account_number": "0000000000",
                                  "account_name": "Siiqo Test"}]}
    monkeypatch.setattr(daya_service, "create_ngn_funding_account", fund)
    vendor, _ = make_vendor()
    code = _promo(client, vendor)["code"]
    link = PaymentLink(vendor_id=vendor.id, title="Ankara", amount=10000, currency="NGN", status="ACTIVE",
                       slug=f"ankara-{uuid.uuid4().hex[:6]}", product_type="physical")
    db.session.add(link)
    db.session.commit()

    preview = client.post("/api/public/promo/check", json={"code": code, "payment_link_id": link.id,
                                                           "email": "ada@example.com"})
    assert preview.status_code == 200 and preview.get_json()["discount_ngn"] == 500.0
    bad = client.post("/api/public/promo/check", json={"code": "NOPE99", "payment_link_id": link.id})
    assert bad.status_code == 404 and bad.get_json()["valid"] is False

    res = client.post(f"/api/marketplace/payment-links/{link.id}/pay", json={
        "buyer_name": "Ada", "buyer_email": "ada@example.com", "buyer_phone": "+2348000000000",
        "payment_method": "bank_transfer", "promo_code": code})
    assert res.status_code == 200, res.get_json()
    assert funded == [9500]                                              # Daya is asked for the discounted amount
    order = db.session.get(Order, res.get_json()["order_id"])
    assert float(order.total_amount) == 9500.0 and order.promo_code == code
    assert float(order.items[0].price_at_purchase) == 10000.0            # the item keeps its listed price
    assert res.get_json()["promo_discount_ngn"] == 500.0


def test_topup_math_uses_the_fee_helper():
    assert svc.promo_topup_amount(500) == Decimal("485.00")
    assert svc.promo_topup_amount(333) == Decimal("323.01")              # 333 - 9.99


# ── 3. seller-to-seller referral ─────────────────────────────────────────────

def _invited(client, make_vendor, auth_headers):
    referrer, _ = make_vendor()
    invite = client.get("/api/vendor/growth-kit", headers=auth_headers(referrer)).get_json()["invite"]["code"]
    seller, store = make_vendor()
    db.session.add(VendorBankAccount(vendor_id=seller.id, bank_name="Test Bank", bank_code="058",
                                     account_number="5555555555", account_name="Seller"))
    db.session.commit()
    svc.credit_signup(seller, {"ref_code": invite})
    db.session.commit()
    assert SellerAcquisition.query.filter_by(seller_user_id=seller.id).one().agent_user_id is None
    return referrer, seller


def _referrals():
    db.session.expire_all()
    return AgentEarning.query.filter_by(kind="referral").all()


def test_referral_is_off_by_default(client, make_user, make_vendor, make_order, auth_headers):
    referrer, seller = _invited(client, make_vendor, auth_headers)
    _sale(make_order, seller, make_user(), 50000)
    assert _referrals() == []
    kit = client.get("/api/vendor/growth-kit", headers=auth_headers(referrer)).get_json()
    assert kit["referral"]["reward_ngn"] == 0


def test_referral_when_set_is_held_then_credited_to_the_ledger(monkeypatch, client, make_user, make_vendor,
                                                               make_order, auth_headers):
    monkeypatch.setenv("SELLER_REFERRAL_REWARD_NGN", "1000")
    referrer, seller = _invited(client, make_vendor, auth_headers)
    _sale(make_order, seller, make_user(), 19999)                         # below N20,000
    assert _referrals() == []
    _sale(make_order, seller, referrer, 30000)                            # the referrer buying: not a real buyer
    assert _referrals() == []
    order = _sale(make_order, seller, make_user(), 20000)
    (row,) = _referrals()
    assert (row.agent_user_id, row.order_id, float(row.amount_ngn), row.status) == (referrer.id, order.id,
                                                                                    1000.0, "HELD")
    _sale(make_order, seller, make_user(), 40000)                         # only one per invited seller
    assert len(_referrals()) == 1

    result = svc.run_marketing_sweep(now=NOW() + timedelta(days=8))
    assert result["referrals_credited"] == 1
    (row,) = _referrals()
    assert row.status == "PAID" and row.paid_reference == f"seller_referral:{row.id}"
    credit = Ledger.query.filter_by(vendor_id=referrer.id, reference_id=f"seller_referral:{row.id}").one()
    assert float(credit.amount) == 1000.0
    svc.run_marketing_sweep(now=NOW() + timedelta(days=9))
    assert Ledger.query.filter_by(vendor_id=referrer.id).count() == 1
    kit = client.get("/api/vendor/growth-kit", headers=auth_headers(referrer)).get_json()
    assert kit["referral"]["reward_ngn"] == 1000 and kit["referral"]["earnings"]["paid"] == 1000.0


def test_referral_refunded_in_hold_is_reversed(monkeypatch, client, make_user, make_vendor, make_order,
                                               auth_headers):
    monkeypatch.setenv("SELLER_REFERRAL_REWARD_NGN", "1000")
    _, seller = _invited(client, make_vendor, auth_headers)
    order = _sale(make_order, seller, make_user(), 25000)
    order.status = "REFUNDED"
    db.session.commit()
    svc.run_marketing_sweep(now=NOW() + timedelta(days=8))
    rows = _referrals()
    assert [r.status for r in rows] == ["REVERSED"]
    assert Ledger.query.filter(Ledger.reference_id.like("seller_referral:%")).count() == 0
