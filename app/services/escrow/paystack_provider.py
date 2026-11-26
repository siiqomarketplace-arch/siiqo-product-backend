"""
paystack_provider.py — Paystack implementation of BaseEscrowProvider

Flow for marketplace checkout (digital/service products — Split Payment):
  1. initiate_transaction()  → looks up vendor's paystack_subaccount_code,
                               calls /transaction/initialize with subaccount
                               + transaction_charge (Siiqo's platform fee, see app/services/fees.py)
  2. Buyer pays on Paystack-hosted page
  3. Paystack automatically splits the payment:
       • vendor's share → settled to their subaccount → their bank (T+1)
       • Siiqo's fee   → stays in Siiqo main balance
  4. Paystack fires charge.success webhook → bridge.py handles it,
     marks EscrowTransaction as RELEASED and Order as COMPLETED
  5. NO manual /transfer call needed — Paystack handles the vendor payout.

Flow for physical products:
  Physical orders are now handled by Daya (bank transfer/crypto) or POD (pay-on-delivery).
  Paystack is used only for digital/service/event products at checkout.

Flow for Payment Links (/pay/[slug]):
  Payment links support Paystack, Flutterwave, or Daya — buyer chooses at payment time.
  Paystack split payment is used if the link creator's vendor account has a subaccount.

NOTE: Payscrow has been fully removed. This provider is used for:
      - Marketplace cart checkout (digital/service/event)
      - Subscriptions (via bridge.py)
      - Payment Links (when buyer selects card payment and vendor has Paystack subaccount)
"""

import os
import uuid
import re
import logging
from app.utils.log_mask import mask_account
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
      `transaction_charge` (Siiqo's platform fee in kobo, see app/services/fees.py).
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
    def initiate_transaction(self, orders, existing_txn_number=None, return_url=None, allow_split=True):
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

        # ── fee accounting ───────────────────────────────────────────────
        # Siiqo platform fee: 3% for all vendors (flat).
        # Buyer pays the listed price PLUS Paystack's processing fee on top.
        #
        # Paystack fee (official NGN rates):
        #   1.5% of transaction amount + ₦100 flat
        #   Cap: ₦2,000 per transaction
        #   Waived if transaction < ₦2,500
        #
        # To ensure the vendor receives exactly the listed price (minus Siiqo 3%),
        # we charge the buyer: gross = listed_price + paystack_fee
        # where paystack_fee = min(gross * 0.015 + 100, 2000)
        #
        # We solve for gross iteratively (one pass is accurate enough):
        #   estimate: gross ≈ listed_price / (1 - 0.015) + 100
        #   then clamp to the ₦2,000 cap.
        #
        # bearer = "account" means Siiqo's main account bears nothing extra —
        # the fee is already included in the gross amount the buyer pays.

        total_listed_ngn = sum(float(o.total_amount) + float(o.logistics_fee or 0) for o in orders)

        # Estimate gross = amount buyer must actually pay
        # Paystack fee formula: fee = gross * 0.015 + 100  (if gross >= 2500)
        # Solving: gross = (listed + 100) / (1 - 0.015)
        if total_listed_ngn >= 2500:
            gross_estimate = (total_listed_ngn + 100.0) / (1.0 - 0.015)
            paystack_fee = gross_estimate * 0.015 + 100.0
            if paystack_fee > 2000.0:
                paystack_fee = 2000.0
            buyer_total_ngn = round(total_listed_ngn + paystack_fee, 2)
        else:
            # Paystack waives fee below ₦2,500
            buyer_total_ngn = total_listed_ngn
            paystack_fee = 0.0

        amount_kobo = int(round(buyer_total_ngn * 100))

        # Siiqo's fee is 3% of the listed price (NOT of the inflated buyer total)
        # One platform fee for every seller (D1): 3% of each order subtotal.
        from app.services.fees import platform_fee
        siiqo_fee_total = Decimal("0.00")
        for o in orders:
            siiqo_fee_total += platform_fee(o.total_amount)
        siiqo_fee_kobo = int(round(float(siiqo_fee_total) * 100))

        # ── buyer info ──────────────────────────────────────────────────
        buyer = orders[0].buyer
        buyer_email = (
            (buyer.email if buyer else None)
            or getattr(orders[0], 'buyer_email', None)
            or "buyer@siiqo.com"
        )
        buyer_name = (
            f"{buyer.first_name or ''} {buyer.last_name or ''}".strip()
            if buyer
            else getattr(orders[0], 'buyer_name', None) or "Siiqo Buyer"
        )
        buyer_phone = _format_phone((buyer.phone if buyer else None) or getattr(orders[0], 'delivery_phone', None))

        # ── Vendor subaccount (for split payments) ───────────────────────
        # For single-vendor digital/service checkouts, attach the vendor's
        # subaccount so Paystack splits the payment natively.
        vendor_subaccount_code = None
        split_code = None
        vendor_ids = list({o.vendor_id for o in orders})
        if not allow_split:
            # A retry of a payment that was not split: keep it unsplit.
            pass
        elif len(vendor_ids) == 1:
            # Single vendor — look up subaccount from VendorBankAccount first,
            # then fall back to Storefront (set during onboarding).
            vendor_subaccount_code = vendor_paystack_subaccount(vendor_ids[0])
        else:
            # Several sellers: one Paystack multi-split when switched on and
            # every seller has a subaccount; otherwise no split (Siiqo pays
            # each seller at release).
            split_code = multi_split_code_for(orders, txn_ref)

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
                "listed_amount_ngn": total_listed_ngn,
                "paystack_fee_ngn": paystack_fee,
                "siiqo_fee_ngn": float(siiqo_fee_total),
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
            # transaction_charge is the flat amount that goes to the MAIN
            # account; the subaccount gets the rest, and with bearer=account
            # Paystack's fee comes out of the main account's part
            # (https://paystack.com/docs/payments/split-payments/). So the main
            # account keeps everything that is not the seller's: Siiqo's 3%,
            # the Paystack fee the buyer paid on top, and any delivery-partner
            # fee. The subaccount then receives exactly the seller's share,
            # which is what the payment-confirmation proof checks
            # (app/services/split_settlement.py).
            from app.services.payment_refs import seller_net_by_vendor
            seller_net_kobo = int((seller_net_by_vendor(orders)[vendor_ids[0]] * 100).to_integral_value())
            payload["transaction_charge"] = max(amount_kobo - seller_net_kobo, siiqo_fee_kobo)
            payload["bearer"] = "account"
            logging.info(
                f"[PAYSTACK] Split payment — subaccount={vendor_subaccount_code}, "
                f"siiqo_fee=₦{float(siiqo_fee_total):,.2f}, "
                f"buyer_total=₦{buyer_total_ngn:,.2f} (listed=₦{total_listed_ngn:,.2f} + psk_fee=₦{paystack_fee:,.2f})"
            )
        elif split_code:
            # Multi-split (https://paystack.com/docs/payments/multi-split-payments/):
            # each seller's flat share is set on the split; the main account
            # keeps the rest and bears Paystack's fee (bearer_type=account).
            payload["split_code"] = split_code
            logging.info(f"[PAYSTACK] Multi-split {split_code} attached to {txn_ref}")
        else:
            logging.warning(
                f"[PAYSTACK] No split for vendor(s) {vendor_ids}. "
                "Processing as non-split transaction."
            )

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

        logging.info(
            f"[PAYSTACK] Initiating transaction {txn_ref} — "
            f"buyer_total=₦{buyer_total_ngn:,.2f}, listed=₦{total_listed_ngn:,.2f}, orders={order_ids}"
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

        if not data.get("status") and split_code:
            # Paystack refused the payment with the split attached: take the
            # payment without a split; Siiqo then pays each seller at release.
            logging.warning(f"[PAYSTACK] Init with split {split_code} refused for {txn_ref}; retrying without split")
            split_code = None
            payload.pop("split_code", None)
            try:
                resp = requests.post(
                    f"{PAYSTACK_BASE_URL}/transaction/initialize",
                    json=payload,
                    headers=headers,
                    timeout=15,
                )
                data = resp.json()
            except Exception as exc:
                logging.error(f"[PAYSTACK] Retry error for {txn_ref}: {exc}")
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
            "amount": buyer_total_ngn,
            "listed_amount": total_listed_ngn,
            "paystack_fee": paystack_fee,
            "fee_amount": float(siiqo_fee_total),
            # Flag so the webhook handler knows Paystack will settle vendor directly
            "used_split": bool(vendor_subaccount_code or split_code),
            "split_code": split_code,
            # {vendor_id: subaccount code} a split was REQUESTED for (not proof).
            "split_subaccounts": (
                {vendor_ids[0]: vendor_subaccount_code} if vendor_subaccount_code
                else ({v: vendor_paystack_subaccount(v) for v in vendor_ids} if split_code else {})
            ),
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
            # The whole verified transaction: split proof reads "subaccount",
            # "fees_split" and "split" from it (app/services/split_settlement.py).
            "raw": txn,
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
# Subaccount lookup and multi-seller split
# ---------------------------------------------------------------------------

def vendor_paystack_subaccount(vendor_id) -> str | None:
    """Seller's Paystack subaccount code: default bank account, any bank
    account, then the storefront. None when there is none."""
    try:
        from app.models.withdrawal import VendorBankAccount
        from app.models.user import Storefront
        bank_acc = VendorBankAccount.query.filter_by(
            vendor_id=vendor_id, is_default=True
        ).first() or VendorBankAccount.query.filter_by(vendor_id=vendor_id).first()
        if bank_acc and bank_acc.paystack_subaccount_code:
            return bank_acc.paystack_subaccount_code
        sf = Storefront.query.filter_by(vendor_id=vendor_id).first()
        if sf and sf.paystack_subaccount_code:
            return sf.paystack_subaccount_code
    except Exception as _exc:  # noqa: BLE001
        logging.warning(f"[PAYSTACK] Could not look up subaccount for vendor {vendor_id}: {_exc}")
    return None


def create_paystack_split(name: str, subaccounts: list, currency: str = "NGN") -> dict:
    """POST /split (https://paystack.com/docs/api/split/).
    subaccounts: [{"subaccount": "ACCT_x", "share": <kobo>}] for a flat split.
    bearer_type "account": Siiqo's main account bears Paystack's fee, as the
    single-seller split does (bearer=account). Returns {success, split_code}."""
    key = _paystack_key()
    if not key:
        return {"success": False, "error_message": "Paystack API key not configured."}
    payload = {
        "name": name,
        "type": "flat",
        "currency": currency,
        "subaccounts": subaccounts,
        "bearer_type": "account",
    }
    try:
        resp = requests.post(
            f"{PAYSTACK_BASE_URL}/split",
            json=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=15,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logging.error(f"[PAYSTACK SPLIT] Request error for {name}: {exc}")
        return {"success": False, "error_message": "Could not reach Paystack."}
    split_code = (data.get("data") or {}).get("split_code") if isinstance(data, dict) else None
    if not (isinstance(data, dict) and data.get("status") and split_code):
        logging.error(f"[PAYSTACK SPLIT] Create failed for {name}: {data.get('message') if isinstance(data, dict) else data}")
        return {"success": False, "error_message": "Split not created."}
    return {"success": True, "split_code": split_code}


def multi_split_code_for(orders, txn_ref: str) -> str | None:
    """Split code for a payment with several sellers, or None (no split).
    None when MULTI_SPLIT_ENABLED is off, an order is not in naira, any
    seller lacks a subaccount or has nothing to receive, or Paystack refuses."""
    from app.services.payment_refs import multi_split_enabled, ngn_only, seller_net_by_vendor
    if not multi_split_enabled() or not ngn_only(orders):
        return None
    nets = seller_net_by_vendor(orders)
    subaccounts = []
    for vendor_id in sorted(nets):
        code = vendor_paystack_subaccount(vendor_id)
        share_kobo = int((nets[vendor_id] * 100).to_integral_value())
        if not code or share_kobo <= 0:
            logging.info(f"[PAYSTACK SPLIT] No multi-split for {txn_ref}: vendor {vendor_id} has no subaccount")
            return None
        subaccounts.append({"subaccount": code, "share": share_kobo})
    if len({s["subaccount"] for s in subaccounts}) != len(subaccounts):
        logging.warning(f"[PAYSTACK SPLIT] No multi-split for {txn_ref}: sellers share a subaccount")
        return None
    result = create_paystack_split(f"Siiqo {txn_ref}", subaccounts)
    if not result.get("success"):
        return None
    return result["split_code"]


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
        "status": data["data"].get("status"),
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

# Map NIBSS / CBN institution codes to Paystack settlement bank codes
NIBSS_TO_PAYSTACK_BANKS = {
    "100004": "999992",  # OPay (Paycom)
    "999992": "999992",  # OPay
    "100033": "999991",  # PalmPay
    "999991": "999991",  # PalmPay
    "090405": "50515",   # Moniepoint MFB
    "50515": "50515",    # Moniepoint MFB
    "090267": "50211",   # Kuda Bank
    "50211": "50211",    # Kuda Bank
    "090551": "51318",   # FairMoney MFB
    "51318": "51318",    # FairMoney MFB
    "090110": "565",     # VFD Microfinance Bank
    "565": "565",        # VFD Microfinance Bank
    "090251": "50383",   # Carbon
    "50383": "50383",    # Carbon
}

PAYSTACK_TO_NIBSS_BANKS = {
    "999992": "100004",  # OPay
    "999991": "100033",  # PalmPay
    "50515": "50515",    # Moniepoint
    "50211": "50211",    # Kuda
    "51318": "51318",    # FairMoney
    "565": "565",        # VFD
    "50383": "50383",    # Carbon
}


def ensure_paystack_transfer_recipient(
    account_number: str,
    bank_code: str,
    account_name: str | None = None,
    name_confirmed: bool = False,
) -> dict:
    """
    Creates or retrieves a Paystack transfer recipient (RCP_xxxx),
    translating NIBSS/CBN codes (e.g. 100004 -> 999992 for OPay) automatically.

    The recipient's name is only ever a bank-confirmed account holder name
    (app/services/payout_account_name.py). Pass name_confirmed=True with a name
    that came from a bank lookup; otherwise the account is looked up here
    (Daya, then Paystack). A bank's name, the store name or "Vendor" is never
    used. With no confirmed name no recipient is created: payouts wait until
    the name is confirmed. Returns name_confirmed and the name used.
    """
    from app.services.payout_account_name import is_suspect_name, resolve_holder_name

    key = _paystack_key()
    if not key:
        return {"success": False, "name_confirmed": False, "account_name": None,
                "error_message": "Paystack API key not configured."}

    paystack_bank_code = NIBSS_TO_PAYSTACK_BANKS.get(str(bank_code).strip(), str(bank_code).strip())
    resolved_name = (account_name or "").strip()

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    if not name_confirmed or not resolved_name or is_suspect_name(resolved_name):
        res = resolve_holder_name(bank_code, account_number)
        resolved_name = res["account_name"] or ""

    if not resolved_name:
        logging.warning(f"[ENSURE RECIPIENT] Name not confirmed for {mask_account(account_number)}; "
                        "no recipient created")
        return {"success": False, "name_confirmed": False, "account_name": None,
                "error_message": "Account name not confirmed by the bank."}

    payload = {
        "type": "nuban",
        "name": resolved_name,
        "account_number": account_number,
        "bank_code": paystack_bank_code,
        "currency": "NGN",
    }

    try:
        resp = requests.post(
            f"{PAYSTACK_BASE_URL}/transferrecipient",
            json=payload,
            headers=headers,
            timeout=15,
        )
        data = resp.json()
        if data.get("status") and data.get("data", {}).get("recipient_code"):
            recipient_code = data["data"]["recipient_code"]
            logging.info(f"[ENSURE RECIPIENT] Created {recipient_code} for {mask_account(account_number)} ({paystack_bank_code})")
            return {
                "success": True,
                "recipient_code": recipient_code,
                "account_name": resolved_name,
                "name_confirmed": True,
            }
        else:
            msg = data.get("message", "Failed to create recipient")
            logging.error(f"[ENSURE RECIPIENT] Error creating recipient for {mask_account(account_number)}: {msg}")
            return {"success": False, "error_message": msg, "account_name": resolved_name,
                    "name_confirmed": True}
    except Exception as exc:
        logging.error(f"[ENSURE RECIPIENT] Exception: {type(exc).__name__}")
        return {"success": False, "error_message": str(exc), "account_name": resolved_name,
                "name_confirmed": True}


def create_paystack_subaccount(
    business_name: str,
    bank_code: str,
    account_number: str,
    description: str = "",
) -> dict:
    key = _paystack_key()
    if not key:
        return {"success": False, "error_message": "Paystack API key not configured."}

    settlement_bank = NIBSS_TO_PAYSTACK_BANKS.get(str(bank_code).strip(), str(bank_code).strip())

    payload = {
        "business_name": business_name,
        "settlement_bank": settlement_bank,
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
        f"bank={settlement_bank} (raw={bank_code}) acct={account_number[-4:].rjust(len(account_number), '*')}"
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



# ---------------------------------------------------------------------------
# Transfer status (final outcome of a payout transfer)
# ---------------------------------------------------------------------------

def paystack_verify_transfer(reference: str) -> dict:
    """GET /transfer/verify/:reference
    (https://paystack.com/docs/api/transfer/#verify). Returns
    {success, status, transfer_code, error_message}; status is Paystack's own
    word (e.g. pending, otp, received, success, failed, reversed)."""
    key = _paystack_key()
    if not key:
        return {"success": False, "error_message": "Paystack key not configured."}
    try:
        resp = requests.get(
            f"{PAYSTACK_BASE_URL}/transfer/verify/{reference}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error_message": str(exc)}
    if not data.get("status") or not isinstance(data.get("data"), dict):
        return {"success": False, "error_message": data.get("message", "Transfer lookup failed.")}
    d = data["data"]
    return {"success": True, "status": d.get("status"), "transfer_code": d.get("transfer_code"),
            "reason": d.get("reason"), "error_message": None}
