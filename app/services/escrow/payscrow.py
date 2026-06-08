import os
import uuid
import logging
import requests
from app.services.escrow.base import BaseEscrowProvider

def _payscrow_env():
    # Return (api_key, base_url)
    key = os.environ.get('PAYSCROW_API_KEY', '')
    url = os.environ.get('PAYSCROW_BASE_URL')
    if not url:
        is_sandbox = (
            not key
            or key.startswith('ps_9')  # sandbox key prefix
            or os.environ.get('PAYSCROW_ENV', '').lower() == 'sandbox'
        )
        url = "https://api.payscrow.dev" if is_sandbox else "https://api.payscrow.net"
    return key, url

class PayscrowProvider(BaseEscrowProvider):
    def initiate_transaction(self, orders, existing_txn_number=None):
        payscrow_key, base_url = _payscrow_env()
        
        if not payscrow_key:
            return {
                "success": False,
                "error_message": "Payscrow API key not configured."
            }

        # Handle both single order and list of orders
        if not isinstance(orders, list):
            orders = [orders]

        if not orders:
            return {"success": False, "error_message": "No orders provided."}

        txn_number = existing_txn_number if existing_txn_number else f"ESC-{uuid.uuid4().hex[:12].upper()}"
        
        from decimal import Decimal
        from app.models.withdrawal import VendorBankAccount
        from app.models.user import User

        # Calculate totals across all orders
        total_amount = sum(float(order.total_amount) for order in orders)

        # Use the first order for buyer details
        buyer = orders[0].buyer
        buyer_name = (buyer.first_name if buyer and buyer.first_name else "Siiqo Buyer")
        
        import re
        def format_phone(phone_str):
            if not phone_str:
                return "08012345678"
            digits = re.sub(r'\D', '', phone_str)
            if digits.startswith('234') and len(digits) == 13:
                return '0' + digits[3:]
            if len(digits) == 11 and digits.startswith('0'):
                return digits
            if len(digits) == 10:
                return '0' + digits
            return "08012345678"
            
        buyer_phone = format_phone(buyer.phone if buyer else None)
        
        # Siiqo Master Bank Account for commission/fee portion
        siiqo_bank_code = os.environ.get("SIIQO_BANK_CODE", "011")
        siiqo_account_number = os.environ.get("SIIQO_BANK_ACCOUNT", "3050025256")
        siiqo_account_name = os.environ.get("SIIQO_BANK_NAME", "Siiqo Marketplace")

        headers = {
            "BrokerApiKey": payscrow_key,
            "Content-Type": "application/json"
        }
        
        siiqo_total_fee = Decimal('0.00')
        total_vendor_amount = Decimal('0.00')
        vendor_settlements = {}
        
        for o in orders:
            order_total = Decimal(str(o.total_amount))
            # 12% platform fee; vendor receives 88%
            fee = (order_total * Decimal('0.12')).quantize(Decimal('0.01'))
            vendor_amount = order_total - fee
            
            siiqo_total_fee += fee
            total_vendor_amount += vendor_amount
            
            # Fetch vendor's default bank account
            bank_acc = VendorBankAccount.query.filter_by(vendor_id=o.vendor_id, is_default=True).first()
            if not bank_acc:
                bank_acc = VendorBankAccount.query.filter_by(vendor_id=o.vendor_id).first()
                
            if not bank_acc:
                # Fallback to storefront details
                sf = o.vendor.storefront if o.vendor else None
                if sf and sf.bank_code and sf.account_number:
                    b_code = sf.bank_code
                    acc_num = sf.account_number
                    acc_name = sf.account_name or (o.vendor.full_name if o.vendor else "Vendor")
                else:
                    raise Exception(f"Vendor {o.vendor.full_name if o.vendor else o.vendor_id} has no bank account configured.")
            else:
                b_code = bank_acc.bank_code
                acc_num = bank_acc.account_number
                acc_name = bank_acc.account_name
                
            v_key = (b_code, acc_num)
            if v_key in vendor_settlements:
                vendor_settlements[v_key]["amount"] += vendor_amount
            else:
                vendor_settlements[v_key] = {
                    "bankCode": b_code,
                    "accountNumber": acc_num,
                    "accountName": acc_name,
                    "amount": vendor_amount
                }

        settlement_accounts = []
        if siiqo_total_fee > 0:
            settlement_accounts.append({
                "bankCode": siiqo_bank_code,
                "accountNumber": siiqo_account_number,
                "accountName": siiqo_account_name,
                "amount": float(siiqo_total_fee)
            })
            
        for v_sett in vendor_settlements.values():
            settlement_accounts.append({
                "bankCode": v_sett["bankCode"],
                "accountNumber": v_sett["accountNumber"],
                "accountName": v_sett["accountName"],
                "amount": float(v_sett["amount"])
            })
        
        # Generate item descriptions for all orders
        items_payload = []
        for o in orders:
            items_payload.append({
                "name": f"Siiqo Order #{o.id}",
                "description": f"Payment for order #{o.id} on Siiqo Marketplace",
                "quantity": 1,
                "price": float(o.total_amount)
            })

        # merchantChargePercentage: 0 = buyer pays no extra; merchant (Siiqo) absorbs fee.
        # This is correct for marketplace escrow where the 12% is already baked into prices.
        payload = {
            "transactionReference": txn_number,
            "merchantEmailAddress": "support@siiqo.com",
            "merchantName": "Siiqo Marketplace",
            "merchantPhoneNo": "08012345678",
            "customerEmailAddress": buyer.email if buyer else "buyer@siiqo.com",
            "customerName": buyer_name,
            "customerPhoneNo": buyer_phone,
            "currencyCode": "NGN",
            "merchantChargePercentage": 0,
            "redirectUrl": "https://siiqo.com/CartSystem?success=true",
            "returnUrl": "https://siiqo.com/CartSystem?success=true",
            "webhookNotificationUrl": os.environ.get(
                'PAYSCROW_WEBHOOK_URL',
                "https://api.siiqo.com/api/escrow/webhook"
            ),
            "items": items_payload,
            "settlementAccounts": settlement_accounts
        }
        
        logging.info(f"[PAYSCROW] Initiating transaction {txn_number} — total={total_amount}, settlements={len(settlement_accounts)}")

        try:
            resp = requests.post(f"{base_url}/api/v3/marketplace/transactions/start", json=payload, headers=headers, timeout=15)
            try:
                resp_data = resp.json()
            except Exception as e:
                logging.error(f"[PAYSCROW] JSON Decode Error: {e}, Status: {resp.status_code}, Body: {resp.text[:500]}")
                return {
                    "success": False,
                    "error_message": f"Payment gateway returned an invalid response (HTTP {resp.status_code}). Please try again."
                }

            if not resp_data.get('success'):
                # Extract the most useful error detail from Payscrow response
                errors = resp_data.get('errors') or resp_data.get('message') or resp_data.get('error') or {}
                if isinstance(errors, dict):
                    error_detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
                elif isinstance(errors, list):
                    error_detail = "; ".join(str(e) for e in errors)
                else:
                    error_detail = str(errors)
                logging.error(f"[PAYSCROW] API rejected payload for {txn_number}: {resp_data}")
                return {
                    "success": False,
                    "error_message": f"Payment gateway error: {error_detail or 'Unknown error. Please try again.'}"
                }
            
            data = resp_data.get('data', {})
            # Compute aggregate fee for the escrow route to store per-order
            total_fee_float = float(siiqo_total_fee)
            return {
                "success": True,
                "payment_link": data.get('paymentLink'),
                "transaction_number": txn_number,
                "provider_transaction_id": str(data.get('transactionId')) if data.get('transactionId') else None,
                "provider_reference": str(data.get('transactionNumber')) if data.get('transactionNumber') else None,
                "amount": total_amount,
                "fee_amount": total_fee_float,
                "error_message": None
            }
            
        except requests.exceptions.Timeout:
            logging.error(f"[PAYSCROW] Request timed out for {txn_number}")
            return {
                "success": False,
                "error_message": "Payment gateway timed out. Please try again."
            }
        except Exception as e:
            logging.error(f"[PAYSCROW] HTTP error for {txn_number}: {e}")
            return {
                "success": False,
                "error_message": "Could not reach the payment gateway. Please check your connection and try again."
            }

    def verify_transaction(self, provider_reference):
        pass

    def handle_webhook(self, payload, signature_header=None):
        pass
