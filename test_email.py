"""
test_email.py - Tests the Siiqo SMTP email system directly.
Run this to see exactly why emails are failing.
"""
import os
import smtplib
import ssl
from dotenv import load_dotenv

load_dotenv()

mail_server   = os.environ.get('MAIL_SERVER')
mail_port     = int(os.environ.get('MAIL_PORT', 465))
mail_username = os.environ.get('MAIL_USERNAME')
mail_password = os.environ.get('MAIL_PASSWORD')
mail_sender   = os.environ.get('MAIL_DEFAULT_SENDER', mail_username)

print("\n===== SIIQO EMAIL DIAGNOSTICS =====")
print(f"  MAIL_SERVER   : {mail_server}")
print(f"  MAIL_PORT     : {mail_port}")
print(f"  MAIL_USERNAME : {mail_username}")
print(f"  MAIL_PASSWORD : {'*' * len(mail_password) if mail_password else 'NOT SET'}")
print(f"  MAIL_SENDER   : {mail_sender}")
print("===================================\n")

if not all([mail_server, mail_username, mail_password]):
    print("ERROR: One or more SMTP credentials are missing from .env!")
    exit(1)

# --- Test 1: DNS / Network Connectivity ---
print(f"[TEST 1] Connecting to {mail_server}:{mail_port}...")
try:
    # cPanel shared hosting uses a server certificate (e.g. server123.web-hosting.com)
    # not matching your custom domain (mail.siiqo.com), so we disable hostname check.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with smtplib.SMTP_SSL(mail_server, mail_port, context=context, timeout=15) as server:
        print("  -> Connection established! Server responded.")

        # --- Test 2: Login ---
        print(f"[TEST 2] Logging in as {mail_username}...")
        server.login(mail_username, mail_password)
        print("  -> Login SUCCESSFUL!")

        # --- Test 3: Send a real test email ---
        TEST_RECIPIENT = mail_username  # Send to yourself
        print(f"[TEST 3] Sending test email to {TEST_RECIPIENT}...")

        from email.message import EmailMessage
        msg = EmailMessage()
        msg['Subject'] = "Siiqo SMTP Test - It Works!"
        msg['From']    = f"Siiqo <{mail_sender}>"
        msg['To']      = TEST_RECIPIENT
        msg.set_content("If you receive this, your Siiqo email system is fully operational!")
        msg.add_alternative("""
        <html><body>
          <div style="font-family:Arial;padding:32px;background:#f4f4f5;">
            <div style="background:#0b1b3b;padding:24px;border-radius:12px;text-align:center;">
              <h1 style="color:#fff;margin:0;">Siiqo</h1>
            </div>
            <div style="padding:24px;background:#fff;border-radius:12px;margin-top:16px;">
              <h2 style="color:#111827;">Email Test Passed!</h2>
              <p style="color:#6b7280;">Your Namecheap SMTP is correctly configured and sending emails.</p>
              <p style="color:#6b7280;">Forgot Password OTPs will be delivered successfully.</p>
            </div>
          </div>
        </body></html>
        """, subtype='html')

        server.send_message(msg)
        print(f"  -> Email SENT to {TEST_RECIPIENT}!")
        print("\n  CHECK YOUR INBOX for 'Siiqo SMTP Test' email!")

except smtplib.SMTPAuthenticationError:
    print("\nERROR: Login FAILED - Wrong username or password!")
    print("  -> Double-check MAIL_USERNAME and MAIL_PASSWORD in your .env file.")
    print("  -> Make sure you are using the EMAIL password, not your Namecheap account password.")

except smtplib.SMTPConnectError as e:
    print(f"\nERROR: Could not connect to {mail_server}:{mail_port}")
    print(f"  -> {e}")
    print("  -> Check that MAIL_SERVER=mail.privateemail.com and MAIL_PORT=465 are correct.")

except TimeoutError:
    print(f"\nERROR: Connection TIMED OUT to {mail_server}:{mail_port}")
    print("  -> Your firewall or ISP may be blocking outbound port 465.")
    print("  -> Try changing MAIL_PORT to 587 in your .env file.")

except Exception as e:
    print(f"\nERROR: {type(e).__name__}: {e}")

print("\n===================================\n")
