"""Auto-end stale-shifts background job (Sprint 7 Task 1, ToR §4.1.3 fallback).

Scans ``shift_sessions`` for active rows older than
``SHIFT_AUTO_END_THRESHOLD_HOURS`` and ends each with
``end_reason='auto_timeout'``. The 10-minute idle case is mobile-owned
(Sprint 6 decision E); this is the safety net for crashed tablets that never
sent ``/sessions/end``.

Decision A of docs/sprint-7.md:

- **Per-iteration try/except**: a failing tick logs ERROR + ``exc_info`` and
  the loop continues. A single bad tick does NOT kill the loop or the app.
- **Cancellation via ``asyncio.Event``**: checked before each iteration AND
  inside the sleep via ``asyncio.wait_for(event.wait(), timeout=...)``.
  Shutdown drains in ≤ 1s regardless of where the loop is sleeping.
- **Multi-replica caveat** — see docs/work-log.md Sprint 7 entry: the backend
  MUST run as single-replica until job ownership is solved (Postgres
  advisory lock OR k8s CronJob, Sprint 8a). N replicas waste Nx DB scans
  even though the partial unique index + idempotent ``auto_timeout``
  prevent the worst-case double-firing outcome.
- **Status reporting on ``/health``** via :class:`AutoEndJobStatus`: a
  silently-dead loop surfaces as ``status='stale'`` instead of vanishing.

Per-row failure handling: ``end_by_id`` runs in its own transaction
(``ShiftSessionService`` opens ``async with self._session.begin()``).
A row that vanishes mid-iteration (deleted, or concurrently ended by a
mobile ``/sessions/end``) raises ``ShiftSessionNotFound`` or
``IllegalShiftTransition``; both are swallowed per-row and the loop
continues — the next iteration will pick up whatever is still stale.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.shift_session import ShiftSessionRepository
from app.domain.shift_session import IllegalShiftTransition, ShiftEndReason
from app.services.shift_session import ShiftSessionNotFound, ShiftSessionService

logger = structlog.get_logger()

HealthStatus = Literal["healthy", "stale"]


@dataclass
class AutoEndJobStatus:
    """Mutable status object read by ``/health`` to expose loop liveness.

    Created unconditionally in the FastAPI lifespan (even when
    ``shift_auto_end_enabled=False``) so ``/health``'s response shape is
    consistent regardless of configuration. ``last_iteration_at`` is updated
    only on a SUCCESSFUL iteration — a loop that ticks but exception-spams
    correctly flips to ``"stale"`` after the threshold, surfacing in
    monitoring even though the loop itself is alive.
    """

    enabled: bool
    last_iteration_at: datetime | None = None

    def health_status(self, *, now: datetime, interval_seconds: int) -> HealthStatus:
        if not self.enabled:
            return "healthy"
        if self.last_iteration_at is None:
            return "healthy"
        elapsed = (now - self.last_iteration_at).total_seconds()
        return "healthy" if elapsed < 3 * interval_seconds else "stale"


async def _run_iteration(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    threshold_hours: int,
    now: datetime,
) -> int:
    """One auto-end pass. Returns the count of rows successfully ended."""
    older_than = now - timedelta(hours=threshold_hours)
    async with sessionmaker() as session:
        repo = ShiftSessionRepository(session)
        stale = await repo.find_stale_active(older_than=older_than)
    if not stale:
        return 0
    ended_count = 0
    for shift in stale:
        # Per-row session/transaction. A failed end_by_id on one row does NOT
        # abort the iteration — log + continue. Vanished rows (concurrently
        # ended by /sessions/end) raise the two expected exceptions; they're
        # idempotent no-ops here.
        async with sessionmaker() as per_row_session:
            per_row_service = ShiftSessionService(
                session=per_row_session,
                repo=ShiftSessionRepository(per_row_session),
            )
            try:
                await per_row_service.end_by_id(
                    session_id=shift.id, reason=ShiftEndReason.AUTO_TIMEOUT
                )
                ended_count += 1
            except (ShiftSessionNotFound, IllegalShiftTransition):
                logger.info(
                    "auto_end_job_row_vanished",
                    session_id=str(shift.id),
                    user_keycloak_id=str(shift.user_keycloak_id),
                )
            except Exception:
                logger.error(
                    "auto_end_job_row_failed",
                    session_id=str(shift.id),
                    user_keycloak_id=str(shift.user_keycloak_id),
                    exc_info=True,
                )
    return ended_count


async def _wait_or_cancel(cancel_event: asyncio.Event, wait_seconds: float) -> bool:
    """Wait up to ``wait_seconds`` for cancellation. Returns True if cancelled."""
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=wait_seconds)
        return True
    except TimeoutError:
        return False


async def auto_end_loop(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    status: AutoEndJobStatus,
    cancel_event: asyncio.Event,
    interval_seconds: float,
    threshold_hours: int,
    initial_grace_seconds: float = 60.0,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Main background loop. Starts with a grace period, then iterates forever.

    ``clock`` is injectable so tests don't need to monkeypatch the module.
    ``interval_seconds`` and ``initial_grace_seconds`` accept floats so tests
    can use sub-second values.
    """
    logger.info(
        "auto_end_job_started",
        interval_seconds=interval_seconds,
        threshold_hours=threshold_hours,
        initial_grace_seconds=initial_grace_seconds,
    )
    if await _wait_or_cancel(cancel_event, initial_grace_seconds):
        logger.info("auto_end_job_cancelled_during_grace")
        return

    while not cancel_event.is_set():
        try:
            ended = await _run_iteration(
                sessionmaker=sessionmaker,
                threshold_hours=threshold_hours,
                now=clock(),
            )
            status.last_iteration_at = clock()
            logger.info("auto_end_job_iteration", ended_count=ended)
        except Exception:
            logger.error("auto_end_job_iteration_failed", exc_info=True)
        if await _wait_or_cancel(cancel_event, interval_seconds):
            logger.info("auto_end_job_cancelled")
            return


__all__ = [
    "AutoEndJobStatus",
    "HealthStatus",
    "_run_iteration",
    "_wait_or_cancel",
    "auto_end_loop",
]
