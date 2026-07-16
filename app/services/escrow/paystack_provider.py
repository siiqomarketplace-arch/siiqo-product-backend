"""
paystack_provider.py — Paystack implementation of BaseEscrowProvider

Flow for marketplace checkout (digital/service products — Split Payment):
  1. initiate_transaction()  → looks up vendor's paystack_subaccount_code,
                               calls /transaction/initialize with subaccount
                               + transaction_charge (Siiqo's 12% fee)
  2. Buyer pays on Paystack-hosted page
  3. Paystack automatically splits the payment:
       • vendor's share → settled to their subaccount → their bank (T+1)
       • Siiqo's fee   → stays in Siiqo main balance
  4. Paystack fires charge.success webhook → bridge.py handles it,
     marks EscrowTransaction as RELEASED and Order as COMPLETED
  5. NO manual /transfer call needed — Paystack handles the vendor payout.

Flow for physical products (Payscrow — unchanged):
  Payscrow handles escrow hold, release, and vendor payout natively.

NOTE: Payscrow is still used for Payment Links (/pay/[slug] flow).
      This provider is ONLY for marketplace cart checkout + subscriptions.
"""

import os
import uuid
import re
import logging
import requests
from decimal import Decimal

from app.services.escrow.base import BaseEscrowProvider

PAYSTACK_BASE_URL = "https://api.paystack.co"


def _paystack_key() -> str:
    return os.environ.get("PAYSTACK_SECRET_KEY", "")


def _format_phone(phone_str: str | None) -> str:
    """Normalise to 11-digit Nigerian format."""
    if not phone_str:
        return "08012345678"
    digits = re.sub(r"\D", "", phone_str)
    if digits.startswith("234") and len(digits) == 13:
        return "0" + digits[3:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits
    if len(digits) == 10:
        return "0" + digits
    return "08012345678"


class PaystackProvider(BaseEscrowProvider):
    """
    Paystack-backed provider — used for digital and service products only.

    Split Payment model (replaces the old Custodian/Transfer model):
    - The vendor's paystack_subaccount_code is looked up at checkout time.
    - The payment payload includes `subaccount` (vendor) and
      `transaction_charge` (Siiqo's 12% fee in kobo).
    - Paystack splits the payment natively at transaction time:
        • vendor's portion → settled to subaccount → vendor's bank (T+1)
        • Siiqo's fee      → stays in Siiqo's main balance
    - The manual /transfer call (paystack_transfer_to_vendor) is bypassed
      for orders that used split payment at checkout.
    - Paystack does NOT require vendor bank details at payment time —
      only when creating the subaccount — so we never block checkout for
      missing vendor bank accounts if no subaccount is found yet.
    """

    # ------------------------------------------------------------------
    # initiate_transaction
    # ------------------------------------------------------------------
    def initiate_transaction(self, orders, existing_txn_number=None, return_url=None):
        key = _paystack_key()
        if not key:
            return {"success": False, "error_message": "Paystack API key not configured."}

        if not isinstance(orders, list):
            orders = [orders]
        if not orders:
            return {"success": False, "error_message": "No orders provided."}

        # Use a stable reference so retrying the same cart doesn't create
        # duplicate Paystack transactions.
        txn_ref = existing_txn_number or f"ORD-{uuid.uuid4().hex[:12].upper()}"

        # ── totals ──────────────────────────────────────────────────────
        total_ngn = sum(
            float(o.total_amount) + float(o.logistics_fee or 0)
            for o in orders
        )
        # Paystack amount is in kobo (smallest unit)
        amount_kobo = int(round(total_ngn * 100))

        # ── buyer info ──────────────────────────────────────────────────
        buyer = orders[0].buyer
        buyer_email = buyer.email if buyer else "buyer@siiqo.com"
        buyer_name = (
            f"{buyer.first_name or ''} {buyer.last_name or ''}".strip()
            if buyer else "Siiqo Buyer"
        )
        buyer_phone = _format_phone(buyer.phone if buyer else None)

        # ── fee accounting ───────────────────────────────────────────────
        # 6% platform fee. For split payments this is the transaction_charge
        # sent to Paystack so Siiqo keeps it in its main balance.
        siiqo_fee_total = Decimal("0.00")
        for o in orders:
            subtotal = Decimal(str(o.total_amount))
            siiqo_fee_total += (subtotal * Decimal("0.06")).quantize(Decimal("0.01"))
        siiqo_fee_kobo = int(round(float(siiqo_fee_total) * 100))

        # ── Vendor subaccount (for split payments) ───────────────────────
        # For single-vendor digital/service checkouts, attach the vendor's
        # subaccount so Paystack splits the payment natively.
        vendor_subaccount_code = None
        vendor_ids = list({o.vendor_id for o in orders})
        if len(vendor_ids) == 1:
            # Single vendor — look up subaccount from VendorBankAccount first,
            # then fall back to Storefront (set during onboarding).
            try:
                from app.models.withdrawal import VendorBankAccount
                from app.models.user import Storefront
                bank_acc = VendorBankAccount.query.filter_by(
                    vendor_id=vendor_ids[0], is_default=True
                ).first() or VendorBankAccount.query.filter_by(
                    vendor_id=vendor_ids[0]
                ).first()
                if bank_acc and bank_acc.paystack_subaccount_code:
                    vendor_subaccount_code = bank_acc.paystack_subaccount_code
                else:
                    sf = Storefront.query.filter_by(vendor_id=vendor_ids[0]).first()
                    if sf and sf.paystack_subaccount_code:
                        vendor_subaccount_code = sf.paystack_subaccount_code
            except Exception as _exc:
                logging.warning(f"[PAYSTACK] Could not look up subaccount: {_exc}")

        # ── Paystack payload ────────────────────────────────────────────
        site_url = os.environ.get("SITE_URL", "https://siiqo.com")
        callback_url = return_url or f"{site_url}/payment/success"

        # Build metadata so the webhook can match back to orders
        order_ids = [str(o.id) for o in orders]

        payload = {
            "email": buyer_email,
            "amount": amount_kobo,
            "reference": txn_ref,
            "callback_url": callback_url,
            "metadata": {
                "order_ids": order_ids,
                "buyer_id": str(orders[0].buyer_id),
                "buyer_name": buyer_name,
                "buyer_phone": buyer_phone,
                "source": "marketplace_checkout",
                # custom_fields appear on the Paystack dashboard receipt
                "custom_fields": [
                    {
                        "display_name": "Siiqo Order",
                        "variable_name": "siiqo_order",
                        "value": ", ".join(f"#{oid}" for oid in order_ids),
                    }
                ],
            },
            "channels": ["card", "bank", "ussd", "bank_transfer"],
        }

        # Attach split payment params if a vendor subaccount was found
        if vendor_subaccount_code:
            payload["subaccount"] = vendor_subaccount_code
            payload["transaction_charge"] = siiqo_fee_kobo
            payload["bearer"] = "subaccount"  # buyer bears Paystack fees; subaccount nets the remainder
            logging.info(
                f"[PAYSTACK] Split payment — subaccount={vendor_subaccount_code}, "
                f"siiqo_fee=₦{float(siiqo_fee_total):,.2f}"
            )
        else:
            logging.warning(
                f"[PAYSTACK] No subaccount found for vendor(s) {vendor_ids}. "
                "Processing as non-split transaction."
            )

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

        logging.info(
            f"[PAYSTACK] Initiating transaction {txn_ref} — "
            f"total=₦{total_ngn:,.2f}, orders={order_ids}"
        )

        try:
            resp = requests.post(
                f"{PAYSTACK_BASE_URL}/transaction/initialize",
                json=payload,
                headers=headers,
                timeout=15,
            )
            data = resp.json()
        except requests.exceptions.Timeout:
            logging.error(f"[PAYSTACK] Timeout for {txn_ref}")
            return {"success": False, "error_message": "Payment gateway timed out. Please try again."}
        except Exception as exc:
            logging.error(f"[PAYSTACK] Request error for {txn_ref}: {exc}")
            return {"success": False, "error_message": "Could not reach payment gateway. Please try again."}

        if not data.get("status"):
            msg = data.get("message", "Payment gateway rejected the request.")
            logging.error(f"[PAYSTACK] Init failed for {txn_ref}: {data}")
            return {"success": False, "error_message": msg}

        payment_url = data["data"]["authorization_url"]
        access_code = data["data"].get("access_code", "")

        return {
            "success": True,
            "payment_link": payment_url,
            "transaction_number": txn_ref,
            # We use access_code as provider_transaction_id for Paystack
            "provider_transaction_id": access_code,
            # Paystack reference == our own reference
            "provider_reference": txn_ref,
            "amount": total_ngn,
            "fee_amount": float(siiqo_fee_total),
            # Flag so the webhook handler knows Paystack will settle vendor directly
            "used_split": bool(vendor_subaccount_code),
            "error_message": None,
        }

    # ------------------------------------------------------------------
    # verify_transaction
    # ------------------------------------------------------------------
    def verify_transaction(self, provider_reference: str) -> dict:
        """
        Verify a Paystack transaction by reference.
        Returns a dict with keys: success, status, amount_ngn, email.
        """
        key = _paystack_key()
        if not key:
            return {"success": False, "error_message": "Paystack key not configured."}

        try:
            resp = requests.get(
                f"{PAYSTACK_BASE_URL}/transaction/verify/{provider_reference}",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            data = resp.json()
        except Exception as exc:
            return {"success": False, "error_message": str(exc)}

        if not data.get("status"):
            return {"success": False, "error_message": data.get("message", "Verification failed.")}

        txn = data["data"]
        paid = txn.get("status") == "success"
        amount_ngn = txn.get("amount", 0) / 100  # kobo → naira

        return {
            "success": paid,
            "status": txn.get("status"),
            "amount_ngn": amount_ngn,
            "email": txn.get("customer", {}).get("email"),
            "reference": txn.get("reference"),
            "metadata": txn.get("metadata", {}),
        }

    # ------------------------------------------------------------------
    # handle_webhook  (called from bridge.py)
    # ------------------------------------------------------------------
    def handle_webhook(self, payload: dict, signature_header: str | None = None) -> dict:
        """
        Process a Paystack webhook event dict.
        Returns {"handled": True/False, "event": str}.
        The caller (bridge.py) is responsible for signature verification.
        """
        event_type = payload.get("event", "")
        data = payload.get("data", {})

        if event_type == "charge.success":
            reference = data.get("reference", "")
            metadata = data.get("metadata", {})
            order_ids_raw = metadata.get("order_ids", [])
            order_ids = [int(x) for x in order_ids_raw if str(x).isdigit()]
            return {
                "handled": True,
                "event": event_type,
                "reference": reference,
                "order_ids": order_ids,
                "amount_kobo": data.get("amount", 0),
                "email": data.get("customer", {}).get("email"),
            }

        return {"handled": False, "event": event_type}


# ---------------------------------------------------------------------------
# Standalone helper — trigger a Paystack transfer to a vendor bank account
# ---------------------------------------------------------------------------

def paystack_transfer_to_vendor(
    recipient_code: str,
    amount_ngn: float,
    reference: str,
    reason: str = "Siiqo vendor payout",
) -> dict:
    """
    Initiate a Paystack transfer (payout) to a vendor.

    Prerequisites:
    - Transfers must be enabled on the Paystack dashboard.
    - Paystack balance must be funded (for live mode this happens automatically
      via settlements; for test mode you top-up the test balance).

    Args:
        recipient_code: VendorBankAccount.recipient_code (stored at bank-account setup)
        amount_ngn:     Amount in Naira (will be converted to kobo)
        reference:      Unique reference string (e.g. f"PAYOUT-{order_id}")
        reason:         Human-readable reason shown on transfer receipt

    Returns dict with keys: success, transfer_code, message, error_message
    """
    key = _paystack_key()
    if not key:
        return {"success": False, "error_message": "Paystack key not configured."}

    amount_kobo = int(round(amount_ngn * 100))

    payload = {
        "source": "balance",
        "amount": amount_kobo,
        "reference": reference,
        "recipient": recipient_code,
        "reason": reason,
    }

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    logging.info(
        f"[PAYSTACK TRANSFER] ₦{amount_ngn:,.2f} → {recipient_code} ref={reference}"
    )

    try:
        resp = requests.post(
            f"{PAYSTACK_BASE_URL}/transfer",
            json=payload,
            headers=headers,
            timeout=15,
        )
        data = resp.json()
    except requests.exceptions.Timeout:
        return {"success": False, "error_message": "Transfer request timed out."}
    except Exception as exc:
        return {"success": False, "error_message": str(exc)}

    if not data.get("status"):
        msg = data.get("message", "Transfer failed.")
        logging.error(f"[PAYSTACK TRANSFER] Failed ref={reference}: {data}")
        return {"success": False, "error_message": msg}

    transfer_code = data["data"].get("transfer_code", "")
    logging.info(f"[PAYSTACK TRANSFER] Initiated {transfer_code} ref={reference}")

    return {
        "success": True,
        "transfer_code": transfer_code,
        "message": data.get("message", "Transfer initiated."),
        "error_message": None,
    }


# ---------------------------------------------------------------------------
# Standalone helper — create a Paystack subaccount for a vendor
# ---------------------------------------------------------------------------

def create_paystack_subaccount(
    business_name: str,
    bank_code: str,
    account_number: str,
    description: str = "",
) -> dict:
    """
    Register a vendor as a Paystack subaccount so we can route their share
    of marketplace payments via Split Payments at checkout.

    Args:
        business_name:  Vendor's store/business name
        bank_code:      Nigerian bank code (e.g. '011' for First Bank)
        account_number: 10-digit NUBAN account number
        description:    Optional description shown on the Paystack dashboard

    Returns dict:
        success:              bool
        subaccount_code:      str  (e.g. 'ACCT_xxxxxxxxxx')  — store this!
        error_message:        str or None
    """
    key = _paystack_key()
    if not key:
        return {"success": False, "error_message": "Paystack API key not configured."}

    payload = {
        "business_name": business_name,
        "settlement_bank": bank_code,
        "account_number": account_number,
        "percentage_charge": 0,  # We use transaction_charge (fixed) at checkout instead
        "description": description or f"Siiqo vendor: {business_name}",
    }

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    logging.info(
        f"[PAYSTACK SUBACCOUNT] Creating subaccount for '{business_name}' "
        f"bank={bank_code} acct={account_number[-4:].rjust(len(account_number), '*')}"
    )

    try:
        resp = requests.post(
            f"{PAYSTACK_BASE_URL}/subaccount",
            json=payload,
            headers=headers,
            timeout=15,
        )
        data = resp.json()
    except requests.exceptions.Timeout:
        return {"success": False, "error_message": "Subaccount request timed out."}
    except Exception as exc:
        return {"success": False, "error_message": str(exc)}

    if not data.get("status"):
        msg = data.get("message", "Subaccount creation failed.")
        logging.error(f"[PAYSTACK SUBACCOUNT] Failed for '{business_name}': {data}")
        return {"success": False, "error_message": msg}

    subaccount_code = data["data"].get("subaccount_code", "")
    logging.info(
        f"[PAYSTACK SUBACCOUNT] Created {subaccount_code} for '{business_name}'"
    )

    return {
        "success": True,
        "subaccount_code": subaccount_code,
        "error_message": None,
    }
