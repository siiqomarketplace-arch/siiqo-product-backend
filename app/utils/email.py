import os
import smtplib
import ssl
from email.message import EmailMessage
from flask import render_template

def send_siiqo_email(to_email, subject, template_name, **context):
    """
    Sends a branded Siiqo email via SMTP (AWS SES / any STARTTLS provider).
    Raises an exception if sending fails so callers can handle/log the error.
    Falls back to console-print during local dev when no credentials are set.
    """
    mail_server   = os.environ.get('MAIL_SERVER')
    mail_port     = int(os.environ.get('MAIL_PORT', 587))
    mail_username = os.environ.get('MAIL_USERNAME')
    mail_password = os.environ.get('MAIL_PASSWORD')
    mail_sender   = os.environ.get('MAIL_DEFAULT_SENDER', mail_username)

    # Render the HTML template
    try:
        html_content = render_template(f"emails/{template_name}.html", **context)
    except Exception as tmpl_err:
        html_content = f"<h1>Siiqo</h1><p>{subject}</p><p>{context}</p>"
        print(f"[EMAIL WARN] Template render failed ({tmpl_err}), using plain fallback.")

    # ── LOCAL DEV FALLBACK ────────────────────────────────────────────────────
    # If SMTP credentials are missing, print OTP to console instead of crashing.
    if not mail_server or not mail_username or not mail_password:
        print("\n" + "="*60)
        print("⚠  SMTP CREDENTIALS NOT SET IN .env — EMAIL NOT SENT")
        print(f"   TO      : {to_email}")
        print(f"   SUBJECT : {subject}")
        if 'otp' in context:
            print(f"   >>>  TEST OTP: {context['otp']}  <<<")
        print("="*60 + "\n")
        return True  # Non-blocking in dev

    # ── BUILD MESSAGE ─────────────────────────────────────────────────────────
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From']    = f"Siiqo <{mail_sender}>"
    msg['To']      = to_email
    msg.set_content("Please enable HTML to view this Siiqo email.")
    msg.add_alternative(html_content, subtype='html')

    ssl_context = ssl.create_default_context()

    # ── SEND ──────────────────────────────────────────────────────────────────
    try:
        if mail_port == 465:
            with smtplib.SMTP_SSL(mail_server, mail_port, context=ssl_context, timeout=15) as server:
                server.login(mail_username, mail_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(mail_server, mail_port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=ssl_context)
                server.ehlo()
                server.login(mail_username, mail_password)
                server.send_message(msg)

        print(f"[EMAIL OK] Sent '{subject}' to {to_email}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        msg_text = (
            f"[EMAIL ERROR] AWS SES authentication failed for user '{mail_username}'.\n"
            f"  → Check that MAIL_USERNAME / MAIL_PASSWORD are your SES SMTP credentials\n"
            f"    (NOT your AWS console login — generate them at: IAM → Users → Security credentials → SMTP credentials).\n"
            f"  → Raw error: {e}"
        )
        print(msg_text)
        raise RuntimeError(msg_text) from e

    except smtplib.SMTPRecipientsRefused as e:
        msg_text = (
            f"[EMAIL ERROR] Recipient '{to_email}' was refused by SES.\n"
            f"  → If your SES account is in SANDBOX mode, you can only send to VERIFIED email addresses.\n"
            f"    Go to AWS Console → SES → Verified Identities and add {to_email} OR request production access.\n"
            f"  → Raw error: {e}"
        )
        print(msg_text)
        raise RuntimeError(msg_text) from e

    except smtplib.SMTPSenderRefused as e:
        msg_text = (
            f"[EMAIL ERROR] Sender '{mail_sender}' was refused by SES.\n"
            f"  → The sender email/domain must be verified in AWS SES.\n"
            f"    Go to AWS Console → SES → Verified Identities → Add '{mail_sender}' or verify 'siiqo.com'.\n"
            f"  → Raw error: {e}"
        )
        print(msg_text)
        raise RuntimeError(msg_text) from e

    except Exception as e:
        msg_text = f"[EMAIL ERROR] {type(e).__name__}: {e}"
        print(msg_text)
        raise RuntimeError(msg_text) from e

