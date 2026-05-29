"""idempotency_keys

Creates the ``idempotency_keys`` table that backs ``with_idempotency`` (Sprint 2
Task 5). The UNIQUE constraint on ``(user_keycloak_id, key)`` serializes
concurrent requests with the same idempotency key — see
``app/services/idempotency.py``.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_keycloak_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        # NULL until ``IdempotencyResult.record()`` writes the response back.
        sa.Column("response_status", sa.SmallInteger(), nullable=True),
        sa.Column("response_body", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_keycloak_id", "key", name="idempotency_keys_user_key_uq"),
    )
    op.create_index("idempotency_keys_created_at_idx", "idempotency_keys", ["created_at"])


def downgrade() -> None:
    op.drop_index("idempotency_keys_created_at_idx", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
