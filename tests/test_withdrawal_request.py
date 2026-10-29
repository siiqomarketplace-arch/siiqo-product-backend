"""M4: the manual withdrawal (used by the Telegram bot for payouts that did not
go through automatically) records the DEBIT before sending money, never pays
the same balance twice, and gives the balance back when the gateway refuses."""
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.withdrawal import VendorBankAccount, Withdrawal
from app.routes.escrow import _credit_vendor_ledger
from app.routes.withdrawal import _get_ledger_balance
from app.services.escrow import paystack_provider


@pytest.fixture
def vendor_owed(make_vendor, monkeypatch):
    monkeypatch.setenv("ACTIVE_ESCROW_PROVIDER", "paystack")
    vendor, _ = make_vendor()
    db.session.add(VendorBankAccount(vendor_id=vendor.id, bank_name="Test Bank", bank_code="000",
                                     account_number="0000000000", account_name="Test Vendor",
                                     recipient_code="RCP_test", is_default=True))
    _credit_vendor_ledger(vendor.id, 5000, "FAILED-PAYOUT-1", "Payout that did not go through")
    db.session.commit()
    return vendor


def _transfers(monkeypatch, result):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor", fake)
    return calls


def _withdraw(client, vendor, auth_headers, amount=None):
    body = {"amount": amount} if amount else {}
    return client.post("/api/withdrawal/withdrawals", json=body, headers=auth_headers(vendor))


def test_success_debits_once_and_marks_sent(client, vendor_owed, auth_headers, monkeypatch):
    calls = _transfers(monkeypatch, {"success": True, "transfer_code": "TRF_1"})
    res = _withdraw(client, vendor_owed, auth_headers)
    assert res.status_code == 200
    assert len(calls) == 1 and calls[0]["amount_ngn"] == 5000.0
    assert _get_ledger_balance(vendor_owed.id) == Decimal("0")
    w = Withdrawal.query.filter_by(vendor_id=vendor_owed.id).one()
    assert w.status == "SENT" and w.transfer_code == "TRF_1"


def test_second_request_cannot_spend_same_balance(client, vendor_owed, auth_headers, monkeypatch):
    calls = _transfers(monkeypatch, {"success": True, "transfer_code": "TRF_1"})
    assert _withdraw(client, vendor_owed, auth_headers).status_code == 200
    assert _withdraw(client, vendor_owed, auth_headers).status_code == 400
    assert len(calls) == 1


def test_debit_is_written_before_transfer(client, vendor_owed, auth_headers, monkeypatch):
    seen = {}

    def fake(**kwargs):
        seen["balance_during_transfer"] = _get_ledger_balance(vendor_owed.id)
        return {"success": True, "transfer_code": "TRF_1"}
    monkeypatch.setattr(paystack_provider, "paystack_transfer_to_vendor", fake)
    assert _withdraw(client, vendor_owed, auth_headers).status_code == 200
    assert seen["balance_during_transfer"] == Decimal("0")


def test_refused_transfer_gives_balance_back(client, vendor_owed, auth_headers, monkeypatch):
    _transfers(monkeypatch, {"success": False, "error_message": "Account blocked"})
    res = _withdraw(client, vendor_owed, auth_headers)
    assert res.status_code == 400
    assert _get_ledger_balance(vendor_owed.id) == Decimal("5000")
    w = Withdrawal.query.filter_by(vendor_id=vendor_owed.id).one()
    assert w.status == "FAILED"


def test_unknown_outcome_keeps_debit(client, vendor_owed, auth_headers, monkeypatch):
    _transfers(monkeypatch, RuntimeError("gateway timed out"))
    res = _withdraw(client, vendor_owed, auth_headers)
    assert res.status_code == 202
    assert _get_ledger_balance(vendor_owed.id) == Decimal("0")
    w = Withdrawal.query.filter_by(vendor_id=vendor_owed.id).one()
    assert w.status == "PENDING" and "unknown" in w.failure_reason


def test_balance_does_not_count_past_withdrawals_twice(client, vendor_owed, auth_headers, monkeypatch):
    _transfers(monkeypatch, {"success": True, "transfer_code": "TRF_1"})
    assert _withdraw(client, vendor_owed, auth_headers, amount=2000).status_code == 200
    res = client.get("/api/withdrawal/balance", headers=auth_headers(vendor_owed))
    assert Decimal(res.get_json()["available"]) == Decimal("3000")
