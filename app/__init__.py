import os
from flask import Flask, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from app.config import config
from app.extensions import db, migrate, jwt, cors, limiter


def create_app(config_name: str | None = None) -> Flask:
    if config_name is None:
        config_name = os.environ.get('FLASK_ENV', 'default')

    app = Flask(__name__)
    # Trust reverse proxy headers (Cloudflare & Elastic Beanstalk ALB/Nginx)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
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
                # ── Product view tracking ────────────────────────────────────────
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0",
                # ── Payment link product type ────────────────────────────────────
                "ALTER TABLE payment_links ADD COLUMN IF NOT EXISTS product_type VARCHAR(20) DEFAULT 'service'",
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
        return jsonify({"message": "Too many requests. Please slow down."}), 429

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
    # Temporary grants migration endpoint
    app.register_blueprint(grants_migration_bp, url_prefix='/api/admin')
    # Bridge: aliases + missing endpoints the frontend expects at /api/*
    app.register_blueprint(bridge_bp,       url_prefix='/api')

    # -----------------------------------------------------------------------
    # Background scheduler — escrow auto-release and delivery reminders
    # Only starts in worker 1 (the first gunicorn worker) to avoid duplicate
    # jobs firing across all 4 workers.
    # Gunicorn sets the env var WORKER_ID via --worker-class; we detect by
    # checking os.getpid() vs the parent. The reliable cross-platform approach
    # is to use a file-based lock so only ONE process actually starts the
    # scheduler regardless of how many workers gunicorn spawns.
    # Set DISABLE_SCHEDULER=true in .env to turn off entirely.
    # -----------------------------------------------------------------------
    _scheduler_lock_acquired = False
    if not app.config.get('TESTING') and not os.environ.get('DISABLE_SCHEDULER'):
        import fcntl, tempfile
        try:
            _lock_file = open(os.path.join(tempfile.gettempdir(), 'siiqo_scheduler.lock'), 'w')
            fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _scheduler_lock_acquired = True
        except (IOError, OSError):
            pass  # Another worker already holds the lock — skip scheduler

    if _scheduler_lock_acquired:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.interval import IntervalTrigger
            from apscheduler.triggers.cron import CronTrigger
            from app.tasks.escrow_tasks import (
                auto_release_escrow,
                send_delivery_reminders,
                check_pending_payments,
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
                trigger=IntervalTrigger(hours=24),
                id='send_delivery_reminders',
                name='Remind buyers to confirm delivery',
                replace_existing=True,
            )
            # Cancel stale unpaid orders once a day
            scheduler.add_job(
                func=lambda: _run_in_context(app, check_pending_payments),
                trigger=IntervalTrigger(hours=24),
                id='check_pending_payments',
                name='Cancel orders stuck in PENDING_PAYMENT for 24 h',
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
    """Run an escrow task function inside the Flask application context."""
    with app.app_context():
        try:
            task_fn()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(
                f"[SCHEDULER] Error in task '{task_fn.__name__}': {e}"
            )
