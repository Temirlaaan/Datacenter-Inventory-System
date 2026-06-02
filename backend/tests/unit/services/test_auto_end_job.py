"""Unit tests for app.services.auto_end_job.

Strategy mirrors test_shift_session.py: stub the sessionmaker + repo + service
layers, no live DB. End-to-end behaviour against a real DB is covered by
tests/integration/test_auto_end_job.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.shift_session import ShiftSessionRepository
from app.domain.shift_session import IllegalShiftTransition, ShiftEndReason, ShiftSession
from app.services.auto_end_job import (
    AutoEndJobStatus,
    _run_iteration,
    _wait_or_cancel,
    auto_end_loop,
)
from app.services.shift_session import ShiftSessionNotFound

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_USER_A = UUID("11111111-1111-1111-1111-111111111111")
_USER_B = UUID("22222222-2222-2222-2222-222222222222")


def _stale_shift(*, user: UUID = _USER_A, hours_ago: int = 13) -> ShiftSession:
    return ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=user,
        shift_start_at=_NOW - timedelta(hours=hours_ago),
        shift_end_at=None,
        tablet_id="tablet-01",
        end_reason=None,
    )


# ---------- AutoEndJobStatus.health_status ----------


def test_health_status_returns_healthy_when_disabled() -> None:
    status = AutoEndJobStatus(enabled=False)
    assert status.health_status(now=_NOW, interval_seconds=300) == "healthy"


def test_health_status_returns_healthy_when_enabled_but_never_run() -> None:
    status = AutoEndJobStatus(enabled=True)
    assert status.health_status(now=_NOW, interval_seconds=300) == "healthy"


def test_health_status_returns_healthy_when_last_iteration_within_3x_interval() -> None:
    status = AutoEndJobStatus(enabled=True, last_iteration_at=_NOW - timedelta(seconds=600))
    assert status.health_status(now=_NOW, interval_seconds=300) == "healthy"


def test_health_status_returns_stale_when_last_iteration_at_or_beyond_3x_interval() -> None:
    status = AutoEndJobStatus(enabled=True, last_iteration_at=_NOW - timedelta(seconds=901))
    assert status.health_status(now=_NOW, interval_seconds=300) == "stale"


def test_health_status_boundary_at_exactly_3x_interval_is_stale() -> None:
    # elapsed == 3 * interval — operator-side rule chose ">= 3x" = stale.
    status = AutoEndJobStatus(enabled=True, last_iteration_at=_NOW - timedelta(seconds=900))
    assert status.health_status(now=_NOW, interval_seconds=300) == "stale"


def test_health_status_disabled_ignores_stale_last_iteration_at() -> None:
    # Operator disabled the job → "healthy" wins even if there's an old timestamp.
    status = AutoEndJobStatus(enabled=False, last_iteration_at=_NOW - timedelta(days=7))
    assert status.health_status(now=_NOW, interval_seconds=300) == "healthy"


# ---------- _wait_or_cancel ----------


async def test_wait_or_cancel_returns_false_when_timeout_elapses() -> None:
    event = asyncio.Event()
    cancelled = await _wait_or_cancel(event, wait_seconds=0.01)
    assert cancelled is False


async def test_wait_or_cancel_returns_true_when_event_set_during_wait() -> None:
    event = asyncio.Event()

    async def _setter() -> None:
        await asyncio.sleep(0.005)
        event.set()

    setter = asyncio.create_task(_setter())
    cancelled = await _wait_or_cancel(event, wait_seconds=1.0)
    await setter
    assert cancelled is True


# ---------- _run_iteration ----------


@dataclass
class _FakeRepo:
    """Stub ShiftSessionRepository — find_stale_active returns canned shifts."""

    stale_to_return: list[ShiftSession] = field(default_factory=list)
    find_stale_active_calls: list[datetime] = field(default_factory=list)
    end_calls: list[UUID] = field(default_factory=list)
    end_raises: dict[UUID, Exception] = field(default_factory=dict)

    # ShiftSessionRepository methods used by _run_iteration -------------------

    async def find_stale_active(self, *, older_than: datetime) -> list[ShiftSession]:
        self.find_stale_active_calls.append(older_than)
        return list(self.stale_to_return)

    async def get_by_id(self, session_id: UUID) -> ShiftSession | None:
        for s in self.stale_to_return:
            if s.id == session_id:
                return s
        return None

    async def update(self, shift: ShiftSession) -> None:
        if shift.id in self.end_raises:
            raise self.end_raises[shift.id]
        self.end_calls.append(shift.id)


class _FakeTx:
    """Minimal async context manager — no-op transaction wrapper for the service."""

    async def __aenter__(self) -> _FakeTx:
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        return False


class _FakeAdvisoryLockResult:
    """Stand-in for the ``execute(...)`` result of pg_try_advisory_lock /
    pg_advisory_unlock — returns True (lock acquired) so existing tests see
    the work proceed as before."""

    def scalar_one(self) -> bool:
        return True


class _FakeAsyncSession:
    """Stand-in for AsyncSession used inside _run_iteration's per-row tx.

    Implements ``execute(...)`` so the Sprint 8a Task 1 advisory-lock
    SELECT statements parse cleanly; always reports acquired=True so the
    lock is invisible to tests that focus on the per-row work."""

    def in_transaction(self) -> bool:
        return False

    def begin(self) -> _FakeTx:
        return _FakeTx()

    async def execute(self, *_args: object, **_kwargs: object) -> _FakeAdvisoryLockResult:
        return _FakeAdvisoryLockResult()


def _fake_sessionmaker(repo: _FakeRepo) -> async_sessionmaker[AsyncSession]:
    """Build a sessionmaker-shaped callable that yields a fake AsyncSession.

    The cast at the boundary lets _run_iteration treat our fake as the real
    type without monkeypatching SQLAlchemy. ShiftSessionRepository
    instantiation inside _run_iteration is monkeypatched separately by the
    repo-injection fixture.
    """

    @asynccontextmanager
    async def _cm() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, _FakeAsyncSession())

    def _factory() -> object:
        return _cm()

    return cast("async_sessionmaker[AsyncSession]", _factory)


@pytest.fixture
def patched_repo_and_service(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[_FakeRepo], None]:
    """Patch ShiftSessionRepository + ShiftSessionService inside the job module
    so _run_iteration uses a fake repo and a service that delegates back to
    the same fake (via repo.update + repo.get_by_id)."""

    def _install(repo: _FakeRepo) -> None:
        monkeypatch.setattr(
            "app.services.auto_end_job.ShiftSessionRepository", lambda session: repo
        )

        class _StubService:
            def __init__(
                self,
                *,
                session: AsyncSession,
                repo: ShiftSessionRepository,
            ) -> None:
                self._repo = repo

            async def end_by_id(self, *, session_id: UUID, reason: ShiftEndReason) -> ShiftSession:
                # mirror real service: get + update (or raise the right exception)
                existing = await self._repo.get_by_id(session_id)
                if existing is None:
                    raise ShiftSessionNotFound(session_id)
                if not existing.is_active:
                    raise IllegalShiftTransition("already ended")
                ended = existing.end(reason=reason, at=_NOW)
                await self._repo.update(ended)
                return ended

        monkeypatch.setattr("app.services.auto_end_job.ShiftSessionService", _StubService)

    return _install


async def test_run_iteration_returns_zero_when_no_stale_shifts(
    patched_repo_and_service: Callable[[_FakeRepo], None],
) -> None:
    repo = _FakeRepo(stale_to_return=[])
    patched_repo_and_service(repo)
    sessionmaker = _fake_sessionmaker(repo)

    count = await _run_iteration(sessionmaker=sessionmaker, threshold_hours=12, now=_NOW)

    assert count == 0
    assert repo.find_stale_active_calls == [_NOW - timedelta(hours=12)]
    assert repo.end_calls == []


async def test_run_iteration_ends_each_stale_row_with_auto_timeout(
    patched_repo_and_service: Callable[[_FakeRepo], None],
) -> None:
    stale_a = _stale_shift(user=_USER_A, hours_ago=20)
    stale_b = _stale_shift(user=_USER_B, hours_ago=13)
    repo = _FakeRepo(stale_to_return=[stale_a, stale_b])
    patched_repo_and_service(repo)
    sessionmaker = _fake_sessionmaker(repo)

    count = await _run_iteration(sessionmaker=sessionmaker, threshold_hours=12, now=_NOW)

    assert count == 2
    assert sorted(repo.end_calls) == sorted([stale_a.id, stale_b.id])


async def test_run_iteration_swallows_vanished_row_and_continues(
    patched_repo_and_service: Callable[[_FakeRepo], None],
) -> None:
    """A row deleted/ended between scan and end raises an expected exception;
    the iteration must NOT abort the remaining rows."""
    stale_a = _stale_shift(user=_USER_A, hours_ago=20)
    stale_b = _stale_shift(user=_USER_B, hours_ago=13)
    # ``update`` for stale_a fails — simulates a "vanished" row pattern.
    repo = _FakeRepo(
        stale_to_return=[stale_a, stale_b],
        end_raises={stale_a.id: ShiftSessionNotFound(stale_a.id)},
    )
    patched_repo_and_service(repo)
    sessionmaker = _fake_sessionmaker(repo)

    count = await _run_iteration(sessionmaker=sessionmaker, threshold_hours=12, now=_NOW)

    # stale_b still succeeded.
    assert count == 1
    assert repo.end_calls == [stale_b.id]


async def test_run_iteration_swallows_already_ended_and_continues(
    patched_repo_and_service: Callable[[_FakeRepo], None],
) -> None:
    stale_a = _stale_shift(user=_USER_A, hours_ago=20)
    stale_b = _stale_shift(user=_USER_B, hours_ago=13)
    repo = _FakeRepo(
        stale_to_return=[stale_a, stale_b],
        end_raises={stale_a.id: IllegalShiftTransition("already ended")},
    )
    patched_repo_and_service(repo)
    sessionmaker = _fake_sessionmaker(repo)

    count = await _run_iteration(sessionmaker=sessionmaker, threshold_hours=12, now=_NOW)

    assert count == 1
    assert repo.end_calls == [stale_b.id]


async def test_run_iteration_logs_unexpected_exception_per_row_and_continues(
    patched_repo_and_service: Callable[[_FakeRepo], None],
) -> None:
    stale_a = _stale_shift(user=_USER_A, hours_ago=20)
    stale_b = _stale_shift(user=_USER_B, hours_ago=13)
    repo = _FakeRepo(
        stale_to_return=[stale_a, stale_b],
        end_raises={stale_a.id: RuntimeError("DB hiccup")},
    )
    patched_repo_and_service(repo)
    sessionmaker = _fake_sessionmaker(repo)

    count = await _run_iteration(sessionmaker=sessionmaker, threshold_hours=12, now=_NOW)

    # stale_a failed and got logged; stale_b succeeded.
    assert count == 1
    assert repo.end_calls == [stale_b.id]


# ---------- auto_end_loop ----------


async def test_auto_end_loop_returns_during_grace_when_cancelled(
    patched_repo_and_service: Callable[[_FakeRepo], None],
) -> None:
    repo = _FakeRepo(stale_to_return=[])
    patched_repo_and_service(repo)
    sessionmaker = _fake_sessionmaker(repo)
    status = AutoEndJobStatus(enabled=True)
    cancel_event = asyncio.Event()

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.01)
        cancel_event.set()

    cancel_task = asyncio.create_task(_cancel_soon())
    await asyncio.wait_for(
        auto_end_loop(
            sessionmaker=sessionmaker,
            status=status,
            cancel_event=cancel_event,
            interval_seconds=1.0,
            threshold_hours=12,
            initial_grace_seconds=1.0,
        ),
        timeout=1.0,
    )
    await cancel_task

    # Loop exited during grace — no iteration ran.
    assert repo.find_stale_active_calls == []
    assert status.last_iteration_at is None


async def test_auto_end_loop_runs_iteration_and_updates_status(
    patched_repo_and_service: Callable[[_FakeRepo], None],
) -> None:
    repo = _FakeRepo(stale_to_return=[])
    patched_repo_and_service(repo)
    sessionmaker = _fake_sessionmaker(repo)
    status = AutoEndJobStatus(enabled=True)
    cancel_event = asyncio.Event()

    iteration_done = asyncio.Event()
    iteration_count = 0
    real_find = repo.find_stale_active

    async def _wrapped_find(*, older_than: datetime) -> list[ShiftSession]:
        nonlocal iteration_count
        result = await real_find(older_than=older_than)
        iteration_count += 1
        if iteration_count >= 1:
            iteration_done.set()
        return result

    repo.find_stale_active = _wrapped_find  # type: ignore[method-assign]

    async def _cancel_after_first_iteration() -> None:
        await iteration_done.wait()
        cancel_event.set()

    cancel_task = asyncio.create_task(_cancel_after_first_iteration())
    await asyncio.wait_for(
        auto_end_loop(
            sessionmaker=sessionmaker,
            status=status,
            cancel_event=cancel_event,
            interval_seconds=10.0,  # large; cancel via event before sleep wakes
            threshold_hours=12,
            initial_grace_seconds=0.0,
        ),
        timeout=1.0,
    )
    await cancel_task

    assert iteration_count >= 1
    assert status.last_iteration_at is not None


async def test_auto_end_loop_continues_after_failed_iteration(
    patched_repo_and_service: Callable[[_FakeRepo], None],
) -> None:
    """A bad tick (e.g., DB down) raises out of _run_iteration. The loop's
    per-iteration try/except must log + proceed without killing the loop.

    Status.last_iteration_at must NOT be updated on the failed tick (decision
    4 contract); only successful iterations bump it."""
    repo = _FakeRepo(stale_to_return=[])
    patched_repo_and_service(repo)
    sessionmaker = _fake_sessionmaker(repo)
    status = AutoEndJobStatus(enabled=True)
    cancel_event = asyncio.Event()

    call_count = 0
    second_iteration_done = asyncio.Event()
    real_find = repo.find_stale_active

    async def _flaky_find(*, older_than: datetime) -> list[ShiftSession]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated DB hiccup")
        result = await real_find(older_than=older_than)
        second_iteration_done.set()
        return result

    repo.find_stale_active = _flaky_find  # type: ignore[method-assign]

    async def _cancel_after_recovery() -> None:
        await second_iteration_done.wait()
        cancel_event.set()

    cancel_task = asyncio.create_task(_cancel_after_recovery())
    await asyncio.wait_for(
        auto_end_loop(
            sessionmaker=sessionmaker,
            status=status,
            cancel_event=cancel_event,
            interval_seconds=0.01,  # short; first sleep ticks fast
            threshold_hours=12,
            initial_grace_seconds=0.0,
        ),
        timeout=2.0,
    )
    await cancel_task

    assert call_count >= 2
    # Status was bumped on the successful second iteration.
    assert status.last_iteration_at is not None


async def test_auto_end_loop_failed_iteration_does_not_bump_status(
    patched_repo_and_service: Callable[[_FakeRepo], None],
) -> None:
    """A single failed iteration must not advance ``last_iteration_at``.

    We cancel after the first iteration completes — and since it always
    fails, ``last_iteration_at`` must stay ``None``.
    """
    repo = _FakeRepo(stale_to_return=[])
    patched_repo_and_service(repo)
    sessionmaker = _fake_sessionmaker(repo)
    status = AutoEndJobStatus(enabled=True)
    cancel_event = asyncio.Event()

    failure_observed = asyncio.Event()

    async def _always_fail(*, older_than: datetime) -> list[ShiftSession]:
        failure_observed.set()
        raise RuntimeError("permanently broken")

    repo.find_stale_active = _always_fail  # type: ignore[method-assign]

    async def _cancel_after_failure() -> None:
        await failure_observed.wait()
        # Wait a tick to let the except handler complete + reach the sleep
        await asyncio.sleep(0.005)
        cancel_event.set()

    cancel_task = asyncio.create_task(_cancel_after_failure())
    await asyncio.wait_for(
        auto_end_loop(
            sessionmaker=sessionmaker,
            status=status,
            cancel_event=cancel_event,
            interval_seconds=10.0,
            threshold_hours=12,
            initial_grace_seconds=0.0,
        ),
        timeout=1.0,
    )
    await cancel_task

    assert status.last_iteration_at is None


async def test_auto_end_loop_uses_injected_clock(
    patched_repo_and_service: Callable[[_FakeRepo], None],
) -> None:
    repo = _FakeRepo(stale_to_return=[])
    patched_repo_and_service(repo)
    sessionmaker = _fake_sessionmaker(repo)
    status = AutoEndJobStatus(enabled=True)
    cancel_event = asyncio.Event()

    fixed_now = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
    iteration_done = asyncio.Event()
    real_find = repo.find_stale_active

    async def _wrapped_find(*, older_than: datetime) -> list[ShiftSession]:
        result = await real_find(older_than=older_than)
        iteration_done.set()
        return result

    repo.find_stale_active = _wrapped_find  # type: ignore[method-assign]

    async def _cancel_after_iter() -> None:
        await iteration_done.wait()
        cancel_event.set()

    cancel_task = asyncio.create_task(_cancel_after_iter())
    await asyncio.wait_for(
        auto_end_loop(
            sessionmaker=sessionmaker,
            status=status,
            cancel_event=cancel_event,
            interval_seconds=10.0,
            threshold_hours=12,
            initial_grace_seconds=0.0,
            clock=lambda: fixed_now,
        ),
        timeout=1.0,
    )
    await cancel_task

    # Iteration computed older_than using the injected clock.
    assert repo.find_stale_active_calls == [fixed_now - timedelta(hours=12)]
    assert status.last_iteration_at == fixed_now


# ---------- Sprint 8a Task 1: advisory-lock skip path ------------------------


class _LockHeldByOtherReplicaResult:
    """``pg_try_advisory_lock`` result that reports the lock was NOT acquired."""

    def scalar_one(self) -> bool:
        return False


class _LockHeldByOtherReplicaSession:
    """Sessionmaker yields this when we want to simulate the case where
    another replica already holds the advisory lock."""

    def in_transaction(self) -> bool:
        return False

    async def execute(self, *_args: object, **_kwargs: object) -> _LockHeldByOtherReplicaResult:
        return _LockHeldByOtherReplicaResult()


def _lock_held_sessionmaker() -> async_sessionmaker[AsyncSession]:
    @asynccontextmanager
    async def _cm() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, _LockHeldByOtherReplicaSession())

    def _factory() -> object:
        return _cm()

    return cast("async_sessionmaker[AsyncSession]", _factory)


async def test_run_iteration_returns_zero_and_skips_work_when_lock_held(
    patched_repo_and_service: Callable[[_FakeRepo], None],
) -> None:
    """Sprint 8a Task 1: when pg_try_advisory_lock returns false, the
    iteration must return 0 WITHOUT calling find_stale_active or end_by_id —
    the other replica owns this tick's work."""
    # Pre-populate the repo with stale shifts that WOULD be ended if the
    # lock-skip path were broken; we'll assert they were left alone.
    stale = [_stale_shift(user=_USER_A, hours_ago=20)]
    repo = _FakeRepo(stale_to_return=stale)
    patched_repo_and_service(repo)

    count = await _run_iteration(
        sessionmaker=_lock_held_sessionmaker(),
        threshold_hours=12,
        now=_NOW,
    )

    assert count == 0
    # The lock-skip path never reaches find_stale_active or end_by_id.
    assert repo.find_stale_active_calls == []
    assert repo.end_calls == []
