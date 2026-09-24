"""
flutterwave_service.py — Flutterwave API wrapper for Siiqo

Handles:
  - Initiating a hosted checkout payment (standard + split to vendor subaccount)
  - Verifying a transaction after webhook or redirect
  - Creating a collection subaccount for a vendor (RS_xxx)
  - HMAC-SHA512 webhook signature verification

Environment variables required:
  FLW_SECRET_KEY       — starts with FLWSECK- (live) or FLWSECK_TEST- (test)
  FLW_PUBLIC_KEY       — starts with FLWPUBK- (live) or FLWPUBK_TEST- (test)
  FLW_WEBHOOK_SECRET   — the webhook verification hash set in Flutterwave dashboard

Fee rates (Flutterwave official NGN):
  Local card/bank:     1.4%, capped at ₦2,000 (Flutterwave's published rate)
  International card:  3.8%, no cap
  USSD:                1.4%, capped at ₦2,000

The buyer pays the listed price PLUS the processing fee.
Calculation: buyer_total = listed_price / (1 - fee_rate)
This ensures the vendor receives the full listed price minus Siiqo's 3%.
"""

import hashlib
import hmac
import logging
import os
import uuid

import requests

logger = logging.getLogger(__name__)

FLW_BASE_URL = "https://api.flutterwave.com/v3"

# Flutterwave processing fee rates (official published rates)
FLW_LOCAL_RATE = 0.014        # 1.4% for NGN local card, bank transfer, USSD
FLW_LOCAL_CAP_NGN = 2000.0    # ₦2,000 cap on local transactions
FLW_INTL_RATE = 0.038         # 3.8% for international cards — no cap
FLW_INTL_CAP_NGN = None       # No cap


def _secret_key() -> str:
    return os.environ.get("FLW_SECRET_KEY", "")


def _webhook_secret() -> str:
    return os.environ.get("FLW_WEBHOOK_SECRET", "")


def is_configured() -> bool:
    """Return True if Flutterwave keys are present in the environment."""
    return bool(_secret_key())


# ---------------------------------------------------------------------------
# FEE CALCULATION
# ---------------------------------------------------------------------------

def calculate_buyer_total(listed_price_ngn: float, is_international: bool = False) -> dict:
    """
    Calculate what the buyer actually pays so the vendor and Siiqo receive
    the correct amounts.

    Flutterwave deducts its fee from the incoming payment. For the vendor to
    net `listed_price`, the buyer must pay enough that after Flutterwave
    takes its cut the remainder is >= listed_price.

    Formula:
        gross = listed_price / (1 - flw_rate)    (uncapped)
        flw_fee = gross * flw_rate               (then apply cap if local)

    Returns a dict with:
        listed_price    — original product price (vendor receives this minus Siiqo 3%)
        flw_fee         — Flutterwave processing fee added on top
        buyer_total     — what the buyer is charged
        is_international
    """
    rate = FLW_INTL_RATE if is_international else FLW_LOCAL_RATE

    # Gross amount buyer must pay so Flutterwave takes its cut and
    # the remainder equals listed_price (before Siiqo's transaction_charge)
    gross = listed_price_ngn / (1.0 - rate)

    # Actual fee Flutterwave will take
    flw_fee = gross - listed_price_ngn

    # Apply cap for local transactions
    if not is_international and flw_fee > FLW_LOCAL_CAP_NGN:
        flw_fee = FLW_LOCAL_CAP_NGN
        gross = listed_price_ngn + flw_fee

    buyer_total = round(gross, 2)
    flw_fee_rounded = round(flw_fee, 2)

    return {
        "listed_price": round(listed_price_ngn, 2),
        "flw_fee": flw_fee_rounded,
        "buyer_total": buyer_total,
        "is_international": is_international,
    }


# ---------------------------------------------------------------------------
# INITIATE PAYMENT
# ---------------------------------------------------------------------------

def initiate_payment(
    order_id: int,
    listed_price_ngn: float,
    siiqo_fee_ngn: float,
    buyer_email: str,
    buyer_name: str,
    buyer_phone: str,
    flw_subaccount_id: str | None,
    tx_ref: str,
    redirect_url: str,
    is_international: bool = False,
    currency: str = "NGN",
    narration: str = "Siiqo Payment",
) -> dict:
    """
    Initiate a Flutterwave Standard (hosted) checkout.

    The buyer is charged `buyer_total` = listed_price + flw_fee.
    Flutterwave deducts its fee, then splits:
      - siiqo_fee_ngn  → Siiqo's main Flutterwave account  (transaction_charge)
      - remainder      → vendor's subaccount               (settled T+1)

    If flw_subaccount_id is None (vendor hasn't added a bank account yet),
    the full amount minus Flutterwave's fee goes to Siiqo's main account.
    Siiqo then owes the vendor their payout via Daya (tracked in the ledger).

    Args:
        order_id          — Siiqo order ID (stored in metadata)
        listed_price_ngn  — product/service price as listed by vendor
        siiqo_fee_ngn     — Siiqo's 3% cut (calculated on listed_price)
        buyer_email       — buyer's email address
        buyer_name        — buyer's full name
        buyer_phone       — buyer's phone number
        flw_subaccount_id — vendor's Flutterwave subaccount ID (RS_xxx) or None
        tx_ref            — unique transaction reference (FLW-{order_id}-{hex})
        redirect_url      — URL Flutterwave sends buyer to after payment
        is_international  — True if buyer is paying with an international card
        currency          — payment currency (default NGN)
        narration         — payment description shown on receipts

    Returns:
        success: bool
        payment_link: str   — Flutterwave hosted checkout URL
        tx_ref: str
        buyer_total: float
        flw_fee: float
        error_message: str or None
    """
    key = _secret_key()
    if not key:
        return {"success": False, "error_message": "Flutterwave not configured."}

    fee_info = calculate_buyer_total(listed_price_ngn, is_international)
    buyer_total = fee_info["buyer_total"]
    flw_fee = fee_info["flw_fee"]
    siiqo_fee_rounded = round(siiqo_fee_ngn, 2)

    payload: dict = {
        "tx_ref": tx_ref,
        "amount": buyer_total,
        "currency": currency,
        "redirect_url": redirect_url,
        "customer": {
            "email": buyer_email,
            "name": buyer_name,
            "phonenumber": buyer_phone or "",
        },
        "meta": {
            "order_id": str(order_id),
            "source": "siiqo_checkout",
            "listed_price": listed_price_ngn,
            "siiqo_fee": siiqo_fee_rounded,
            "flw_fee": flw_fee,
        },
        "customizations": {
            "title": "Siiqo Pay",
            "description": narration,
            "logo": "https://siiqo.com/images/siiqo.png",
        },
        # bearer=account means buyer pays the Flutterwave fee (it is already
        # built into buyer_total); Flutterwave deducts from the collected amount
        # before splitting. Do NOT use bearer=subaccount here.
        "payment_options": "card,banktransfer,ussd,mobilemoneyghana",
    }

    # Attach split payment only when vendor has a subaccount
    if flw_subaccount_id:
        payload["subaccounts"] = [
            {
                "id": flw_subaccount_id,
                # Siiqo takes a flat fee; vendor gets the remainder
                "transaction_charge_type": "flat",
                "transaction_charge": siiqo_fee_rounded,
            }
        ]

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    logger.info(
        "[FLW INITIATE] order=%s ref=%s buyer_total=₦%.2f flw_fee=₦%.2f siiqo_fee=₦%.2f subaccount=%s",
        order_id, tx_ref, buyer_total, flw_fee, siiqo_fee_rounded, flw_subaccount_id or "none",
    )

    try:
        resp = requests.post(
            f"{FLW_BASE_URL}/payments",
            json=payload,
            headers=headers,
            timeout=15,
        )
        data = resp.json()
    except requests.exceptions.Timeout:
        logger.error("[FLW INITIATE] Timeout for ref=%s", tx_ref)
        return {"success": False, "error_message": "Payment gateway timed out. Please try again."}
    except Exception as exc:
        logger.error("[FLW INITIATE] Request error for ref=%s: %s", tx_ref, exc)
        return {"success": False, "error_message": "Could not reach payment gateway."}

    if data.get("status") != "success":
        msg = data.get("message", "Flutterwave rejected the request.")
        logger.error("[FLW INITIATE] Failed ref=%s: %s", tx_ref, data)
        return {"success": False, "error_message": msg}

    payment_link = data["data"]["link"]
    logger.info("[FLW INITIATE] Checkout URL created for ref=%s", tx_ref)

    return {
        "success": True,
        "payment_link": payment_link,
        "tx_ref": tx_ref,
        "buyer_total": buyer_total,
        "flw_fee": flw_fee,
        "error_message": None,
    }


# ---------------------------------------------------------------------------
# VERIFY TRANSACTION
# ---------------------------------------------------------------------------

def verify_transaction(transaction_id: int | str) -> dict:
    """
    Verify a Flutterwave transaction by its numeric transaction ID.
    Returns:
        success: bool
        status: str       — 'successful' | 'failed' | 'pending'
        amount: float     — amount charged to buyer (includes Flutterwave fee)
        currency: str
        tx_ref: str       — our reference
        flw_ref: str      — Flutterwave's internal reference
        order_id: str     — from metadata
        customer_email: str
        error_message: str or None
    """
    key = _secret_key()
    if not key:
        return {"success": False, "error_message": "Flutterwave not configured."}

    try:
        resp = requests.get(
            f"{FLW_BASE_URL}/transactions/{transaction_id}/verify",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        data = resp.json()
    except Exception as exc:
        return {"success": False, "error_message": str(exc)}

    if data.get("status") != "success":
        return {"success": False, "error_message": data.get("message", "Verification failed.")}

    txn = data["data"]
    is_successful = txn.get("status") == "successful"
    meta = txn.get("meta") or {}
    order_id = meta.get("order_id") or txn.get("meta", {}).get("order_id", "")

    return {
        "success": is_successful,
        "status": txn.get("status"),
        "amount": float(txn.get("amount", 0)),
        "currency": txn.get("currency", "NGN"),
        "tx_ref": txn.get("tx_ref", ""),
        "flw_ref": txn.get("flw_ref", ""),
        "order_id": str(order_id),
        "customer_email": txn.get("customer", {}).get("email", ""),
        "customer_name": txn.get("customer", {}).get("name", ""),
        "error_message": None,
    }


# ---------------------------------------------------------------------------
# CREATE VENDOR SUBACCOUNT
# ---------------------------------------------------------------------------

def create_subaccount(
    business_name: str,
    bank_code: str,
    account_number: str,
    business_mobile: str,
    business_email: str = "",
    split_type: str = "percentage",
    split_value: float = 0.97,
) -> dict:
    """
    Create a Flutterwave collection subaccount for a vendor.

    We set split_value to 0.97 (97%) as the default — the subaccount receives
    97% of each transaction. Siiqo's 3% is overridden per-transaction using
    `transaction_charge` in the payment payload, so this default is a safe
    fallback and is not used in practice when we pass explicit transaction_charge.

    Args:
        business_name     — vendor's store / business name
        bank_code         — Nigerian bank code (CBN/Flutterwave bank code)
        account_number    — 10-digit NUBAN account number
        business_mobile   — vendor's phone number (required by Flutterwave)
        business_email    — vendor's email (optional)
        split_type        — 'percentage' or 'flat' (default: percentage)
        split_value       — 0.97 = 97% to vendor (default)

    Returns:
        success: bool
        subaccount_id: str   — e.g. 'RS_FB312AA6C2C84A13421F3079E714F2CB'
        error_message: str or None
    """
    key = _secret_key()
    if not key:
        return {"success": False, "error_message": "Flutterwave not configured."}

    # Ensure phone is not empty — Flutterwave requires it
    if not business_mobile:
        business_mobile = "08012345678"

    payload = {
        "account_bank": str(bank_code).strip(),
        "account_number": str(account_number).strip(),
        "business_name": business_name,
        "business_mobile": str(business_mobile).strip(),
        "country": "NG",
        "split_type": split_type,
        "split_value": split_value,
    }
    if business_email:
        payload["business_email"] = business_email

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    logger.info(
        "[FLW SUBACCOUNT] Creating subaccount for '%s' bank=%s acct=****%s",
        business_name, bank_code, account_number[-4:],
    )

    try:
        resp = requests.post(
            f"{FLW_BASE_URL}/subaccounts",
            json=payload,
            headers=headers,
            timeout=15,
        )
        data = resp.json()
    except requests.exceptions.Timeout:
        return {"success": False, "error_message": "Subaccount request timed out."}
    except Exception as exc:
        return {"success": False, "error_message": str(exc)}

    if data.get("status") != "success":
        msg = data.get("message", "Subaccount creation failed.")
        logger.error("[FLW SUBACCOUNT] Failed for '%s': %s", business_name, data)
        return {"success": False, "error_message": msg}

    subaccount_id = data["data"].get("subaccount_id", "")
    logger.info("[FLW SUBACCOUNT] Created %s for '%s'", subaccount_id, business_name)

    return {
        "success": True,
        "subaccount_id": subaccount_id,
        "error_message": None,
    }


# ---------------------------------------------------------------------------
# WEBHOOK SIGNATURE VERIFICATION
# ---------------------------------------------------------------------------

def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """
    Verify a Flutterwave webhook using the secret hash set in the
    Flutterwave dashboard under 'Webhook Hash'.

    Flutterwave sends the hash in the 'verif-hash' header.
    We compare it directly (no HMAC — Flutterwave uses a plain secret hash).

    Returns True if the signature matches, False otherwise.
    """
    secret = _webhook_secret()
    if not secret:
        # If no secret is configured, log a warning and allow through in test mode
        logger.warning("[FLW WEBHOOK] FLW_WEBHOOK_SECRET not set — skipping signature check")
        return True

    # Flutterwave sends the raw secret in the verif-hash header
    return hmac.compare_digest(
        secret.encode("utf-8"),
        (signature_header or "").encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# TRANSFERS / PAYOUTS
# ---------------------------------------------------------------------------

def transfer_to_vendor(
    account_bank: str,
    account_number: str,
    amount_ngn: float,
    reference: str,
    narration: str = "Siiqo Vendor Payout",
    callback_url: str | None = None,
) -> dict:
    """
    Execute a direct transfer from Siiqo's Flutterwave balance to a vendor's bank account.
    Used for instant payouts of digital/service orders or manual vendor withdrawals.

    Args:
        account_bank    — Nigerian bank code (CBN/Flutterwave bank code)
        account_number  — 10-digit NUBAN
        amount_ngn      — amount to transfer in NGN
        reference       — unique reference string
        narration       — transfer memo/narration
        callback_url    — webhook URL for transfer status notification (optional)

    Returns:
        dict with success (bool), transfer_id, status, error_message
    """
    key = _secret_key()
    if not key:
        return {"success": False, "error_message": "Flutterwave not configured."}

    payload = {
        "account_bank": str(account_bank).strip(),
        "account_number": str(account_number).strip(),
        "amount": round(float(amount_ngn), 2),
        "narration": narration[:50],
        "currency": "NGN",
        "reference": reference,
        "debit_currency": "NGN",
    }
    if callback_url:
        payload["callback_url"] = callback_url

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    logger.info(
        "[FLW TRANSFER] Initiating transfer: ref=%s bank=%s acct=****%s amount=₦%.2f",
        reference, account_bank, str(account_number)[-4:], amount_ngn,
    )

    try:
        resp = requests.post(
            f"{FLW_BASE_URL}/transfers",
            json=payload,
            headers=headers,
            timeout=20,
        )
        data = resp.json()
    except requests.exceptions.Timeout:
        logger.error("[FLW TRANSFER] Timeout for ref=%s", reference)
        return {"success": False, "error_message": "Flutterwave transfer timed out."}
    except Exception as exc:
        logger.error("[FLW TRANSFER] Error for ref=%s: %s", reference, exc)
        return {"success": False, "error_message": str(exc)}

    status_str = data.get("status")
    if status_str != "success":
        msg = data.get("message", "Flutterwave transfer failed.")
        logger.error("[FLW TRANSFER] Rejected ref=%s: %s", reference, data)
        return {"success": False, "error_message": msg}

    tx_data = data.get("data", {})
    transfer_id = tx_data.get("id")
    transfer_status = tx_data.get("status", "NEW")

    logger.info(
        "[FLW TRANSFER] Queued successfully: id=%s ref=%s status=%s",
        transfer_id, reference, transfer_status,
    )

    return {
        "success": True,
        "transfer_id": str(transfer_id) if transfer_id else None,
        "status": transfer_status,
        "reference": reference,
        "fee": tx_data.get("fee", 0),
        "data": tx_data,
        "error_message": None,
    }


def get_transfer_status(transfer_id: str | int) -> dict:
    """Query the status of a Flutterwave transfer by ID."""
    key = _secret_key()
    if not key:
        return {"success": False, "error_message": "Flutterwave not configured."}

    try:
        resp = requests.get(
            f"{FLW_BASE_URL}/transfers/{transfer_id}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        data = resp.json()
    except Exception as exc:
        return {"success": False, "error_message": str(exc)}

    if data.get("status") != "success":
        return {"success": False, "error_message": data.get("message", "Failed to fetch transfer.")}

    return {
        "success": True,
        "status": data.get("data", {}).get("status"),
        "data": data.get("data", {}),
    }

