# Siiqo Backend v2.0.1 - Deployment Trigger
import os
import logging
from flask import Flask, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from app.config import config
from app.extensions import db, migrate, jwt, cors, limiter

logger = logging.getLogger(__name__)
_SCHEDULER_LOCK_FILE = None


def create_app(config_name: str | None = None) -> Flask:
    if config_name is None:
        config_name = os.environ.get('FLASK_ENV', 'default')

    # Error monitoring (P4): only when SENTRY_DSN is set; no personal data sent.
    if os.environ.get('SENTRY_DSN'):
        try:
            import sentry_sdk
            sentry_sdk.init(dsn=os.environ['SENTRY_DSN'], send_default_pii=False,
                            traces_sample_rate=float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '0')),
                            environment=os.environ.get('SENTRY_ENVIRONMENT', 'production'))
        except Exception:
            pass

    app = Flask(__name__)
    # Trust reverse proxy headers (Cloudflare & Elastic Beanstalk ALB/Nginx)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=2, x_proto=2, x_host=1)
    app.url_map.strict_slashes = False
    app.config.from_object(config[config_name])

    # -----------------------------------------------------------------------
    # Extensions
    # -----------------------------------------------------------------------
    db.init_app(app)

    # Import all models so Alembic can detect them
    from app import models  # noqa: F401

    migrate.init_app(app, db)

    # -----------------------------------------------------------------------
    # One-time startup backfill: generate escrow_code for any NULL rows.
    # This is safe to run on every boot — it's a no-op if nothing needs fixing.
    # -----------------------------------------------------------------------
    with app.app_context():
        try:
            import random as _random
            from sqlalchemy import text as _text
            result = db.session.execute(_text(
                "UPDATE escrow_transactions SET escrow_code = "
                "LPAD(FLOOR(RANDOM() * 900000 + 100000)::TEXT, 6, '0') "
                "WHERE escrow_code IS NULL OR escrow_code = '' "
                "RETURNING id, order_id"
            ))
            fixed = result.fetchall()
            db.session.commit()
            if fixed:
                import logging as _log
                _log.getLogger(__name__).info(
                    f"[STARTUP] Backfilled escrow_code for {len(fixed)} order(s): "
                    f"{[r[1] for r in fixed]}"
                )
        except Exception as _e:
            db.session.rollback()
            import logging as _log
            _log.getLogger(__name__).warning(f"[STARTUP] escrow_code backfill skipped: {_e}")

        # NOTE (0A-2): every schema statement below (this list, the grants
        # table bootstrap and the blog/article_slug_redirects block) is now
        # also applied, idempotently, by Alembic revision a1f19c7ec765
        # (migrations/versions/a1f19c7ec765_consolidate_boot_time_schema.py).
        # Keep this list until staging proves that revision; then remove it.
        # See docs/MIGRATIONS_RUNBOOK.md.
        # ── Database Schema Auto-Creation & alters for telemetry/partners ─────
        try:
            # db.create_all() only creates tables that don't exist yet — it never
            # drops or alters existing columns, so it is safe to run alongside
            # Alembic migrations. It handles new partner tables added outside
            # the migration cycle.
            db.create_all()

            # Run column additions for telemetry coordinates (if not already present)
            from sqlalchemy import text as _text
            for col_def in [
                "ALTER TABLE logistics_assignments ADD COLUMN current_latitude NUMERIC(9, 6)",
                "ALTER TABLE logistics_assignments ADD COLUMN current_longitude NUMERIC(9, 6)",
                "ALTER TABLE logistics_assignments ADD COLUMN location_updated_at TIMESTAMP",
                # ── Storefront engagement & onboarding tracking ──────────────────
                "ALTER TABLE storefronts ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0",
                "ALTER TABLE storefronts ADD COLUMN IF NOT EXISTS is_pro_verified BOOLEAN DEFAULT FALSE",
                "ALTER TABLE storefronts ADD COLUMN IF NOT EXISTS pro_verified_expires_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE storefronts ADD COLUMN IF NOT EXISTS onboarding_emails_sent JSONB DEFAULT '{}'",
                # ── Storefront ad tracking pixels (Meta, TikTok, GA4) ────────────
                "ALTER TABLE storefronts ADD COLUMN IF NOT EXISTS meta_pixel_id VARCHAR(50)",
                "ALTER TABLE storefronts ADD COLUMN IF NOT EXISTS tiktok_pixel_id VARCHAR(50)",
                "ALTER TABLE storefronts ADD COLUMN IF NOT EXISTS ga4_measurement_id VARCHAR(50)",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS confirmation_email_sent BOOLEAN DEFAULT FALSE",
                "ALTER TABLE sponsored_listings ADD COLUMN IF NOT EXISTS payment_reference VARCHAR(100)",
                "ALTER TABLE vendor_subscriptions ADD COLUMN IF NOT EXISTS paystack_subscription_code VARCHAR(100)",
                "ALTER TABLE vendor_subscriptions ADD COLUMN IF NOT EXISTS paystack_email_token VARCHAR(100)",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS currency VARCHAR(10) DEFAULT 'NGN'",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS fx_rate_to_ngn NUMERIC(18, 6)",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS released_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS source VARCHAR(40)",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS campaign VARCHAR(100)",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS ref_code VARCHAR(24)",
                "CREATE INDEX IF NOT EXISTS ix_orders_ref_code ON orders (ref_code)",
                "ALTER TABLE storefronts ADD COLUMN IF NOT EXISTS delivery_settings JSON",
                # Admin close/reopen a store (NULL = not closed).
                "ALTER TABLE storefronts ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS buyer_reminded_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE passport_api_clients ADD COLUMN IF NOT EXISTS owner_user_id INTEGER",
                "CREATE INDEX IF NOT EXISTS ix_passport_api_clients_owner_user_id ON passport_api_clients (owner_user_id)",
                "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_secret_enc VARCHAR(255)",
                "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_pending_secret_enc VARCHAR(255)",
                "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN DEFAULT FALSE",
                "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_enabled_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_last_used_step BIGINT",
                "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_recovery_hashes JSON",
                "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_challenge_nonce VARCHAR(64)",
                "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_challenge_failures INTEGER DEFAULT 0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_locale VARCHAR(8)",
                # ── Product view tracking ────────────────────────────────────────
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0",
                # ── Payment link product type, digital file & currency ──────────
                "ALTER TABLE payment_links ADD COLUMN IF NOT EXISTS product_type VARCHAR(20) DEFAULT 'service'",
                "ALTER TABLE payment_links ADD COLUMN IF NOT EXISTS file_url VARCHAR(500)",
                "ALTER TABLE payment_links ADD COLUMN IF NOT EXISTS currency VARCHAR(10) DEFAULT 'NGN'",
                # ── Multi-currency support for products & event tickets ──────────
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS currency VARCHAR(10) DEFAULT 'NGN'",
                "ALTER TABLE ticket_types ADD COLUMN IF NOT EXISTS currency VARCHAR(10) DEFAULT 'NGN'",
                # ── Invoice standalone billing columns ───────────────────────────
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS customer_name VARCHAR(255)",
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS customer_email VARCHAR(255)",
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS customer_phone VARCHAR(50)",
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS customer_address TEXT",
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS line_items JSONB",
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS subtotal NUMERIC(10, 2)",
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS discount NUMERIC(10, 2)",
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS tax_rate NUMERIC(5, 2)",
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS tax_amount NUMERIC(10, 2)",
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS total NUMERIC(10, 2)",
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS currency VARCHAR(10) DEFAULT 'NGN'",
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS notes TEXT",
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payment_link_token VARCHAR(100)",
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50)",
                # ── Receipt standalone billing columns ──────────────────────────
                "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS customer_name VARCHAR(255)",
                "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS customer_email VARCHAR(255)",
                "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS customer_phone VARCHAR(50)",
                "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS line_items JSONB",
                "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS subtotal NUMERIC(10, 2)",
                "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS tax_amount NUMERIC(10, 2)",
                "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS discount NUMERIC(10, 2)",
                "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS total NUMERIC(10, 2)",
                "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS currency VARCHAR(10) DEFAULT 'NGN'",
                "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50) DEFAULT 'Cash'",
                "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS notes TEXT",
                "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'paid'",
                # ── Events & Ticketing ───────────────────────────────────────────
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_free BOOLEAN DEFAULT FALSE",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS show_on_storefront BOOLEAN DEFAULT TRUE",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS show_on_marketplace BOOLEAN DEFAULT TRUE",
                # ── Events Enhancements (Organizer, Schedules, FAQs, Custom Questions) ───
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS organizer_name VARCHAR(255)",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS organizer_bio TEXT",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS organizer_avatar VARCHAR(500)",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS organizer_socials JSONB DEFAULT '{}'",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS agenda JSONB DEFAULT '[]'",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS faqs JSONB DEFAULT '[]'",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS schedules JSONB DEFAULT '[]'",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS custom_fields JSONB DEFAULT '[]'",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS cta_button_text VARCHAR(100) DEFAULT 'Get Tickets'",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS registration_type VARCHAR(20) DEFAULT 'native'",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS external_registration_url VARCHAR(500)",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS external_registration_instructions TEXT",
                # ── Events Manual Bank Transfer Payment Fields ───────────────
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS manual_payment_bank_name VARCHAR(100)",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS manual_payment_account_name VARCHAR(255)",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS manual_payment_account_number VARCHAR(50)",
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS manual_payment_instructions TEXT",
                # ── Ticket Purchases Guest & Custom Fields ──────────────────
                "ALTER TABLE ticket_purchases ALTER COLUMN buyer_id DROP NOT NULL",
                "ALTER TABLE ticket_purchases ADD COLUMN IF NOT EXISTS selected_schedule_id VARCHAR(100)",
                "ALTER TABLE ticket_purchases ADD COLUMN IF NOT EXISTS selected_schedule_title VARCHAR(255)",
                "ALTER TABLE ticket_purchases ADD COLUMN IF NOT EXISTS custom_responses JSONB DEFAULT '{}'",
                # ── Ticket Purchases Manual Payment Tracking ─────────────────
                "ALTER TABLE ticket_purchases ADD COLUMN IF NOT EXISTS payment_proof_url VARCHAR(500)",
                "ALTER TABLE ticket_purchases ADD COLUMN IF NOT EXISTS manual_payment_status VARCHAR(30)",
                "ALTER TABLE ticket_purchases ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP",
                "ALTER TABLE ticket_purchases ADD COLUMN IF NOT EXISTS rejection_reason TEXT",
                "ALTER TABLE ticket_purchases ADD COLUMN IF NOT EXISTS confirmed_by INTEGER REFERENCES users(id)",
                "ALTER TABLE ticket_purchases ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMP",
                # ── Orders Guest Checkout ────────────────────────────────────
                "ALTER TABLE orders ALTER COLUMN buyer_id DROP NOT NULL",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS buyer_email VARCHAR(255)",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS buyer_name VARCHAR(255)",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS buyer_phone VARCHAR(50)",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS is_guest BOOLEAN DEFAULT FALSE",
                # ── Unpaid-order reminder, seller ship reminders ─────────────
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_reminded_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS ship_reminders_sent INTEGER DEFAULT 0",
                # ── Reviews from guest buyers (private review link) ──────────
                "ALTER TABLE reviews ALTER COLUMN buyer_id DROP NOT NULL",
                "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS reviewer_name VARCHAR(80)",
                # ── Daya Payments Guest Support ──────────────────────────────
                "ALTER TABLE daya_payments ALTER COLUMN buyer_id DROP NOT NULL",
                # ── Articles Schema Enhancements ─────────────────────────────
                "ALTER TABLE articles ADD COLUMN IF NOT EXISTS sub_category VARCHAR(100)",
                "ALTER TABLE articles ADD COLUMN IF NOT EXISTS author_name VARCHAR(100)",
                "ALTER TABLE articles ADD COLUMN IF NOT EXISTS subcategory VARCHAR(100)",
                """CREATE TABLE IF NOT EXISTS events (
                    id SERIAL PRIMARY KEY,
                    storefront_id INTEGER REFERENCES storefronts(id),
                    vendor_id INTEGER REFERENCES users(id),
                    title VARCHAR(255) NOT NULL,
                    slug VARCHAR(300) UNIQUE NOT NULL,
                    description TEXT NOT NULL,
                    cover_image VARCHAR(500),
                    images JSONB DEFAULT '[]',
                    start_date TIMESTAMP,
                    end_date TIMESTAMP,
                    timezone VARCHAR(50) DEFAULT 'Africa/Lagos',
                    event_type VARCHAR(50),
                    event_format VARCHAR(20) DEFAULT 'in-person',
                    venue_name VARCHAR(255),
                    venue_address VARCHAR(500),
                    city VARCHAR(100),
                    state VARCHAR(100),
                    country VARCHAR(100) DEFAULT 'Nigeria',
                    latitude FLOAT,
                    longitude FLOAT,
                    meeting_url VARCHAR(500),
                    meeting_password VARCHAR(100),
                    total_capacity INTEGER,
                    tickets_sold INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    is_published BOOLEAN DEFAULT FALSE,
                    is_deleted BOOLEAN DEFAULT FALSE,
                    show_on_storefront BOOLEAN DEFAULT TRUE,
                    show_on_marketplace BOOLEAN DEFAULT TRUE,
                    view_count INTEGER DEFAULT 0,
                    meta_title VARCHAR(255),
                    meta_description TEXT,
                    terms_and_conditions TEXT,
                    contact_email VARCHAR(255),
                    contact_phone VARCHAR(20),
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )""",
                """CREATE TABLE IF NOT EXISTS ticket_types (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
                    name VARCHAR(100) NOT NULL,
                    description TEXT,
                    price NUMERIC(10,2) DEFAULT 0,
                    quantity_available INTEGER,
                    quantity_sold INTEGER DEFAULT 0,
                    is_free BOOLEAN DEFAULT FALSE,
                    benefits JSONB DEFAULT '[]',
                    sale_start TIMESTAMP,
                    sale_end TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )""",
                """CREATE TABLE IF NOT EXISTS ticket_purchases (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
                    ticket_type_id INTEGER REFERENCES ticket_types(id),
                    buyer_id INTEGER REFERENCES users(id),
                    order_id INTEGER REFERENCES orders(id),
                    ticket_code VARCHAR(20) UNIQUE NOT NULL,
                    qr_code_url VARCHAR(500),
                    buyer_name VARCHAR(255),
                    buyer_email VARCHAR(255),
                    buyer_phone VARCHAR(20),
                    price_paid NUMERIC(10,2) DEFAULT 0,
                    quantity INTEGER DEFAULT 1,
                    status VARCHAR(20) DEFAULT 'PENDING',
                    is_checked_in BOOLEAN DEFAULT FALSE,
                    checked_in_at TIMESTAMP,
                    checked_in_by INTEGER,
                    is_valid BOOLEAN DEFAULT TRUE,
                    pdf_ticket_url VARCHAR(500),
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )""",
                """CREATE TABLE IF NOT EXISTS event_reviews (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
                    reviewer_id INTEGER REFERENCES users(id),
                    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                    comment TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE (event_id, reviewer_id)
                )""",
            ]:
                try:
                    db.session.execute(_text(col_def))
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            import logging as _log
            _log.getLogger(__name__).info("[STARTUP] Telemetry columns & partner database schema verified/created successfully.")
        except Exception as _e:
            db.session.rollback()
            import logging as _log
            _log.getLogger(__name__).warning(f"[STARTUP] Database schema update failed: {_e}")

        # ── Grants Table Auto-Initialization ─────────────────────────────────
        # One-time grants table creation with sample data. Safe to run on every
        # boot — checks if table exists first and skips if already populated.
        try:
            from sqlalchemy import text as _text
            import logging as _log
            
            # Check if grants table exists
            _result = db.session.execute(_text("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'grants'
                )
            """))
            _grants_table_exists = _result.scalar()
            
            if not _grants_table_exists:
                _log.getLogger(__name__).info("[STARTUP] Grants table not found, creating...")
                
                # Read and execute SQL migration file
                import os as _os
                _sql_file = _os.path.join(_os.path.dirname(__file__), '..', 'migrations', 'create_grants_table.sql')
                
                if _os.path.exists(_sql_file):
                    with open(_sql_file, 'r', encoding='utf-8') as _f:
                        _sql_content = _f.read()
                    
                    db.session.execute(_text(_sql_content))
                    db.session.commit()
                    
                    # Verify grants were created
                    _verify = db.session.execute(_text("SELECT COUNT(*) FROM grants"))
                    _count = _verify.scalar()
                    _log.getLogger(__name__).info(f"[STARTUP] ✓ Grants table created with {_count} sample grants")
                else:
                    _log.getLogger(__name__).warning(f"[STARTUP] Grants migration SQL file not found: {_sql_file}")
            else:
                # Table exists, check if it has data
                _verify = db.session.execute(_text("SELECT COUNT(*) FROM grants"))
                _count = _verify.scalar()
                if _count == 0:
                    _log.getLogger(__name__).info("[STARTUP] Grants table exists but is empty (no sample data loaded)")
                else:
                    _log.getLogger(__name__).info(f"[STARTUP] Grants table verified ({_count} grants)")
                    
        except Exception as _e:
            db.session.rollback()
            import logging as _log
            _log.getLogger(__name__).warning(f"[STARTUP] Grants table initialization skipped: {_e}")

        # ── Blog Articles Auto-Initialization ─────────────────────────────────
        try:
            from sqlalchemy import text as _text
            import logging as _log

            # 1. Create blog_authors table if not exists
            try:
                db.session.execute(_text("""
                    CREATE TABLE IF NOT EXISTS blog_authors (
                        id              SERIAL PRIMARY KEY,
                        name            VARCHAR(100) NOT NULL,
                        slug            VARCHAR(120) NOT NULL UNIQUE,
                        title           VARCHAR(150),
                        bio             TEXT,
                        avatar          VARCHAR(255),
                        twitter_handle  VARCHAR(100),
                        linkedin_url    VARCHAR(255),
                        is_active       BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """))
                db.session.commit()
            except Exception as _auth_err:
                db.session.rollback()

            # 2. Add missing columns to articles table
            _article_columns = [
                "ALTER TABLE articles ADD COLUMN IF NOT EXISTS author_id INTEGER REFERENCES blog_authors(id) ON DELETE SET NULL",
                "ALTER TABLE articles ADD COLUMN IF NOT EXISTS author_name VARCHAR(100)",
                "ALTER TABLE articles ADD COLUMN IF NOT EXISTS sub_category VARCHAR(100)",
                "ALTER TABLE articles ADD COLUMN IF NOT EXISTS meta_title VARCHAR(255)",
                "ALTER TABLE articles ADD COLUMN IF NOT EXISTS meta_description VARCHAR(500)",
                "ALTER TABLE articles ADD COLUMN IF NOT EXISTS cover_image VARCHAR(255)",
                "ALTER TABLE articles ADD COLUMN IF NOT EXISTS excerpt VARCHAR(500)",
            ]
            for _col_sql in _article_columns:
                try:
                    db.session.execute(_text(_col_sql))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

            # 3. Seed default author if not exists
            try:
                db.session.execute(_text("""
                    INSERT INTO blog_authors (name, slug, title, bio, avatar, is_active)
                    VALUES (
                        'Siiqo Editorial Team',
                        'siiqo-editorial-team',
                        'Official Siiqo Content Team',
                        'In-house content team at Siiqo covering e-commerce, vendor growth, logistics and SME insights across West Africa.',
                        'https://siiqo.com/images/siiqo.png',
                        TRUE
                    )
                    ON CONFLICT (slug) DO NOTHING
                """))
                db.session.commit()
            except Exception:
                db.session.rollback()

            # 3b. Create article_slug_redirects table (301 redirect history)
            try:
                db.session.execute(_text("""
                    CREATE TABLE IF NOT EXISTS article_slug_redirects (
                        id          SERIAL PRIMARY KEY,
                        article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                        old_slug    VARCHAR(300) NOT NULL UNIQUE,
                        created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """))
                db.session.execute(_text("""
                    CREATE INDEX IF NOT EXISTS idx_article_slug_redirects_old_slug
                    ON article_slug_redirects (old_slug)
                """))
                db.session.commit()
            except Exception:
                db.session.rollback()

            # 4. Check if articles table exists and has data
            _result = db.session.execute(_text("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'articles'
                )
            """))
            _articles_table_exists = _result.scalar()
            
            if _articles_table_exists:
                _verify = db.session.execute(_text("SELECT COUNT(*) FROM articles"))
                _count = _verify.scalar() or 0
                if _count == 0:
                    _log.getLogger(__name__).info("[STARTUP] Articles table is empty. Auto-seeding default SME blog articles...")
                    try:
                        from seed_blog import articles_data
                        import re
                        for item in articles_data:
                            slug = re.sub(r'[^a-z0-9]+', '-', item['title'].lower()).strip('-')
                            db.session.execute(_text("""
                                INSERT INTO articles (title, slug, category, cover_image, excerpt, content, is_published, created_at, updated_at)
                                VALUES (:title, :slug, :category, :cover_image, :excerpt, :content, true, NOW(), NOW())
                                ON CONFLICT (slug) DO NOTHING
                            """), {
                                "title": item['title'],
                                "slug": slug,
                                "category": item.get('category'),
                                "cover_image": item.get('cover_image'),
                                "excerpt": item.get('excerpt'),
                                "content": item.get('content'),
                            })
                        db.session.commit()
                        _log.getLogger(__name__).info("[STARTUP] ✓ Default SME blog articles seeded successfully.")
                    except Exception as seed_err:
                        db.session.rollback()
                        _log.getLogger(__name__).warning(f"[STARTUP] Blog auto-seeding failed: {seed_err}")
                else:
                    _log.getLogger(__name__).info(f"[STARTUP] Articles table verified ({_count} articles)")
        except Exception as _e:
            db.session.rollback()
            import logging as _log
            _log.getLogger(__name__).warning(f"[STARTUP] Articles table verification skipped: {_e}")

        # ── Store place backfill (BUYING_FIX_PLAN C2) ────────────────────────
        # Stores set up through the website interview saved their place as one
        # free-text "location" answer that publish never read. Fill empty
        # store city/state from those answers, then empty physical product
        # locations from their store. Only fills blanks; a no-op once done.
        try:
            from app.services.places import backfill_store_places
            _stats = backfill_store_places()
            if _stats["stores"] or _stats["products"]:
                logging.getLogger(__name__).info(
                    f"[STARTUP] Place backfill: {_stats['stores']} store(s), {_stats['products']} product(s)")
        except Exception as _e:
            db.session.rollback()
            logging.getLogger(__name__).warning(f"[STARTUP] Place backfill skipped: {_e}")

    # -----------------------------------------------------------------------
    # Basic Logging Configuration
    # -----------------------------------------------------------------------
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    logger = logging.getLogger(__name__)
    logger.info("Initializing Siiqo backend...")
    jwt.init_app(app)
    cors.init_app(app, resources={r"/api/*": {"origins": app.config['CORS_ORIGINS']}})
    limiter.init_app(app)
    
    # -----------------------------------------------------------------------
    # Security Middleware — Applied to ALL responses
    # -----------------------------------------------------------------------
    from app.middleware.security import add_security_headers
    
    @app.after_request
    def apply_security_headers(response):
        """Add security headers to every response."""
        return add_security_headers(response)
    
    logger.info("Security middleware activated — headers, rate limiting, anomaly detection enabled")

    # -----------------------------------------------------------------------
    # JWT error handlers
    # -----------------------------------------------------------------------
    @jwt.expired_token_loader
    def expired_token_callback(jwt_header, jwt_payload):
        return jsonify({"message": "Token has expired. Please log in again."}), 401

    @jwt.invalid_token_loader
    def invalid_token_callback(error):
        return jsonify({"message": "Invalid token."}), 401

    @jwt.unauthorized_loader
    def missing_token_callback(error):
        return jsonify({"message": "Authentication required."}), 401

    # -----------------------------------------------------------------------
    # Global error handlers
    # -----------------------------------------------------------------------
    @app.errorhandler(404)
    def not_found_error(error):
        return jsonify({"message": "Resource not found"}), 404

    @app.errorhandler(405)
    def method_not_allowed(error):
        return jsonify({"message": "Method not allowed"}), 405

    @app.errorhandler(429)
    def rate_limit_exceeded(error):
        # A route can set its own plain message with
        # @limiter.limit(..., error_message="...").
        custom = getattr(getattr(error, "limit", None), "error_message", None)
        if callable(custom):
            custom = custom()
        return jsonify({"code": "RATE_LIMITED",
                        "message": custom or "Too many requests. Please slow down."}), 429

    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return jsonify({"message": "Internal server error"}), 500

    # -----------------------------------------------------------------------
    # Health & root
    # -----------------------------------------------------------------------
    @app.route("/health")
    @limiter.exempt
    def health_check():
        return jsonify({"status": "healthy", "version": "2.0.0"}), 200

    @app.route("/health/ready")
    @limiter.exempt
    def readiness_check():
        """Liveness stays on /health; this also checks the database."""
        from sqlalchemy import text as _health_text
        try:
            db.session.execute(_health_text("SELECT 1"))
            return jsonify({"status": "ready", "database": "ok"}), 200
        except Exception:
            db.session.rollback()
            return jsonify({"status": "unavailable", "database": "error"}), 503

    @app.route("/")
    @limiter.exempt
    def index():
        return jsonify({
            "status": "online",
            "message": "Siiqo Business OS API",
            "version": "2.0.0",
            "docs": "/api/",
        }), 200

    @app.route('/uploads/<path:filename>')
    def serve_uploads(filename):
        from flask import send_from_directory
        uploads_dir = os.path.join(os.getcwd(), 'uploads')
        return send_from_directory(uploads_dir, filename)

    # -----------------------------------------------------------------------
    # Blueprints
    # -----------------------------------------------------------------------
    from app.routes.auth import auth_bp
    from app.routes.public import public_bp
    from app.routes.cart import cart_bp
    from app.routes.vendor import vendor_bp
    from app.routes.escrow import escrow_bp
    from app.routes.logistics import logistics_bp
    from app.routes.admin import admin_bp
    from app.routes.chat import chat_bp
    from app.routes.bridge import bridge_bp
    from app.routes.community import community_bp
    from app.routes.finance import finance_bp
    from app.routes.withdrawal import withdrawal_bp
    from app.routes.negotiation import negotiation_bp
    from app.routes.payment_links import payment_links_bp
    from app.routes.payments import payments_bp
    from app.routes.grants import grants_bp
    from app.routes.grants_migration import grants_migration_bp
    from app.routes.events import events_bp
    from app.routes.daya_admin import daya_admin_bp
    from app.routes.verification import verification_bp

    app.register_blueprint(auth_bp,         url_prefix='/api/auth')
    app.register_blueprint(public_bp,       url_prefix='/api/marketplace')
    app.register_blueprint(cart_bp,         url_prefix='/api/cart')
    app.register_blueprint(vendor_bp,       url_prefix='/api/vendor')
    app.register_blueprint(escrow_bp,       url_prefix='/api/escrow')
    app.register_blueprint(logistics_bp,    url_prefix='/api/logistics')
    app.register_blueprint(admin_bp,        url_prefix='/api/admin')
    app.register_blueprint(chat_bp,         url_prefix='/api/chat')
    app.register_blueprint(community_bp,    url_prefix='/api/community')
    app.register_blueprint(finance_bp,      url_prefix='/api/finance')
    app.register_blueprint(withdrawal_bp,   url_prefix='/api/withdrawal')
    app.register_blueprint(negotiation_bp,  url_prefix='/api/negotiations')
    app.register_blueprint(payment_links_bp, url_prefix='/api')
    # Daya crypto payments + vendor crypto wallet management
    app.register_blueprint(payments_bp,     url_prefix='/api/payments')
    # Grants and funding opportunities
    app.register_blueprint(grants_bp,       url_prefix='/api/grants')
    # Events and ticketing system
    app.register_blueprint(events_bp,       url_prefix='/api')
    # Temporary grants migration endpoint
    app.register_blueprint(grants_migration_bp, url_prefix='/api/admin')
    # Daya admin: live balance + platform fee sweep
    app.register_blueprint(daya_admin_bp,   url_prefix='/api/admin/daya')
    # Verification v2 (provider-based; "coming soon" until VERIFICATION_V2_ENABLED)
    app.register_blueprint(verification_bp, url_prefix='/api/verification')
    # Bridge: aliases + missing endpoints the frontend expects at /api/*
    app.register_blueprint(bridge_bp,       url_prefix='/api')

    from app.routes.passport import passport_bp
    app.register_blueprint(passport_bp, url_prefix='/api/passport')
    from app.routes.product_questions import product_questions_bp
    app.register_blueprint(product_questions_bp, url_prefix='/api/public')
    # Marketing system: short codes, agents, seller growth kit
    from app.routes.marketing import agents_bp, growth_kit_bp, marketing_public_bp
    app.register_blueprint(marketing_public_bp, url_prefix='/api/public')
    app.register_blueprint(agents_bp, url_prefix='/api/agents')
    app.register_blueprint(growth_kit_bp, url_prefix='/api/vendor')
    from app.routes.check import check_bp
    app.register_blueprint(check_bp, url_prefix='/api/check')
    from app.routes.trust_report import trust_report_bp
    app.register_blueprint(trust_report_bp, url_prefix='/api/check')
    from app.routes.privacy import privacy_bp
    app.register_blueprint(privacy_bp, url_prefix='/api/privacy')
    # Phase 1: AI seller onboarding (/start on the website)
    from app.routes.onboarding import onboarding_bp
    app.register_blueprint(onboarding_bp, url_prefix='/api/onboarding')
    # Phase 2: Trust Score v1 shadow comparison (admin only)
    from app.routes.trust_admin import trust_admin_bp
    app.register_blueprint(trust_admin_bp, url_prefix='/api/admin/trust')
    # Phase 2B: Trust Platform API v1 (docs/PLATFORM_API_V1.md) and its console
    from app.routes.v1 import v1_bp
    app.register_blueprint(v1_bp, url_prefix='/v1')
    from app.routes.platform import platform_bp
    app.register_blueprint(platform_bp, url_prefix='/api/platform')
    # Phase 4: Evidence API consent (seller) and controls (superadmin)
    from app.routes.partner_evidence import evidence_admin_bp, trust_partners_bp
    app.register_blueprint(trust_partners_bp, url_prefix='/api/vendor/trust-partners')
    app.register_blueprint(evidence_admin_bp, url_prefix='/api/admin/evidence-partners')
    # Phase 4: mandatory verification flag (D11, VERIFICATION_REQUIRED_FOR)
    from app.routes.verification_requirement import (verification_requirement_admin_bp,
                                                     verification_requirement_bp)
    app.register_blueprint(verification_requirement_bp, url_prefix='/api/vendor/verification-requirement')
    app.register_blueprint(verification_requirement_admin_bp, url_prefix='/api/admin/verification-requirement')
    # Phase 3: business directory and claim flow
    from app.routes.directory import directory_admin_bp, directory_bp
    app.register_blueprint(directory_bp, url_prefix='/api/directory')
    app.register_blueprint(directory_admin_bp, url_prefix='/api/admin/directory')
    # Safe Pay requests: buyers bring sellers (docs/SAFE_PAY_REQUEST_PLAN.md)
    from app.routes.pay_requests import pay_requests_admin_bp, pay_requests_bp
    app.register_blueprint(pay_requests_bp, url_prefix='/api/pay-requests')
    app.register_blueprint(pay_requests_admin_bp, url_prefix='/api/admin/pay-requests')
    from app.routes.seller_reports import seller_reports_admin_bp, seller_reports_bp
    app.register_blueprint(seller_reports_bp, url_prefix='/api/check')
    app.register_blueprint(seller_reports_admin_bp, url_prefix='/api/admin/seller-reports')
    # Phase 3: Ask (AI search) and Market (server-paginated browsing)
    from app.routes.search import market_bp, search_bp
    app.register_blueprint(search_bp, url_prefix='/api/search')
    app.register_blueprint(market_bp, url_prefix='/api/market')
    # Phase 4: account language setting (D19) and optional address verification (V5)
    from app.routes.account import account_bp
    app.register_blueprint(account_bp, url_prefix='/api/account')
    from app.routes.address_verification import address_verification_bp
    app.register_blueprint(address_verification_bp, url_prefix='/api/verification/address')
    # Phase T/W: signed internal API for SiiqoBot (sessions, intent router, voice notes)
    from app.routes.bot_internal import bot_internal_bp
    app.register_blueprint(bot_internal_bp, url_prefix='/api/internal/bot')
    # Phase 4: store concierge (buyer chat, opt-in per store) and seller copilot (read-only)
    from app.routes.assistant import concierge_bp, copilot_bp
    app.register_blueprint(concierge_bp, url_prefix='/api/concierge')
    app.register_blueprint(copilot_bp, url_prefix='/api/copilot')

    # Phase 0B: record order history, fees, trust evidence, funnel events and
    # seller identifiers automatically on every database write.
    from app.services.foundations import register as _register_foundations
    _register_foundations()

    # -----------------------------------------------------------------------
    # Background scheduler — escrow auto-release and delivery reminders
    # Only starts in worker 1 (the first gunicorn worker) to avoid duplicate
    # jobs firing across all 4 workers.
    # We use a persistent file-based lock so only ONE process actually starts
    # the scheduler regardless of how many workers gunicorn spawns.
    # Set DISABLE_SCHEDULER=true in .env to turn off entirely.
    # -----------------------------------------------------------------------
    global _SCHEDULER_LOCK_FILE
    _scheduler_lock_acquired = False
    if not app.config.get('TESTING') and not os.environ.get('DISABLE_SCHEDULER'):
        try:
            import fcntl, tempfile
            lock_path = os.path.join(tempfile.gettempdir(), 'siiqo_scheduler.lock')
            _lock = open(lock_path, 'a')
            fcntl.flock(_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _SCHEDULER_LOCK_FILE = _lock
            app._scheduler_lock_file = _lock
            _scheduler_lock_acquired = True
            logger.info(f"[SCHEDULER] Acquired exclusive lock (PID {os.getpid()}) — starting background scheduler.")
        except (IOError, OSError):
            # Another Gunicorn worker process already holds the lock — skip scheduler
            logger.info(f"[SCHEDULER] PID {os.getpid()} skipped scheduler startup (already active in primary worker).")
        except ImportError:
            # Platform without fcntl (e.g. Windows in local development)
            if not _SCHEDULER_LOCK_FILE:
                _scheduler_lock_acquired = True
                _SCHEDULER_LOCK_FILE = True

    if _scheduler_lock_acquired:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.interval import IntervalTrigger
            from apscheduler.triggers.cron import CronTrigger
            from app.tasks.escrow_tasks import (
                auto_release_escrow,
                send_delivery_reminders,
                check_pending_payments,
                send_unpaid_reminders,
                send_ship_reminders,
            )
            from app.tasks.onboarding_tasks import run_onboarding_email_sequence
            from app.tasks.recap_tasks import run_monday_recap_task

            scheduler = BackgroundScheduler(daemon=True)

            # Run auto-release every hour
            scheduler.add_job(
                func=lambda: _run_in_context(app, auto_release_escrow),
                trigger=IntervalTrigger(hours=1),
                id='auto_release_escrow',
                name='Auto-release delivered escrow after 72 h',
                replace_existing=True,
            )
            # Send buyer delivery reminders once a day
            scheduler.add_job(
                func=lambda: _run_in_context(app, send_delivery_reminders),
                trigger=IntervalTrigger(hours=1),
                id='send_delivery_reminders',
                name='Remind buyers to confirm delivery',
                replace_existing=True,
            )
            # Cancel unpaid orders every hour, PENDING_PAYMENT_CANCEL_HOURS
            # (default 24) after they were placed.
            scheduler.add_job(
                func=lambda: _run_in_context(app, check_pending_payments),
                trigger=IntervalTrigger(hours=1),
                id='check_pending_payments',
                name='Cancel orders unpaid after PENDING_PAYMENT_CANCEL_HOURS',
                replace_existing=True,
            )
            # Remind buyers once about an unpaid order (45 min to 20 h old)
            scheduler.add_job(
                func=lambda: _run_in_context(app, send_unpaid_reminders),
                trigger=IntervalTrigger(hours=1),
                id='send_unpaid_reminders',
                name='Remind buyers to finish paying',
                replace_existing=True,
            )
            # Remind sellers to ship paid orders (24 h and 48 h after payment)
            scheduler.add_job(
                func=lambda: _run_in_context(app, send_ship_reminders),
                trigger=IntervalTrigger(hours=1),
                id='send_ship_reminders',
                name='Remind sellers to ship paid orders',
                replace_existing=True,
            )
            # Onboarding email sequence — check every 6 hours (lightweight)
            scheduler.add_job(
                func=lambda: _run_in_context(app, run_onboarding_email_sequence),
                trigger=IntervalTrigger(hours=6),
                id='onboarding_email_sequence',
                name='Day-1/3/7 vendor onboarding emails',
                replace_existing=True,
            )
            # Monday morning recap — runs every Monday at 7:00 UTC (8:00 WAT)
            scheduler.add_job(
                func=lambda: _run_in_context(app, run_monday_recap_task),
                trigger=CronTrigger(day_of_week='mon', hour=7, minute=0),
                id='monday_recap',
                name='Weekly store recap email to all active vendors',
                replace_existing=True,
            )
            # Event reminder emails — check every hour, send 24h before event
            try:
                from app.tasks.event_reminder_tasks import run_event_reminders
                scheduler.add_job(
                    func=lambda: _run_in_context(app, run_event_reminders),
                    trigger=IntervalTrigger(hours=1),
                    id='event_reminders',
                    name='24h event reminder emails to ticket holders',
                    replace_existing=True,
                )
            except Exception as reminder_err:
                logger.warning(f"Event reminder task registration skipped: {reminder_err}")

            # Ticket expiry — cancel pending manual tickets after 48h (check hourly)
            try:
                from app.tasks.ticket_expiry_tasks import run_ticket_expiry_check
                scheduler.add_job(
                    func=lambda: _run_in_context(app, run_ticket_expiry_check),
                    trigger=IntervalTrigger(hours=1),
                    id='ticket_expiry_check',
                    name='Cancel 48h-expired pending manual ticket submissions',
                    replace_existing=True,
                )
            except Exception as expiry_err:
                logger.warning(f"Ticket expiry task registration skipped: {expiry_err}")

            # Verified Pro: renewal reminders and Verified-mark sync, once a day
            try:
                from app.services.billing import run_daily_billing_tasks
                scheduler.add_job(
                    func=lambda: _run_in_context(app, run_daily_billing_tasks),
                    trigger=IntervalTrigger(hours=24),
                    id='daily_billing_tasks',
                    name='Verified Pro renewal reminders and Verified-mark sync',
                    replace_existing=True,
                )
            except Exception as billing_err:
                logger.warning(f"Billing task registration skipped: {billing_err}")

            # Data retention (NDPA): daily; reports only unless PRIVACY_RETENTION_APPLY=true
            try:
                from app.tasks.privacy_retention import run_scheduled as run_privacy_retention
                scheduler.add_job(
                    func=lambda: _run_in_context(app, run_privacy_retention),
                    trigger=IntervalTrigger(hours=24),
                    id='privacy_retention',
                    name='Remove or reduce personal data past its retention period',
                    replace_existing=True,
                )
            except Exception as retention_err:
                logger.warning(f"Privacy retention task registration skipped: {retention_err}")

            # Platform fee auto-sweep — check every 6 hours, sweep when >= threshold
            try:
                from app.tasks.fee_sweep_tasks import run_platform_fee_sweep
                scheduler.add_job(
                    func=lambda: _run_in_context(app, run_platform_fee_sweep),
                    trigger=IntervalTrigger(hours=6),
                    id='platform_fee_sweep',
                    name='Auto-sweep Siiqo platform fees (5% standard / 3% verified) to corporate bank when >= threshold',
                    replace_existing=True,
                )
            except Exception as sweep_err:
                logger.warning(f"Platform fee sweep task registration skipped: {sweep_err}")

            # Pro Badge Auto-Revocation — check every 6 hours
            try:
                from app.tasks.pro_badge_expiry_tasks import run_pro_badge_expiry_check
                scheduler.add_job(
                    func=lambda: _run_in_context(app, run_pro_badge_expiry_check),
                    trigger=IntervalTrigger(hours=6),
                    id='pro_badge_expiry_check',
                    name='Auto-revoke expired Pro Verified badges and notify vendors',
                    replace_existing=True,
                )
            except Exception as badge_err:
                logger.warning(f"Pro badge expiry task registration skipped: {badge_err}")

            # Trust Score v1 shadow run — daily 02:30 UTC; stores v1, never changes the public score
            try:
                from app.tasks.trust_v1_shadow import run_trust_v1_shadow
                scheduler.add_job(
                    func=lambda: _run_in_context(app, run_trust_v1_shadow),
                    trigger=CronTrigger(hour=2, minute=30),
                    id='trust_v1_shadow',
                    name='Trust Score v1 shadow scores for every seller',
                    replace_existing=True,
                )
            except Exception as trust_v1_err:
                logger.warning(f"Trust v1 shadow task registration skipped: {trust_v1_err}")

            # Trust Platform (2B): webhook retries every minute; Passport change
            # sweep and domain/handle ownership re-checks hourly.
            try:
                from app.services.platform_webhooks import run_due_deliveries, run_passport_sweep
                from app.services.ownership import run_ownership_rechecks
                scheduler.add_job(
                    func=lambda: _run_in_context(app, run_due_deliveries),
                    trigger=IntervalTrigger(minutes=1),
                    id='platform_webhook_retries',
                    name='Retry Trust Platform webhook deliveries whose backoff has passed',
                    replace_existing=True,
                )
                scheduler.add_job(
                    func=lambda: _run_in_context(app, run_passport_sweep),
                    trigger=IntervalTrigger(hours=1),
                    id='platform_passport_sweep',
                    name='Trust Platform webhooks: detect Passport changes over time',
                    replace_existing=True,
                )
                scheduler.add_job(
                    func=lambda: _run_in_context(app, run_ownership_rechecks),
                    trigger=IntervalTrigger(hours=1),
                    id='platform_ownership_rechecks',
                    name='Re-check verified domains and handles',
                    replace_existing=True,
                )
            except Exception as platform_err:
                logger.warning(f"Trust Platform task registration skipped: {platform_err}")

            # Mandatory verification (D11): start the grace clock for a newly set
            # VERIFICATION_REQUIRED_FOR, release holds of sellers who verified. Hourly.
            try:
                from app.services.verification_requirement import run_verification_requirement_sweep
                scheduler.add_job(
                    func=lambda: _run_in_context(app, run_verification_requirement_sweep),
                    trigger=IntervalTrigger(hours=1),
                    id='verification_requirement_sweep',
                    name='Mandatory verification: grace clock and release of held payouts',
                    replace_existing=True,
                )
            except Exception as verify_req_err:
                logger.warning(f"Verification requirement task registration skipped: {verify_req_err}")

            # Address verification (V5): read results of pending checks. Hourly; no-op while
            # ADDRESS_VERIFICATION_ENABLED is off.
            try:
                from app.services.address_verification.service import refresh_pending as _refresh_addresses
                scheduler.add_job(
                    func=lambda: _run_in_context(app, _refresh_addresses),
                    trigger=IntervalTrigger(hours=1),
                    id='address_verification_refresh',
                    name='Address verification: read pending results',
                    replace_existing=True,
                )
            except Exception as address_err:
                logger.warning(f"Address verification task registration skipped: {address_err}")

            # Marketing: catch missed seller milestones, release held agent
            # rewards after the hold or reverse them on a refund. Hourly.
            try:
                from app.services.marketing import run_marketing_sweep
                scheduler.add_job(
                    func=lambda: _run_in_context(app, run_marketing_sweep),
                    trigger=IntervalTrigger(hours=1),
                    id='marketing_sweep',
                    name='Marketing: seller milestones and agent reward hold',
                    replace_existing=True,
                )
            except Exception as marketing_err:
                logger.warning(f"Marketing task registration skipped: {marketing_err}")

            scheduler.start()
            logger.info("APScheduler started — escrow background tasks are active.")
        except ImportError:
            logger.warning(
                "APScheduler not installed. Escrow auto-release will NOT run automatically. "
                "Install it with: pip install APScheduler==3.10.4"
            )
        except Exception as e:
            logger.error(f"Failed to start APScheduler: {e}")

    return app


def _run_in_context(app, task_fn):
    """Run a scheduled task inside the Flask application context.

    The file lock above keeps one scheduler per server. With several servers,
    each would run the money jobs (auto-release, fee sweep), so every task also
    takes a cluster-wide Postgres advisory lock and is skipped if another
    server is already running it."""
    import logging
    import zlib
    with app.app_context():
        conn = None
        try:
            if db.engine.dialect.name == "postgresql":
                from sqlalchemy import text as _lock_text
                key = zlib.crc32(task_fn.__name__.encode())
                conn = db.engine.connect()
                got = conn.execute(_lock_text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
                if not got:
                    logging.getLogger(__name__).info(
                        f"[SCHEDULER] '{task_fn.__name__}' already running on another server; skipped")
                    conn.close()
                    return
            task_fn()
        except Exception as e:
            logging.getLogger(__name__).error(
                f"[SCHEDULER] Error in task '{task_fn.__name__}': {e}"
            )
        finally:
            if conn is not None and not conn.closed:
                try:
                    from sqlalchemy import text as _unlock_text
                    conn.execute(_unlock_text("SELECT pg_advisory_unlock(:k)"),
                                 {"k": zlib.crc32(task_fn.__name__.encode())})
                finally:
                    conn.close()
