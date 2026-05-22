import sys
import os

# Ensure the correct path
sys.path.insert(0, os.path.abspath('.'))

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
try:
    from app import create_app
    from app.utils.email import send_siiqo_email

    # Temporarily override environment variables for this test
    os.environ['MAIL_SERVER'] = 'email-smtp.us-east-1.amazonaws.com'
    os.environ['MAIL_PORT'] = '587'
    os.environ['MAIL_USERNAME'] = 'AKIARYY7SWDJKAX5MNIX'
    os.environ['MAIL_PASSWORD'] = 'BEfmobr/+qImw/vwUF78GNe4x/ZpUY5UOSzms0hDmtFR'
    os.environ['MAIL_DEFAULT_SENDER'] = 'auths@siiqo.com'

    app = create_app()
    with app.app_context():
        print("Attempting to send an Auth OTP email via AWS SES...")
        
        success = send_siiqo_email(
            to_email="auths@siiqo.com",
            subject="Your Siiqo Registration OTP (TEST)",
            template_name="otp_email",
            first_name="Admin",
            otp="123456",
            action="Account Registration"
        )
        
        if success:
            print("SUCCESS! Auth OTP was sent to auths@siiqo.com using AWS SES.")
        else:
            print("FAILED to send email.")
except Exception as e:
    print(f"Error: {e}")
