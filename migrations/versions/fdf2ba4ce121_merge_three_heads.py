"""Merge the three open heads into one.

Revision ID: fdf2ba4ce121
Revises: a2b3c4d5e6f7, b1c2d3e4f5a7, e4f5a6b7c8d9
Create Date: 2026-10-30 12:00:00.000000

Before this revision the script directory had three heads:
  - a2b3c4d5e6f7  (storefront ad-pixel columns)
  - b1c2d3e4f5a7  (flw_subaccount_id; itself a merge of the telegram and
                   category-attribute branches)
  - e4f5a6b7c8d9  (article_slug_redirects table)

This revision contains no schema changes. It only joins those heads so that
`flask db upgrade` has a single target. See docs/MIGRATIONS_RUNBOOK.md.
"""

revision = 'fdf2ba4ce121'
down_revision = ('a2b3c4d5e6f7', 'b1c2d3e4f5a7', 'e4f5a6b7c8d9')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
