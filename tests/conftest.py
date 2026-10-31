"""
Test harness for the Siiqo backend.

Safety rules enforced here, before the app is imported:
  * the real .env is never loaded (it holds live database and gateway keys);
  * the database is a throwaway SQLite file in a temp directory;
  * every gateway/email/storage key is blanked;
  * the background scheduler is disabled;
  * any outbound network connection raises, so a missed mock fails loudly
    instead of calling Paystack, Flutterwave, Daya, SES, S3 or Telegram.
"""
import os
import socket
import sys
import tempfile
import uuid

import pytest

# ── 1. Never load the real .env ─────────────────────────────────────────────
import dotenv
import dotenv.main

dotenv.load_dotenv = lambda *args, **kwargs: False
dotenv.main.load_dotenv = lambda *args, **kwargs: False

# ── 2. Isolated environment ─────────────────────────────────────────────────
_TMP_DIR = tempfile.mkdtemp(prefix="siiqo-tests-")
_DB_PATH = os.path.join(_TMP_DIR, "test.db").replace("\\", "/")

_BLANKED_KEYS = [
    "PAYSTACK_SECRET_KEY", "PAYSTACK_PUBLIC_KEY", "PAYSTACK_MONTHLY_PLAN_CODE",
    "PAYSTACK_ANNUAL_PLAN_CODE", "FLW_SECRET_KEY", "FLW_PUBLIC_KEY",
    "FLW_WEBHOOK_SECRET", "FLW_ENCRYPTION_KEY", "DAYA_API_KEY",
    "DAYA_WEBHOOK_SECRET", "MAIL_SERVER", "MAIL_USERNAME", "MAIL_PASSWORD",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_S3_BUCKET_NAME",
    "ALGOLIA_APP_ID", "ALGOLIA_ADMIN_KEY", "ALGOLIA_API_KEY",
    "BRIGHT_DATA_PROXY", "BRIGHT_DATA_API_KEY", "TELEGRAM_BOT_TOKEN",
    "TG_AUTH_SECRET", "BOT_ORCHESTRATOR_URL", "SIIQO_CORP_BANK_CODE",
    "SIIQO_CORP_ACCOUNT_NUMBER", "REDIS_URL",
]
for _key in _BLANKED_KEYS:
    os.environ.pop(_key, None)

os.environ.update({
    "DATABASE_URL": f"sqlite:///{_DB_PATH}",
    "FLASK_ENV": "development",
    "DISABLE_SCHEDULER": "1",
    "SECRET_KEY": "test-secret-key-not-for-production",
    "JWT_SECRET_KEY": "test-jwt-secret-not-for-production",
    "ADMIN_2FA_REQUIRED": "false",  # tests that need enforcement set it explicitly
    "JOBS_MODE": "sync",  # background jobs run inline (app/services/jobs.py)
})

# Make `import app` resolve to this repository's package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ── 3. Block outbound network ───────────────────────────────────────────────
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host in _LOCAL_HOSTS or isinstance(address, str):
        return _real_connect(self, address)
    raise RuntimeError(f"Network access is blocked in tests: {address!r}")


def _guarded_getaddrinfo(host, *args, **kwargs):
    if host in _LOCAL_HOSTS or host is None:
        return _real_getaddrinfo(host, *args, **kwargs)
    raise RuntimeError(f"DNS lookup is blocked in tests: {host!r}")


socket.socket.connect = _guarded_connect
socket.getaddrinfo = _guarded_getaddrinfo

# ── 4. Let Postgres-only column types create on SQLite ──────────────────────
from sqlalchemy.dialects.postgresql import ARRAY, JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


@compiles(ARRAY, "sqlite")
def _array_on_sqlite(element, compiler, **kw):
    return "JSON"


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(element, compiler, **kw):
    return "JSON"


# ── 5. App and database fixtures ────────────────────────────────────────────
from app import create_app  # noqa: E402
from app.extensions import db, limiter  # noqa: E402


@pytest.fixture(scope="session")
def app():
    application = create_app("development")
    application.config.update(TESTING=True)
    limiter.enabled = False
    with application.app_context():
        db.drop_all()
        db.create_all()
    yield application


@pytest.fixture(autouse=True)
def _fresh_database(app):
    """Every test starts with empty tables (schema is built once per session)."""
    with app.app_context():
        db.session.remove()
        for table in reversed(db.metadata.sorted_tables):
            db.session.execute(table.delete())
        db.session.commit()
        yield
        db.session.remove()


@pytest.fixture
def client(app):
    return app.test_client()


# ── 6. Data helpers ─────────────────────────────────────────────────────────
from app.models.order import Order  # noqa: E402
from app.models.product import Product  # noqa: E402
from app.models.user import Storefront, User, UserRole  # noqa: E402


@pytest.fixture
def make_user():
    def _make(role=UserRole.BUYER, email=None):
        user = User(
            email=email or f"user-{uuid.uuid4().hex[:8]}@example.com",
            role=role,
            first_name="Test",
            last_name="User",
        )
        user.set_password("not-a-real-password")
        db.session.add(user)
        db.session.commit()
        return user
    return _make


@pytest.fixture
def make_vendor(make_user):
    def _make():
        vendor = make_user(role=UserRole.VENDOR)
        store = Storefront(
            vendor_id=vendor.id,
            store_name=f"Store {vendor.id}",
            store_slug=f"store-{vendor.id}-{uuid.uuid4().hex[:4]}",
        )
        db.session.add(store)
        db.session.commit()
        return vendor, store
    return _make


@pytest.fixture
def make_product():
    def _make(store, price=1000, stock=10, product_type="physical", **extra):
        product = Product(
            storefront_id=store.id,
            name="Test product",
            description="A product used in tests",
            price=price,
            currency="NGN",
            stock_quantity=stock,
            product_type=product_type,
            is_active=True,
            **extra,
        )
        db.session.add(product)
        db.session.commit()
        return product
    return _make


@pytest.fixture
def make_order():
    def _make(vendor, buyer=None, total=10000, logistics_fee=0, payment_method="CRYPTO", **extra):
        order = Order(
            vendor_id=vendor.id,
            buyer_id=buyer.id if buyer else None,
            buyer_email=buyer.email if buyer else "guest@example.com",
            buyer_name="Test Buyer",
            total_amount=total,
            logistics_fee=logistics_fee,
            status="PENDING",
            payment_method=payment_method,
            **extra,
        )
        db.session.add(order)
        db.session.commit()
        return order
    return _make


@pytest.fixture
def auth_headers(app):
    from flask_jwt_extended import create_access_token

    def _headers(user):
        token = create_access_token(identity=str(user.id))
        return {"Authorization": f"Bearer {token}"}
    return _headers
