"""Phase 3 search index: tsvector columns, GIN indexes and pg_trgm.

Revision ID: 5e7a9c3b1d42
Revises: a1f19c7ec765
Create Date: 2026-10-31 10:00:00.000000

Creates (Postgres only, every statement idempotent):

  * extension pg_trgm (skipped with a NOTICE if the database user may not
    create it; search then works without trigram matching);
  * products.search_tsv: tsvector GENERATED ALWAYS AS (...) STORED from
    name (weight A), description (B) and attributes (C), 'simple' config;
  * storefronts.search_tsv: the same from store_name (A),
    store_description (B), city / state / address (C);
  * GIN indexes ix_products_search_tsv and ix_storefronts_search_tsv;
  * trigram GIN indexes ix_products_name_trgm and ix_storefronts_name_trgm
    (only when pg_trgm is installed).

Generated columns need Postgres 12 or later. On an older server the columns
and their indexes are skipped with a NOTICE, and app/services/search.py keeps
using ILIKE (it checks for the columns at run time).

Adding a STORED generated column rewrites the table once (products and
storefronts take a short exclusive lock). Run it in a quiet period.

On any other dialect (the SQLite test harness) upgrade and downgrade do
nothing; the application falls back to ILIKE there.
"""
from alembic import op

revision = '5e7a9c3b1d42'
down_revision = 'a1f19c7ec765'
branch_labels = None
depends_on = None


def _col_exists(table, column):
    return (
        "EXISTS (SELECT 1 FROM information_schema.columns "
        f"WHERE table_schema = current_schema() AND table_name = '{table}' "
        f"AND column_name = '{column}')"
    )


_TRGM = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
        BEGIN
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
        EXCEPTION WHEN insufficient_privilege OR feature_not_supported OR undefined_file THEN
            RAISE NOTICE 'pg_trgm not created (%); search runs without trigram matching', SQLERRM;
        END;
    END IF;
END $$;
"""


def _generated_tsv(table, expression, optional_column=None, without_optional=None):
    """ADD COLUMN search_tsv once. If ``optional_column`` is missing on this
    database, ``without_optional`` is used as the expression instead."""
    if optional_column:
        add = f"""
        IF {_col_exists(table, optional_column)} THEN
            ALTER TABLE {table} ADD COLUMN search_tsv tsvector
                GENERATED ALWAYS AS ({expression}) STORED;
        ELSE
            ALTER TABLE {table} ADD COLUMN search_tsv tsvector
                GENERATED ALWAYS AS ({without_optional}) STORED;
        END IF;"""
    else:
        add = f"""
        ALTER TABLE {table} ADD COLUMN search_tsv tsvector
            GENERATED ALWAYS AS ({expression}) STORED;"""
    return f"""
DO $$
BEGIN
    IF to_regclass('{table}') IS NULL THEN
        RETURN;
    END IF;
    IF current_setting('server_version_num')::int < 120000 THEN
        RAISE NOTICE '{table}.search_tsv skipped: generated columns need Postgres 12+';
        RETURN;
    END IF;
    IF NOT {_col_exists(table, 'search_tsv')} THEN{add}
    END IF;
END $$;
"""


_PRODUCTS_TSV_BASE = (
    "setweight(to_tsvector('simple', coalesce(name, '')), 'A') || "
    "setweight(to_tsvector('simple', coalesce(description, '')), 'B')"
)
_PRODUCTS_TSV = (
    _PRODUCTS_TSV_BASE + " || "
    "setweight(to_tsvector('simple', coalesce(attributes::text, '')), 'C')"
)

_STOREFRONTS_TSV = (
    "setweight(to_tsvector('simple', coalesce(store_name, '')), 'A') || "
    "setweight(to_tsvector('simple', coalesce(store_description, '')), 'B') || "
    "setweight(to_tsvector('simple', coalesce(city, '') || ' ' || coalesce(state, '') "
    "|| ' ' || coalesce(address, '')), 'C')"
)


def _gin_on_tsv(index, table):
    return f"""
DO $$
BEGIN
    IF to_regclass('{table}') IS NOT NULL AND {_col_exists(table, 'search_tsv')} THEN
        CREATE INDEX IF NOT EXISTS {index} ON {table} USING GIN (search_tsv);
    END IF;
END $$;
"""


def _trgm_index(index, table, column):
    return f"""
DO $$
BEGIN
    IF to_regclass('{table}') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')
       AND {_col_exists(table, column)} THEN
        CREATE INDEX IF NOT EXISTS {index} ON {table} USING GIN ({column} gin_trgm_ops);
    END IF;
END $$;
"""


UPGRADE = [
    _TRGM,
    _generated_tsv('products', _PRODUCTS_TSV, 'attributes', _PRODUCTS_TSV_BASE),
    _generated_tsv('storefronts', _STOREFRONTS_TSV),
    _gin_on_tsv('ix_products_search_tsv', 'products'),
    _gin_on_tsv('ix_storefronts_search_tsv', 'storefronts'),
    _trgm_index('ix_products_name_trgm', 'products', 'name'),
    _trgm_index('ix_storefronts_name_trgm', 'storefronts', 'store_name'),
]

DOWNGRADE = [
    "DROP INDEX IF EXISTS ix_storefronts_name_trgm",
    "DROP INDEX IF EXISTS ix_products_name_trgm",
    "DROP INDEX IF EXISTS ix_storefronts_search_tsv",
    "DROP INDEX IF EXISTS ix_products_search_tsv",
    "ALTER TABLE IF EXISTS storefronts DROP COLUMN IF EXISTS search_tsv",
    "ALTER TABLE IF EXISTS products DROP COLUMN IF EXISTS search_tsv",
    # pg_trgm is left installed: other objects may depend on it.
]


def _is_postgres():
    return op.get_context().dialect.name == 'postgresql'


def upgrade():
    if not _is_postgres():
        return
    for sql in UPGRADE:
        op.execute(sql)


def downgrade():
    if not _is_postgres():
        return
    for sql in DOWNGRADE:
        op.execute(sql)
