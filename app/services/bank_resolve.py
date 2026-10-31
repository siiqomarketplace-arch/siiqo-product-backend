"""
Confirm a Nigerian bank account's name with Paystack (GET /bank/resolve).

The website shows the name live as the seller types; the server resolves it
again before saving, so the saved name always comes from the bank, never from
the browser.
"""
import logging
import os

import requests

log = logging.getLogger(__name__)

PAYSTACK_BASE_URL = "https://api.paystack.co"


class BankResolveResult(dict):
    """{"status": "resolved" | "rejected" | "unavailable", "account_name": str | None, "message": str}"""

    @property
    def resolved(self):
        return self["status"] == "resolved"

    @property
    def rejected(self):
        return self["status"] == "rejected"


def resolve_account(bank_code: str, account_number: str, timeout: float = 10) -> BankResolveResult:
    key = os.environ.get("PAYSTACK_SECRET_KEY", "")
    if not key:
        return BankResolveResult(status="unavailable", account_name=None, message="Paystack not configured")
    try:
        resp = requests.get(
            f"{PAYSTACK_BASE_URL}/bank/resolve",
            headers={"Authorization": f"Bearer {key}"},
            params={"account_number": account_number, "bank_code": bank_code},
            timeout=timeout,
        )
        data = resp.json()
    except Exception as exc:  # network, timeout, bad JSON
        log.warning("[BANK RESOLVE] unavailable: %s", exc)
        return BankResolveResult(status="unavailable", account_name=None, message=str(exc))

    name = ((data.get("data") or {}).get("account_name") or "").strip() if data.get("status") else ""
    if name:
        return BankResolveResult(status="resolved", account_name=name, message="ok")
    if 400 <= resp.status_code < 500:
        # Paystack answered and could not match the account: the details are wrong.
        return BankResolveResult(status="rejected", account_name=None,
                                 message=data.get("message") or "Could not resolve account")
    return BankResolveResult(status="unavailable", account_name=None,
                             message=data.get("message") or f"HTTP {resp.status_code}")
