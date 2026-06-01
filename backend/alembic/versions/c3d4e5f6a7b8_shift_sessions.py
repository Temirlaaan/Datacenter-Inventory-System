"""shift_sessions

Creates the ``shift_sessions`` table (Sprint 6 Task 1) — backend-owned shift
tracking that supersedes the JWT ``sid`` claim as the source of
``audit_log.session_id`` (Sprint 6 Task 4 re-sources existing services).

Decision H of docs/sprint-6.md:

- ``shift_end_reason`` is a native Postgres enum so downgrade can drop it
  explicitly (same pattern as ``qr_status``/``audit_result`` from Sprint 2).
- ``shift_end_consistency`` CHECK pairs ``shift_end_at`` with ``end_reason`` —
  an active session has neither, a closed session must have both.
- ``shift_sessions_one_active_per_user`` partial unique index enforces "≤1
  active session per user" without blocking the user from opening a new shift
  after the previous one ends (mirrors Sprint 2's ``qr_one_per_device``).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create the enum type up-front so the column DDL can reference it without
    # the column trying (and failing) to create it.
    shift_end_reason = postgresql.ENUM(
        "manual",
        "inactivity_timeout",
        "admin_force_close",
        name="shift_end_reason",
        create_type=False,
    )
    shift_end_reason.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "shift_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_email", sa.Text(), nullable=False),
        sa.Column("user_keycloak_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "shift_start_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("shift_end_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("tablet_id", sa.Text(), nullable=False),
        sa.Column("end_reason", shift_end_reason, nullable=True),
        sa.CheckConstraint(
            "(shift_end_at IS NULL AND end_reason IS NULL)"
            " OR (shift_end_at IS NOT NULL AND end_reason IS NOT NULL)",
            name="shift_end_consistency",
        ),
    )
    op.create_index(
        "shift_sessions_one_active_per_user",
        "shift_sessions",
        ["user_keycloak_id"],
        unique=True,
        postgresql_where=sa.text("shift_end_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("shift_sessions_one_active_per_user", table_name="shift_sessions")
    op.drop_table("shift_sessions")
    postgresql.ENUM(name="shift_end_reason").drop(op.get_bind(), checkfirst=False)
