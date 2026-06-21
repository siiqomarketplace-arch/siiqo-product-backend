import os
from flask import Flask, jsonify
from app.config import config
from app.extensions import db, migrate, jwt, cors, limiter


def create_app(config_name: str | None = None) -> Flask:
    if config_name is None:
        config_name = os.environ.get('FLASK_ENV', 'default')

    app = Flask(__name__)
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
    # Bridge: aliases + missing endpoints the frontend expects at /api/*
    app.register_blueprint(bridge_bp,       url_prefix='/api')

    # -----------------------------------------------------------------------
    # Background scheduler — escrow auto-release and delivery reminders
    # Only starts in the main process to avoid duplicate jobs under gunicorn.
    # Set DISABLE_SCHEDULER=true in .env to turn off (e.g. when running
    # Alembic migrations or one-off management commands).
    # -----------------------------------------------------------------------
    if not app.config.get('TESTING') and not os.environ.get('DISABLE_SCHEDULER'):
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.interval import IntervalTrigger
            from app.tasks.escrow_tasks import (
                auto_release_escrow,
                send_delivery_reminders,
                check_pending_payments,
            )

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
