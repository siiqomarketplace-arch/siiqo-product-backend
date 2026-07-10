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
    idem_key = f"customer-{email.lower().replace('@', '-').replace('.', '-')}"
    data = _request(
        "POST",
        "/v1/customers",
        headers=_headers(idem_key),
        json={
            "email": email,
            "first_name": first_name or email.split("@")[0],
            "last_name": last_name or "",
        },
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
    """Create a temporary CRYPTO_ADDRESS funding account. Buyer sends USDT/USDC."""
    daya_chain = NETWORK_TO_DAYA_CHAIN.get(network, network)
    payload = {
        "type": "TEMPORARY",
        "rail": "CRYPTO_ADDRESS",
        "asset": asset,
        "chain": daya_chain,
        "customer": {"customer_id": customer_id},
        "developer_fee": {"percentage": str(developer_fee_pct)},
        "settlement_destination": {
            "type": "INTERNAL_BALANCE",
            "rate_id": rate_id,
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

def transfer_ngn_to_vendor(
    amount_ngn: float,
    bank_code: str,
    account_number: str,
    reference: str,
    order_id: int,
) -> dict:
    """Send NGN from Siiqo Daya withdrawal balance to vendor bank account."""
    if not _key():
        return {"success": False, "error_message": "DAYA_API_KEY not configured"}

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
                    "bank_account": {
                        "account_number": account_number,
                        "bank_code": bank_code,
                    },
                },
            },
        )
        logger.info("[DAYA TRANSFER] NGN%s -> %s/%s ref=%s id=%s status=%s",
                    amount_ngn, bank_code, account_number,
                    reference, data.get("id"), data.get("status"))
        return {"success": True, "transfer_id": data.get("id"),
                "status": data.get("status"), "error_message": None}
    except RuntimeError as exc:
        logger.error("[DAYA TRANSFER] Failed ref=%s: %s", reference, exc)
        return {"success": False, "error_message": str(exc)}


# ---------------------------------------------------------------------------
# Merchant balance
# ---------------------------------------------------------------------------

def get_merchant_balance() -> dict:
    """Return Siiqo Daya merchant balance {collection_balance_usd, withdrawal_balance_usd}."""
    return _request("GET", "/v1/merchant-balance", headers=_headers())
