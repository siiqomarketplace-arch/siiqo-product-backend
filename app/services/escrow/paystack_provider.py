"""
paystack_provider.py — Paystack implementation of BaseEscrowProvider

Flow for marketplace checkout:
  1. initiate_transaction()  → calls Paystack /transaction/initialize
                               returns a hosted payment URL for the buyer
  2. Buyer pays on Paystack-hosted page
  3. Paystack fires charge.success webhook → bridge.py handles it,
     marks EscrowTransaction as IN_ESCROW and Order as PAID
  4. Vendor ships → buyer confirms received
  5. release_vendor_payout()  → calls Paystack /transfer using the vendor's
                                 stored recipient_code (already in VendorBankAccount)

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
    Paystack-backed provider.

    Key differences from Payscrow:
    - No hosted escrow hold.  Siiqo itself holds the money (it lands in
      Siiqo's Paystack settlement account) until buyer confirms delivery.
    - On release, Siiqo calls POST /transfer to push the vendor's net
      amount to their bank using the recipient_code already stored in DB.
    - Paystack does NOT require vendor bank details at payment time —
      only at transfer time — so we never block checkout for missing
      vendor bank accounts the way Payscrow did.
    """

    # ------------------------------------------------------------------
    # initiate_transaction
    # ------------------------------------------------------------------
    def initiate_transaction(self, orders, existing_txn_number=None):
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

        # ── fee accounting (for ledger only — not sent to Paystack) ─────
        # 6 % platform fee deducted from vendor at release time.
        siiqo_fee_total = Decimal("0.00")
        for o in orders:
            subtotal = Decimal(str(o.total_amount))
            siiqo_fee_total += (subtotal * Decimal("0.06")).quantize(Decimal("0.01"))

        # ── Paystack payload ────────────────────────────────────────────
        site_url = os.environ.get("SITE_URL", "https://siiqo.com")
        callback_url = f"{site_url}/payment/success"
        webhook_url = os.environ.get(
            "PAYSTACK_WEBHOOK_URL",
            "https://devapi.siiqo.app/api/payments/webhook",
        )

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
