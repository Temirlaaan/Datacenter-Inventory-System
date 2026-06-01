"""rename shift_end_reason values to ToR §7.2.4 canon

Aligns the ``shift_end_reason`` Postgres enum to the ToR §7.2.4 canonical
labels before any post-Sprint-6 admin endpoints write to the column. Sprint 6
shipped descriptive labels (``inactivity_timeout`` / ``admin_force_close``);
ToR §7.2.4 specifies ``auto_timeout`` / ``forced``. See docs/sprint-7.md
decision E.

This migration is **non-destructive** under the CLAUDE.md §7 destructive-
migration policy: ``ALTER TYPE ... RENAME VALUE`` rewrites the enum label in
place, preserves enum sort order and OIDs, and leaves existing rows pointing
to the renamed label. No column, table, constraint, or index is touched.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-01
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE shift_end_reason RENAME VALUE 'inactivity_timeout' TO 'auto_timeout'")
    op.execute("ALTER TYPE shift_end_reason RENAME VALUE 'admin_force_close' TO 'forced'")


def downgrade() -> None:
    op.execute("ALTER TYPE shift_end_reason RENAME VALUE 'forced' TO 'admin_force_close'")
    op.execute("ALTER TYPE shift_end_reason RENAME VALUE 'auto_timeout' TO 'inactivity_timeout'")
