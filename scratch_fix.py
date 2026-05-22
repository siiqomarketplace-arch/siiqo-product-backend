import os

file_path = "app/routes/admin.py"
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# We know the duplicate blog and settings block starts right after the broken broadcast
# Instead of complex regex, let's just find the last "Platform Settings" block, and
# keep everything BEFORE the *first* "Broadcast Email" block, and then append the correct
# "Broadcast Email" block to the end.

broadcast_header = "# ---------------------------------------------------------------------------\n# Broadcast Email\n# ---------------------------------------------------------------------------"

# Split at the FIRST occurrence of the broadcast header
parts = content.split(broadcast_header, 1)

if len(parts) == 2:
    clean_top_half = parts[0]
    
    correct_broadcast_code = """# ---------------------------------------------------------------------------
# Broadcast Email
# ---------------------------------------------------------------------------

@admin_bp.route('/broadcast', methods=['POST'])
@jwt_required()
def send_email_broadcast():
    admin_id = get_jwt_identity()
    if not _require_superadmin(_parse_admin_id(admin_id)):
        return jsonify({"message": "SuperAdmin required"}), 403

    data = request.get_json() or {}
    target_audience = data.get('target_audience') or data.get('audience', 'ALL')
    subject = data.get('subject', '')
    body = data.get('body', '')
    critical = data.get('critical', False)

    if not subject or not body:
        return jsonify({"message": "Subject and body are required"}), 400

    if target_audience == 'VENDORS':
        recipients = User.query.filter_by(role='VENDOR', is_subscribed_to_broadcasts=True).all()
    elif target_audience == 'BUYERS':
        recipients = User.query.filter_by(role='BUYER', is_subscribed_to_broadcasts=True).all()
    elif target_audience == 'PARTNERS':
        recipients = User.query.filter_by(role='PARTNER', is_subscribed_to_broadcasts=True).all()
    elif target_audience == 'CUSTOM':
        custom_emails_raw = data.get('customEmails') or data.get('custom_emails', '')
        custom_emails = [e.strip() for e in custom_emails_raw.split(',') if e.strip()]
        if not custom_emails:
            return jsonify({"message": "No valid email addresses provided for custom broadcast"}), 400
        class _FakeUser:
            def __init__(self, email):
                self.email = email
                self.first_name = None
                self.is_subscribed_to_broadcasts = True
        recipients = [_FakeUser(e) for e in custom_emails]
    else:
        recipients = User.query.filter_by(is_subscribed_to_broadcasts=True).all()

    sent, failed = 0, 0
    import hashlib
    from flask import current_app
    secret = current_app.config['SECRET_KEY']

    for user in recipients:
        try:
            token = hashlib.sha256(f"{user.email}{secret}".encode()).hexdigest()[:16]
            # Replace /api/admin/broadcast with just /unsubscribe for frontend routing
            base_url = request.host_url.rstrip('/')
            unsubscribe_link = f"{base_url}/unsubscribe?email={user.email}&token={token}"
            
            ok = send_siiqo_email(
                to_email=user.email,
                subject=subject,
                template_name="broadcast",
                first_name=getattr(user, 'first_name', None) or "Siiqo Member",
                body_content=body,
                unsubscribe_url=None if critical else unsubscribe_link
            )
            if ok:
                sent += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1

    return jsonify({
        "message": "Broadcast dispatched.",
        "details": {
            "audience": target_audience,
            "total_recipients": len(recipients),
            "sent": sent,
            "failed": failed,
        },
    }), 200
"""

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(clean_top_half + correct_broadcast_code)
    print("Fixed admin.py")
else:
    print("Could not find broadcast header")
