"""qr_batches qr_codes audit_log

Creates the QR registry tables (``qr_batches``, ``qr_codes``) and the audit log
table (``audit_log``) with the full ToR §7.2 schema plus the database-level
invariants from Architecture §4:

- ``qr_status`` and ``audit_result`` are native Postgres enum types so downgrade
  can drop them explicitly.
- ``qr_state_consistency`` CHECK enforces that ``status``, ``bound_to_device_id``,
  and ``retired_at`` agree at all times.
- ``qr_one_per_device`` partial unique index enforces "one bound QR per device"
  without blocking re-binding after retirement.
- ``audit_log`` carries all 12 columns from ToR §7.2.3; ``session_id`` is
  nullable until ``shift_sessions`` lands.

Revision ID: a1b2c3d4e5f6
Revises: 068437e38dd9
Create Date: 2026-05-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "068437e38dd9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Native Postgres enum types. We create them up-front so columns can
    # reference them without the column DDL trying (and failing) to create them.
    qr_status = postgresql.ENUM("free", "bound", "retired", name="qr_status", create_type=False)
    audit_result = postgresql.ENUM(
        "success", "failure", "conflict", name="audit_result", create_type=False
    )
    qr_status.create(op.get_bind(), checkfirst=False)
    audit_result.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "qr_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("created_by_email", sa.String(255), nullable=False),
        sa.Column("created_by_keycloak_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("intended_site_id", sa.Integer(), nullable=True),
        sa.Column("intended_location_id", sa.Integer(), nullable=True),
        sa.Column("intended_rack_id", sa.Integer(), nullable=True),
        sa.Column("comment", sa.String(200), nullable=True),
        sa.Column("pdf_path", sa.Text(), nullable=True),
    )

    op.create_table(
        "qr_codes",
        sa.Column("id", sa.String(13), primary_key=True),
        sa.Column(
            "batch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("qr_batches.id"),
            nullable=False,
        ),
        sa.Column("status", qr_status, nullable=False),
        sa.Column("bound_to_device_id", sa.Integer(), nullable=True),
        sa.Column("bound_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("bound_by_email", sa.String(255), nullable=True),
        sa.Column("retired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("retired_reason", sa.String(500), nullable=True),
        sa.CheckConstraint(
            "(status = 'free'    AND bound_to_device_id IS NULL     AND retired_at IS NULL)"
            " OR (status = 'bound'   AND bound_to_device_id IS NOT NULL AND retired_at IS NULL)"
            " OR (status = 'retired' AND retired_at IS NOT NULL)",
            name="qr_state_consistency",
        ),
    )
    op.create_index(
        "qr_one_per_device",
        "qr_codes",
        ["bound_to_device_id"],
        unique=True,
        postgresql_where=sa.text("status = 'bound'"),
    )
    op.create_index("qr_codes_batch_id_idx", "qr_codes", ["batch_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "timestamp",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("user_email", sa.String(255), nullable=False),
        sa.Column("user_keycloak_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Nullable until shift_sessions lands.
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation", sa.String(50), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(50), nullable=False),
        sa.Column(
            "before_json",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "after_json",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("result", audit_result, nullable=False),
    )
    op.create_index("audit_log_timestamp_idx", "audit_log", [sa.text("timestamp DESC")])
    op.create_index("audit_log_entity_idx", "audit_log", ["entity_type", "entity_id"])


def downgrade() -> None:
    # Drop in reverse-dependency order: indexes -> tables -> enum types. Enum
    # types must come last so no live column depends on them.
    op.drop_index("audit_log_entity_idx", table_name="audit_log")
    op.drop_index("audit_log_timestamp_idx", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("qr_codes_batch_id_idx", table_name="qr_codes")
    op.drop_index("qr_one_per_device", table_name="qr_codes")
    op.drop_table("qr_codes")

    op.drop_table("qr_batches")

    postgresql.ENUM(name="audit_result").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="qr_status").drop(op.get_bind(), checkfirst=False)
