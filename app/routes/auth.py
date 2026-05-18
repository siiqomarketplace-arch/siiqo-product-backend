"""
auth.py — Authentication routes
Handles: register, verify-email, login, logout, refresh, profile,
         forgot-password, reset-password, upload-profile-pic, delete-account
"""
import random
from datetime import datetime, timedelta, timezone

from flask import Blueprint, request, jsonify
from flask_jwt_extended import (
    create_access_token, create_refresh_token,
    jwt_required, get_jwt_identity,
    verify_jwt_in_request, get_jwt
)

from app.extensions import db, limiter
from app.models.user import User, UserRole
from app.utils.upload import save_uploaded_file
from app.utils.email import send_siiqo_email

auth_bp = Blueprint('auth', __name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow():
    return datetime.now(timezone.utc)


def _make_tokens(user: User) -> dict:
    identity = str(user.id)
    return {
        "access_token": create_access_token(identity=identity),
        "refresh_token": create_refresh_token(identity=identity),
    }


def _user_payload(user: User) -> dict:
    payload = {
        **user.to_public_dict(),
        "has_storefront": user.storefront is not None,
        "storefront_slug": user.storefront.store_slug if user.storefront else None,
        "storefront_verified": user.storefront.is_verified if user.storefront else False,
        "storefront_published": user.storefront.is_published if user.storefront else False,
        # business_name: storefront name for vendors, None for buyers
        "business_name": user.storefront.store_name if user.storefront else None,
    }
    return payload


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

@auth_bp.route('/register', methods=['POST'])
@limiter.limit("5 per minute")
def register():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password', '')
    first_name = (data.get('first_name') or '').strip()
    last_name = (data.get('last_name') or '').strip()
    referral_code_used = (data.get('referral_code') or '').strip().upper()

    if not email or not password:
        return jsonify({"message": "Email and password are required"}), 400

    if len(password) < 8:
        return jsonify({"message": "Password must be at least 8 characters"}), 400

    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        if existing_user.is_verified:
            return jsonify({"message": "An account with this email already exists"}), 409
        else:
            # User exists but hasn't verified email yet. Resend OTP and return 201 so frontend proceeds.
            otp = str(random.randint(100000, 999999))
            existing_user.reset_otp = otp
            existing_user.otp_expiry = _utcnow() + timedelta(minutes=10)
            
            # Ensure password gets updated if they typed a new one during this new signup attempt
            existing_user.set_password(password)
            if first_name: existing_user.first_name = first_name
            if last_name: existing_user.last_name = last_name
            
            db.session.commit()
            
            try:
                send_siiqo_email(
                    to_email=existing_user.email,
                    subject="Verify Your Siiqo Account",
                    template_name="verify_email_otp",
                    first_name=existing_user.first_name or "there",
                    otp=otp,
                    verification_link=f"{os.environ.get('FRONTEND_URL', 'https://siiqo.com').rstrip('/')}/auth/verify-otp?email={existing_user.email}&otp={otp}"
                )
            except Exception as e:
                print(f"[WARN] OTP email failed: {e}")
                
            return jsonify({
                "status": "success",
                "message": "Account exists but is unverified. We have resent the verification code.",
                "email": existing_user.email,
                "debug_otp": otp,
            }), 201

    new_user = User(
        email=email,
        first_name=first_name or None,
        last_name=last_name or None,
        role=UserRole.BUYER,
        is_verified=False,
        is_active=True,
    )
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.flush()
    new_user.generate_referral_code()

    # Process referral
    if referral_code_used:
        referrer = User.query.filter_by(referral_code=referral_code_used).first()
        if referrer and referrer.id != new_user.id:
            from app.models.partnerships import Referral
            db.session.add(Referral(
                referrer_id=referrer.id,
                referred_id=new_user.id,
                referral_code_used=referral_code_used,
                status='PENDING',
                reward_earned=50.0,
            ))
            referrer.points_balance = float(referrer.points_balance or 0) + 50.0

    # Generate OTP
    otp = str(random.randint(100000, 999999))
    new_user.reset_otp = otp
    new_user.otp_expiry = _utcnow() + timedelta(minutes=10)
    db.session.commit()

    try:
        send_siiqo_email(
            to_email=new_user.email,
            subject="Verify Your Siiqo Account",
            template_name="verify_email_otp",
            first_name=new_user.first_name or "there",
            otp=otp,
            verification_link=f"{os.environ.get('FRONTEND_URL', 'https://siiqo.com').rstrip('/')}/auth/verify-otp?email={new_user.email}&otp={otp}"
        )
    except Exception as e:
        print(f"[WARN] OTP email failed: {e}")

    return jsonify({
        "status": "success",
        "message": "Account created. Please check your email for the verification code.",
        "email": new_user.email,
        "debug_otp": otp,
    }), 201


# ---------------------------------------------------------------------------
# Verify Email
# ---------------------------------------------------------------------------

@auth_bp.route('/verify-email', methods=['POST'])
def verify_email():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    otp = str(data.get('otp') or '').strip()

    if not email or not otp:
        return jsonify({"message": "Email and OTP are required"}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"message": "User not found"}), 404

    if not user.reset_otp or user.reset_otp.strip() != otp:
        return jsonify({"message": "Invalid OTP. Please check and try again."}), 400

    if user.otp_expiry and _utcnow() > user.otp_expiry.replace(tzinfo=timezone.utc):
        return jsonify({"message": "OTP has expired. Please request a new one."}), 400

    user.is_verified = True
    user.reset_otp = None
    user.otp_expiry = None
    db.session.commit()

    tokens = _make_tokens(user)
    return jsonify({
        "message": "Email verified successfully. Welcome to Siiqo!",
        "status": "success",
        "user": _user_payload(user),
        **tokens,
    }), 200


# ---------------------------------------------------------------------------
# Resend OTP
# ---------------------------------------------------------------------------

@auth_bp.route('/resend-otp', methods=['POST'])
@limiter.limit("3 per minute")
def resend_otp():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()

    user = User.query.filter_by(email=email).first()
    if not user:
        # Don't reveal whether email exists
        return jsonify({"message": "If that email is registered, a new code has been sent."}), 200

    otp = str(random.randint(100000, 999999))
    user.reset_otp = otp
    user.otp_expiry = _utcnow() + timedelta(minutes=10)
    db.session.commit()

    try:
        send_siiqo_email(
            to_email=user.email,
            subject="Your Siiqo Verification Code",
            template_name="verify_email_otp",
            first_name=user.first_name or "there",
            otp=otp,
            verification_link=f"{os.environ.get('FRONTEND_URL', 'https://siiqo.com').rstrip('/')}/auth/verify-otp?email={user.email}&otp={otp}"
        )
    except Exception as e:
        print(f"[WARN] Resend OTP email failed: {e}")

    return jsonify({
        "message": "If that email is registered, a new code has been sent.",
        "debug_otp": otp
    }), 200


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@auth_bp.route('/login', methods=['POST'])
@limiter.limit("10 per minute")
def login():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password', '')

    user = User.query.filter_by(email=email).first()

    if not user or not user.check_password(password):
        return jsonify({"message": "Invalid email or password"}), 401

    if not user.is_active:
        return jsonify({"message": "Your account has been suspended. Please contact support."}), 403

    if not user.is_verified:
        # Re-send OTP so they can verify
        otp = str(random.randint(100000, 999999))
        user.reset_otp = otp
        user.otp_expiry = _utcnow() + timedelta(minutes=10)
        db.session.commit()
        try:
            send_siiqo_email(
                to_email=user.email,
                subject="Verify Your Siiqo Account",
                template_name="verify_email_otp",
                first_name=user.first_name or "there",
                otp=otp,
                verification_link=f"{os.environ.get('FRONTEND_URL', 'https://siiqo.com').rstrip('/')}/auth/verify-otp?email={user.email}&otp={otp}"
            )
        except Exception:
            pass
        return jsonify({
            "message": "Please verify your email first. A new code has been sent.",
            "requires_verification": True,
            "email": user.email,
        }), 403

    tokens = _make_tokens(user)
    return jsonify({
        "message": "Login successful",
        "user": _user_payload(user),
        **tokens,
        # Legacy alias — some frontend code reads 'token'
        "token": tokens["access_token"],
        "access_token": tokens["access_token"],
    }), 200


# ---------------------------------------------------------------------------
# Refresh Token  (uses proper refresh token, not access token)
# ---------------------------------------------------------------------------

@auth_bp.route('/refresh', methods=['POST'])
@jwt_required(refresh=True)
def refresh_token():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user or not user.is_active:
        return jsonify({"message": "User not found or suspended"}), 404

    new_access = create_access_token(identity=str(user.id))
    return jsonify({
        "access_token": new_access,
        "user": _user_payload(user),
    }), 200


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@auth_bp.route('/profile', methods=['GET'])
@jwt_required()
def profile():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "User not found"}), 404
    payload = _user_payload(user)
    # Ensure city falls back to storefront city for vendors
    if not payload.get('city') and user.storefront:
        payload['city'] = user.storefront.city
    return jsonify(payload), 200


# ---------------------------------------------------------------------------
# Switch Mode (vendor ↔ buyer view — frontend routing hint only)
# ---------------------------------------------------------------------------

@auth_bp.route('/switch-mode', methods=['POST'])
@jwt_required()
def switch_mode():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "User not found"}), 404

    data = request.get_json() or {}
    target_view = data.get('target_view', 'buyer')

    if target_view == 'vendor' and user.role not in [UserRole.VENDOR, UserRole.ADMIN]:
        return jsonify({"message": "You must complete vendor onboarding first."}), 403

    return jsonify({
        "message": f"Switched to {target_view} view",
        "user": _user_payload(user),
    }), 200


# ---------------------------------------------------------------------------
# Forgot Password
# ---------------------------------------------------------------------------

@auth_bp.route('/forgot-password', methods=['POST'])
@limiter.limit("3 per minute")
def forgot_password():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()

    user = User.query.filter_by(email=email).first()
    if user:
        otp = str(random.randint(100000, 999999))
        user.reset_otp = otp
        user.otp_expiry = _utcnow() + timedelta(minutes=15)
        db.session.commit()
        try:
            send_siiqo_email(
                to_email=user.email,
                subject="Siiqo Password Reset Code",
                template_name="reset_password_otp",
                first_name=user.first_name or "there",
                otp=otp,
            )
        except Exception as e:
            print(f"[WARN] Forgot password email failed: {e}")

    # Always return 200 to prevent email enumeration
    return jsonify({"message": "If that email is registered, a reset code has been sent."}), 200


# ---------------------------------------------------------------------------
# Reset Password
# ---------------------------------------------------------------------------

@auth_bp.route('/reset-password', methods=['POST'])
def reset_password():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    otp = str(data.get('otp') or '').strip()
    new_password = data.get('password') or data.get('new_password', '')

    if not new_password:
        return jsonify({"message": "New password is required"}), 400

    if len(new_password) < 8:
        return jsonify({"message": "Password must be at least 8 characters"}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"message": "User not found"}), 404

    if not user.reset_otp or user.reset_otp.strip() != otp:
        return jsonify({"message": "Invalid or expired code"}), 400

    if user.otp_expiry and _utcnow() > user.otp_expiry.replace(tzinfo=timezone.utc):
        return jsonify({"message": "Code has expired. Please request a new one."}), 400

    user.set_password(new_password)
    user.reset_otp = None
    user.otp_expiry = None
    db.session.commit()

    return jsonify({"message": "Password reset successful. You can now log in."}), 200


# ---------------------------------------------------------------------------
# Upload Profile Picture
# ---------------------------------------------------------------------------

@auth_bp.route('/upload-profile-pic', methods=['POST'])
@jwt_required()
def upload_profile_pic():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "User not found"}), 404

    pic_file = request.files.get('profile_pic') or request.files.get('file')
    if not pic_file:
        return jsonify({"message": "No file uploaded"}), 400

    try:
        saved_url = save_uploaded_file(pic_file, subfolder='profiles')
        user.profile_pic = saved_url
        db.session.commit()
        return jsonify({"message": "Profile picture updated", "url": saved_url}), 200
    except ValueError as e:
        return jsonify({"message": str(e)}), 400


# ---------------------------------------------------------------------------
# Delete Account
# ---------------------------------------------------------------------------

@auth_bp.route('/delete-account', methods=['DELETE'])
@jwt_required()
def delete_account():
    user_id = get_jwt_identity()
    user = db.session.get(User, int(user_id))
    if not user:
        return jsonify({"message": "User not found"}), 404

    data = request.get_json() or {}
    password = data.get('password', '')

    if not password:
        return jsonify({"message": "Password confirmation required"}), 400

    if not user.check_password(password):
        return jsonify({"message": "Incorrect password"}), 403

    try:
        db.session.delete(user)
        db.session.commit()
        return jsonify({"status": "success", "message": "Account permanently deleted"}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"message": "Failed to delete account. Please try again."}), 500
