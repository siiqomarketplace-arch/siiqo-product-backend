# daya_service.py - Daya API wrapper for Siiqo
# Docs: https://docs.daya.co
# All calls go to https://api.daya.co (prod) or https://api.sandbox.daya.co (sandbox)
import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

from app.utils.log_mask import mask_account

logger = logging.getLogger(__name__)


def _base_url() -> str:
    key = os.environ.get("DAYA_API_KEY", "")
    sandbox = (
        os.environ.get("DAYA_SANDBOX", "").lower() == "true"
        or key.startswith("sk_sandbox_")
    )
    return "https://api.sandbox.daya.co" if sandbox else "https://api.daya.co"


def _key() -> str:
    return os.environ.get("DAYA_API_KEY", "")


def _headers(idempotency_key: str | None = None) -> dict:
    h = {
        "X-Api-Key": _key(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if idempotency_key:
        h["X-Idempotency-Key"] = idempotency_key
    return h


class DayaHTTPError(RuntimeError):
    """Daya answered with an HTTP error status (a RuntimeError, as before).
    ``status_code`` lets callers tell a definite refusal (most 4xx: nothing
    was sent) from an unknown outcome (timeouts, 5xx, 409 duplicate)."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


# 4xx answers that do not prove the transfer was refused: 408 timeout,
# 409 duplicate idempotency key (an earlier call may have gone through),
# 425/429 retry later.
_UNCERTAIN_4XX = {408, 409, 425, 429}


def _request(method: str, path: str, **kwargs) -> dict:
    """Central HTTP helper. Raises RuntimeError on failure (DayaHTTPError for
    an HTTP error status)."""
    url = f"{_base_url()}{path}"
    try:
        resp = requests.request(method, url, timeout=15, **kwargs)
    except requests.exceptions.Timeout:
        raise RuntimeError("Daya API timed out. Please try again.")
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Daya API connection error: {exc}")

    if not resp.ok:
        try:
            body = resp.json()
            msg = (
                body.get("error", {}).get("message")
                or body.get("message")
                or resp.text[:200]
            )
        except Exception:
            msg = resp.text[:200]
        logger.error("[DAYA] %s %s -> %s: %s", method, path, resp.status_code, msg)
        raise DayaHTTPError(f"Daya error ({resp.status_code}): {msg}", resp.status_code)

    return resp.json()


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------

def get_rate(asset: str = "USDT", side: str = "BUY") -> dict:
    """Fetch a firm FX rate from Daya (valid ~30 minutes).
    side='BUY'  -> NGN to stablecoin (onramp)
    side='SELL' -> stablecoin to NGN (offramp / crypto direct)
    """
    data = _request(
        "GET",
        "/v1/rates",
        headers=_headers(),
        params={"from": "NGN", "to": asset, "side": side},
    )
    logger.info("[DAYA] Rate %s/%s: NGN%s (id=%s expires=%s)",
                asset, side, data.get("rate"), data.get("rate_id"), data.get("expires_at"))
    return data


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

def get_or_create_customer(email: str, first_name: str = "", last_name: str = "") -> str:
    """Ensure a Daya customer record exists. Returns the Daya customer_id."""
    # Note: POST /v1/customers does NOT use an idempotency key header.
    # Only funding-accounts, transfers, and balance-transfers require it.
    body = {
        "email": email,
        "first_name": first_name or email.split("@")[0],
    }
    # Only include last_name if it has a value -- Daya rejects empty strings
    if last_name and last_name.strip():
        body["last_name"] = last_name.strip()

    data = _request(
        "POST",
        "/v1/customers",
        headers=_headers(),   # no idempotency key
        json=body,
    )
    return data["id"]


# ---------------------------------------------------------------------------
# NGN Onramp - buyer pays Naira, Daya converts to stablecoin
# ---------------------------------------------------------------------------

def create_ngn_funding_account(
    customer_id: str,
    amount_ngn: int,
    rate_id: str,
    idempotency_key: str,
    developer_fee_pct: str = "0",
) -> dict:
    """Create a temporary NGN virtual account. Buyer sends exact NGN amount."""
    payload = {
        "type": "TEMPORARY",
        "rail": "NGN_VIRTUAL_ACCOUNT",
        "currency": "NGN",
        "amount": amount_ngn,
        "customer": {"customer_id": customer_id},
        "developer_fee": {"percentage": str(developer_fee_pct)},
        "settlement_destination": {
            "type": "INTERNAL_BALANCE",
            "rate_id": rate_id,
        },
    }
    data = _request("POST", "/v1/funding-accounts", headers=_headers(idempotency_key), json=payload)
    logger.info("[DAYA] NGN funding account created: id=%s status=%s", data.get("id"), data.get("status"))
    return data


# ---------------------------------------------------------------------------
# Crypto Direct - buyer sends USDT/USDC
# ---------------------------------------------------------------------------

NETWORK_TO_DAYA_CHAIN = {
    "TRC20": "TRON",
    "ERC20": "ETHEREUM",
    "BASE":  "BASE",
    "BEP20": "BSC",
}


def create_crypto_funding_account(
    customer_id: str,
    asset: str,
    network: str,
    rate_id: str,
    idempotency_key: str,
    developer_fee_pct: str = "0",
) -> dict:
    """Create a PERMANENT CRYPTO_ADDRESS funding account settling to INTERNAL_BALANCE.
    Permanent is required because TEMPORARY crypto accounts only support NGN_PAYOUT.
    Buyer sends USDT/USDC to the returned wallet address."""
    daya_chain = NETWORK_TO_DAYA_CHAIN.get(network, network)
    payload = {
        "type": "PERMANENT",
        "rail": "CRYPTO_ADDRESS",
        "asset": asset,
        "chain": daya_chain,
        "customer": {"customer_id": customer_id},
        "developer_fee": {"percentage": str(developer_fee_pct)},
        "settlement_destination": {
            "type": "INTERNAL_BALANCE",
        },
    }
    data = _request("POST", "/v1/funding-accounts", headers=_headers(idempotency_key), json=payload)
    logger.info("[DAYA] Crypto funding account created: id=%s asset=%s chain=%s",
                data.get("id"), asset, daya_chain)
    return data


# ---------------------------------------------------------------------------
# Deposit status polling
# ---------------------------------------------------------------------------

def get_deposit_by_funding_account(funding_account_id: str) -> dict | None:
    """Return the most recent deposit for a funding account, or None."""
    try:
        data = _request(
            "GET",
            "/v1/deposits",
            headers=_headers(),
            params={"funding_account_id": funding_account_id, "limit": 1},
        )
    except RuntimeError:
        return None
    deposits = data.get("data", [])
    return deposits[0] if deposits else None


def _map_daya_status(daya_status: str) -> str:
    """Map Daya deposit status to our internal DayaPayment.status."""
    mapping = {
        "RECEIVED":        "RECEIVED",
        "PROCESSING":      "RECEIVED",
        "REQUIRES_REVIEW": "REQUIRES_REVIEW",
        "COMPLETED":       "COMPLETED",
        "FAILED":          "FAILED",
        "REVERSED":        "FAILED",
        "PENDING":         "PENDING",
        "FLAGGED":         "REQUIRES_REVIEW",
    }
    return mapping.get(daya_status.upper(), "PENDING")


# ---------------------------------------------------------------------------
# Settlement and amount checks (before an order is treated as paid)
# ---------------------------------------------------------------------------
# Daya's webhook docs (docs.daya.co/api-reference/webhooks/events): deposit
# events are deposit.received / processing / requires_review / completed /
# failed / reversed, and "deposit.completed" is the terminal event to credit
# on; RECEIVED and PROCESSING are not final for NGN and crypto deposits. The
# deposit object carries `status`, `amount` (a string) and `currency` (NGN for
# a bank transfer, USDT/USDC for crypto). A deposit is only accepted as payment
# when it is COMPLETED and its amount and currency cover what the server asked
# for; anything missing fails closed (held for admin review).

SETTLED_DEPOSIT_EVENTS = ("deposit.completed", "deposit.received", "deposit.settled")
# Shortfall tolerated for rounding only.
_AMOUNT_TOLERANCE = {"NGN": Decimal("1.00")}
_CRYPTO_TOLERANCE = Decimal("0.01")


def _dec(value):
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def deposit_is_settled(deposit: dict | None, event_type: str | None = None) -> bool:
    """True only for a deposit Daya reports as COMPLETED (never PROCESSING or
    RECEIVED-in-progress). With a webhook, the event must also be a deposit
    completion event."""
    if not deposit:
        return False
    if event_type is not None and event_type not in SETTLED_DEPOSIT_EVENTS:
        return False
    return str(deposit.get("status") or "").upper() == "COMPLETED"


def expected_deposit(dp) -> tuple:
    """(amount, currency) the buyer was asked to send, from server records."""
    if dp.payment_type == "crypto_direct":
        expected = _dec(dp.amount_crypto)
        if expected is None:
            ngn, rate = _dec(dp.amount_ngn), _dec(dp.rate)
            if ngn is not None and rate is not None and rate > 0:
                expected = (ngn / rate).quantize(Decimal("0.000001"))
        return expected, (dp.asset or "USDT").upper()
    return _dec(dp.amount_ngn), "NGN"


def check_deposit_amount(dp, deposit: dict) -> tuple[bool, dict]:
    """(ok, details). ok is True only when the deposited amount and currency
    are present and cover the expected amount. Fails closed otherwise."""
    amount = _dec((deposit or {}).get("amount"))
    currency = str((deposit or {}).get("currency") or "").upper()
    expected, expected_currency = expected_deposit(dp)
    details = {
        "deposit_id": (deposit or {}).get("id"),
        "received_amount": str(amount) if amount is not None else None,
        "received_currency": currency or None,
        "expected_amount": str(expected) if expected is not None else None,
        "expected_currency": expected_currency,
    }
    if amount is None or not currency or expected is None or expected <= 0:
        details["reason"] = "unverified"
        return False, details
    if currency != expected_currency:
        details["reason"] = "currency_mismatch"
        return False, details
    tolerance = _AMOUNT_TOLERANCE.get(currency, _CRYPTO_TOLERANCE)
    if amount + tolerance < expected:
        details["reason"] = "underpaid"
        return False, details
    details["reason"] = "ok"
    return True, details


# ---------------------------------------------------------------------------
# Webhook verification
# ---------------------------------------------------------------------------

def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """Verify Daya HMAC-SHA256 webhook signature from X-Daya-Signature header."""
    secret = os.environ.get("DAYA_WEBHOOK_SECRET", "")
    if not secret:
        # Fail closed: without a configured secret anyone could forge a deposit event.
        logger.error("[DAYA WEBHOOK] DAYA_WEBHOOK_SECRET not set -- rejecting webhook")
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header or "")


# ---------------------------------------------------------------------------
# NGN payout to vendor (post-escrow release)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Paystack → Daya bank code translation
# ---------------------------------------------------------------------------
# Paystack uses their own internal codes for mobile money operators that
# differ from the CBN codes Daya requires. When a vendor saves their bank
# account via Paystack's /bank/resolve flow, we get Paystack's code.
# This map translates those to Daya-compatible CBN codes at payout time.
#
# Sources:
#   OPay CBN code:     100004  (confirmed working in manual transfers)
#   PalmPay CBN code:  999991  (Paystack and CBN use same code for PalmPay)
#   Moniepoint:        50515
#   Kuda:              50211
#   Opay (alt):        100004
# ---------------------------------------------------------------------------
PAYSTACK_TO_DAYA_BANK_CODE: dict[str, str] = {
    "999992": "100004",   # Paystack OPay code → CBN OPay code (confirmed working)
    "100004": "100004",   # Already correct CBN code
    "999991": "100033",   # Paystack PalmPay code → CBN PalmPay code
    "100033": "100033",   # Already correct CBN PalmPay code
    "50515":  "50515",    # Moniepoint — same on both
    "50211":  "50211",    # Kuda — same on both
}


def _translate_bank_code(bank_code: str) -> str:
    """Translate a Paystack bank code to the Daya-compatible CBN code.
    Returns the original code unchanged if no translation is needed.
    """
    translated = PAYSTACK_TO_DAYA_BANK_CODE.get(bank_code, bank_code)
    if translated != bank_code:
        logger.info("[DAYA] Translating Paystack bank code %s → %s", bank_code, translated)
    return translated


def transfer_ngn_to_vendor(
    amount_ngn: float,
    bank_code: str,
    account_number: str,
    reference: str,
    order_id: int,
    account_name: str = "",
) -> dict:
    """Send NGN from Siiqo Daya withdrawal balance to vendor bank account.

    Automatically translates Paystack bank codes to Daya-compatible CBN codes.
    Including account_name in the payload tells Daya to skip its internal
    bank resolution step — prevents INTEGRATION_FAILED for mobile money
    operators (OPay, PalmPay, etc.).
    """
    if not _key():
        return {"success": False, "error_message": "DAYA_API_KEY not configured",
                "outcome": "not_configured"}

    # Translate Paystack code → Daya CBN code before sending
    daya_bank_code = _translate_bank_code(bank_code)

    bank_account_payload: dict = {
        "account_number": account_number,
        "bank_code": daya_bank_code,
    }
    # Only include account_name if provided — it bypasses Daya's resolution
    if account_name and account_name.strip():
        bank_account_payload["account_name"] = account_name.strip()

    try:
        data = _request(
            "POST",
            "/v1/transfers",
            headers=_headers(idempotency_key=reference),
            json={
                "currency": "NGN",
                "amount": str(round(amount_ngn, 2)),
                "reference": reference,
                "destination": {
                    "type": "BANK_ACCOUNT",
                    "bank_account": bank_account_payload,
                },
            },
        )
        logger.info("[DAYA TRANSFER] NGN%s -> %s/%s (original_code=%s) ref=%s id=%s status=%s",
                    amount_ngn, daya_bank_code, mask_account(account_number), bank_code,
                    reference, data.get("id"), data.get("status"))
        return {"success": True, "transfer_id": data.get("id"),
                "status": data.get("status"), "error_message": None, "outcome": "sent"}
    except RuntimeError as exc:
        logger.error("[DAYA TRANSFER] Failed ref=%s: %s", reference, exc)
        # "refused": Daya answered 4xx, nothing was sent. "unknown": timeout,
        # connection error, 5xx or duplicate -- the money may have left.
        status = getattr(exc, "status_code", None)
        refused = status is not None and 400 <= status < 500 and status not in _UNCERTAIN_4XX
        return {"success": False, "error_message": str(exc),
                "outcome": "refused" if refused else "unknown"}


def get_transfer(transfer_id: str) -> dict:
    """GET /v1/transfers/{id} (https://docs.daya.co/api-reference/transfers/get-transfer).
    Returns {success, status, error_message}; status is Daya's word
    (PROCESSING, SETTLED, FAILED, ...)."""
    if not _key():
        return {"success": False, "error_message": "DAYA_API_KEY not configured"}
    try:
        data = _request("GET", f"/v1/transfers/{transfer_id}", headers=_headers())
    except RuntimeError as exc:
        return {"success": False, "error_message": str(exc)}
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    return {"success": True, "status": body.get("status"), "reference": body.get("reference"),
            "error_message": None}


# ---------------------------------------------------------------------------
# Merchant balance transfer (collection -> withdrawal)
# ---------------------------------------------------------------------------

def transfer_collection_to_withdrawal(amount_usd: float, idempotency_key: str) -> dict:
    """Move USD from collection_balance to withdrawal_balance so it can be used for transfers.
    Uses the correct path /v1/merchant/balance/transfer (confirmed working in production).
    """
    data = _request(
        "POST",
        "/v1/merchant/balance/transfer",
        headers=_headers(idempotency_key),
        json={"amount_usd": f"{amount_usd:.4f}"},
    )
    logger.info("[DAYA BALANCE] Moved $%s from collection to withdrawal. New withdrawal: $%s",
                amount_usd, data.get("data", {}).get("withdrawal_balance_usd"))
    return data


# ---------------------------------------------------------------------------
# Merchant balance
# ---------------------------------------------------------------------------

def get_merchant_balance() -> dict:
    """Return Siiqo Daya merchant balance {collection_balance_usd, withdrawal_balance_usd}.
    Correct production path is GET /v1/merchant/balance (confirmed from Daya docs).
    """
    return _request("GET", "/v1/merchant/balance", headers=_headers())


# ---------------------------------------------------------------------------
# Bank account resolution (verify before transferring)
# ---------------------------------------------------------------------------

def resolve_bank_account(bank_code: str, account_number: str) -> dict:
    """
    Verify a Nigerian bank account exists and return the account holder's name.
    Raises RuntimeError if the account cannot be resolved.
    Daya docs: POST /v1/banks/resolve with JSON body {account_number, bank_code}.
    """
    data = _request(
        "POST",
        "/v1/banks/resolve",
        headers=_headers(),
        json={"account_number": account_number, "bank_code": bank_code},
    )
    from app.utils.log_mask import mask_account
    logger.info("[DAYA] Resolved bank account %s/%s", bank_code, mask_account(account_number))
    return data


# ---------------------------------------------------------------------------
# On-chain USDT/USDC withdrawal to vendor wallet (Flow B — crypto direct)
# ---------------------------------------------------------------------------

def withdraw_usdt_to_wallet(
    amount_usd: float,
    token: str,
    chain: str,
    destination_address: str,
    idempotency_key: str,
) -> dict:
    """
    Send USDT or USDC on-chain from Siiqo's Daya withdrawal balance to a vendor's wallet.
    Used when buyer paid with crypto_direct — vendor receives crypto directly.

    Args:
        amount_usd:           Amount in USD to send (Daya converts to token amount)
        token:                "USDT" or "USDC"
        chain:                "TRON" | "ETHEREUM" | "BASE" | "POLYGON" | "SOLANA"
        destination_address:  Vendor's on-chain wallet address
        idempotency_key:      Unique reference per request

    Returns dict with transaction_id on success.
    Raises RuntimeError on failure.
    """
    data = _request(
        "POST",
        "/v1/merchant/balance/withdraw",
        headers=_headers(idempotency_key),
        json={
            "amount_usd":           f"{amount_usd:.6f}",
            "token":                token,
            "chain":                chain,
            "destination_address":  destination_address,
        },
    )
    logger.info(
        "[DAYA WITHDRAW] %s $%.6f -> %s on %s txn=%s",
        token, amount_usd, destination_address, chain,
        data.get("data", {}).get("transaction_id")
    )
    return data
