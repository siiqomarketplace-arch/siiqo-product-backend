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

def fee_added_by() -> str:
    """Who adds Flutterwave's processing fee to what the buyer pays, so the
    buyer pays it exactly ONCE (Order 639: Siiqo added NGN 35.50 AND
    Flutterwave added its own NGN 50.71 "paid by customer").

    Flutterwave's API docs document no /v3/payments parameter for who bears
    the fee (https://developer.flutterwave.com/api/payments/create-payment ;
    only a fee lookup, https://developer.flutterwave.com/api/transactions/get-transaction-fee),
    so it is the dashboard setting. FLW_FEE_ADDED_BY must match it:

      "flutterwave" (default; the dashboard makes customers pay the fee, as
                    seen on 26 Nov 2026): Siiqo charges the listed price and
                    adds nothing; Flutterwave shows and adds its fee on its
                    own payment page.
      "siiqo"       (only after the dashboard is switched so the merchant pays
                    the fee): Siiqo adds its fee estimate to the amount and
                    Flutterwave takes its fee out of what Siiqo receives."""
    v = (os.environ.get("FLW_FEE_ADDED_BY") or "flutterwave").strip().lower()
    return "siiqo" if v == "siiqo" else "flutterwave"


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
    if fee_added_by() == "flutterwave":
        # Flutterwave adds its own fee on its payment page: Siiqo adds nothing.
        return {
            "listed_price": round(listed_price_ngn, 2),
            "flw_fee": 0.0,
            "buyer_total": round(listed_price_ngn, 2),
            "is_international": is_international,
            "fee_added_by": "flutterwave",
        }

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
        "fee_added_by": "siiqo",
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
    subaccounts: list | None = None,
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
        # Who pays Flutterwave's fee: see fee_added_by().
        "payment_options": "card,banktransfer,ussd,mobilemoneyghana",
    }

    # No subaccount split is ever sent (Order 639). Flutterwave's verify
    # response has no per-payment proof of a split
    # (https://developer.flutterwave.com/api/transactions/verify-transaction),
    # and a bank-transfer payment with a subaccount attached settled entirely
    # to Siiqo. All Flutterwave money settles to Siiqo; Siiqo transfers the
    # seller's share at release (app/routes/escrow.py).
    if flw_subaccount_id or subaccounts:
        logger.warning("[FLW INITIATE] Subaccount split ignored for ref=%s: Siiqo pays sellers by transfer", tx_ref)

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    logger.info(
        "[FLW INITIATE] order=%s ref=%s buyer_total=₦%.2f flw_fee=₦%.2f (added by %s) siiqo_fee=₦%.2f",
        order_id, tx_ref, buyer_total, flw_fee, fee_info.get("fee_added_by"), siiqo_fee_rounded,
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
        "fee_added_by": fee_info.get("fee_added_by"),
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

def normalize_flw_bank_code(code: str) -> str:
    """
    Normalize bank codes for Flutterwave.
    PalmPay uses CBN code '100033' (and NIBSS '090405') across Paystack & Daya,
    but Flutterwave's routing directory uses '999991'.
    """
    c = str(code or "").strip()
    if c in ("100033", "090405"):
        return "999991"
    return c


# Siiqo's commission on a Flutterwave subaccount, as a decimal (3%).
SIIQO_FLW_SUBACCOUNT_SPLIT = 0.03


def create_subaccount(
    business_name: str,
    bank_code: str,
    account_number: str,
    business_mobile: str,
    business_email: str = "",
    split_type: str = "percentage",
    split_value: float | None = None,
) -> dict:
    """
    Create a Flutterwave collection subaccount for a vendor.

    split_value is the commission the MAIN account (Siiqo) gets: Flutterwave's
    create-subaccount reference says split_value is "The amount you want to
    get as a commission on each transaction" and percentages are decimals
    (https://developer.flutterwave.com/api/collection-subaccounts/create-a-sub-account).
    The dashboard confirmed it: 0.97 showed "Your share 97% / Subaccount's
    share 3%". So the default is SIIQO_FLW_SUBACCOUNT_SPLIT (0.03 = Siiqo's
    3%, app/services/fees.py). Siiqo no longer attaches subaccounts at
    checkout; this default is a safety net if one is ever used.

    Args:
        business_name     — vendor's store / business name
        bank_code         — Nigerian bank code (CBN/Flutterwave bank code)
        account_number    — 10-digit NUBAN account number
        business_mobile   — vendor's phone number (required by Flutterwave)
        business_email    — vendor's email (optional)
        split_type        — 'percentage' or 'flat' (default: percentage)
        split_value       — Siiqo's commission (default 0.03 = 3%)

    Returns:
        success: bool
        subaccount_id: str   — e.g. 'RS_FB312AA6C2C84A13421F3079E714F2CB'
        error_message: str or None
    """
    key = _secret_key()
    if not key:
        return {"success": False, "error_message": "Flutterwave not configured."}

    if split_value is None:
        split_value = SIIQO_FLW_SUBACCOUNT_SPLIT

    # Ensure phone is not empty — Flutterwave requires it
    if not business_mobile:
        business_mobile = "08012345678"

    normalized_bank = normalize_flw_bank_code(bank_code)

    payload = {
        "account_bank": normalized_bank,
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
        # Fail closed: without a configured secret anyone could forge a webhook.
        logger.error("[FLW WEBHOOK] FLW_WEBHOOK_SECRET not set — rejecting webhook")
        return False

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

    normalized_bank = normalize_flw_bank_code(account_bank)

    payload = {
        "account_bank": normalized_bank,
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


def get_transfer_rate(source_currency: str, destination_currency: str, amount: float) -> dict:
    """Flutterwave's rate for a transfer that debits `source_currency` and pays
    `amount` of `destination_currency` (GET /v3/transfers/rates)."""
    key = _secret_key()
    if not key:
        return {"success": False, "error_message": "Flutterwave not configured."}
    try:
        resp = requests.get(
            f"{FLW_BASE_URL}/transfers/rates",
            params={"amount": amount, "destination_currency": destination_currency,
                    "source_currency": source_currency},
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        data = resp.json()
    except Exception as exc:
        return {"success": False, "error_message": str(exc)}
    if data.get("status") != "success" or not isinstance(data.get("data"), dict):
        return {"success": False, "error_message": data.get("message", "Rate unavailable.")}
    d = data["data"]
    return {"success": True, "rate": d.get("rate"), "source": d.get("source") or {},
            "destination": d.get("destination") or {}}


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



# ---------------------------------------------------------------------------
# SUBACCOUNT SPLIT REPAIR (existing subaccounts created with 0.97)
# ---------------------------------------------------------------------------

def list_subaccounts(page: int = 1) -> dict:
    """GET /v3/subaccounts?page=N
    (https://developer.flutterwave.com/api/collection-subaccounts/list-all-subaccounts).
    Returns {success, items: [{id, subaccount_id, split_type, split_value}], has_more}."""
    key = _secret_key()
    if not key:
        return {"success": False, "error_message": "Flutterwave not configured."}
    try:
        resp = requests.get(f"{FLW_BASE_URL}/subaccounts", params={"page": page},
                            headers={"Authorization": f"Bearer {key}"}, timeout=15)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error_message": str(exc)}
    if data.get("status") != "success":
        return {"success": False, "error_message": data.get("message", "Could not list subaccounts.")}
    items = [{"id": d.get("id"), "subaccount_id": d.get("subaccount_id"),
              "split_type": d.get("split_type"), "split_value": d.get("split_value")}
             for d in (data.get("data") or []) if isinstance(d, dict)]
    info = (data.get("meta") or {}).get("page_info") or {}
    try:
        has_more = int(info.get("current_page") or page) < int(info.get("total_pages") or page)
    except (TypeError, ValueError):
        has_more = False
    return {"success": True, "items": items, "has_more": has_more}


def update_subaccount_split(numeric_id, split_type: str = "percentage",
                            split_value: float = SIIQO_FLW_SUBACCOUNT_SPLIT) -> dict:
    """PUT /v3/subaccounts/{id} with split_type/split_value
    (https://developer.flutterwave.com/api/collection-subaccounts/update-a-subaccount).
    `numeric_id` is the subaccount's numeric id, not its RS_ code."""
    key = _secret_key()
    if not key:
        return {"success": False, "error_message": "Flutterwave not configured."}
    try:
        resp = requests.put(f"{FLW_BASE_URL}/subaccounts/{numeric_id}",
                            json={"split_type": split_type, "split_value": split_value},
                            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                            timeout=15)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error_message": str(exc)}
    if data.get("status") != "success":
        return {"success": False, "error_message": data.get("message", "Update refused.")}
    return {"success": True, "error_message": None}


def fix_subaccount_splits(subaccount_ids=None, max_pages: int = 50) -> dict:
    """Set Siiqo's commission to 3% on every Flutterwave subaccount (or only
    the RS_ codes in `subaccount_ids`) whose split is not already
    percentage/0.03. Idempotent: subaccounts already right are skipped.
    Returns counts and the RS_ codes that failed."""
    fixed, skipped, failed, seen = [], 0, [], 0
    page = 1
    while page <= max_pages:
        res = list_subaccounts(page)
        if not res.get("success"):
            return {"success": False, "error_message": res.get("error_message"), "seen": seen,
                    "fixed": fixed, "skipped": skipped, "failed": failed}
        for item in res["items"]:
            rs = item.get("subaccount_id")
            if subaccount_ids is not None and rs not in subaccount_ids:
                continue
            seen += 1
            try:
                current = float(item.get("split_value"))
            except (TypeError, ValueError):
                current = None
            if (item.get("split_type") or "").lower() == "percentage" and current is not None \
                    and abs(current - SIIQO_FLW_SUBACCOUNT_SPLIT) < 1e-9:
                skipped += 1
                continue
            up = update_subaccount_split(item.get("id"))
            if up.get("success"):
                fixed.append(rs)
            else:
                failed.append({"subaccount_id": rs, "error": str(up.get("error_message"))[:200]})
        if not res.get("has_more"):
            break
        page += 1
    logger.info("[FLW SUBACCOUNT] Split repair: %d fixed, %d already right, %d failed",
                len(fixed), skipped, len(failed))
    return {"success": not failed, "seen": seen, "fixed": fixed, "skipped": skipped, "failed": failed}
