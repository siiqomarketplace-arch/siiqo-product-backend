import os
import smtplib
import ssl
from email.message import EmailMessage
from flask import render_template

def send_siiqo_email(to_email, subject, template_name, **context):
    """
    Sends a beautifully branded Siiqo email via cPanel SMTP.
    Handles SSL certificate hostname mismatches common with shared hosting.
    Falls back to printing the OTP to the console during local testing.
    """
    mail_server   = os.environ.get('MAIL_SERVER')
    mail_port     = int(os.environ.get('MAIL_PORT', 465))
    mail_username = os.environ.get('MAIL_USERNAME')
    mail_password = os.environ.get('MAIL_PASSWORD')
    mail_sender   = os.environ.get('MAIL_DEFAULT_SENDER', mail_username)

    # Render the HTML template
    try:
        html_content = render_template(f"emails/{template_name}.html", **context)
    except Exception:
        html_content = f"<h1>Siiqo</h1><p>{subject}</p><p>{context}</p>"

    # If no credentials configured, fall back to console (safe for local dev)
    if not mail_server or not mail_username or not mail_password:
        print("\n" + "="*50)
        print("SMTP CREDENTIALS NOT FOUND in .env")
        print(f"TO: {to_email} | SUBJECT: {subject}")
        if 'otp' in context:
            print(f">>> YOUR TEST OTP IS: {context['otp']} <<<")
        print("="*50 + "\n")
        return True

    # Build the email message
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From']    = f"Siiqo <{mail_sender}>"
    msg['To']      = to_email
    msg.set_content("Please enable HTML to view this Siiqo email.")
    msg.add_alternative(html_content, subtype='html')

    # SSL context that allows cPanel's server-hostname certificate
    # (cPanel shared hosting SSL cert is for the server, not the custom domain)
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    # Try port 465 (SSL) first, then fall back to port 587 (STARTTLS)
    try:
        if mail_port == 465:
            with smtplib.SMTP_SSL(mail_server, 465, context=ssl_context, timeout=15) as server:
                server.login(mail_username, mail_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(mail_server, mail_port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=ssl_context)
                server.login(mail_username, mail_password)
                server.send_message(msg)
        return True
    except smtplib.SMTPAuthenticationError:
        print(f"[EMAIL ERROR] Login failed for {mail_username}. Check MAIL_PASSWORD in .env.")
        return False
    except Exception as e:
        print(f"[EMAIL ERROR] {type(e).__name__}: {e}")
        return False

