"""
apply_telemetry_migration.py
Safely creates the 'platform_events' and 'trust_evidence' tables
and their corresponding indices without touching any existing tables.
"""
import os
import sys

os.environ.setdefault('SECRET_KEY', 'dev-secret-key-siiqo-telemetry')
os.environ.setdefault('JWT_SECRET_KEY', 'dev-jwt-secret-siiqo-telemetry')
os.environ.setdefault('FLASK_ENV', 'development')

if '--local' in sys.argv or not os.environ.get('DATABASE_URL') or 'rds.amazonaws.com' in os.environ.get('DATABASE_URL', ''):
    # If running locally outside AWS CloudShell/VPC, use local SQLite database
    if '--local' in sys.argv:
        os.makedirs('instance', exist_ok=True)
        abs_db = os.path.abspath('instance/siiqo.db').replace('\\', '/')
        os.environ['DATABASE_URL'] = f'sqlite:///{abs_db}'

from app import create_app
from app.extensions import db
from sqlalchemy import inspect, text

app = create_app('development')

with app.app_context():
    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()
    print(f"[INFO] Existing tables in database: {len(existing_tables)}")

    # 1. Create platform_events table
    if 'platform_events' not in existing_tables:
        print("[MIGRATE] Creating 'platform_events' table...")
        is_postgres = db.engine.dialect.name == 'postgresql'
        
        if is_postgres:
            create_events_sql = """
            CREATE TABLE IF NOT EXISTS platform_events (
                id BIGSERIAL PRIMARY KEY,
                event_name VARCHAR(100) NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                session_id VARCHAR(100),
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                business_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                storefront_id INTEGER REFERENCES storefronts(id) ON DELETE SET NULL,
                product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
                source VARCHAR(50),
                properties JSONB DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_pe_event_name_time ON platform_events(event_name, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_pe_session_id ON platform_events(session_id);
            CREATE INDEX IF NOT EXISTS idx_pe_business_id ON platform_events(business_id);
            CREATE INDEX IF NOT EXISTS idx_pe_user_id ON platform_events(user_id);
            """
        else:
            # SQLite fallback
            create_events_sql = """
            CREATE TABLE IF NOT EXISTS platform_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_name VARCHAR(100) NOT NULL,
                timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                session_id VARCHAR(100),
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                business_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                storefront_id INTEGER REFERENCES storefronts(id) ON DELETE SET NULL,
                product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
                source VARCHAR(50),
                properties JSON DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_pe_event_name_time ON platform_events(event_name, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_pe_session_id ON platform_events(session_id);
            CREATE INDEX IF NOT EXISTS idx_pe_business_id ON platform_events(business_id);
            """
        
        for statement in create_events_sql.strip().split(';'):
            stmt = statement.strip()
            if stmt:
                db.session.execute(text(stmt))
        db.session.commit()
        print("[SUCCESS] 'platform_events' table created.")
    else:
        print("[SKIP] 'platform_events' table already exists.")

    # 2. Create trust_evidence table
    if 'trust_evidence' not in existing_tables:
        print("[MIGRATE] Creating 'trust_evidence' table...")
        is_postgres = db.engine.dialect.name == 'postgresql'
        
        if is_postgres:
            create_trust_sql = """
            CREATE TABLE IF NOT EXISTS trust_evidence (
                id BIGSERIAL PRIMARY KEY,
                business_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                evidence_type VARCHAR(100) NOT NULL,
                provenance VARCHAR(50) NOT NULL DEFAULT 'business_claimed',
                source VARCHAR(100) NOT NULL,
                source_id VARCHAR(100),
                status VARCHAR(50) NOT NULL DEFAULT 'ACTIVE',
                confidence NUMERIC(3, 2) NOT NULL DEFAULT 1.00,
                properties JSONB DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                verified_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_te_business_type ON trust_evidence(business_id, evidence_type);
            CREATE INDEX IF NOT EXISTS idx_te_created_at ON trust_evidence(created_at DESC);
            """
        else:
            create_trust_sql = """
            CREATE TABLE IF NOT EXISTS trust_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                evidence_type VARCHAR(100) NOT NULL,
                provenance VARCHAR(50) NOT NULL DEFAULT 'business_claimed',
                source VARCHAR(100) NOT NULL,
                source_id VARCHAR(100),
                status VARCHAR(50) NOT NULL DEFAULT 'ACTIVE',
                confidence NUMERIC(3, 2) NOT NULL DEFAULT 1.00,
                properties JSON DEFAULT '{}',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                verified_at DATETIME
            );
            CREATE INDEX IF NOT EXISTS idx_te_business_type ON trust_evidence(business_id, evidence_type);
            """
        
        for statement in create_trust_sql.strip().split(';'):
            stmt = statement.strip()
            if stmt:
                db.session.execute(text(stmt))
        db.session.commit()
        print("[SUCCESS] 'trust_evidence' table created.")
    else:
        print("[SKIP] 'trust_evidence' table already exists.")

    print("\n[COMPLETE] Telemetry and Trust Evidence schema migration finished successfully.")
