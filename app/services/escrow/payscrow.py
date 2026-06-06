import os
import uuid
import logging
import requests
from app.services.escrow.base import BaseEscrowProvider

def _payscrow_env():
    # Return (api_key, base_url)
    key = os.environ.get('PAYSCROW_API_KEY', '')
    url = os.environ.get('PAYSCROW_BASE_URL', 'https://api.payscrow.net')
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
        
        # Calculate totals across all orders
        total_amount = sum(float(order.total_amount) for order in orders)
        fee_amount = round(total_amount * 0.12, 2)

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
        
        # Siiqo Master Bank Account (Unified Payment)
        # Siiqo acts as the sole merchant. We collect 100% of the funds in the Siiqo Master Account.
        # Payouts to individual vendors will be handled by Siiqo internally upon delivery.
        siiqo_bank_code = os.environ.get("SIIQO_BANK_CODE", "011") # Default First Bank
        siiqo_account_number = os.environ.get("SIIQO_BANK_ACCOUNT", "3050025256")
        siiqo_account_name = os.environ.get("SIIQO_BANK_NAME", "Siiqo Marketplace")

        headers = {
            "BrokerApiKey": payscrow_key,
            "Content-Type": "application/json"
        }
        
        settlement_accounts = [
            {
                "bankCode": siiqo_bank_code,
                "accountNumber": siiqo_account_number,
                "accountName": siiqo_account_name,
                "amount": total_amount
            }
        ]
        
        # Generate item descriptions for all orders
        items_payload = []
        for o in orders:
            items_payload.append({
                "name": f"Siiqo Order #{o.id}",
                "description": f"Payment for order #{o.id} on Siiqo Marketplace",
                "quantity": 1,
                "price": float(o.total_amount)
            })

        payload = {
            "transactionReference": txn_number,
            "merchantEmailAddress": "support@siiqo.com",
            "merchantName": "Siiqo Marketplace",
            "merchantPhoneNo": "08012345678",
            "customerEmailAddress": buyer.email if buyer else "buyer@siiqo.com",
            "customerName": buyer_name,
            "customerPhoneNo": buyer_phone,
            "currencyCode": "NGN",
            "merchantChargePercentage": 100,
            "redirectUrl": "https://siiqo.com/CartSystem?success=true",
            "returnUrl": "https://siiqo.com/CartSystem?success=true",
            "webhookNotificationUrl": os.environ.get(
                'PAYSCROW_WEBHOOK_URL',
                "https://devapi.siiqo.app/api/escrow/webhook"
            ),
            "items": items_payload,
            "settlementAccounts": settlement_accounts
        }
        
        try:
            resp = requests.post(f"{base_url}/api/v3/marketplace/transactions/start", json=payload, headers=headers)
            try:
                resp_data = resp.json()
            except Exception as e:
                logging.error(f"Payscrow JSON Decode Error: {e}, Status: {resp.status_code}, Text: {resp.text}")
                return {
                    "success": False,
                    "error_message": f"Payscrow invalid response: {resp.status_code}"
                }
                
            if not resp_data.get('success'):
                logging.error(f"Payscrow API error: {resp_data}")
                return {
                    "success": False,
                    "error_message": "Escrow init failed: " + str(resp_data.get('errors'))
                }
            
            data = resp_data.get('data', {})
            return {
                "success": True,
                "payment_link": data.get('paymentLink'),
                "transaction_number": txn_number,
                "provider_transaction_id": str(data.get('transactionId')) if data.get('transactionId') else None,
                "provider_reference": str(data.get('transactionNumber')) if data.get('transactionNumber') else None,
                "amount": float(order.total_amount),
                "fee_amount": fee_amount,
                "error_message": None
            }
            
        except Exception as e:
            logging.error(f"Payscrow HTTP error: {e}")
            return {
                "success": False,
                "error_message": "Could not connect to Escrow provider"
            }

    def verify_transaction(self, provider_reference):
        pass

    def handle_webhook(self, payload, signature_header=None):
        pass
