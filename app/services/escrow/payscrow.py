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
    def initiate_transaction(self, order, vendor_bank, existing_txn_number=None):
        payscrow_key, base_url = _payscrow_env()
        
        if not payscrow_key:
            return {
                "success": False,
                "error_message": "Payscrow API key not configured."
            }

        txn_number = existing_txn_number if existing_txn_number else f"ESC-{uuid.uuid4().hex[:12].upper()}"
        vendor_payout = float(order.total_amount)
        fee_amount = 0.0

        # Safe defaults to satisfy strict API constraints
        vendor_name = (order.vendor.first_name if order.vendor and order.vendor.first_name else "Siiqo Vendor")
        buyer_name = (order.buyer.first_name if order.buyer and order.buyer.first_name else "Siiqo Buyer")
        
        import re
        def format_phone(phone_str):
            if not phone_str:
                return "08012345678"
            # Strip non-digits
            digits = re.sub(r'\D', '', phone_str)
            if digits.startswith('234') and len(digits) == 13:
                return '0' + digits[3:]
            if len(digits) == 11 and digits.startswith('0'):
                return digits
            if len(digits) == 10:
                return '0' + digits
            # Fallback for completely invalid strings
            return "08012345678"
            
        vendor_phone = format_phone(order.vendor.phone if order.vendor else None)
        buyer_phone = format_phone(order.buyer.phone if order.buyer else None)

        total_amount = float(order.total_amount)
        
        # Calculate splits
        # Vendor gets 88%
        vendor_payout = round(total_amount * 0.88, 2)
        
        # Siiqo gets 12% MINUS Payscrow's Escrow fee (3.5% + 100 NGN)
        siiqo_payout = round((total_amount * 0.12) - (total_amount * 0.035 + 100), 2)
        
        # Clamp Siiqo payout to 0 if it goes negative for very small amounts
        if siiqo_payout < 0:
            vendor_payout = vendor_payout + siiqo_payout # absorb the negative balance
            siiqo_payout = 0

        fee_amount = round(total_amount * 0.12, 2) # Logical Siiqo fee to save in our DB
        
        siiqo_bank_code = os.environ.get("SIIQO_BANK_CODE", "011") # Default First Bank
        siiqo_account_number = os.environ.get("SIIQO_BANK_ACCOUNT", "3050025256")
        siiqo_account_name = os.environ.get("SIIQO_BANK_NAME", "Siiqo Marketplace")

        headers = {
            "BrokerApiKey": payscrow_key,
            "Content-Type": "application/json"
        }
        
        settlement_accounts = [
            {
                "bankCode": vendor_bank.bank_code,
                "accountNumber": vendor_bank.account_number,
                "accountName": vendor_bank.account_name,
                "amount": vendor_payout
            }
        ]
        
        if siiqo_payout > 0:
            settlement_accounts.append({
                "bankCode": siiqo_bank_code,
                "accountNumber": siiqo_account_number,
                "accountName": siiqo_account_name,
                "amount": siiqo_payout
            })
        
        payload = {
            "transactionReference": txn_number,
            "merchantEmailAddress": order.vendor.email if order.vendor else "vendor@siiqo.com",
            "merchantName": vendor_name,
            "merchantPhoneNo": vendor_phone,
            "customerEmailAddress": order.buyer.email if order.buyer else "buyer@siiqo.com",
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
            "items": [
                {
                    "name": f"Siiqo Order #{order.id}",
                    "description": f"Payment for order #{order.id} on Siiqo Marketplace",
                    "quantity": 1,
                    "price": float(order.total_amount)
                }
            ],
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
