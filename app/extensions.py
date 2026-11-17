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
    The client's IP for rate limits, abuse checks and audit logs.

    Walks X-Forwarded-For from the right, skipping trusted proxies (private
    ranges, AWS CloudFront, TRUSTED_PROXY_CIDRS) — never the client-chosen
    left-most entry. Full rule and hop chain: app/utils/client_ip.py.
    """
    try:
        if not request:
            return '127.0.0.1'
        from app.utils.client_ip import client_ip_from_environ
        return client_ip_from_environ(request.environ)
    except Exception:
        return '127.0.0.1'


# Rate limiter — uses Redis in production (set REDIS_URL env var),
# falls back to in-memory for local dev.
limiter = Limiter(
    key_func=get_real_client_ip,
    default_limits=["10000 per day", "2000 per hour"],
    storage_uri=os.environ.get('REDIS_URL', 'memory://'),
)

