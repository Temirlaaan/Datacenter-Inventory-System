"""SQLAlchemy model for the ``shift_sessions`` table.

The CHECK constraint and partial unique index defined in ``__table_args__``
mirror what the Alembic migration creates. Repeating them here keeps
``Base.metadata`` aligned with DB reality, which any future autogenerate run
relies on.

The ``shift_end_reason`` Postgres enum type is created by the migration; this
model sets ``create_type=False`` so SQLAlchemy never tries to issue a duplicate
CREATE TYPE. The ``ShiftEndReason`` enum itself lives in
``app/domain/shift_session.py`` so the domain owns the literals.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base
from app.domain.shift_session import ShiftEndReason


class ShiftSessionModel(Base):
    __tablename__ = "shift_sessions"

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    user_email: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_keycloak_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    shift_start_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
    )
    shift_end_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    tablet_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    end_reason: Mapped[ShiftEndReason | None] = mapped_column(
        # See qr.py / audit.py: values_callable sends the lowercase value
        # instead of the StrEnum name so binds match the Postgres enum literals.
        sa.Enum(
            ShiftEndReason,
            name="shift_end_reason",
            create_type=False,
            native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )

    __table_args__ = (
        sa.CheckConstraint(
            "(shift_end_at IS NULL AND end_reason IS NULL)"
            " OR (shift_end_at IS NOT NULL AND end_reason IS NOT NULL)",
            name="shift_end_consistency",
        ),
        sa.Index(
            "shift_sessions_one_active_per_user",
            "user_keycloak_id",
            unique=True,
            postgresql_where=sa.text("shift_end_at IS NULL"),
        ),
    )
