"""
chat.py — Messaging and notifications
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.extensions import db
from app.models.communication import Message, Notification
from app.models.user import User

chat_bp = Blueprint('chat', __name__)


# ---------------------------------------------------------------------------
# POST /chat/send
# ---------------------------------------------------------------------------

@chat_bp.route('/send', methods=['POST'])
@jwt_required()
def send_message():
    user_id = get_jwt_identity()
    
    if request.content_type and request.content_type.startswith('multipart/form-data'):
        data = request.form
    else:
        data = request.get_json() or {}

    receiver_id = data.get('receiver_id')
    content = (data.get('content') or '').strip()
    order_id = data.get('order_id')

    if not receiver_id:
        return jsonify({"message": "receiver_id is required"}), 400

    # Verify receiver exists
    receiver = db.session.get(User, int(receiver_id))
    if not receiver:
        return jsonify({"message": "Recipient not found"}), 404

    image_url = None
    if request.files and 'image' in request.files:
        from app.utils.upload import save_uploaded_file
        try:
            image_url = save_uploaded_file(request.files['image'], subfolder='chat')
        except ValueError as e:
            return jsonify({"message": str(e)}), 400

    if not content and not image_url:
        return jsonify({"message": "content or image is required"}), 400

    msg = Message(
        sender_id=user_id,
        receiver_id=receiver_id,
        content=content,
        image_url=image_url,
        order_id=order_id,
    )
    db.session.add(msg)

    # Create notification for receiver
    sender = db.session.get(User, int(user_id))
    db.session.add(Notification(
        user_id=receiver_id,
        title=f"New message from {sender.full_name if sender else 'Someone'}",
        message="Sent an image" if (image_url and not content) else content[:100],
        type="CHAT",
        order_id=order_id,
    ))

    db.session.commit()
    return jsonify({
        "message": "Message sent",
        "id": msg.id,
        "image_url": image_url,
        "status": "success",
    }), 201


# ---------------------------------------------------------------------------
# GET /chat/conversation/<partner_id>
# ---------------------------------------------------------------------------

@chat_bp.route('/conversation/<int:partner_id>', methods=['GET'])
@jwt_required()
def get_conversation(partner_id):
    user_id = get_jwt_identity()
    order_id = request.args.get('order_id')
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 50)), 100)

    query = Message.query.filter(
        ((Message.sender_id == user_id) & (Message.receiver_id == partner_id)) |
        ((Message.sender_id == partner_id) & (Message.receiver_id == user_id))
    )

    if order_id:
        query = query.filter_by(order_id=order_id)

    paginated = query.order_by(Message.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    messages = list(paginated.items)
    messages.reverse()

    # Mark received messages as read
    unread_found = False
    for m in messages:
        if m.receiver_id == int(user_id) and not m.is_read:
            m.is_read = True
            unread_found = True
            
    if unread_found:
        db.session.commit()

    return jsonify({
        "messages": [{
            "id": m.id,
            "sender_id": m.sender_id,
            "receiver_id": m.receiver_id,
            "content": m.content,
            "image_url": m.image_url,
            "is_read": m.is_read,
            "order_id": m.order_id,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        } for m in messages],
        "page": page,
        "pages": paginated.pages,
        "has_more": paginated.has_next
    }), 200


# ---------------------------------------------------------------------------
# GET /chat/threads  — list of all conversations
# ---------------------------------------------------------------------------

@chat_bp.route('/threads', methods=['GET'])
@jwt_required()
def get_threads():
    user_id = int(get_jwt_identity())

    # Get distinct conversation partners
    from sqlalchemy import or_, func
    subq = (
        db.session.query(
            db.case(
                (Message.sender_id == user_id, Message.receiver_id),
                else_=Message.sender_id
            ).label('partner_id'),
            func.max(Message.id).label('last_msg_id')
        )
        .filter(
            or_(Message.sender_id == user_id, Message.receiver_id == user_id)
        )
        .group_by('partner_id')
        .subquery()
    )

    results = db.session.query(Message, subq.c.partner_id).join(
        subq, Message.id == subq.c.last_msg_id
    ).all()

    threads = []
    for msg, partner_id in results:
        partner = db.session.get(User, partner_id)
        unread = Message.query.filter_by(
            sender_id=partner_id, receiver_id=user_id, is_read=False
        ).count()
        threads.append({
            "partner_id": partner_id,
            "partner_name": partner.full_name if partner else "Unknown",
            "partner_pic": partner.profile_pic if partner else None,
            "last_message": msg.content[:80],
            "last_message_at": msg.created_at.isoformat() if msg.created_at else None,
            "unread_count": unread,
        })

    return jsonify({"status": "success", "threads": threads}), 200


# ---------------------------------------------------------------------------
# GET /chat/unread
# ---------------------------------------------------------------------------

@chat_bp.route('/unread', methods=['GET'])
@jwt_required()
def get_unread_count():
    user_id = get_jwt_identity()
    count = Message.query.filter_by(receiver_id=user_id, is_read=False).count()
    return jsonify({"unread_count": count}), 200


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

@chat_bp.route('/notifications', methods=['GET'])
@jwt_required()
def get_notifications():
    user_id = get_jwt_identity()
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 50)

    paginated = (
        Notification.query
        .filter_by(user_id=user_id)
        .order_by(Notification.created_at.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )

    unread_count = Notification.query.filter_by(user_id=user_id, is_read=False).count()

    return jsonify({
        "notifications": [n.to_dict() for n in paginated.items],
        "unread_count": unread_count,
        "total": paginated.total,
        "pages": paginated.pages,
    }), 200


@chat_bp.route('/notifications/<int:notif_id>/read', methods=['PATCH'])
@jwt_required()
def mark_notification_read(notif_id):
    user_id = get_jwt_identity()
    notif = db.session.get(Notification, notif_id)
    if not notif or notif.user_id != int(user_id):
        return jsonify({"message": "Not found"}), 404

    notif.is_read = True
    db.session.commit()
    return jsonify({"message": "Marked as read"}), 200


@chat_bp.route('/notifications/read-all', methods=['PATCH'])
@jwt_required()
def mark_all_read():
    user_id = get_jwt_identity()
    Notification.query.filter_by(user_id=user_id, is_read=False).update({"is_read": True})
    db.session.commit()
    return jsonify({"message": "All notifications marked as read"}), 200
