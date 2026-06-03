"""Admin-dashboard snapshot domain type — pure Python, no SQLAlchemy or Pydantic.

Returned by ``DashboardRepository.snapshot()`` and re-shaped at the API boundary
into ``DashboardSnapshotResponse``. Same pattern as ``app.domain.qr.QR``.

Counters are populated from a single round-trip SELECT with six scalar
subqueries — see the repo for the query shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """Aggregated counts surfaced on the admin dashboard.

    All counts are non-negative integers (DB-level ``COUNT()``). ``generated_at``
    is the wall-clock used by the repo to compute the 30-day / 24-hour windows
    — surfaced to clients so the dashboard can show "as of {{ generated_at }}".
    """

    qr_free_count: int
    qr_bound_count: int
    qr_retired_count: int
    batches_last_30_days: int
    active_shifts_count: int
    audit_rows_last_24h: int
    generated_at: datetime
