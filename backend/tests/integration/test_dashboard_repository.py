"""Integration tests for ``DashboardRepository.snapshot`` (Sprint 8b Task 1).

Exercises the single-round-trip SELECT against real Postgres: empty DB,
mixed QR statuses, 30-day and 24-hour cutoff boundaries, active vs ended
shifts, and the one-round-trip guarantee (counted via SQLAlchemy
``after_cursor_execute`` events).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.dashboard import DashboardRepository
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.repositories.shift_session import ShiftSessionRepository
from app.db.session import get_engine, get_sessionmaker
from app.domain.audit import AuditLogEntry, AuditResult
from app.domain.qr import QR, QRBatch, QRStatus
from app.domain.shift_session import ShiftEndReason, ShiftSession

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_USER_A = UUID("11111111-1111-1111-1111-111111111111")
_USER_B = UUID("22222222-2222-2222-2222-222222222222")


def _alembic(*args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=_BACKEND_DIR,
        timeout=30,
    )
    assert (
        result.returncode == 0
    ), f"alembic {args!r} failed: stdout={result.stdout!r} stderr={result.stderr!r}"


@pytest.fixture(scope="module", autouse=True)
def _clean_schema() -> Generator[None, None, None]:
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    _alembic("downgrade", "base")


@pytest.fixture(autouse=True)
async def _truncate() -> AsyncGenerator[None, None]:
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(
            text("TRUNCATE qr_codes, qr_batches, shift_sessions, audit_log CASCADE")
        )
        await session.commit()
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


async def _seed_batch(session: AsyncSession, *, created_at: datetime) -> UUID:
    """Insert a single qr_batches row at the given created_at; returns its id."""
    batch_id = uuid4()
    batch = QRBatch(
        id=batch_id,
        created_at=created_at,
        created_by_email="alice@example.com",
        created_by_keycloak_id=_USER_A,
        count=0,
        intended_site_id=None,
        intended_location_id=None,
        intended_rack_id=None,
        comment=None,
    )
    await QRBatchRepository(session).insert(batch)
    return batch_id


async def _seed_qrs(
    session: AsyncSession,
    *,
    batch_id: UUID,
    free: int = 0,
    bound: int = 0,
    retired: int = 0,
) -> None:
    qrs: list[QR] = []
    index = 0
    for _ in range(free):
        qrs.append(
            QR(
                id=f"DCQR-F{index:04d}",
                batch_id=batch_id,
                status=QRStatus.FREE,
                bound_to_device_id=None,
                bound_at=None,
                bound_by_email=None,
                retired_at=None,
                retired_reason=None,
            )
        )
        index += 1
    for i in range(bound):
        qrs.append(
            QR(
                id=f"DCQR-B{i:04d}",
                batch_id=batch_id,
                # bound_to_device_id must be unique (partial unique index) so
                # each test bound row gets a deterministic, distinct id.
                status=QRStatus.BOUND,
                bound_to_device_id=1000 + i,
                bound_at=_NOW - timedelta(hours=1),
                bound_by_email="alice@example.com",
                retired_at=None,
                retired_reason=None,
            )
        )
    for i in range(retired):
        qrs.append(
            QR(
                id=f"DCQR-R{i:04d}",
                batch_id=batch_id,
                status=QRStatus.RETIRED,
                bound_to_device_id=None,
                bound_at=None,
                bound_by_email=None,
                retired_at=_NOW - timedelta(hours=2),
                retired_reason="lost",
            )
        )
    await QRCodeRepository(session).bulk_insert(qrs)


async def _seed_shift(
    session: AsyncSession,
    *,
    user_keycloak_id: UUID,
    active: bool,
) -> None:
    shift_id = uuid4()
    shift = ShiftSession(
        id=shift_id,
        user_email="alice@example.com",
        user_keycloak_id=user_keycloak_id,
        shift_start_at=_NOW - timedelta(hours=3),
        shift_end_at=None if active else _NOW - timedelta(hours=1),
        tablet_id="test-tablet",
        end_reason=None if active else ShiftEndReason.MANUAL,
    )
    await ShiftSessionRepository(session).insert(shift)


async def _seed_audit(session: AsyncSession, *, timestamp: datetime) -> None:
    await AuditLogRepository(session).insert(
        AuditLogEntry(
            request_id=uuid4(),
            timestamp=timestamp,
            user_email="alice@example.com",
            user_keycloak_id=_USER_A,
            session_id=None,
            operation="qr.bind",
            entity_type="qr",
            entity_id="DCQR-X",
            before_json={},
            after_json={},
            result=AuditResult.SUCCESS,
        )
    )


# ---------- counters ---------------------------------------------------------


async def test_snapshot_returns_all_zeros_on_empty_db() -> None:
    async with get_sessionmaker()() as session:
        snap = await DashboardRepository(session).snapshot(now=_NOW)
    assert snap.qr_free_count == 0
    assert snap.qr_bound_count == 0
    assert snap.qr_retired_count == 0
    assert snap.batches_last_30_days == 0
    assert snap.active_shifts_count == 0
    assert snap.audit_rows_last_24h == 0
    assert snap.generated_at == _NOW


async def test_snapshot_counts_qr_codes_per_status() -> None:
    async with get_sessionmaker()() as session:
        batch_id = await _seed_batch(session, created_at=_NOW - timedelta(days=1))
        await _seed_qrs(session, batch_id=batch_id, free=3, bound=2, retired=1)
        await session.commit()

    async with get_sessionmaker()() as session:
        snap = await DashboardRepository(session).snapshot(now=_NOW)
    assert snap.qr_free_count == 3
    assert snap.qr_bound_count == 2
    assert snap.qr_retired_count == 1


async def test_snapshot_counts_batches_within_last_30_days_inclusive() -> None:
    """Lower bound is closed: a batch at exactly ``now - 30 days`` IS counted."""
    async with get_sessionmaker()() as session:
        await _seed_batch(session, created_at=_NOW - timedelta(days=30))  # boundary
        await _seed_batch(session, created_at=_NOW - timedelta(days=5))  # inside
        await _seed_batch(session, created_at=_NOW - timedelta(days=31))  # outside
        await session.commit()

    async with get_sessionmaker()() as session:
        snap = await DashboardRepository(session).snapshot(now=_NOW)
    assert snap.batches_last_30_days == 2


async def test_snapshot_excludes_batches_older_than_30_days() -> None:
    async with get_sessionmaker()() as session:
        await _seed_batch(session, created_at=_NOW - timedelta(days=90))
        await session.commit()

    async with get_sessionmaker()() as session:
        snap = await DashboardRepository(session).snapshot(now=_NOW)
    assert snap.batches_last_30_days == 0


async def test_snapshot_counts_only_active_shifts() -> None:
    """Active = ``shift_end_at IS NULL`` (matches the partial unique index)."""
    async with get_sessionmaker()() as session:
        await _seed_shift(session, user_keycloak_id=_USER_A, active=True)
        await _seed_shift(session, user_keycloak_id=_USER_B, active=False)
        await session.commit()

    async with get_sessionmaker()() as session:
        snap = await DashboardRepository(session).snapshot(now=_NOW)
    assert snap.active_shifts_count == 1


async def test_snapshot_counts_audit_rows_within_last_24h_inclusive() -> None:
    """Lower bound is closed: a row at exactly ``now - 24 hours`` IS counted."""
    async with get_sessionmaker()() as session:
        await _seed_audit(session, timestamp=_NOW - timedelta(hours=24))  # boundary
        await _seed_audit(session, timestamp=_NOW - timedelta(hours=2))  # inside
        await _seed_audit(session, timestamp=_NOW - timedelta(hours=25))  # outside
        await session.commit()

    async with get_sessionmaker()() as session:
        snap = await DashboardRepository(session).snapshot(now=_NOW)
    assert snap.audit_rows_last_24h == 2


async def test_snapshot_excludes_audit_rows_older_than_24h() -> None:
    async with get_sessionmaker()() as session:
        await _seed_audit(session, timestamp=_NOW - timedelta(days=3))
        await session.commit()

    async with get_sessionmaker()() as session:
        snap = await DashboardRepository(session).snapshot(now=_NOW)
    assert snap.audit_rows_last_24h == 0


async def test_snapshot_generated_at_echoes_injected_now() -> None:
    custom_now = datetime(2030, 1, 1, 0, 0, 0, tzinfo=UTC)
    async with get_sessionmaker()() as session:
        snap = await DashboardRepository(session).snapshot(now=custom_now)
    assert snap.generated_at == custom_now


# ---------- one-round-trip guarantee -----------------------------------------


async def test_snapshot_is_single_round_trip() -> None:
    """The repo must issue exactly one cursor execute, no N+1.

    Counts via SQLAlchemy's ``after_cursor_execute`` event on the underlying
    sync engine — same hook the rest of the integration suite uses to verify
    atomic-write paths.
    """
    engine = get_engine()
    executes: list[str] = []

    def _on_execute(
        _conn: object,
        _cursor: object,
        statement: str,
        _params: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        # Filter out connection-setup pings + SAVEPOINT bookkeeping; count only
        # SELECTs that the repo issues.
        if statement.lstrip().upper().startswith("SELECT"):
            executes.append(statement)

    event.listen(engine.sync_engine, "after_cursor_execute", _on_execute)
    try:
        async with get_sessionmaker()() as session:
            await DashboardRepository(session).snapshot(now=_NOW)
    finally:
        event.remove(engine.sync_engine, "after_cursor_execute", _on_execute)

    assert len(executes) == 1, f"expected 1 SELECT, got {len(executes)}: {executes!r}"
