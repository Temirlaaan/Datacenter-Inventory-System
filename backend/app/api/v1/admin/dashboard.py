"""Admin-dashboard counters endpoint (Sprint 8b Task 1).

``GET /api/v1/admin/dashboard`` — six aggregations in one round-trip via
``DashboardRepository.snapshot``. Role ``dcinv-admin`` + active shift.

Produces NO audit row: counters are an operational read, parallel to
``GET /admin/sessions`` (Sprint 7 decision 8). The web dashboard at ``/web/``
calls the repository directly through FastAPI dep injection rather than via
HTTP, so the rate-limit middleware bypass for ``/web/*`` is not bypassing
two separate budgets.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser, require_role_with_active_shift
from app.db.repositories.dashboard import DashboardRepository
from app.db.session import get_session
from app.domain.dashboard import DashboardSnapshot

router = APIRouter()


class DashboardSnapshotResponse(BaseModel):
    """Wire shape of the dashboard snapshot.

    Mirrors :class:`DashboardSnapshot` field-for-field so the mobile/web client
    just deserialises into the same shape the repo returns.
    """

    qr_free_count: int
    qr_bound_count: int
    qr_retired_count: int
    batches_last_30_days: int
    active_shifts_count: int
    audit_rows_last_24h: int
    generated_at: datetime


def _to_response(snap: DashboardSnapshot) -> DashboardSnapshotResponse:
    return DashboardSnapshotResponse(
        qr_free_count=snap.qr_free_count,
        qr_bound_count=snap.qr_bound_count,
        qr_retired_count=snap.qr_retired_count,
        batches_last_30_days=snap.batches_last_30_days,
        active_shifts_count=snap.active_shifts_count,
        audit_rows_last_24h=snap.audit_rows_last_24h,
        generated_at=snap.generated_at,
    )


@router.get(
    "",
    response_model=DashboardSnapshotResponse,
    description=(
        "Aggregated counters for the admin dashboard: QR counts per status,"
        " batches created in the last 30 days, currently-active shifts, and"
        " audit_log rows from the last 24 hours. Single round-trip; no audit"
        " row (operational read)."
    ),
)
async def get_dashboard(
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-admin")),
    session: AsyncSession = Depends(get_session),
) -> DashboardSnapshotResponse:
    """Return the dashboard snapshot. Role-gating side effect only — ``user``
    isn't recorded since this read isn't audited."""
    _ = user  # gate side effect only
    snap = await DashboardRepository(session).snapshot(now=datetime.now(UTC))
    return _to_response(snap)
