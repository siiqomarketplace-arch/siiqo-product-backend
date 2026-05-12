import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()


def _require_env(key: str, fallback: str | None = None) -> str:
    """Return env var or fallback. In production, missing keys raise an error."""
    val = os.environ.get(key)
    if val:
        return val
    if fallback is not None and os.environ.get('FLASK_ENV') != 'production':
        return fallback
    if fallback is None:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            "Check your .env file."
        )
    return fallback


class Config:
    # Security
    SECRET_KEY = _require_env('SECRET_KEY', 'dev-secret-key-change-in-production')
    JWT_SECRET_KEY = _require_env('JWT_SECRET_KEY', 'dev-jwt-secret-change-in-production')
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=1)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)

    # Database
    _db_url = os.environ.get('DATABASE_URL') or 'sqlite:///siiqo.db'
    # Fix Heroku/Render postgres:// → postgresql://
    if _db_url.startswith("postgres://"):
        _db_url = _db_url.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = _db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,       # Detect stale connections
        "pool_recycle": 300,         # Recycle connections every 5 min
        "pool_size": 10,
        "max_overflow": 20,
    }

    # CORS
    CORS_ORIGINS = os.environ.get('CORS_ORIGINS', 'http://localhost:3000').split(',')

    # Rate Limiting — use Redis in production
    RATELIMIT_STORAGE_URI = os.environ.get('REDIS_URL', 'memory://')

    # Third-Party
    PAYSCROW_API_KEY = os.environ.get('PAYSCROW_API_KEY')
    PAYSCROW_WEBHOOK_SECRET = os.environ.get('PAYSCROW_WEBHOOK_SECRET')
    MAPBOX_TOKEN = os.environ.get('MAPBOX_TOKEN')

    # AWS S3
    AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY')
    AWS_S3_BUCKET_NAME = os.environ.get('AWS_S3_BUCKET_NAME')
    AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')

    # Email
    MAIL_SERVER = os.environ.get('MAIL_SERVER')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 465))
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER', 'support@siiqo.com')


class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_ECHO = False  # Set True to log SQL queries


class ProductionConfig(Config):
    DEBUG = False
    # Enforce secure secrets in production
    SECRET_KEY = _require_env('SECRET_KEY')
    JWT_SECRET_KEY = _require_env('JWT_SECRET_KEY')


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig,
}
