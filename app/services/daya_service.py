"""
"""
daya_service.py — Daya API wrapper for Siiqo

Daya docs: https://docs.daya.co
All calls go to https://api.daya.co (production) or
https://api.sandbox.daya.co (sandbox when DAYA_SANDBOX=true).

Money flow recap
----------------
NGN onramp  (buyer pays Naira):
  GET  /v1/rates?from=NGN&to=<asset>&side=BUY  → firm rate (valid ~30 min)
  POST /v1/funding-accounts                     → temporary NGN virtual account
  Buyer transfers NGN → Daya converts → credits Siiqo merchant balance
  Webhook: deposit.completed

Crypto direct (buyer sends USDT/USDC):
  GET  /v1/rates?from=NGN&to=<asset>&side=SELL → firm rate
  POST /v1/funding-accounts (CRYPTO_ADDRESS)   → on-chain address
  Buyer sends crypto → Daya credits Siiqo merchant balance
  Webhook: deposit.completed

Siiqo then pays vendor via POST /v1/transfers (NGN bank) or on-chain
withdrawal once the order is confirmed — handled in payments.py.

Environment variables
---------------------
DAYA_API_KEY          — live or sandbox key (sk_live_... / sk_sandbox_...)
DAYA_SANDBOX          — "true" forces sandbox base URL regardless of key prefix
DAYA_WEBHOOK_SECRET   — HMAC-SHA256 secret for webhook signature verification
"""

import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# ── Base URL ──────────────────────────────────────────────────────────────────

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
    """
    Central HTTP helper.
    Returns the parsed JSON response dict on success.
    Raises RuntimeError with a human-readable message on failure.
    """
    url = f"{_base_url()}{path}"
    try:
        resp = requests.request(method, url, timeout=15, **kwargs)
    except requests.exceptions.Timeout:
        raise RuntimeError("Daya API timed out. Please try again.")
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Daya API connection error: {exc}")

    if not resp.ok:
        # Try to extract Daya's error message
        try:
            body = resp.json()
            msg = (
                body.get("error", {}).get("message")
                or body.get("message")
                or resp.text[:200]
            )
        except Exception:
            msg = resp.text[:200]
        logger.error("[DAYA] %s %s → %s: %s", method, path, resp.status_code, msg)
        raise RuntimeError(f"Daya error ({resp.status_code}): {msg}")

    return resp.json()


# ── Rates ─────────────────────────────────────────────────────────────────────

def get_rate(asset: str = "USDT", side: str = "BUY") -> dict:
    """
    Fetch a firm FX rate from Daya (valid ~30 minutes).

    Args:
        asset: "USDT" or "USDC"
        side:  "BUY"  — NGN → stablecoin (onramp, buyer pays NGN)
               "SELL" — stablecoin → NGN (offramp, buyer sends crypto)

    Returns dict with: rate_id, rate (NGN per 1 stablecoin),
    inverse_rate, expires_at, min_deposit_ngn, fee_bps
    """
    data = _request(
        "GET",
        "/v1/rates",
        headers=_headers(),
        params={"from": "NGN", "to": asset, "side": side},
    )
    logger.info("[DAYA] Rate %s/%s: ₦%s (id=%s expires=%s)",
                asset, side, data.get("rate"), data.get("rate_id"), data.get("expires_at"))
    return data


# ── Customers ─────────────────────────────────────────────────────────────────

def get_or_create_customer(email: str, first_name: str = "", last_name: str = "") -> str:
    """
    Ensure a Daya customer record exists for this email.
    Daya customers are scoped per merchant so the same email is fine
    across different merchants.

    Returns the Daya customer_id (UUID string).
    We store this on DayaPayment.buyer_daya_customer_id for reuse.
    """
    # Daya has no "get by email" endpoint — we always POST and let them
    # deduplicate on their side (idempotent by email within same merchant).
    idem_key = f"customer-{email.lower().replace('@', '-').replace('.', '-')}"
    try:
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
    except RuntimeError as exc:
        # If customer already exists Daya may return 409 — surface the id
        # from the error body if possible; otherwise re-raise.
        raise exc


# ── NGN Onramp — buyer pays Naira ─────────────────────────────────────────────

def create_ngn_funding_account(
    customer_id: str,
    amount_ngn: int,
    rate_id: str,
    idempotency_key: str,
    developer_fee_pct: str = "0",
) -> dict:
    """
    Create a temporary NGN virtual account for a specific order amount.
    The buyer transfers exactly amount_ngn to the returned bank account.
    Daya converts to stablecoin and credits Siiqo's INTERNAL_BALANCE.

    Args:
        customer_id:      Daya customer id for the buyer
        amount_ngn:       Exact NGN amount the buyer must send (integer)
        rate_id:          Rate id from get_rate() — guarantees locked FX
        idempotency_key:  Unique key per request (use order reference)
        developer_fee_pct: Siiqo's cut as a percentage string e.g. "6"

    Returns the full Daya funding_account object including instructions[]
    with bank_name, account_number, account_name.
    """
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
    data = _request(
        "POST",
        "/v1/funding-accounts",
        headers=_headers(idempotency_key),
        json=payload,
    )
    logger.info(
        "[DAYA] NGN funding account created: id=%s status=%s",
        data.get("id"), data.get("status"),
    )
    return data


# ── Crypto Direct — buyer sends USDT/USDC ─────────────────────────────────────

# Map our network labels to Daya chain identifiers
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
    """
    Create a temporary CRYPTO_ADDRESS funding account.
    Buyer sends USDT/USDC to the returned wallet address.
    Daya credits Siiqo's INTERNAL_BALANCE after settlement.

    Args:
        customer_id:  Daya customer id
        asset:        "USDT" or "USDC"
        network:      "TRC20" | "ERC20" | "BASE" | "BEP20"
        rate_id:      From get_rate(side="SELL")
        idempotency_key: Unique per request

    Returns the Daya funding_account object including instructions[]
    with asset, chain, address.
    """
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
    data = _request(
        "POST",
        "/v1/funding-accounts",
        headers=_headers(idempotency_key),
        json=payload,
    )
    logger.info(
        "[DAYA] Crypto funding account created: id=%s asset=%s chain=%s",
        data.get("id"), asset, daya_chain,
    )
    return data


# ── Deposit status polling ────────────────────────────────────────────────────

def get_deposit_by_funding_account(funding_account_id: str) -> dict | None:
    """
    List deposits for a funding account and return the most recent one.
    Returns None if no deposits exist yet.

    Mapped Daya statuses → our DayaPayment.status:
      RECEIVED / PROCESSING      → RECEIVED
      REQUIRES_REVIEW            → REQUIRES_REVIEW
      COMPLETED                  → COMPLETED
      FAILED / REVERSED          → FAILED
    """
    try:
        data = _request(
            "GET",
            "/v1/deposits",
            headers=_headers(),
            params={
                "funding_account_id": funding_account_id,
                "limit": 1,
            },
        )
    except RuntimeError:
        return None

    deposits = data.get("data", [])
    if not deposits:
        return None
    return deposits[0]


def _map_daya_status(daya_status: str) -> str:
    """Map Daya deposit status to our internal DayaPayment.status."""
    mapping = {
        "RECEIVED":        "RECEIVED",
        "PROCESSING":      "RECEIVED",     # still in-flight, treat as received
        "REQUIRES_REVIEW": "REQUIRES_REVIEW",
        "COMPLETED":       "COMPLETED",
        "FAILED":          "FAILED",
        "REVERSED":        "FAILED",
        "PENDING":         "PENDING",
        "FLAGGED":         "REQUIRES_REVIEW",
    }
    return mapping.get(daya_status.upper(), "PENDING")


# ── Webhook verification ──────────────────────────────────────────────────────

def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """
    Verify Daya's HMAC-SHA256 webhook signature.

    Daya sends the signature in the X-Daya-Signature header as a hex digest.
    We compute HMAC-SHA256(secret, raw_body) and compare.

    Returns True if the signature is valid, False otherwise.
    """
    secret = os.environ.get("DAYA_WEBHOOK_SECRET", "")
    if not secret:
        logger.warning("[DAYA WEBHOOK] DAYA_WEBHOOK_SECRET not set — skipping verification")
        return True  # Allow in dev; enforce in production via env var

    expected = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header or "")


# ── NGN payout to vendor (post-escrow release) ────────────────────────────────

def transfer_ngn_to_vendor(
    amount_ngn: float,
    bank_code: str,
    account_number: str,
    reference: str,
    order_id: int,
) -> dict:
    """
    Send NGN from Siiqo's Daya withdrawal balance to a vendor's bank account.
    Used when an escrow order paid via crypto is released.

    Prerequisites:
    - Siiqo's Daya withdrawal_balance_usd must have sufficient funds.
    - The collection_balance must first be moved to withdrawal_balance via
      the balance transfer endpoint (handled separately / manually for now).

    Returns dict with: success, transfer_id, status, error_message
    """
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
        logger.info(
            "[DAYA TRANSFER] ₦%s → %s/%s ref=%s id=%s status=%s",
            amount_ngn, bank_code, account_number,
            reference, data.get("id"), data.get("status"),
        )
        return {
            "success": True,
            "transfer_id": data.get("id"),
            "status": data.get("status"),
            "error_message": None,
        }
    except RuntimeError as exc:
        logger.error("[DAYA TRANSFER] Failed ref=%s: %s", reference, exc)
        return {"success": False, "error_message": str(exc)}


# ── Merchant balance ──────────────────────────────────────────────────────────

def get_merchant_balance() -> dict:
    """
    Return Siiqo's current Daya merchant balance.
    {collection_balance_usd, withdrawal_balance_usd}
    """
    return _request("GET", "/v1/merchant-balance", headers=_headers())
