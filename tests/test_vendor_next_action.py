"""GET /api/vendor/next-action returns the read-only facts the dashboard home
uses to pick one next best action; /api/vendor/balance adds released_this_month."""
from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.payment_link import PaymentLink
from app.models.telemetry import PlatformEvent
from app.models.withdrawal import VendorBankAccount


def _facts(client, auth_headers, vendor):
    res = client.get("/api/vendor/next-action", headers=auth_headers(vendor))
    assert res.status_code == 200
    return res.get_json()


def _link(vendor, slug="pay-abc", status="ACTIVE", link_type="PAY_LINK"):
    link = PaymentLink(vendor_id=vendor.id, title="Shoes", slug=slug, status=status,
                       link_type=link_type)
    db.session.add(link)
    db.session.commit()
    return link


def test_requires_login(client):
    assert client.get("/api/vendor/next-action").status_code == 401


def test_buyer_is_refused(client, make_user, auth_headers):
    buyer = make_user()
    assert client.get("/api/vendor/next-action", headers=auth_headers(buyer)).status_code == 403


def test_new_seller_has_nothing(client, make_vendor, auth_headers):
    vendor, store = make_vendor()
    f = _facts(client, auth_headers, vendor)
    assert f["has_storefront"] is True
    assert f["store_slug"] == store.store_slug
    assert f["product_count"] == 0
    assert f["pay_link_count"] == 0
    assert f["latest_pay_link_slug"] is None
    assert f["pay_link_opened"] is False
    assert f["unpaid_orders"] == 0
    assert f["orders_to_ship"] == 0
    assert f["has_bank_account"] is False


def test_counts_only_live_products(client, make_vendor, make_product, auth_headers):
    vendor, store = make_vendor()
    make_product(store)
    make_product(store, is_deleted=True)
    make_product(store).is_active = False
    db.session.commit()
    assert _facts(client, auth_headers, vendor)["product_count"] == 1


def test_pay_links_ignore_expired_and_invoices(client, make_vendor, auth_headers):
    vendor, _ = make_vendor()
    _link(vendor, slug="gone", status="EXPIRED")
    _link(vendor, slug="inv", link_type="INVOICE")
    assert _facts(client, auth_headers, vendor)["pay_link_count"] == 0
    _link(vendor, slug="live")
    f = _facts(client, auth_headers, vendor)
    assert f["pay_link_count"] == 1
    assert f["latest_pay_link_slug"] == "live"
    assert f["pay_link_opened"] is False


def test_pay_link_opened_by_view_event(client, make_vendor, auth_headers):
    vendor, _ = make_vendor()
    _link(vendor)
    db.session.add(PlatformEvent(event_name="funnel.paylink_viewed", business_id=vendor.id,
                                 source="server"))
    db.session.commit()
    assert _facts(client, auth_headers, vendor)["pay_link_opened"] is True


def test_pay_link_opened_by_order(client, make_vendor, make_order, auth_headers):
    vendor, _ = make_vendor()
    link = _link(vendor)
    make_order(vendor, payment_link_id=link.id)
    assert _facts(client, auth_headers, vendor)["pay_link_opened"] is True


def test_other_sellers_views_do_not_count(client, make_vendor, auth_headers):
    vendor, _ = make_vendor()
    other, _ = make_vendor()
    _link(vendor)
    db.session.add(PlatformEvent(event_name="funnel.paylink_viewed", business_id=other.id,
                                 source="server"))
    db.session.commit()
    assert _facts(client, auth_headers, vendor)["pay_link_opened"] is False


def test_unpaid_orders_are_recent_pending_only(client, make_vendor, make_order, auth_headers):
    vendor, _ = make_vendor()
    make_order(vendor)  # PENDING, now
    make_order(vendor, created_at=datetime.utcnow() - timedelta(days=30))  # abandoned
    for status in ("PAID", "PROCESSING", "SHIPPED", "COMPLETED", "CANCELLED"):
        make_order(vendor).status = status
    db.session.commit()
    f = _facts(client, auth_headers, vendor)
    assert f["unpaid_orders"] == 1
    assert f["orders_to_ship"] == 2  # PAID + PROCESSING


def test_bank_account_from_table_or_storefront(client, make_vendor, auth_headers):
    vendor, store = make_vendor()
    store.bank_code = "058"
    store.account_number = "0000000000"
    db.session.commit()
    assert _facts(client, auth_headers, vendor)["has_bank_account"] is True

    vendor2, _ = make_vendor()
    db.session.add(VendorBankAccount(vendor_id=vendor2.id, bank_name="Test Bank", bank_code="999",
                                     account_number="0000000001", account_name="Test User"))
    db.session.commit()
    assert _facts(client, auth_headers, vendor2)["has_bank_account"] is True


def test_endpoint_is_read_only(client, make_vendor, auth_headers):
    vendor, _ = make_vendor()
    before = (VendorBankAccount.query.count(), PlatformEvent.query.count(), PaymentLink.query.count())
    _facts(client, auth_headers, vendor)
    after = (VendorBankAccount.query.count(), PlatformEvent.query.count(), PaymentLink.query.count())
    assert before == after


def test_balance_released_this_month_is_net_and_this_month(client, make_vendor, make_order,
                                                          auth_headers):
    vendor, _ = make_vendor()
    now = datetime.now(timezone.utc)
    last_month = now.replace(day=1) - timedelta(days=2)
    rows = [
        (EscrowStatus.RELEASED, 10000, 300, now),
        (EscrowStatus.RELEASED, 5000, 150, last_month),  # before this month
        (EscrowStatus.IN_ESCROW, 8000, 240, None),        # still held
    ]
    for i, (status, amount, fee, released_at) in enumerate(rows):
        order = make_order(vendor, total=amount)
        db.session.add(EscrowTransaction(order_id=order.id, transaction_number=f"R-{i}-{order.id}",
                                         status=status, amount=amount, fee_amount=fee,
                                         released_at=released_at))
    db.session.commit()

    body = client.get("/api/vendor/balance", headers=auth_headers(vendor)).get_json()
    assert body["released_this_month"] == 9700.0
    assert body["locked_balance"] == 8000.0
    assert body["total_fees"] == 450.0
