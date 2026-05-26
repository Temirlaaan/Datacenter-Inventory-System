"""SQLAlchemy models for the QR registry tables (``qr_batches``, ``qr_codes``).

The CHECK constraint and partial unique index defined in ``__table_args__``
mirror what the Alembic migration creates. Repeating them here keeps
``Base.metadata`` aligned with DB reality, which any future autogenerate run
relies on.

The ``qr_status`` Postgres enum type is created by the migration. Models set
``create_type=False`` so SQLAlchemy never tries to issue a duplicate CREATE TYPE.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base
from app.domain.qr import QRStatus


class QRBatchModel(Base):
    __tablename__ = "qr_batches"

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
    )
    created_by_email: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_by_keycloak_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=False
    )
    count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    intended_site_id: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    intended_location_id: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    intended_rack_id: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    comment: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class QRCodeModel(Base):
    __tablename__ = "qr_codes"

    id: Mapped[str] = mapped_column(sa.String(13), primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("qr_batches.id"),
        nullable=False,
    )
    status: Mapped[QRStatus] = mapped_column(
        # values_callable sends the StrEnum *value* (lowercase) to Postgres
        # instead of the default *name* (uppercase) — required so binds match
        # the qr_status enum literals defined in the migration.
        sa.Enum(
            QRStatus,
            name="qr_status",
            create_type=False,
            native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    bound_to_device_id: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    bound_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    bound_by_email: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    retired_reason: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)

    __table_args__ = (
        sa.CheckConstraint(
            "(status = 'free'    AND bound_to_device_id IS NULL     AND retired_at IS NULL)"
            " OR (status = 'bound'   AND bound_to_device_id IS NOT NULL AND retired_at IS NULL)"
            " OR (status = 'retired' AND retired_at IS NOT NULL)",
            name="qr_state_consistency",
        ),
        sa.Index(
            "qr_one_per_device",
            "bound_to_device_id",
            unique=True,
            postgresql_where=sa.text("status = 'bound'"),
        ),
        sa.Index("qr_codes_batch_id_idx", "batch_id"),
    )
