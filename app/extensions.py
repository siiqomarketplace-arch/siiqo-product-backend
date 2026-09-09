import os
from flask import request
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_jwt_extended import JWTManager
from flask_cors import CORS
from flask_limiter import Limiter

db = SQLAlchemy()
migrate = Migrate()
jwt = JWTManager()
cors = CORS()


def get_real_client_ip() -> str:
    """
    Safely extract the client's real public IP address behind reverse proxies.
    Prioritizes:
      1. CF-Connecting-IP (Cloudflare Edge)
      2. True-Client-IP (Cloudflare Enterprise / CDN)
      3. First IP in X-Forwarded-For (originating client before ALB & Nginx hops)
      4. request.remote_addr (direct socket connection fallback)
    """
    try:
        if not request:
            return '127.0.0.1'

        # 1. Cloudflare sends the verified visitor IP
        cf_ip = request.headers.get('CF-Connecting-IP')
        if cf_ip and cf_ip.strip():
            return cf_ip.strip().split(',')[0].strip()

        # 2. True-Client-IP header
        true_client = request.headers.get('True-Client-IP')
        if true_client and true_client.strip():
            return true_client.strip().split(',')[0].strip()

        # 3. X-Forwarded-For: client, proxy1, proxy2...
        xff = request.headers.get('X-Forwarded-For')
        if xff and xff.strip():
            parts = [p.strip() for p in xff.split(',') if p.strip()]
            if parts:
                return parts[0]

        # 4. Fallback to socket remote_addr
        return request.remote_addr or '127.0.0.1'
    except Exception:
        return '127.0.0.1'


# Rate limiter — uses Redis in production (set REDIS_URL env var),
# falls back to in-memory for local dev.
limiter = Limiter(
    key_func=get_real_client_ip,
    default_limits=["10000 per day", "2000 per hour"],
    storage_uri=os.environ.get('REDIS_URL', 'memory://'),
)

