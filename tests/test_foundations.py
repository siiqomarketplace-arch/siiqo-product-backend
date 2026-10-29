"""Phase 0B foundations are recorded automatically on every write: order
history and timestamps, fee ledger, trust evidence, funnel events and the
seller identifier graph."""
from app.extensions import db
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.models.foundations import FeeLedgerEntry, OrderStatusHistory, SellerIdentifier
from app.models.payment_link import PaymentLink
from app.models.telemetry import PlatformEvent, TrustEvidence
from app.models.withdrawal import VendorBankAccount
from app.services import foundations


def _pay(order, amount=10000, fee=300, currency="NGN"):
    escrow = EscrowTransaction(order_id=order.id, transaction_number=f"T-{order.id}",
                               status=EscrowStatus.PENDING_PAYMENT, amount=amount, fee_amount=fee,
                               fee_percent=3, currency=currency)
    db.session.add(escrow)
    db.session.commit()
    escrow.status = EscrowStatus.IN_ESCROW
    order.status = "PAID"
    db.session.commit()
    return escrow


def _events(name, **filters):
    return PlatformEvent.query.filter_by(event_name=f"funnel.{name}", **filters).all()


def test_order_history_and_timestamps(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=10000)
    escrow = _pay(order)
    order.status = "DELIVERED"
    db.session.commit()
    escrow.status = EscrowStatus.RELEASED
    order.status = "COMPLETED"
    db.session.commit()

    steps = [h.to_status for h in OrderStatusHistory.query.filter_by(order_id=order.id).order_by(OrderStatusHistory.id)]
    assert steps == ["PENDING", "PAID", "DELIVERED", "COMPLETED"]
    assert order.paid_at and order.delivered_at and order.released_at
    assert order.currency == "NGN" and float(order.fx_rate_to_ngn) == 1.0


def test_fee_ledger_charged_once_and_reversed(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=10000)
    escrow = _pay(order)
    escrow.status = EscrowStatus.IN_ESCROW  # no change: nothing new
    db.session.commit()
    escrow.status = EscrowStatus.REFUNDED
    db.session.commit()
    events = sorted(f.event for f in FeeLedgerEntry.query.filter_by(escrow_id=escrow.id))
    assert events == ["CHARGED", "REVERSED"]
    assert float(FeeLedgerEntry.query.filter_by(escrow_id=escrow.id, event="CHARGED").one().fee_amount) == 300.0


def test_trust_evidence_and_funnel(make_vendor, make_order, make_user):
    vendor, _ = make_vendor()
    buyer = make_user()
    first = make_order(vendor, buyer=buyer, total=5000)
    _pay(first)
    second = make_order(vendor, buyer=buyer, total=7000)
    escrow = _pay(second)
    escrow.status = EscrowStatus.RELEASED
    db.session.commit()

    kinds = [e.evidence_type for e in TrustEvidence.query.filter_by(business_id=vendor.id)]
    assert kinds.count("order_paid") == 2
    assert "escrow_released" in kinds and "repeat_purchase" in kinds
    assert len(_events("paid", business_id=vendor.id)) == 2
    assert len(_events("repeat", business_id=vendor.id)) == 1
    assert len(_events("store", business_id=vendor.id)) == 1
    assert _events("signup", user_id=buyer.id)


def test_identifier_graph(make_vendor):
    vendor, store = make_vendor()
    store.phone = "0803 123 4567"
    store.website = "https://www.mystore.example/shop"
    store.social_links = {"instagram": "https://instagram.com/My.Store", "whatsapp": "+234 803 123 4567"}
    db.session.add(PaymentLink(vendor_id=vendor.id, title="Cake", amount=5000, slug="Cake-Order-1"))
    db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Bank", bank_code="058",
                                     account_number="0123456789", account_name="Owner"))
    db.session.commit()

    ids = {(i.kind, i.value) for i in SellerIdentifier.query.filter_by(vendor_id=vendor.id)}
    assert ("phone", "2348031234567") in ids
    assert ("domain", "mystore.example") in ids
    assert ("handle", "instagram:my.store") in ids
    assert ("paylink_slug", "cake-order-1") in ids
    assert ("payout_account_hash", foundations.payout_account_hash("058", "0123456789")) in ids
    assert not any("0123456789" in v for _, v in ids)  # account number never stored in clear


def test_paylink_view_event(client, make_vendor):
    vendor, _ = make_vendor()
    link = PaymentLink(vendor_id=vendor.id, title="Cake", amount=5000, slug="cake-view-1")
    db.session.add(link)
    db.session.commit()
    client.get("/api/marketplace/payment-links/cake-view-1")
    assert _events("paylink_viewed", business_id=vendor.id)


def test_backfill_is_idempotent(make_vendor):
    vendor, store = make_vendor()
    foundations.backfill_identifiers()
    count = SellerIdentifier.query.filter_by(vendor_id=vendor.id).count()
    foundations.backfill_identifiers()
    assert count > 0 and SellerIdentifier.query.filter_by(vendor_id=vendor.id).count() == count
