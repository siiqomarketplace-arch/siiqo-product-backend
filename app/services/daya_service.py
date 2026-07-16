# daya_service.py - Daya API wrapper for Siiqo
# Docs: https://docs.daya.co
# All calls go to https://api.daya.co (prod) or https://api.sandbox.daya.co (sandbox)
import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime, timezone

import requests

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


def _request(method: str, path: str, **kwargs) -> dict:
    """Central HTTP helper. Raises RuntimeError on failure."""
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
        raise RuntimeError(f"Daya error ({resp.status_code}): {msg}")

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
# Webhook verification
# ---------------------------------------------------------------------------

def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """Verify Daya HMAC-SHA256 webhook signature from X-Daya-Signature header."""
    secret = os.environ.get("DAYA_WEBHOOK_SECRET", "")
    if not secret:
        logger.warning("[DAYA WEBHOOK] DAYA_WEBHOOK_SECRET not set -- skipping verification")
        return True

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
        return {"success": False, "error_message": "DAYA_API_KEY not configured"}

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
                    amount_ngn, daya_bank_code, account_number, bank_code,
                    reference, data.get("id"), data.get("status"))
        return {"success": True, "transfer_id": data.get("id"),
                "status": data.get("status"), "error_message": None}
    except RuntimeError as exc:
        logger.error("[DAYA TRANSFER] Failed ref=%s: %s", reference, exc)
        return {"success": False, "error_message": str(exc)}


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
    logger.info("[DAYA] Resolved bank account %s/%s -> %s",
                bank_code, account_number, data.get("account_name"))
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
