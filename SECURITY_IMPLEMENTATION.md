# 🔒 Siiqo Platform — Security Implementation Guide

**Date Implemented:** August 8, 2026  
**Security Level:** Google/Apple Enterprise Grade  
**Status:** ✅ Production Ready

---

## 🎯 Security Features Implemented

### 1. ✅ Email Validation & SMTP Injection Prevention
**Location:** `app/utils/email.py`

**Features:**
- RFC 5322 compliant email validation
- Header injection prevention (blocks `\n`, `\r`, `<`, `>`)
- Disposable email domain blocking
- Length validation (max 254 characters)
- XSS pattern detection in email addresses

**Test:**
```python
from app.utils.email import send_siiqo_email

# These will be blocked:
send_siiqo_email("test@", "Subject", "template")  # Invalid format
send_siiqo_email("user\n@evil.com", "Subject", "template")  # Header injection
send_siiqo_email("spam@mailinator.com", "Subject", "template")  # Disposable domain
```

---

### 2. ✅ Admin Login Brute Force Protection
**Location:** `app/middleware/security.py`, `app/routes/admin.py`

**Features:**
- Rate limiting: 10 attempts per minute per IP
- Failed attempt tracking with progressive delays
- 15-minute lockout after 5 failures
- 24-hour ban after 10 failures
- IP-based blocking with fingerprinting
- Constant-time password comparison (prevents timing attacks)

**Configuration:**
```python
MAX_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=15)
PROGRESSIVE_DELAYS = [0, 1, 2, 5, 10]  # seconds
BAN_DURATION = timedelta(hours=24)
```

**Test:**
```bash
# Try logging in with wrong password 6 times
curl -X POST http://localhost:5000/api/admin/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@siiqo.com","password":"wrong"}' \
  -v

# After 5 failures, you'll get:
# HTTP 429 Too Many Requests
# {"message": "Too many failed attempts. Try again in 15 minutes."}
```

---

### 3. ✅ Admin IP Whitelist
**Location:** `app/middleware/security.py`

**Features:**
- Restricts `/api/admin/*` routes to whitelisted IPs only
- Supports individual IPs and CIDR ranges
- Development mode bypass (when `FLASK_ENV=development`)
- Automatic 403 Forbidden for non-whitelisted IPs

**Configuration (.env):**
```bash
# Single IP
ADMIN_IP_WHITELIST=102.91.132.249

# Multiple IPs
ADMIN_IP_WHITELIST=102.91.132.249,105.112.45.67

# CIDR range (office network)
ADMIN_IP_WHITELIST=102.91.132.0/24,105.112.0.0/16

# Development (allow all)
ADMIN_IP_WHITELIST=*
```

**Test:**
```bash
# From non-whitelisted IP:
curl -X POST http://localhost:5000/api/admin/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@siiqo.com","password":"password"}'

# Response:
# HTTP 403 Forbidden
# {"message": "Access denied. Admin panel requires whitelisted IP."}
```

---

### 4. ✅ Comprehensive Audit Logging
**Location:** `app/models/audit.py`, integrated in `app/routes/admin.py`

**Features:**
- Logs ALL admin actions (user updates, deletions, escrow operations)
- Captures IP address, User-Agent, timestamp
- Immutable audit trail for compliance
- Indexed for fast querying

**Logged Actions:**
- `ADMIN_LOGIN` - Admin authentication
- `USER_STATUS_UPDATE` - User verification, suspension, approval
- `USER_DELETE` - User account deletion
- `ESCROW_REFUND` - Escrow refund to buyer
- `ESCROW_RELEASE` - Escrow release to vendor
- `CATEGORY_CREATE` / `CATEGORY_DELETE` - Category management

**Query Audit Logs:**
```python
from app.models.audit import AdminAuditLog

# Get all actions by a specific admin
logs = AdminAuditLog.query.filter_by(admin_email='admin@siiqo.com').all()

# Get all user deletions
logs = AdminAuditLog.query.filter_by(action='USER_DELETE').all()

# Get logs for specific resource
logs = AdminAuditLog.query.filter_by(
    resource_type='User', 
    resource_id='123'
).all()

# Get logs from specific IP
logs = AdminAuditLog.query.filter_by(ip_address='102.91.132.249').all()
```

---

### 5. ✅ Anomaly Detection & Attack Prevention
**Location:** `app/middleware/security.py`

**Features:**
- SQL injection pattern detection
- XSS payload detection
- Path traversal detection
- Command injection detection
- Suspicious User-Agent blocking (sqlmap, nikto, nmap, etc.)
- Automatic 24-hour IP ban on detection

**Blocked Patterns:**
```python
# SQL Injection
"SELECT * FROM users WHERE id = 1"
"1' OR '1'='1"
"'; DROP TABLE users;--"

# XSS
"<script>alert('xss')</script>"
"javascript:alert(1)"
"<img src=x onerror=alert(1)>"

# Path Traversal
"../../../etc/passwd"
"..\\..\\windows\\system32"

# Suspicious User-Agents
"sqlmap/1.0"
"nikto"
"nmap"
```

**Test:**
```bash
# Try SQL injection in query parameter
curl "http://localhost:5000/api/marketplace/products?search=1' OR '1'='1"

# Response:
# HTTP 403 Forbidden
# {"message": "Request blocked due to suspicious activity."}
# IP automatically banned for 24 hours
```

---

### 6. ✅ Security Headers (All Responses)
**Location:** `app/middleware/security.py`, integrated in `app/__init__.py`

**Headers Added:**
```
X-Content-Type-Options: nosniff
X-XSS-Protection: 1; mode=block
X-Frame-Options: DENY
Strict-Transport-Security: max-age=31536000; includeSubDomains (production only)
Content-Security-Policy: [strict CSP rules]
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: geolocation=(), microphone=(), camera=()
X-Permitted-Cross-Domain-Policies: none
```

**Test:**
```bash
curl -I http://localhost:5000/api/health

# Check response headers include:
# X-Frame-Options: DENY
# X-Content-Type-Options: nosniff
# Content-Security-Policy: ...
```

---

### 7. ✅ Rate Limiting (Endpoint-Specific)
**Location:** Applied via decorators in `app/routes/admin.py`

**Rate Limits:**
```python
# Admin login: 10 attempts/minute
@limiter.limit("10 per minute")

# User status updates: 30/minute
@limiter.limit("30 per minute")

# User deletion: 10/hour (strict!)
@limiter.limit("10 per hour")

# Escrow operations: 20/hour
@limiter.limit("20 per hour")
```

---

## 🚀 Deployment Checklist

### 1. Environment Variables
Copy `.env.security.example` to `.env` and configure:

```bash
cd siiqo-product-backend
cp .env.security.example .env
nano .env  # Edit with your values
```

**Critical Settings:**
- `ADMIN_IP_WHITELIST` - Set to your office/admin IPs
- `REDIS_URL` - Required for distributed rate limiting in production
- `JWT_SECRET_KEY` - Generate new secret: `python -c "import secrets; print(secrets.token_hex(32))"`
- `FLASK_ENV=production` - Enables strict security mode

### 2. Database Migration
Create audit log table:

```bash
# Option A: Manual SQL
psql -U your_db_user -d siiqo_db -f create_audit_table.sql

# Option B: Flask-Migrate (recommended)
flask db migrate -m "Add admin audit logs table"
flask db upgrade
```

### 3. Redis Setup (Production)
For distributed rate limiting across multiple servers:

```bash
# AWS ElastiCache Redis
REDIS_URL=redis://your-cluster.cache.amazonaws.com:6379/0

# Redis Cloud
REDIS_URL=redis://default:password@redis-12345.cloud.redislabs.com:12345

# Self-hosted
REDIS_URL=redis://localhost:6379/0
```

### 4. Test Security Features

```bash
# Test email validation
python -c "from app.utils.email import _validate_email; print(_validate_email('test@example.com'))"

# Test brute force protection
for i in {1..6}; do
  curl -X POST http://localhost:5000/api/admin/login \
    -H "Content-Type: application/json" \
    -d '{"email":"admin@siiqo.com","password":"wrong"}'
done

# Test IP whitelist
curl -X GET http://localhost:5000/api/admin/stats
# Should return 403 if IP not whitelisted

# Test rate limiting
for i in {1..15}; do
  curl -X POST http://localhost:5000/api/admin/login \
    -H "Content-Type: application/json" \
    -d '{"email":"admin@siiqo.com","password":"test"}'
done
# Should hit rate limit after 10 attempts
```

---

## 📊 Security Monitoring

### View Audit Logs
```python
from app import create_app
from app.models.audit import AdminAuditLog

app = create_app()
with app.app_context():
    # Recent admin actions
    logs = AdminAuditLog.query.order_by(AdminAuditLog.timestamp.desc()).limit(100).all()
    for log in logs:
        print(f"{log.timestamp} | {log.admin_email} | {log.action} | {log.resource_type}#{log.resource_id}")
```

### Check Brute Force Attempts
```python
from app.middleware.security import brute_force

# View blocked IPs
print("Blocked IPs:", brute_force.banned_ips)

# View failed attempts
print("Failed attempts:", brute_force.failed_attempts)

# Manually unblock IP
ip_to_unblock = "192.168.1.100"
if ip_to_unblock in brute_force.banned_ips:
    del brute_force.banned_ips[ip_to_unblock]
if ip_to_unblock in brute_force.failed_attempts:
    del brute_force.failed_attempts[ip_to_unblock]
```

---

## 🔥 Incident Response

### If Admin Account Compromised:

1. **Immediately rotate JWT secret:**
   ```bash
   # Generate new secret
   python -c "import secrets; print(secrets.token_hex(32))"
   
   # Update .env
   JWT_SECRET_KEY=new-secret-here
   
   # Restart app (invalidates all tokens)
   sudo systemctl restart siiqo-backend
   ```

2. **Check audit logs for unauthorized actions:**
   ```sql
   SELECT * FROM admin_audit_logs 
   WHERE admin_email = 'compromised@siiqo.com'
   AND timestamp > '2026-08-07 00:00:00'
   ORDER BY timestamp DESC;
   ```

3. **Ban attacker IP:**
   ```python
   from app.middleware.security import brute_force
   from datetime import datetime, timedelta, timezone
   
   attacker_ip = "malicious.ip.address"
   brute_force.banned_ips[attacker_ip] = datetime.now(timezone.utc) + timedelta(days=365)
   ```

4. **Reset compromised admin password:**
   ```python
   from app.models.admin import AdminUser
   admin = AdminUser.query.filter_by(email='compromised@siiqo.com').first()
   admin.set_password('new-secure-password')
   db.session.commit()
   ```

---

## 🎓 Security Best Practices

### For Admins:
1. **Use strong passwords:** 16+ characters, mix of letters/numbers/symbols
2. **Enable 2FA** (coming soon — integrate Google Authenticator)
3. **Access admin panel from trusted networks only**
4. **Log out after every session**
5. **Never share credentials**

### For Developers:
1. **Never commit `.env` to git**
2. **Rotate secrets every 90 days**
3. **Review audit logs weekly**
4. **Keep dependencies updated:** `pip list --outdated`
5. **Run security scans:** `bandit -r app/`
6. **Never disable security features in production**

---

## 📞 Support & Reporting

**Security Issues:** security@siiqo.com  
**Bug Reports:** bugs@siiqo.com  
**Urgent:** Contact CTO directly

---

**Last Updated:** August 8, 2026  
**Next Security Audit:** November 8, 2026
