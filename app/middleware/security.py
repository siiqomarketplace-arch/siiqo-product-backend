"""
security.py — Enterprise-grade security middleware for Siiqo Platform

Features:
- Rate limiting with distributed Redis storage
- IP-based brute force protection
- Admin route IP whitelisting
- Request fingerprinting & anomaly detection
- Automatic threat blocking & logging
- CSRF protection for state-changing operations
- Request signing for sensitive operations
"""
import os
import hashlib
import hmac
import secrets
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional, Dict, Tuple

from flask import request, jsonify, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RATE LIMITER CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_redis_url():
    """Get Redis URL from environment or fallback to in-memory storage."""
    redis_url = os.environ.get('REDIS_URL')
    if redis_url:
        return redis_url
    # Fallback to in-memory for local development
    return "memory://"


limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=get_redis_url(),
    default_limits=["2000 per hour", "100 per minute"],
    strategy="fixed-window",
    headers_enabled=True,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BRUTE FORCE PROTECTION — IN-MEMORY TRACKING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BruteForceProtector:
    """
    Tracks failed login attempts per IP address.
    Implements exponential backoff and automatic banning.
    """
    
    def __init__(self):
        # IP -> {'count': int, 'first_attempt': datetime, 'banned_until': datetime}
        self.failed_attempts: Dict[str, Dict] = {}
        self.banned_ips: Dict[str, datetime] = {}
        
        # Configuration
        self.MAX_ATTEMPTS = 5
        self.LOCKOUT_DURATION = timedelta(minutes=15)
        self.PROGRESSIVE_DELAYS = [0, 1, 2, 5, 10]  # seconds after each failure
        self.BAN_DURATION = timedelta(hours=24)
    
    def _now(self) -> datetime:
        return datetime.now(timezone.utc)
    
    def _get_client_fingerprint(self) -> str:
        """
        Generate unique client fingerprint using IP + User-Agent.
        Prevents bypassing via IP rotation if User-Agent stays same.
        """
        ip = get_remote_address()
        user_agent = request.headers.get('User-Agent', '')
        fingerprint = f"{ip}:{user_agent}"
        return hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
    
    def is_blocked(self, ip: str) -> Tuple[bool, Optional[str]]:
        """
        Check if IP is currently blocked.
        Returns: (is_blocked: bool, reason: str)
        """
        # Check permanent ban
        if ip in self.banned_ips:
            banned_until = self.banned_ips[ip]
            if self._now() < banned_until:
                remaining = int((banned_until - self._now()).total_seconds() / 60)
                return True, f"IP temporarily banned. Try again in {remaining} minutes."
            else:
                del self.banned_ips[ip]
        
        # Check rate limit lockout
        if ip in self.failed_attempts:
            data = self.failed_attempts[ip]
            
            # Reset if lockout period expired
            if 'locked_until' in data and self._now() > data['locked_until']:
                del self.failed_attempts[ip]
                return False, None
            
            # Check if currently locked out
            if 'locked_until' in data:
                remaining = int((data['locked_until'] - self._now()).total_seconds() / 60)
                return True, f"Too many failed attempts. Try again in {remaining} minutes."
        
        return False, None
    
    def record_failure(self, ip: str, username: str = None):
        """
        Record a failed login attempt.
        Implements progressive delays and eventual lockout.
        """
        now = self._now()
        
        if ip not in self.failed_attempts:
            self.failed_attempts[ip] = {
                'count': 1,
                'first_attempt': now,
                'username': username
            }
            logger.warning(f"[SECURITY] Failed login from {ip} (Attempt 1/{self.MAX_ATTEMPTS})")
            return
        
        data = self.failed_attempts[ip]
        data['count'] += 1
        attempt_num = data['count']
        
        logger.warning(f"[SECURITY] Failed login from {ip} (Attempt {attempt_num}/{self.MAX_ATTEMPTS})")
        
        # Trigger lockout after MAX_ATTEMPTS
        if attempt_num >= self.MAX_ATTEMPTS:
            data['locked_until'] = now + self.LOCKOUT_DURATION
            logger.error(f"[SECURITY ALERT] IP {ip} locked out for {self.LOCKOUT_DURATION.seconds // 60} minutes after {attempt_num} failed attempts")
            
            # After 10 failed attempts, escalate to 24-hour ban
            if attempt_num >= 10:
                self.banned_ips[ip] = now + self.BAN_DURATION
                logger.critical(f"[SECURITY CRITICAL] IP {ip} BANNED for 24 hours after {attempt_num} failed attempts")
    
    def record_success(self, ip: str):
        """Clear failed attempts after successful login."""
        if ip in self.failed_attempts:
            logger.info(f"[SECURITY] Successful login from {ip}, clearing failed attempts")
            del self.failed_attempts[ip]
        if ip in self.banned_ips:
            del self.banned_ips[ip]
    
    def get_delay(self, ip: str) -> int:
        """Get progressive delay in seconds based on failed attempts."""
        if ip not in self.failed_attempts:
            return 0
        count = self.failed_attempts[ip]['count']
        if count < len(self.PROGRESSIVE_DELAYS):
            return self.PROGRESSIVE_DELAYS[count]
        return self.PROGRESSIVE_DELAYS[-1]


# Global brute force protector instance
brute_force = BruteForceProtector()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IP WHITELIST FOR ADMIN ROUTES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_admin_whitelist() -> set:
    """
    Load admin IP whitelist from environment variable.
    Format: ADMIN_IP_WHITELIST=192.168.1.1,10.0.0.0/8,102.91.132.0/24
    """
    whitelist = os.environ.get('ADMIN_IP_WHITELIST', '')
    if not whitelist:
        # Development mode: allow all IPs if whitelist not configured
        if os.environ.get('FLASK_ENV') == 'development':
            return {'*'}
        return set()
    
    ips = [ip.strip() for ip in whitelist.split(',') if ip.strip()]
    return set(ips)


def is_ip_whitelisted(ip: str) -> bool:
    """
    Check if IP is in admin whitelist.
    Supports individual IPs and CIDR ranges.
    """
    whitelist = get_admin_whitelist()
    
    # Development mode: allow all
    if '*' in whitelist:
        return True
    
    # Check exact IP match
    if ip in whitelist:
        return True
    
    # Check CIDR ranges
    try:
        import ipaddress
        ip_obj = ipaddress.ip_address(ip)
        
        for allowed in whitelist:
            if '/' in allowed:  # CIDR range
                network = ipaddress.ip_network(allowed, strict=False)
                if ip_obj in network:
                    return True
    except Exception as e:
        logger.error(f"[SECURITY] IP whitelist check error: {e}")
    
    return False


def require_admin_whitelist(f):
    """
    Decorator to restrict admin routes to whitelisted IPs only.
    Returns 403 Forbidden for non-whitelisted IPs.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        ip = get_remote_address()
        
        if not is_ip_whitelisted(ip):
            logger.warning(f"[SECURITY ALERT] Blocked non-whitelisted IP attempting admin access: {ip}")
            return jsonify({
                "message": "Access denied. Admin panel requires whitelisted IP.",
                "error": "FORBIDDEN"
            }), 403
        
        return f(*args, **kwargs)
    
    return decorated_function


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REQUEST ANOMALY DETECTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AnomalyDetector:
    """
    Detects suspicious patterns in requests:
    - Rapid requests from single IP
    - Requests with SQL injection patterns
    - Requests with XSS patterns
    - Suspicious User-Agent strings
    """
    
    SUSPICIOUS_PATTERNS = [
        # SQL injection
        r"(\b(SELECT|UNION|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|EXEC)\b)",
        r"(--|#|\/\*|\*\/)",
        r"(\bOR\b\s+\d+\s*=\s*\d+)",
        r"(';\s*DROP\s+TABLE)",
        
        # XSS patterns
        r"(<script[^>]*>)",
        r"(javascript:)",
        r"(onerror\s*=)",
        r"(onload\s*=)",
        
        # Path traversal
        r"(\.\./|\.\.\\)",
        r"(%2e%2e%2f|%2e%2e/|%2e%2e%5c)",
        
        # Command injection
        r"(;\s*(cat|ls|whoami|wget|curl|nc|bash))",
    ]
    
    SUSPICIOUS_USER_AGENTS = [
        'sqlmap', 'nikto', 'nmap', 'masscan', 'nessus',
        'metasploit', 'burp', 'acunetix', 'appscan',
        'havij', 'pangolin', 'hydra', 'brutus'
    ]
    
    @staticmethod
    def check_request_anomalies() -> Tuple[bool, Optional[str]]:
        """
        Analyze current request for suspicious patterns.
        Returns: (is_suspicious: bool, reason: str)
        """
        import re
        
        # Check User-Agent
        user_agent = request.headers.get('User-Agent', '').lower()
        for suspicious_ua in AnomalyDetector.SUSPICIOUS_USER_AGENTS:
            if suspicious_ua in user_agent:
                return True, f"Suspicious User-Agent detected: {suspicious_ua}"
        
        # Check all request data for injection patterns
        data_to_check = []
        
        # URL parameters
        data_to_check.extend(request.args.values())
        
        # POST body
        if request.is_json:
            data_to_check.extend(str(v) for v in request.get_json(silent=True).values() if v)
        elif request.form:
            data_to_check.extend(request.form.values())
        
        # Check patterns
        for data in data_to_check:
            data_str = str(data)
            for pattern in AnomalyDetector.SUSPICIOUS_PATTERNS:
                if re.search(pattern, data_str, re.IGNORECASE):
                    return True, f"Injection pattern detected: {pattern[:50]}"
        
        return False, None


def detect_anomalies(f):
    """
    Decorator to detect and block suspicious requests.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        is_suspicious, reason = AnomalyDetector.check_request_anomalies()
        
        if is_suspicious:
            ip = get_remote_address()
            logger.critical(f"[SECURITY CRITICAL] Suspicious request from {ip}: {reason}")
            logger.critical(f"[SECURITY] URL: {request.url}")
            logger.critical(f"[SECURITY] User-Agent: {request.headers.get('User-Agent')}")
            
            # Auto-ban IP for 24 hours
            brute_force.banned_ips[ip] = datetime.now(timezone.utc) + timedelta(hours=24)
            
            return jsonify({
                "message": "Request blocked due to suspicious activity.",
                "error": "FORBIDDEN"
            }), 403
        
        return f(*args, **kwargs)
    
    return decorated_function


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECURITY HEADERS MIDDLEWARE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def add_security_headers(response):
    """
    Add comprehensive security headers to all responses.
    Prevents XSS, clickjacking, MIME sniffing, and other attacks.
    """
    # Prevent XSS attacks
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    
    # Prevent clickjacking
    response.headers['X-Frame-Options'] = 'DENY'
    
    # Force HTTPS in production
    if os.environ.get('FLASK_ENV') == 'production':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    
    # Content Security Policy
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://js.paystack.co; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data: https: blob:; "
        "font-src 'self' https://fonts.gstatic.com; "
        "connect-src 'self' https://api.paystack.co https://devapi.siiqo.app https://api.siiqo.com; "
        "frame-src 'self' https://js.paystack.co; "
    )
    response.headers['Content-Security-Policy'] = csp
    
    # Prevent referrer leakage
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    
    # Additional security headers
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    response.headers['X-Permitted-Cross-Domain-Policies'] = 'none'
    
    return response


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CSRF PROTECTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def generate_csrf_token() -> str:
    """Generate a cryptographically secure CSRF token."""
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']


def validate_csrf_token(token: str) -> bool:
    """
    Validate CSRF token using constant-time comparison.
    Returns True if valid, False otherwise.
    """
    if 'csrf_token' not in session:
        return False
    
    expected_token = session['csrf_token']
    
    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(token, expected_token)


def csrf_protect(f):
    """
    Decorator to enforce CSRF protection on state-changing endpoints.
    Validates token from header or form data.
    
    Usage:
        @app.route('/api/admin/users/<id>/delete', methods=['POST'])
        @csrf_protect
        def delete_user(id):
            # Your code here
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Only enforce CSRF on state-changing methods
        if request.method in ['POST', 'PUT', 'PATCH', 'DELETE']:
            # Get token from header or form
            token = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
            
            if not token:
                logger.warning(f"[CSRF] Missing CSRF token from {get_remote_address()}")
                return jsonify({
                    "message": "CSRF token missing",
                    "error": "CSRF_TOKEN_MISSING"
                }), 403
            
            if not validate_csrf_token(token):
                logger.error(f"[CSRF] Invalid CSRF token from {get_remote_address()}")
                return jsonify({
                    "message": "CSRF token invalid",
                    "error": "CSRF_TOKEN_INVALID"
                }), 403
        
        return f(*args, **kwargs)
    
    return decorated_function


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REQUEST SIGNING FOR SENSITIVE OPERATIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_signing_secret() -> str:
    """Get request signing secret from environment."""
    secret = os.environ.get('REQUEST_SIGNING_SECRET', '')
    if not secret and os.environ.get('FLASK_ENV') == 'production':
        logger.critical("[SECURITY] REQUEST_SIGNING_SECRET not set in production!")
        raise RuntimeError("REQUEST_SIGNING_SECRET required in production")
    return secret or 'dev-signing-secret-change-in-production'


def generate_request_signature(data: dict, timestamp: int = None) -> Tuple[str, int]:
    """
    Generate HMAC signature for request data.
    
    Args:
        data: Request payload dictionary
        timestamp: Unix timestamp (auto-generated if not provided)
    
    Returns:
        (signature, timestamp) tuple
    """
    if timestamp is None:
        timestamp = int(datetime.now(timezone.utc).timestamp())
    
    secret = get_signing_secret()
    
    # Serialize data consistently (sorted keys)
    import json
    payload = json.dumps(data, sort_keys=True)
    message = f"{timestamp}:{payload}"
    
    signature = hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return signature, timestamp


def verify_request_signature(signature: str, timestamp: int, data: dict) -> Tuple[bool, Optional[str]]:
    """
    Verify request signature.
    
    Returns:
        (is_valid, error_message) tuple
    """
    # Check timestamp freshness (prevent replay attacks)
    now = int(datetime.now(timezone.utc).timestamp())
    max_age = 300  # 5 minutes
    
    if abs(now - timestamp) > max_age:
        return False, f"Signature expired (max age: {max_age}s)"
    
    # Generate expected signature
    expected_sig, _ = generate_request_signature(data, timestamp)
    
    # Constant-time comparison
    if not hmac.compare_digest(signature, expected_sig):
        return False, "Signature mismatch"
    
    return True, None


def require_signed_request(f):
    """
    Decorator to enforce request signing on ultra-sensitive endpoints.
    
    Client must send:
    - X-Signature header: HMAC-SHA256 signature
    - X-Timestamp header: Unix timestamp
    
    Usage:
        @app.route('/api/admin/users/<id>/delete', methods=['DELETE'])
        @require_signed_request
        def delete_user(id):
            # Your code here
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        signature = request.headers.get('X-Signature')
        timestamp_str = request.headers.get('X-Timestamp')
        
        if not signature or not timestamp_str:
            logger.warning(f"[REQUEST SIGNING] Missing signature headers from {get_remote_address()}")
            return jsonify({
                "message": "Request signature required",
                "error": "SIGNATURE_MISSING",
                "hint": "Send X-Signature and X-Timestamp headers"
            }), 403
        
        try:
            timestamp = int(timestamp_str)
        except ValueError:
            return jsonify({
                "message": "Invalid timestamp format",
                "error": "INVALID_TIMESTAMP"
            }), 400
        
        # Get request data
        if request.is_json:
            data = request.get_json(silent=True) or {}
        else:
            data = dict(request.form)
        
        # Add URL params to signature
        data['_path'] = request.path
        data['_method'] = request.method
        
        is_valid, error = verify_request_signature(signature, timestamp, data)
        
        if not is_valid:
            logger.error(f"[REQUEST SIGNING] Signature verification failed: {error} from {get_remote_address()}")
            return jsonify({
                "message": "Invalid request signature",
                "error": "SIGNATURE_INVALID",
                "details": error
            }), 403
        
        return f(*args, **kwargs)
    
    return decorated_function


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HONEYPOT FIELDS — BOT DETECTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Track IPs caught by honeypot
honeypot_caught: Dict[str, datetime] = {}


def check_honeypot() -> Tuple[bool, Optional[str]]:
    """
    Check if request filled honeypot fields.
    
    Honeypot fields are hidden form fields that humans won't see but bots will fill.
    Common honeypot field names: email_confirm, phone_confirm, website, url, comment
    
    Returns:
        (is_bot, field_name) tuple
    """
    honeypot_fields = [
        'email_confirm',   # Fake confirmation field
        'phone_confirm',   # Fake phone confirmation
        'website',         # Bots love to fill website fields
        'url',            # Another bot magnet
        'comment',        # Hidden comment field
        'user_email',     # Confusingly similar to real field
        'backup_email',   # Sounds legitimate but isn't used
    ]
    
    # Check form data
    if request.form:
        for field in honeypot_fields:
            if request.form.get(field):
                return True, field
    
    # Check JSON data
    if request.is_json:
        data = request.get_json(silent=True) or {}
        for field in honeypot_fields:
            if data.get(field):
                return True, field
    
    return False, None


def honeypot_protect(f):
    """
    Decorator to add honeypot bot detection to forms.
    
    Usage:
        @app.route('/api/auth/register', methods=['POST'])
        @honeypot_protect
        def register():
            # Your code here
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        is_bot, field = check_honeypot()
        
        if is_bot:
            ip = get_remote_address()
            honeypot_caught[ip] = datetime.now(timezone.utc)
            
            logger.critical(f"[HONEYPOT] Bot detected from {ip} (filled field: {field})")
            
            # Auto-ban for 24 hours
            brute_force.banned_ips[ip] = datetime.now(timezone.utc) + timedelta(hours=24)
            
            # Return fake success to not alert bot
            return jsonify({"status": "success", "message": "Registration successful"}), 200
        
        return f(*args, **kwargs)
    
    return decorated_function


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DATABASE EMAIL VALIDATION UTILITY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def scan_database_for_invalid_emails(db_session):
    """
    Scan database for invalid/malicious emails.
    
    Returns:
        dict with statistics and list of invalid emails
    """
    from app.utils.email import _validate_email
    from app.models.user import User
    
    invalid_emails = []
    total_users = User.query.count()
    
    logger.info(f"[EMAIL SCAN] Scanning {total_users} users for invalid emails...")
    
    for user in User.query.all():
        if not _validate_email(user.email):
            invalid_emails.append({
                'id': user.id,
                'email': user.email,
                'role': user.role,
                'created_at': user.created_at.isoformat() if user.created_at else None,
            })
    
    logger.warning(f"[EMAIL SCAN] Found {len(invalid_emails)} invalid emails out of {total_users} users")
    
    return {
        'total_users': total_users,
        'invalid_count': len(invalid_emails),
        'invalid_emails': invalid_emails,
    }
