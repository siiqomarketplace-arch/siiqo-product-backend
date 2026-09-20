"""
backfill_vendor_recipients.py
Resolves and creates Paystack transfer recipients (recipient_code)
for any VendorBankAccount records that have a null or blank recipient_code.
Translates CBN codes (100004 for OPay, 100033 for PalmPay) to Paystack codes.
"""
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def run():
    from app import create_app, db
    from app.models.withdrawal import VendorBankAccount
    from app.services.escrow.paystack_provider import ensure_paystack_transfer_recipient

    app = create_app()
    with app.app_context():
        accounts = VendorBankAccount.query.filter(
            (VendorBankAccount.recipient_code == None) | (VendorBankAccount.recipient_code == "")
        ).all()

        logging.info(f"Found {len(accounts)} vendor bank account(s) missing recipient_code")
        updated = 0

        for acc in accounts:
            if not acc.account_number or not acc.bank_code:
                continue
            logging.info(f"Processing vendor {acc.vendor_id}: {acc.bank_name} {acc.account_number} ({acc.bank_code})")
            res = ensure_paystack_transfer_recipient(
                account_number=acc.account_number,
                bank_code=acc.bank_code,
                account_name=acc.account_name or acc.bank_name,
            )
            if res.get("success"):
                acc.recipient_code = res["recipient_code"]
                if not acc.account_name and res.get("account_name"):
                    acc.account_name = res["account_name"]
                logging.info(f"  -> SUCCESS: Assigned {acc.recipient_code} (Name: {acc.account_name})")
                updated += 1
            else:
                logging.warning(f"  -> FAILED: {res.get('error_message')}")

        if updated > 0:
            db.session.commit()
            logging.info(f"Committed {updated} updated recipient_code(s) to database.")
        else:
            logging.info("No accounts updated.")

if __name__ == "__main__":
    run()
