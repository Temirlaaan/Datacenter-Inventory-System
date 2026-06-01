"""End-to-end integration test for the auto-end stale-shifts job.

Verifies _run_iteration against a real DB: seeds active + stale + already-
ended rows, runs one iteration with a fixed clock, asserts only the stale
active row ends up with end_reason='auto_timeout'.

The loop-level guardrails (cancellation, per-iteration try/except, status
updates) are exercised in tests/unit/services/test_auto_end_job.py — there's
no benefit to retesting them against the real DB.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.db.repositories.shift_session import ShiftSessionRepository
from app.db.session import get_engine, get_sessionmaker
from app.domain.shift_session import ShiftEndReason, ShiftSession
from app.services.auto_end_job import _run_iteration

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]


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


_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_USER_A = UUID("11111111-1111-1111-1111-111111111111")
_USER_B = UUID("22222222-2222-2222-2222-222222222222")
_USER_C = UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture(autouse=True)
async def _truncate_shift_sessions() -> AsyncGenerator[None, None]:
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE shift_sessions CASCADE"))
        await session.commit()
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


def _active(*, user: UUID, hours_ago: int, tablet_id: str) -> ShiftSession:
    return ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=user,
        shift_start_at=_NOW - timedelta(hours=hours_ago),
        shift_end_at=None,
        tablet_id=tablet_id,
        end_reason=None,
    )


async def test_run_iteration_ends_only_stale_active_rows_in_real_db() -> None:
    stale = _active(user=_USER_A, hours_ago=20, tablet_id="t1")
    fresh = _active(user=_USER_B, hours_ago=1, tablet_id="t2")
    already_ended = _active(user=_USER_C, hours_ago=20, tablet_id="t3").end(
        reason=ShiftEndReason.MANUAL, at=_NOW - timedelta(hours=18)
    )
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        await repo.insert(stale)
        await repo.insert(fresh)
        await repo.insert(already_ended)
        await db.commit()

    count = await _run_iteration(
        sessionmaker=get_sessionmaker(),
        threshold_hours=12,
        now=_NOW,
    )

    assert count == 1
    # Verify the persisted state.
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        stale_after = await repo.get_by_id(stale.id)
        fresh_after = await repo.get_by_id(fresh.id)
        ended_after = await repo.get_by_id(already_ended.id)
    assert stale_after is not None
    assert stale_after.is_active is False
    assert stale_after.end_reason is ShiftEndReason.AUTO_TIMEOUT
    assert fresh_after is not None
    assert fresh_after.is_active is True  # untouched
    assert ended_after is not None
    assert ended_after.end_reason is ShiftEndReason.MANUAL  # untouched


async def test_run_iteration_returns_zero_when_no_stale_rows() -> None:
    fresh = _active(user=_USER_A, hours_ago=1, tablet_id="t1")
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(fresh)
        await db.commit()

    count = await _run_iteration(
        sessionmaker=get_sessionmaker(),
        threshold_hours=12,
        now=_NOW,
    )

    assert count == 0
