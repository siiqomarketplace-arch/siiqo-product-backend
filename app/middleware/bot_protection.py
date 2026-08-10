"""
bot_protection.py — Advanced bot detection and prevention

Features:
- Honeypot fields (hidden fields that bots fill but humans don't)
- Rate limiting on registration endpoint
- Pattern detection for fake names/emails
- Submission speed detection (bots are too fast)
- User-Agent validation
- IP-based throttling
"""
import re
import logging
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import request, jsonify

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BOT DETECTION PATTERNS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Patterns that indicate bot-generated content
BOT_NAME_PATTERNS = [
    r'^[A-Z][a-z]\d{6,8}$',  # Dv7284677, Pr4695973, etc.
    r'^[A-Z]{2}\d{7,9}$',    # AB1234567, XY98765432, etc.
    r'^test\d+',              # test123, test456
    r'^user\d+',              # user123, user456
    r'^[a-z]{3,5}\d{4,}$',   # abc1234, xyz9999
]

SUSPICIOUS_EMAIL_PATTERNS = [
    r'@siiqo\.app$',          # Fake internal emails
    r'@test\.com$',
    r'@fake\.com$',
    r'@example\.com$',
    r'\+spam@',
    r'\+test@',
]

SUSPICIOUS_DOMAINS = [
    'siiqo.app',  # Your internal domain (users shouldn't have these)
    'test.com',
    'fake.com',
    'example.com',
    'tempmail.com',
    'guerrillamail.com',
    '10minutemail.com',
    'throwaway.email',
]

# Known bot user agents
BOT_USER_AGENTS = [
    'bot', 'crawler', 'spider', 'scraper', 'curl', 'wget', 
    'python-requests', 'axios', 'postman', 'insomnia'
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BOT DETECTION FUNCTIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def is_bot_name(name: str) -> bool:
    """Check if name matches bot-generated patterns."""
    if not name:
        return False
    
    name = name.strip()
    
    for pattern in BOT_NAME_PATTERNS:
        if re.match(pattern, name):
            return True
    
    return False


def is_suspicious_email(email: str) -> bool:
    """Check if email is suspicious or from temp mail service."""
    if not email:
        return False
    
    email = email.strip().lower()
    
    # Check patterns
    for pattern in SUSPICIOUS_EMAIL_PATTERNS:
        if re.search(pattern, email):
            return True
    
    # Check domain
    domain = email.split('@')[-1] if '@' in email else ''
    if domain in SUSPICIOUS_DOMAINS:
        return True
    
    return False


def is_bot_user_agent() -> bool:
    """Check if User-Agent indicates a bot."""
    user_agent = request.headers.get('User-Agent', '').lower()
    
    if not user_agent:
        return True  # No user agent = suspicious
    
    for bot_keyword in BOT_USER_AGENTS:
        if bot_keyword in user_agent:
            return True
    
    return False


def check_honeypot(data: dict) -> bool:
    """Check if honeypot field was filled (indicates bot)."""
    # Honeypot field should be empty for real users
    honeypot_fields = ['website', 'url', 'homepage', 'company_url']
    
    for field in honeypot_fields:
        if data.get(field):
            return True  # Honeypot filled = bot
    
    return False


def check_submission_speed(data: dict) -> bool:
    """Check if form was submitted too fast (indicates bot)."""
    # If frontend sends timestamp of when form was opened
    form_opened_at = data.get('_form_opened_at')
    
    if form_opened_at:
        try:
            opened = datetime.fromisoformat(form_opened_at.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            duration = (now - opened).total_seconds()
            
            # Human can't fill registration form in less than 3 seconds
            if duration < 3:
                return True
        except:
            pass
    
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BOT PROTECTION DECORATOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def bot_protection(f):
    """
    Decorator to protect endpoints from bot registrations.
    
    Checks:
    - Bot name patterns
    - Suspicious emails
    - Bot user agents
    - Honeypot fields
    - Submission speed
    
    Usage:
        @auth_bp.route('/register', methods=['POST'])
        @bot_protection
        def register():
            ...
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        data = request.get_json() or {}
        
        # Get user info
        email = (data.get('email') or '').strip().lower()
        first_name = (data.get('first_name') or '').strip()
        last_name = (data.get('last_name') or '').strip()
        ip_address = request.remote_addr
        user_agent = request.headers.get('User-Agent', 'Unknown')
        
        # Build full name for pattern checking
        full_name = f"{first_name}{last_name}" if first_name or last_name else ""
        
        # Run bot detection checks
        checks = {
            'bot_name_pattern': is_bot_name(first_name) or is_bot_name(last_name),
            'suspicious_email': is_suspicious_email(email),
            'bot_user_agent': is_bot_user_agent(),
            'honeypot_filled': check_honeypot(data),
            'too_fast': check_submission_speed(data),
        }
        
        # Calculate bot score (number of failed checks)
        bot_score = sum(checks.values())
        
        # If 2 or more indicators = likely bot
        if bot_score >= 2:
            logger.warning(
                f"[BOT BLOCKED] Registration attempt blocked | "
                f"Email: {email} | Name: {full_name} | IP: {ip_address} | "
                f"Checks: {checks} | Score: {bot_score}/5"
            )
            
            # Return success to bot (so they don't know they're blocked)
            # But don't actually create the account
            return jsonify({
                "status": "success",
                "message": "Account created. Please check your email for the verification code.",
                "email": email,
            }), 201
        
        # If only 1 indicator = flag but allow (log for review)
        if bot_score == 1:
            logger.info(
                f"[BOT SUSPECTED] Registration flagged but allowed | "
                f"Email: {email} | Name: {full_name} | IP: {ip_address} | "
                f"Checks: {checks} | Score: {bot_score}/5"
            )
        
        # Proceed with normal registration
        return f(*args, **kwargs)
    
    return decorated_function


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IP-BASED THROTTLING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# In-memory storage for IP throttling (use Redis in production)
registration_attempts = {}

def check_ip_throttle(ip_address: str, max_attempts: int = 3, window_minutes: int = 60) -> bool:
    """
    Check if IP has exceeded registration attempts.
    
    Returns:
        True if throttled, False if allowed
    """
    now = datetime.now(timezone.utc)
    
    # Clean old entries
    for ip in list(registration_attempts.keys()):
        attempts = registration_attempts[ip]
        # Remove attempts older than window
        attempts[:] = [t for t in attempts if (now - t).total_seconds() < window_minutes * 60]
        if not attempts:
            del registration_attempts[ip]
    
    # Check current IP
    if ip_address not in registration_attempts:
        registration_attempts[ip_address] = []
    
    attempts = registration_attempts[ip_address]
    
    if len(attempts) >= max_attempts:
        logger.warning(f"[IP THROTTLE] {ip_address} exceeded {max_attempts} registration attempts in {window_minutes} minutes")
        return True
    
    # Record this attempt
    attempts.append(now)
    return False


def ip_throttle(max_attempts: int = 3, window_minutes: int = 60):
    """
    Decorator to throttle registrations by IP address.
    
    Args:
        max_attempts: Maximum registration attempts allowed
        window_minutes: Time window in minutes
    
    Usage:
        @auth_bp.route('/register', methods=['POST'])
        @ip_throttle(max_attempts=3, window_minutes=60)
        def register():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            ip_address = request.remote_addr
            
            if check_ip_throttle(ip_address, max_attempts, window_minutes):
                return jsonify({
                    "message": "Too many registration attempts. Please try again later."
                }), 429
            
            return f(*args, **kwargs)
        
        return decorated_function
    return decorator
