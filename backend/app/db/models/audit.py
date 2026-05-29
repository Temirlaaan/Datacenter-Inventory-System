"""SQLAlchemy model for the ``audit_log`` table.

All 12 columns from ToR §7.2.3 are present even though Sprint 2 only writes
``qr.generate_batch`` rows. ``session_id`` is nullable until ``shift_sessions``
lands in a later sprint.

The ``audit_result`` Postgres enum type is created by the migration; this model
sets ``create_type=False`` to avoid a duplicate CREATE TYPE. The ``AuditResult``
enum itself lives in ``app/domain/audit.py`` so the domain owns the literals.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base
from app.domain.audit import AuditResult


class AuditLogModel(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
    )
    user_email: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    user_keycloak_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    # Nullable until shift_sessions lands.
    session_id: Mapped[UUID | None] = mapped_column(postgresql.UUID(as_uuid=True), nullable=True)
    operation: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    before_json: Mapped[dict] = mapped_column(
        postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    after_json: Mapped[dict] = mapped_column(
        postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    result: Mapped[AuditResult] = mapped_column(
        # See qr.py: values_callable sends the lowercase value, not the
        # uppercase StrEnum name, so binds match the audit_result literals.
        sa.Enum(
            AuditResult,
            name="audit_result",
            create_type=False,
            native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )

    __table_args__ = (
        sa.Index("audit_log_timestamp_idx", sa.text("timestamp DESC")),
        sa.Index("audit_log_entity_idx", "entity_type", "entity_id"),
    )
