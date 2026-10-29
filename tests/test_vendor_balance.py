"""The earnings card shows the Siiqo fees actually taken on released orders."""
from app.extensions import db
from app.models.escrow import EscrowStatus, EscrowTransaction


def test_total_fees_is_sum_of_released_fees(client, make_vendor, make_order, auth_headers):
    vendor, _ = make_vendor()
    for i, (status, fee) in enumerate([(EscrowStatus.RELEASED, 300), (EscrowStatus.RELEASED, 150),
                                       (EscrowStatus.IN_ESCROW, 999)]):
        order = make_order(vendor, total=10000)
        db.session.add(EscrowTransaction(order_id=order.id, transaction_number=f"T-{i}-{order.id}",
                                         status=status, amount=10000, fee_amount=fee))
    db.session.commit()

    res = client.get("/api/vendor/balance", headers=auth_headers(vendor))
    assert res.status_code == 200
    assert res.get_json()["total_fees"] == 450.0
