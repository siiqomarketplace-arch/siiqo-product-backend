"""M1: a successful Flutterwave transaction for one order must never confirm
another order, through /payments/flutterwave/status or /escrow/status."""
import pytest

from app.extensions import db
from app.models.escrow import EscrowStatus, EscrowTransaction
from app.routes.payments import flutterwave_verification_matches
from app.services import flutterwave_service


def _escrow(order, ref, amount=5000, currency="NGN"):
    e = EscrowTransaction(order_id=order.id, transaction_number=ref, status=EscrowStatus.PENDING_PAYMENT,
                          amount=amount, currency=currency, fee_amount=amount * 0.03,
                          payscrow_transaction_id=ref)
    db.session.add(e)
    db.session.commit()
    return e


def _verif(tx_ref, order_id, amount=5150.0, currency="NGN", status="successful"):
    return {"success": status == "successful", "status": status, "tx_ref": tx_ref,
            "order_id": str(order_id), "amount": amount, "currency": currency}


# ── the shared rule ─────────────────────────────────────────────────────────

def test_matching_transaction_is_accepted(make_vendor, make_order):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=5000, payment_method="FLUTTERWAVE")
    e = _escrow(order, f"FLW-PL-{order.id}-AAAA")
    assert flutterwave_verification_matches(e, _verif(e.transaction_number, order.id)) is True


@pytest.mark.parametrize("change", ["other_order", "low_amount", "wrong_currency", "failed"])
def test_non_matching_transaction_is_rejected(make_vendor, make_order, change):
    vendor, _ = make_vendor()
    order = make_order(vendor, total=5000, payment_method="FLUTTERWAVE")
    other = make_order(vendor, total=100, payment_method="FLUTTERWAVE")
    e = _escrow(order, f"FLW-PL-{order.id}-AAAA")
    v = {
        "other_order": _verif(f"FLW-PL-{other.id}-BBBB", other.id, amount=103.0),
        "low_amount": _verif(e.transaction_number, order.id, amount=100.0),
        "wrong_currency": _verif(e.transaction_number, order.id, currency="USD"),
        "failed": _verif(e.transaction_number, order.id, status="failed"),
    }[change]
    assert flutterwave_verification_matches(e, v) is False


# ── both endpoints ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/api/payments/flutterwave/status", "/api/escrow/status"])
def test_other_orders_transaction_does_not_confirm(client, make_vendor, make_order, monkeypatch, path):
    vendor, _ = make_vendor()
    victim = make_order(vendor, total=500000, payment_method="FLUTTERWAVE")
    cheap = make_order(vendor, total=100, payment_method="FLUTTERWAVE")
    e = _escrow(victim, f"FLW-PL-{victim.id}-AAAA", amount=500000)
    monkeypatch.setattr(flutterwave_service, "verify_transaction",
                        lambda tx_id: _verif(f"FLW-PL-{cheap.id}-BBBB", cheap.id, amount=103.0))

    client.get(f"{path}?order_id={victim.id}&transaction_id=999")

    db.session.expire_all()
    assert db.session.get(EscrowTransaction, e.id).status == EscrowStatus.PENDING_PAYMENT
