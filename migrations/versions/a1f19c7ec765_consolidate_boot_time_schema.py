"""Consolidate boot-time and ad-hoc schema changes into Alembic.

Revision ID: a1f19c7ec765
Revises: fdf2ba4ce121
Create Date: 2026-10-30 12:05:00.000000

Until now, part of the schema was applied outside Alembic:

  1. app/__init__.py runs a list of ALTER TABLE / CREATE TABLE / CREATE INDEX
     statements on every boot (plus the blog_authors / article columns /
     article_slug_redirects block and the grants table bootstrap).
  2. .ebextensions/01_flask_migrate.config runs three ad-hoc scripts:
       - run_events_migration.py   (migrations/create_events_and_tickets_tables.sql)
       - create_article_slug_redirects_migration.py
       - apply_telemetry_migration.py (platform_events, trust_evidence)

This revision applies all of those schema statements once, idempotently, so
that it is a no-op wherever the change already exists:

  * every ALTER uses `ALTER TABLE IF EXISTS ... ADD COLUMN IF NOT EXISTS`;
  * every CREATE uses `IF NOT EXISTS`;
  * statements that Postgres cannot express idempotently (DROP NOT NULL,
    ADD COLUMN ... REFERENCES, ADD CONSTRAINT, CREATE TRIGGER, indexes on
    columns that may be missing) are wrapped in DO blocks that check the
    catalog first.

Deliberately NOT included (they are data, not schema, or model-driven):
  * the escrow_code backfill UPDATE at boot;
  * db.create_all() at boot (creates any model table that is missing);
  * the grants sample-data INSERT and the default blog author / blog article
    seeds.

The boot-time list in app/__init__.py is intentionally left in place until
staging proves this revision. See docs/MIGRATIONS_RUNBOOK.md.

Postgres only: on any other dialect (e.g. the SQLite test harness) the
upgrade does nothing.
"""
from alembic import op

revision = 'a1f19c7ec765'
down_revision = 'fdf2ba4ce121'
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Helpers that build idempotent Postgres SQL
# ---------------------------------------------------------------------------

def _add(table, column, ddl):
    return (
        f"ALTER TABLE IF EXISTS {table} "
        f"ADD COLUMN IF NOT EXISTS {column} {ddl}"
    )


def _col_exists(table, column):
    return (
        "EXISTS (SELECT 1 FROM information_schema.columns "
        f"WHERE table_schema = current_schema() AND table_name = '{table}' "
        f"AND column_name = '{column}')"
    )


def _add_fk_column(table, column, ddl, ref_table):
    """ADD COLUMN ... REFERENCES, only when the column is missing."""
    return f"""
DO $$
BEGIN
    IF to_regclass('{table}') IS NOT NULL
       AND to_regclass('{ref_table}') IS NOT NULL
       AND NOT {_col_exists(table, column)} THEN
        ALTER TABLE {table} ADD COLUMN {column} {ddl};
    END IF;
END $$;
"""


def _drop_not_null(table, column):
    return f"""
DO $$
BEGIN
    IF {_col_exists(table, column)} THEN
        ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL;
    END IF;
END $$;
"""


def _index(name, table, expr, columns):
    """CREATE INDEX IF NOT EXISTS, only when every referenced column exists."""
    cond = " AND ".join(_col_exists(table, c) for c in columns)
    return f"""
DO $$
BEGIN
    IF {cond} THEN
        CREATE INDEX IF NOT EXISTS {name} ON {table} {expr};
    END IF;
END $$;
"""


def _trigger(name, table, function):
    return f"""
DO $$
BEGIN
    IF to_regclass('{table}') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM pg_trigger
           WHERE tgname = '{name}' AND tgrelid = to_regclass('{table}')
       ) THEN
        CREATE TRIGGER {name}
            BEFORE UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION {function}();
    END IF;
END $$;
"""


# ---------------------------------------------------------------------------
# 1. Tables (created first so later ALTERs / indexes can find them)
# ---------------------------------------------------------------------------

CREATE_TABLES = [
    # app/__init__.py (blog block)
    """CREATE TABLE IF NOT EXISTS blog_authors (
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
    )""",
    # app/__init__.py boot list (also migrations/create_events_and_tickets_tables.sql)
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
    # app/__init__.py grants bootstrap (migrations/create_grants_table.sql),
    # schema only; the sample-data INSERT is not part of this revision.
    """CREATE TABLE IF NOT EXISTS grants (
        id SERIAL PRIMARY KEY,
        slug VARCHAR(255) UNIQUE NOT NULL,
        name VARCHAR(255) NOT NULL,
        amount VARCHAR(100) NOT NULL,
        category VARCHAR(50)[] NOT NULL,
        country VARCHAR(100) NOT NULL,
        eligibility TEXT NOT NULL,
        description TEXT NOT NULL,
        application_tips TEXT,
        deadline VARCHAR(100) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'upcoming',
        last_verified TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        official_url VARCHAR(500) NOT NULL,
        cover_image VARCHAR(255),
        featured BOOLEAN DEFAULT FALSE,
        is_published BOOLEAN DEFAULT TRUE,
        meta_title VARCHAR(255),
        meta_description VARCHAR(500),
        admin_author_id INTEGER REFERENCES admin_users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    # create_article_slug_redirects_migration.py (matches the model)
    """CREATE TABLE IF NOT EXISTS article_slug_redirects (
        id SERIAL PRIMARY KEY,
        article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
        old_slug VARCHAR(255) NOT NULL UNIQUE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    )""",
    # apply_telemetry_migration.py
    """CREATE TABLE IF NOT EXISTS platform_events (
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
    )""",
    """CREATE TABLE IF NOT EXISTS trust_evidence (
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
    )""",
]


# ---------------------------------------------------------------------------
# 2. Columns (app/__init__.py boot list + blog block + ad-hoc scripts)
# ---------------------------------------------------------------------------

ADD_COLUMNS = [
    # logistics telemetry
    ("logistics_assignments", "current_latitude", "NUMERIC(9, 6)"),
    ("logistics_assignments", "current_longitude", "NUMERIC(9, 6)"),
    ("logistics_assignments", "location_updated_at", "TIMESTAMP"),
    # storefront engagement & onboarding
    ("storefronts", "view_count", "INTEGER DEFAULT 0"),
    ("storefronts", "is_pro_verified", "BOOLEAN DEFAULT FALSE"),
    ("storefronts", "pro_verified_expires_at", "TIMESTAMP WITH TIME ZONE"),
    ("storefronts", "onboarding_emails_sent", "JSONB DEFAULT '{}'"),
    # storefront ad pixels
    ("storefronts", "meta_pixel_id", "VARCHAR(50)"),
    ("storefronts", "tiktok_pixel_id", "VARCHAR(50)"),
    ("storefronts", "ga4_measurement_id", "VARCHAR(50)"),
    ("orders", "confirmation_email_sent", "BOOLEAN DEFAULT FALSE"),
    ("sponsored_listings", "payment_reference", "VARCHAR(100)"),
    ("vendor_subscriptions", "paystack_subscription_code", "VARCHAR(100)"),
    ("vendor_subscriptions", "paystack_email_token", "VARCHAR(100)"),
    ("orders", "currency", "VARCHAR(10) DEFAULT 'NGN'"),
    ("orders", "fx_rate_to_ngn", "NUMERIC(18, 6)"),
    ("orders", "paid_at", "TIMESTAMP WITH TIME ZONE"),
    ("orders", "delivered_at", "TIMESTAMP WITH TIME ZONE"),
    ("orders", "released_at", "TIMESTAMP WITH TIME ZONE"),
    ("passport_api_clients", "owner_user_id", "INTEGER"),
    # admin TOTP
    ("admin_users", "totp_secret_enc", "VARCHAR(255)"),
    ("admin_users", "totp_pending_secret_enc", "VARCHAR(255)"),
    ("admin_users", "totp_enabled", "BOOLEAN DEFAULT FALSE"),
    ("admin_users", "totp_enabled_at", "TIMESTAMP WITH TIME ZONE"),
    ("admin_users", "totp_last_used_step", "BIGINT"),
    ("admin_users", "totp_recovery_hashes", "JSON"),
    ("admin_users", "totp_challenge_nonce", "VARCHAR(64)"),
    ("admin_users", "totp_challenge_failures", "INTEGER DEFAULT 0"),
    # products
    ("products", "view_count", "INTEGER DEFAULT 0"),
    # payment links
    ("payment_links", "product_type", "VARCHAR(20) DEFAULT 'service'"),
    ("payment_links", "file_url", "VARCHAR(500)"),
    ("payment_links", "currency", "VARCHAR(10) DEFAULT 'NGN'"),
    # multi-currency
    ("products", "currency", "VARCHAR(10) DEFAULT 'NGN'"),
    ("ticket_types", "currency", "VARCHAR(10) DEFAULT 'NGN'"),
    # invoices
    ("invoices", "customer_name", "VARCHAR(255)"),
    ("invoices", "customer_email", "VARCHAR(255)"),
    ("invoices", "customer_phone", "VARCHAR(50)"),
    ("invoices", "customer_address", "TEXT"),
    ("invoices", "line_items", "JSONB"),
    ("invoices", "subtotal", "NUMERIC(10, 2)"),
    ("invoices", "discount", "NUMERIC(10, 2)"),
    ("invoices", "tax_rate", "NUMERIC(5, 2)"),
    ("invoices", "tax_amount", "NUMERIC(10, 2)"),
    ("invoices", "total", "NUMERIC(10, 2)"),
    ("invoices", "currency", "VARCHAR(10) DEFAULT 'NGN'"),
    ("invoices", "notes", "TEXT"),
    ("invoices", "payment_link_token", "VARCHAR(100)"),
    ("invoices", "payment_method", "VARCHAR(50)"),
    # receipts
    ("receipts", "customer_name", "VARCHAR(255)"),
    ("receipts", "customer_email", "VARCHAR(255)"),
    ("receipts", "customer_phone", "VARCHAR(50)"),
    ("receipts", "line_items", "JSONB"),
    ("receipts", "subtotal", "NUMERIC(10, 2)"),
    ("receipts", "tax_amount", "NUMERIC(10, 2)"),
    ("receipts", "discount", "NUMERIC(10, 2)"),
    ("receipts", "total", "NUMERIC(10, 2)"),
    ("receipts", "currency", "VARCHAR(10) DEFAULT 'NGN'"),
    ("receipts", "payment_method", "VARCHAR(50) DEFAULT 'Cash'"),
    ("receipts", "notes", "TEXT"),
    ("receipts", "status", "VARCHAR(50) DEFAULT 'paid'"),
    # events & ticketing
    ("products", "is_free", "BOOLEAN DEFAULT FALSE"),
    ("events", "show_on_storefront", "BOOLEAN DEFAULT TRUE"),
    ("events", "show_on_marketplace", "BOOLEAN DEFAULT TRUE"),
    ("events", "organizer_name", "VARCHAR(255)"),
    ("events", "organizer_bio", "TEXT"),
    ("events", "organizer_avatar", "VARCHAR(500)"),
    ("events", "organizer_socials", "JSONB DEFAULT '{}'"),
    ("events", "agenda", "JSONB DEFAULT '[]'"),
    ("events", "faqs", "JSONB DEFAULT '[]'"),
    ("events", "schedules", "JSONB DEFAULT '[]'"),
    ("events", "custom_fields", "JSONB DEFAULT '[]'"),
    ("events", "cta_button_text", "VARCHAR(100) DEFAULT 'Get Tickets'"),
    ("events", "registration_type", "VARCHAR(20) DEFAULT 'native'"),
    ("events", "external_registration_url", "VARCHAR(500)"),
    ("events", "external_registration_instructions", "TEXT"),
    ("events", "manual_payment_bank_name", "VARCHAR(100)"),
    ("events", "manual_payment_account_name", "VARCHAR(255)"),
    ("events", "manual_payment_account_number", "VARCHAR(50)"),
    ("events", "manual_payment_instructions", "TEXT"),
    ("ticket_purchases", "selected_schedule_id", "VARCHAR(100)"),
    ("ticket_purchases", "selected_schedule_title", "VARCHAR(255)"),
    ("ticket_purchases", "custom_responses", "JSONB DEFAULT '{}'"),
    ("ticket_purchases", "payment_proof_url", "VARCHAR(500)"),
    ("ticket_purchases", "manual_payment_status", "VARCHAR(30)"),
    ("ticket_purchases", "expires_at", "TIMESTAMP"),
    ("ticket_purchases", "rejection_reason", "TEXT"),
    ("ticket_purchases", "confirmed_at", "TIMESTAMP"),
    # guest checkout
    ("orders", "buyer_email", "VARCHAR(255)"),
    ("orders", "buyer_name", "VARCHAR(255)"),
    ("orders", "buyer_phone", "VARCHAR(50)"),
    ("orders", "is_guest", "BOOLEAN DEFAULT FALSE"),
    # articles
    ("articles", "sub_category", "VARCHAR(100)"),
    ("articles", "author_name", "VARCHAR(100)"),
    ("articles", "subcategory", "VARCHAR(100)"),
    ("articles", "meta_title", "VARCHAR(255)"),
    ("articles", "meta_description", "VARCHAR(500)"),
    ("articles", "cover_image", "VARCHAR(255)"),
    ("articles", "excerpt", "VARCHAR(500)"),
    # create_article_slug_redirects_migration.py shape (boot shape lacked it)
    ("article_slug_redirects", "updated_at", "TIMESTAMP WITH TIME ZONE DEFAULT NOW()"),
]

FK_COLUMNS = [
    ("ticket_purchases", "confirmed_by", "INTEGER REFERENCES users(id)", "users"),
    ("articles", "author_id",
     "INTEGER REFERENCES blog_authors(id) ON DELETE SET NULL", "blog_authors"),
]

DROP_NOT_NULL = [
    ("ticket_purchases", "buyer_id"),
    ("orders", "buyer_id"),
    ("daya_payments", "buyer_id"),
]


# ---------------------------------------------------------------------------
# 3. Indexes (name, table, expression, columns that must exist)
# ---------------------------------------------------------------------------

INDEXES = [
    ("ix_passport_api_clients_owner_user_id", "passport_api_clients", "(owner_user_id)", ["owner_user_id"]),
    # create_events_and_tickets_tables.sql
    ("idx_events_storefront", "events", "(storefront_id)", ["storefront_id"]),
    ("idx_events_vendor", "events", "(vendor_id)", ["vendor_id"]),
    ("idx_events_slug", "events", "(slug)", ["slug"]),
    ("idx_events_start_date", "events", "(start_date)", ["start_date"]),
    ("idx_events_published", "events", "(is_published, is_active, is_deleted)",
     ["is_published", "is_active", "is_deleted"]),
    ("idx_events_location", "events", "(city, state, country)", ["city", "state", "country"]),
    ("idx_ticket_types_event", "ticket_types", "(event_id)", ["event_id"]),
    ("idx_ticket_types_active", "ticket_types", "(is_active)", ["is_active"]),
    ("idx_ticket_purchases_event", "ticket_purchases", "(event_id)", ["event_id"]),
    ("idx_ticket_purchases_ticket_type", "ticket_purchases", "(ticket_type_id)", ["ticket_type_id"]),
    ("idx_ticket_purchases_buyer", "ticket_purchases", "(buyer_id)", ["buyer_id"]),
    ("idx_ticket_purchases_order", "ticket_purchases", "(order_id)", ["order_id"]),
    ("idx_ticket_purchases_code", "ticket_purchases", "(ticket_code)", ["ticket_code"]),
    ("idx_ticket_purchases_status", "ticket_purchases", "(status)", ["status"]),
    ("idx_ticket_purchases_email", "ticket_purchases", "(buyer_email)", ["buyer_email"]),
    ("idx_products_is_free", "products", "(is_free)", ["is_free"]),
    # grants (create_grants_table.sql)
    ("idx_grants_slug", "grants", "(slug)", ["slug"]),
    ("idx_grants_status", "grants", "(status)", ["status"]),
    ("idx_grants_featured", "grants", "(featured)", ["featured"]),
    ("idx_grants_country", "grants", "(country)", ["country"]),
    ("idx_grants_created_at", "grants", "(created_at DESC)", ["created_at"]),
    ("idx_grants_category", "grants", "USING GIN (category)", ["category"]),
    # article_slug_redirects (boot block + ad-hoc script)
    ("idx_article_slug_redirects_old_slug", "article_slug_redirects", "(old_slug)", ["old_slug"]),
    ("ix_article_slug_redirects_article_id", "article_slug_redirects", "(article_id)", ["article_id"]),
    ("ix_article_slug_redirects_old_slug", "article_slug_redirects", "(old_slug)", ["old_slug"]),
    # telemetry (apply_telemetry_migration.py)
    ("idx_pe_event_name_time", "platform_events", "(event_name, timestamp DESC)", ["event_name", "timestamp"]),
    ("idx_pe_session_id", "platform_events", "(session_id)", ["session_id"]),
    ("idx_pe_business_id", "platform_events", "(business_id)", ["business_id"]),
    ("idx_pe_user_id", "platform_events", "(user_id)", ["user_id"]),
    ("idx_te_business_type", "trust_evidence", "(business_id, evidence_type)", ["business_id", "evidence_type"]),
    ("idx_te_created_at", "trust_evidence", "(created_at DESC)", ["created_at"]),
]


# ---------------------------------------------------------------------------
# 4. Constraints, comments, trigger functions and triggers
# ---------------------------------------------------------------------------

MISC = [
    # create_grants_table.sql: status check constraint
    """
DO $$
BEGIN
    IF to_regclass('grants') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM pg_constraint
           WHERE conname = 'chk_grants_status' AND conrelid = to_regclass('grants')
       ) THEN
        ALTER TABLE grants ADD CONSTRAINT chk_grants_status
            CHECK (status IN ('open', 'upcoming', 'closed'));
    END IF;
END $$;
""",
    # create_events_and_tickets_tables.sql: column comment
    f"""
DO $$
BEGIN
    IF {_col_exists('products', 'is_free')} THEN
        COMMENT ON COLUMN products.is_free IS
            'Whether this digital product or service is free (no payment required)';
    END IF;
END $$;
""",
    """CREATE OR REPLACE FUNCTION update_grants_updated_at()
RETURNS TRIGGER AS $fn$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$fn$ LANGUAGE plpgsql""",
    """CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $fn$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$fn$ LANGUAGE plpgsql""",
    _trigger("trigger_grants_updated_at", "grants", "update_grants_updated_at"),
    _trigger("update_events_updated_at", "events", "update_updated_at_column"),
    _trigger("update_ticket_types_updated_at", "ticket_types", "update_updated_at_column"),
    _trigger("update_ticket_purchases_updated_at", "ticket_purchases", "update_updated_at_column"),
]


def statements():
    """All SQL statements of this revision, in execution order."""
    out = list(CREATE_TABLES)
    out += [_add(t, c, d) for t, c, d in ADD_COLUMNS]
    out += [_add_fk_column(t, c, d, ref) for t, c, d, ref in FK_COLUMNS]
    out += [_drop_not_null(t, c) for t, c in DROP_NOT_NULL]
    out += [_index(n, t, e, cols) for n, t, e, cols in INDEXES]
    out += MISC
    # Alembic adds its own terminator in --sql output.
    return [s.strip().rstrip(";").rstrip() for s in out]


def upgrade():
    if op.get_context().dialect.name != 'postgresql':
        # The SQLite test harness builds its schema from the models.
        return
    for sql in statements():
        op.execute(sql)


def downgrade():
    # Intentional no-op. Every change here is additive and was already live in
    # production (applied at boot or by the .ebextensions scripts) before this
    # revision existed. Dropping these columns/tables would destroy data that
    # the running application depends on. Revert by restoring the pre-upgrade
    # snapshot instead (see docs/MIGRATIONS_RUNBOOK.md).
    pass
