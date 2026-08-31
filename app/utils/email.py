import os
import re
import smtplib
import ssl
import threading
import logging
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import render_template, current_app, request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EMAIL VALIDATION - PREVENT SMTP INJECTION & MALFORMED ADDRESSES
# ---------------------------------------------------------------------------

def _validate_email(email: str) -> bool:
    """
    RFC 5322 compliant email validation with security hardening.
    Blocks: SQL injection, XSS, header injection, and malformed addresses.
    
    Returns: True if valid, False otherwise
    """
    if not email or not isinstance(email, str):
        return False
    
    email = email.strip()
    
    # Length checks (RFC 5321)
    if len(email) < 3 or len(email) > 254:
        return False
    
    # Block dangerous characters (header injection prevention)
    dangerous_chars = ['\n', '\r', '\0', '<', '>', '"', '\\', ',', ';']
    if any(char in email for char in dangerous_chars):
        logger.warning(f"[SECURITY] Blocked email with dangerous characters: {email[:20]}...")
        return False
    
    # RFC 5322 email pattern with strict validation
    pattern = r'^[a-zA-Z0-9][a-zA-Z0-9._%+-]*@[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)*\.[a-zA-Z]{2,}$'
    
    if not re.match(pattern, email):
        return False
    
    # Additional security checks
    local, domain = email.split('@')
    
    # Local part (before @) validation
    if len(local) > 64 or len(local) < 1:
        return False
    if local.startswith('.') or local.endswith('.'):
        return False
    if '..' in local:
        return False
    
    # Domain validation
    if len(domain) > 253 or len(domain) < 3:
        return False
    if domain.startswith('-') or domain.endswith('-'):
        return False
    if '..' in domain:
        return False
    
    # Block common typosquatting/malicious domains
    blocked_patterns = ['temp-mail', 'throwaway', 'guerrilla', 'mailinator', '10minutemail']
    if any(pattern in domain.lower() for pattern in blocked_patterns):
        logger.warning(f"[SECURITY] Blocked disposable email domain: {domain}")
        return False
    
    return True

def _send_email_async(mail_server, mail_port, mail_username, mail_password, mail_sender, to_email, subject, html_content, context):
    """Background worker for sending emails."""
    # â”€â”€ LOCAL DEV FALLBACK â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # If SMTP credentials are missing, print OTP to console instead of crashing.
    if not mail_server or not mail_username or not mail_password:
        logger.info("\n" + "="*60)
        logger.info("âš   SMTP CREDENTIALS NOT SET IN .env â€” EMAIL NOT SENT")
        logger.info(f"   TO      : {to_email}")
        logger.info(f"   SUBJECT : {subject}")
        if 'otp' in context:
            logger.info(f"   >>>  TEST OTP: {context['otp']}  <<<")
        logger.info("="*60 + "\n")
        return

    # â”€â”€ BUILD MESSAGE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Use MIMEMultipart so we can explicitly set charset=utf-8 on every part.
    # This fixes emojis (ðŸŽ‰ âš¡) and Naira sign (â‚¦) rendering as garbled Latin-1
    # characters like Ã°Å¸Å½â€° and Ã‚Å Â¡ in Gmail and other clients.
    msg = MIMEMultipart('alternative')

    # Subject: RFC 2047 encoded-word â€” supports emojis and any Unicode in headers
    msg['Subject'] = Header(subject, charset='utf-8')
    msg['From']    = f"Siiqo <{mail_sender}>"
    msg['To']      = to_email

    # Plain-text fallback (utf-8)
    plain_part = MIMEText("Please enable HTML to view this Siiqo email.", 'plain', 'utf-8')
    # HTML body (utf-8) â€” last attachment wins per RFC 2046 multipart/alternative
    html_part  = MIMEText(html_content, 'html', 'utf-8')

    msg.attach(plain_part)
    msg.attach(html_part)

    ssl_context = ssl.create_default_context()

    # â”€â”€ SEND â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

        logger.info(f"[EMAIL OK] Sent '{subject}' to {to_email}")

    except Exception as e:
        import traceback
        import logging
        logging.error(f"[EMAIL ERROR] Failed to send email to {to_email}. Error: {str(e)}\n{traceback.format_exc()}")


def send_siiqo_email(to_email, subject, template_name, **context):
    """
    Sends a branded Siiqo email via SMTP (AWS SES / any STARTTLS provider).
    Execution is offloaded to a background thread to prevent blocking HTTP requests.
    
    Security: Validates email before sending to prevent SMTP injection attacks.
    """
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # SECURITY: Validate email address before any processing
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if not _validate_email(to_email):
        logger.error(f"[EMAIL BLOCKED] Invalid or malicious email format: {to_email[:50]}")
        return False
    
    mail_server   = os.environ.get('MAIL_SERVER')
    mail_port     = int(os.environ.get('MAIL_PORT', 587))
    mail_username = os.environ.get('MAIL_USERNAME')
    mail_password = os.environ.get('MAIL_PASSWORD')
    mail_sender   = os.environ.get('MAIL_DEFAULT_SENDER', mail_username)

    try:
        context['logo_url'] = f"{request.host_url.rstrip('/')}/static/logo.png"
    except Exception:
        context['logo_url'] = "https://siiqo.com/images/Siiqo.png"  # Fallback

    # Render the HTML template synchronously (requires request context)
    try:
        html_content = render_template(f"emails/{template_name}.html", **context)
    except Exception as tmpl_err:
        first_name = context.get('first_name', 'Vendor')
        html_content = f"<h1>Siiqo</h1><p>Hi {first_name},</p><p>{subject}</p>"
        logger.info(f"[EMAIL WARN] Template render failed ({tmpl_err}), using plain fallback.")

    # Dispatch to background thread
    thread = threading.Thread(
        target=_send_email_async,
        args=(mail_server, mail_port, mail_username, mail_password, mail_sender, to_email, subject, html_content, context)
    )
    thread.daemon = True
    thread.start()
    return True

