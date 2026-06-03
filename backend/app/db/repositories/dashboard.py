"""Admin-dashboard counters repository (Sprint 8b Task 1).

A single ``snapshot()`` method that returns six aggregations in one round-trip
via a SELECT with six scalar subqueries — no UNION-ALL gymnastics, no
``COUNT(*)`` round-trips per metric, no Python-side counting. The 30-day and
24-hour windows are computed in Python from an injected ``now`` so tests can
pin time deterministically.

The endpoint at ``/api/v1/admin/dashboard`` is gated on ``dcinv-admin`` +
active shift and produces NO audit row (operational read, parallels
``/admin/sessions`` — Sprint 7 decision 8).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLogModel
from app.db.models.qr import QRBatchModel, QRCodeModel
from app.db.models.shift_session import ShiftSessionModel
from app.domain.dashboard import DashboardSnapshot
from app.domain.qr import QRStatus


class DashboardRepository:
    """Repository for the admin dashboard's aggregated counters.

    No writes, no audit-row coupling. The caller owns the session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def snapshot(self, *, now: datetime) -> DashboardSnapshot:
        """Read six counters in a single round-trip.

        ``now`` is injected so the 30-day and 24-hour cutoffs are
        deterministic in tests. The repo does not call ``datetime.now`` itself.
        Returned snapshot's ``generated_at`` field echoes the injected ``now``.
        """
        cutoff_30d = now - timedelta(days=30)
        cutoff_24h = now - timedelta(hours=24)

        qr_free = (
            select(func.count())
            .select_from(QRCodeModel)
            .where(QRCodeModel.status == QRStatus.FREE)
            .scalar_subquery()
        )
        qr_bound = (
            select(func.count())
            .select_from(QRCodeModel)
            .where(QRCodeModel.status == QRStatus.BOUND)
            .scalar_subquery()
        )
        qr_retired = (
            select(func.count())
            .select_from(QRCodeModel)
            .where(QRCodeModel.status == QRStatus.RETIRED)
            .scalar_subquery()
        )
        batches_30d = (
            select(func.count())
            .select_from(QRBatchModel)
            .where(QRBatchModel.created_at >= cutoff_30d)
            .scalar_subquery()
        )
        active_shifts = (
            select(func.count())
            .select_from(ShiftSessionModel)
            .where(ShiftSessionModel.shift_end_at.is_(None))
            .scalar_subquery()
        )
        audit_24h = (
            select(func.count())
            .select_from(AuditLogModel)
            .where(AuditLogModel.timestamp >= cutoff_24h)
            .scalar_subquery()
        )

        stmt = select(
            qr_free.label("qr_free"),
            qr_bound.label("qr_bound"),
            qr_retired.label("qr_retired"),
            batches_30d.label("batches_30d"),
            active_shifts.label("active_shifts"),
            audit_24h.label("audit_24h"),
        )
        row = (await self.session.execute(stmt)).one()
        return DashboardSnapshot(
            qr_free_count=row.qr_free,
            qr_bound_count=row.qr_bound,
            qr_retired_count=row.qr_retired,
            batches_last_30_days=row.batches_30d,
            active_shifts_count=row.active_shifts,
            audit_rows_last_24h=row.audit_24h,
            generated_at=now,
        )
