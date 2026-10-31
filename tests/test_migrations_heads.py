"""
Alembic chain checks, by script inspection only.

Nothing here opens a database connection: the script directory is read from
disk, and the consolidation revision's SQL is rendered with Alembic's offline
("as_sql") mode against the Postgres dialect into a string buffer.
"""
import io
import os

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

_MIGRATIONS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)

MERGE_REV = "fdf2ba4ce121"
CONSOLIDATE_REV = "a1f19c7ec765"


def _scripts():
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS)
    return ScriptDirectory.from_config(cfg)


def _offline_sql(revision_id, dialect="postgresql"):
    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name=dialect,
        opts={"as_sql": True, "output_buffer": buf, "literal_binds": True},
    )
    module = _scripts().get_revision(revision_id).module
    with Operations.context(ctx):
        module.upgrade()
    return buf.getvalue()


def test_single_head():
    heads = _scripts().get_heads()
    assert heads == [CONSOLIDATE_REV], heads


def test_merge_revision_joins_previous_heads():
    merge = _scripts().get_revision(MERGE_REV)
    assert set(merge.down_revision) == {"a2b3c4d5e6f7", "b1c2d3e4f5a7", "e4f5a6b7c8d9"}
    assert _scripts().get_revision(CONSOLIDATE_REV).down_revision == MERGE_REV


def test_every_revision_reaches_the_head():
    """Walking down from the single head visits every revision on disk."""
    sd = _scripts()
    all_revs = {s.revision for s in sd.walk_revisions()}
    reachable = {s.revision for s in sd.iterate_revisions(CONSOLIDATE_REV, "base")}
    assert reachable == all_revs


def test_consolidation_sql_is_generated_offline_and_idempotent():
    sql = _offline_sql(CONSOLIDATE_REV)
    module = _scripts().get_revision(CONSOLIDATE_REV).module

    # Every statement was emitted.
    assert sql.count(";") >= len(module.statements())
    # Spot-check items from the boot list and each ad-hoc script.
    for needle in [
        "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS paid_at",
        "ALTER TABLE IF EXISTS admin_users ADD COLUMN IF NOT EXISTS totp_recovery_hashes",
        "ALTER TABLE IF EXISTS logistics_assignments ADD COLUMN IF NOT EXISTS current_latitude",
        "CREATE TABLE IF NOT EXISTS events",
        "CREATE TABLE IF NOT EXISTS platform_events",
        "CREATE TABLE IF NOT EXISTS trust_evidence",
        "CREATE TABLE IF NOT EXISTS article_slug_redirects",
        "CREATE TABLE IF NOT EXISTS grants",
        "CREATE INDEX IF NOT EXISTS idx_ticket_purchases_email",
        "CREATE INDEX IF NOT EXISTS ix_passport_api_clients_owner_user_id",
        "ALTER TABLE daya_payments ALTER COLUMN buyer_id DROP NOT NULL",
        "chk_grants_status",
        "update_ticket_purchases_updated_at",
    ]:
        assert needle in sql, needle

    # Idempotence: no bare CREATE / ADD COLUMN without a guard.
    for line in sql.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith("CREATE TABLE") or stripped.startswith("CREATE INDEX"):
            assert "IF NOT EXISTS" in stripped, line
        if stripped.startswith("ALTER TABLE") and "ADD COLUMN" in stripped:
            # unguarded ADD COLUMN only appears inside a DO block's IF
            assert "IF NOT EXISTS" in stripped or line.startswith("        "), line


def test_consolidation_is_noop_on_sqlite():
    assert _offline_sql(CONSOLIDATE_REV, dialect="sqlite").strip() == ""


def test_merge_revision_emits_no_sql():
    assert _offline_sql(MERGE_REV).strip() == ""
