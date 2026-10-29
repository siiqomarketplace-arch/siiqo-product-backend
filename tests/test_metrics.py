"""Monthly metrics report counts real records only."""
from datetime import datetime, timezone

from app.extensions import db
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.services.metrics import monthly_metrics


def test_monthly_metrics(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=10000)
    escrow = EscrowTransaction(order_id=order.id, transaction_number="T-M1", status=EscrowStatus.PENDING_PAYMENT,
                               amount=10000, fee_amount=300, fee_percent=3)
    db.session.add(escrow)
    db.session.commit()
    escrow.status = EscrowStatus.IN_ESCROW
    escrow.paid_at = datetime.now(timezone.utc)
    db.session.commit()

    m = monthly_metrics(datetime.now(timezone.utc).strftime("%Y-%m"))
    assert m["orders"]["paid"] == 1 and m["orders"]["gmv_ngn"] == 10000.0
    assert m["revenue"]["fees_charged_ngn"] == 300.0
    assert m["sellers"]["stores_with_a_paid_order"] == 1
    assert m["funnel"].get("paid") == 1


def test_legacy_cleanup_dry_run_changes_nothing(make_vendor):
    from app.tasks.legacy_verification_cleanup import run
    vendor, store = make_vendor()
    vendor.nin = "12345678901"
    store.nin_document_url = "https://bucket/verifications/a.pdf"
    db.session.commit()
    report = run()
    assert report["users_with_stored_nin"] == 1 and report["applied"] is False
    assert vendor.nin == "12345678901"
    assert run(apply=True)["document_urls"] == ["https://bucket/verifications/a.pdf"]
    db.session.refresh(vendor)
    assert vendor.nin is None
