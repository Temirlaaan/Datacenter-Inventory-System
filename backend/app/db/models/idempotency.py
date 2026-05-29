"""SQLAlchemy model for the ``idempotency_keys`` table.

Backs ``with_idempotency`` in ``app/services/idempotency.py``. The UNIQUE
constraint on ``(user_keycloak_id, key)`` is the serialization mechanism for
concurrent requests — see that module for the full algorithm.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class IdempotencyKeyModel(Base):
    __tablename__ = "idempotency_keys"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    user_keycloak_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # NULL until ``IdempotencyResult.record()`` writes the response back.
    response_status: Mapped[int | None] = mapped_column(sa.SmallInteger, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        sa.UniqueConstraint("user_keycloak_id", "key", name="idempotency_keys_user_key_uq"),
        sa.Index("idempotency_keys_created_at_idx", "created_at"),
    )
