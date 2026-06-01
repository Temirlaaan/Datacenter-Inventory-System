"""Unit tests for app.services.shift_session.ShiftSessionService.

Strategy mirrors test_lifecycle.py: fake the ``ShiftSessionRepository`` and
``AsyncSession``, no live DB. The repo's IntegrityError race (partial unique
index `shift_sessions_one_active_per_user`) is covered by injecting a fake
that raises on ``insert`` — the migration-level behaviour is asserted in
``tests/integration/test_shift_sessions_migration.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.shift_session import ShiftSessionRepository
from app.domain.shift_session import (
    IllegalShiftTransition,
    ShiftEndReason,
    ShiftSession,
)
from app.services.shift_session import (
    NoActiveShift,
    SessionAlreadyActive,
    ShiftSessionNotFound,
    ShiftSessionService,
)

_USER_A = UUID("11111111-1111-1111-1111-111111111111")
_USER_B = UUID("22222222-2222-2222-2222-222222222222")
_NOW_FROZEN = datetime(2026, 5, 29, 9, 0, 0, tzinfo=UTC)


# ---------- fakes ----------


class _FakeTx:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeTx:
        self._session._in_tx = True
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        self._session._in_tx = False
        if exc_type is not None:
            self._session.rollbacks += 1
            return False
        self._session.commits += 1
        return False


class _FakeSession:
    """Stand-in for AsyncSession.

    ``in_transaction()`` flips while inside the ``async with begin()`` block so
    the defensive guard exercises the same code path as the real session.
    """

    def __init__(self, *, in_transaction: bool = False) -> None:
        self._in_tx = in_transaction
        self.commits = 0
        self.rollbacks = 0

    def in_transaction(self) -> bool:
        return self._in_tx

    def begin(self) -> _FakeTx:
        return _FakeTx(self)


class _FakeShiftSessionRepo:
    def __init__(self) -> None:
        self.by_id: dict[UUID, ShiftSession] = {}
        # Per-user FIFO of return values for ``get_active_for_user``. Used by
        # the race-re-read tests to make the first read return None and the
        # second return the winner. When the queue empties, the call falls
        # through to a normal scan of ``by_id``.
        self.active_returns: dict[UUID, list[ShiftSession | None]] = {}
        # If set, the next insert() raises this exception (then resets to None).
        self.next_insert_raises: Exception | None = None
        self.inserts: list[ShiftSession] = []
        self.updates: list[ShiftSession] = []

    async def get_by_id(self, session_id: UUID) -> ShiftSession | None:
        return self.by_id.get(session_id)

    async def get_active_for_user(self, user_keycloak_id: UUID) -> ShiftSession | None:
        queue = self.active_returns.get(user_keycloak_id)
        if queue:
            return queue.pop(0)
        for s in self.by_id.values():
            if s.user_keycloak_id == user_keycloak_id and s.is_active:
                return s
        return None

    async def insert(self, shift: ShiftSession) -> None:
        if self.next_insert_raises is not None:
            err = self.next_insert_raises
            self.next_insert_raises = None
            raise err
        self.inserts.append(shift)
        self.by_id[shift.id] = shift

    async def update(self, shift: ShiftSession) -> None:
        self.updates.append(shift)
        self.by_id[shift.id] = shift


# ---------- helpers ----------


def _active_for(user_id: UUID = _USER_A, *, session_id: UUID | None = None) -> ShiftSession:
    return ShiftSession(
        id=session_id or uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=user_id,
        shift_start_at=datetime(2026, 5, 29, 8, 0, 0, tzinfo=UTC),
        shift_end_at=None,
        tablet_id="tablet-01",
        end_reason=None,
    )


def _build_service(
    *,
    session: _FakeSession | None = None,
    repo: _FakeShiftSessionRepo | None = None,
) -> tuple[ShiftSessionService, _FakeSession, _FakeShiftSessionRepo]:
    session = session or _FakeSession()
    repo = repo or _FakeShiftSessionRepo()
    service = ShiftSessionService(
        session=cast(AsyncSession, session),
        repo=cast(ShiftSessionRepository, repo),
    )
    return service, session, repo


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze ``datetime.now(UTC)`` inside the service module."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return _NOW_FROZEN

    monkeypatch.setattr("app.services.shift_session.datetime", _Frozen)
    return _NOW_FROZEN


# ---------- get_active ----------


async def test_get_active_returns_none_when_no_session_for_user() -> None:
    service, _session, _repo = _build_service()
    assert await service.get_active(_USER_A) is None


async def test_get_active_returns_active_session_when_present() -> None:
    active = _active_for(_USER_A)
    service, _session, repo = _build_service()
    repo.by_id[active.id] = active

    assert await service.get_active(_USER_A) == active


async def test_get_active_ignores_other_users_active_session() -> None:
    other = _active_for(_USER_B)
    service, _session, repo = _build_service()
    repo.by_id[other.id] = other

    assert await service.get_active(_USER_A) is None


# ---------- start ----------


async def test_start_succeeds_and_persists_active_session_when_no_existing(
    frozen_now: datetime,
) -> None:
    service, session, repo = _build_service()

    started = await service.start(
        user_email="alice@example.com",
        user_keycloak_id=_USER_A,
        tablet_id="tablet-01",
    )

    assert started.is_active
    assert started.user_email == "alice@example.com"
    assert started.user_keycloak_id == _USER_A
    assert started.tablet_id == "tablet-01"
    assert started.shift_start_at == frozen_now
    assert repo.inserts == [started]
    assert session.commits == 1


async def test_start_assigns_new_uuid() -> None:
    service, _session, _repo = _build_service()

    first = await service.start(
        user_email="alice@example.com", user_keycloak_id=_USER_A, tablet_id="tablet-01"
    )
    # Subsequent start for a different user must get its own UUID.
    second = await service.start(
        user_email="bob@example.com", user_keycloak_id=_USER_B, tablet_id="tablet-02"
    )

    assert first.id != second.id


async def test_start_with_existing_active_raises_session_already_active_with_existing() -> None:
    existing = _active_for(_USER_A)
    service, session, repo = _build_service()
    repo.by_id[existing.id] = existing

    with pytest.raises(SessionAlreadyActive) as exc:
        await service.start(
            user_email="alice@example.com", user_keycloak_id=_USER_A, tablet_id="tablet-99"
        )

    assert exc.value.active == existing
    # No insert attempted.
    assert repo.inserts == []
    # The opened pre-check tx rolled back when the exception fired.
    assert session.rollbacks == 1
    assert session.commits == 0


async def test_start_race_integrity_error_re_reads_and_raises_with_winner() -> None:
    """The partial unique index can fire AFTER the read-active-as-None check
    if a concurrent ``start`` won between our read and our insert. The service
    must re-read in a fresh tx and raise ``SessionAlreadyActive`` with the
    winner so the endpoint can surface it in the 409 body.
    """
    winner = _active_for(_USER_A)
    service, _session, repo = _build_service()
    # First read (inside the start() tx) sees no active so the service proceeds
    # to insert. Insert raises IntegrityError. The re-read inside the second tx
    # then returns the winner.
    repo.active_returns[_USER_A] = [None, winner]
    repo.next_insert_raises = IntegrityError("INSERT", {}, Exception("partial unique index"))

    with pytest.raises(SessionAlreadyActive) as exc:
        await service.start(
            user_email="alice@example.com", user_keycloak_id=_USER_A, tablet_id="tablet-01"
        )

    assert exc.value.active == winner


async def test_start_race_integrity_error_with_no_winner_propagates_integrity_error() -> None:
    """Triple race: the IntegrityError winner ended between our insert failure
    and our re-read. Re-read returns None. Documented as acceptable — let the
    IntegrityError propagate so the endpoint surfaces it as a 500 rather than
    fabricating a SessionAlreadyActive with no payload.
    """
    service, _session, repo = _build_service()
    # Both reads return None; insert fires IntegrityError between them.
    repo.active_returns[_USER_A] = [None, None]
    repo.next_insert_raises = IntegrityError("INSERT", {}, Exception("partial unique index"))

    with pytest.raises(IntegrityError):
        await service.start(
            user_email="alice@example.com", user_keycloak_id=_USER_A, tablet_id="tablet-01"
        )


async def test_start_inside_active_transaction_raises_runtime_error() -> None:
    service, _session, _repo = _build_service(session=_FakeSession(in_transaction=True))

    with pytest.raises(RuntimeError, match="active transaction"):
        await service.start(
            user_email="alice@example.com", user_keycloak_id=_USER_A, tablet_id="tablet-01"
        )


# ---------- end ----------


@pytest.mark.parametrize(
    "reason",
    [
        ShiftEndReason.MANUAL,
        ShiftEndReason.AUTO_TIMEOUT,
        ShiftEndReason.FORCED,
    ],
)
async def test_end_succeeds_with_each_reason(reason: ShiftEndReason, frozen_now: datetime) -> None:
    existing = _active_for(_USER_A)
    service, session, repo = _build_service()
    repo.by_id[existing.id] = existing

    ended = await service.end(user_keycloak_id=_USER_A, reason=reason)

    assert ended.id == existing.id
    assert ended.shift_end_at == frozen_now
    assert ended.end_reason is reason
    assert ended.is_active is False
    assert repo.updates == [ended]
    assert session.commits == 1


async def test_end_with_no_active_raises_no_active_shift() -> None:
    service, session, _repo = _build_service()

    with pytest.raises(NoActiveShift) as exc:
        await service.end(user_keycloak_id=_USER_A, reason=ShiftEndReason.MANUAL)

    assert exc.value.user_keycloak_id == _USER_A
    assert session.rollbacks == 1
    assert session.commits == 0


async def test_end_inside_active_transaction_raises_runtime_error() -> None:
    service, _session, _repo = _build_service(session=_FakeSession(in_transaction=True))

    with pytest.raises(RuntimeError, match="active transaction"):
        await service.end(user_keycloak_id=_USER_A, reason=ShiftEndReason.MANUAL)


async def test_end_propagates_illegal_shift_transition_if_domain_rejects() -> None:
    """Defensive: ``get_active_for_user`` should never return an ended session
    (the partial unique index guarantees only active rows match its predicate),
    but if a fake/buggy repo did, the domain ``end()`` would raise
    ``IllegalShiftTransition``. Service propagates it rather than swallowing.
    """
    already_ended = ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=_USER_A,
        shift_start_at=datetime(2026, 5, 29, 8, 0, 0, tzinfo=UTC),
        shift_end_at=datetime(2026, 5, 29, 16, 0, 0, tzinfo=UTC),
        tablet_id="tablet-01",
        end_reason=ShiftEndReason.MANUAL,
    )
    service, _session, repo = _build_service()
    # Force get_active_for_user to return an ended session via the queue.
    repo.active_returns[_USER_A] = [already_ended]

    with pytest.raises(IllegalShiftTransition):
        await service.end(user_keycloak_id=_USER_A, reason=ShiftEndReason.MANUAL)


# ---------- exception payload checks ----------


def test_session_already_active_carries_active_session_attribute() -> None:
    active = _active_for(_USER_A)
    exc = SessionAlreadyActive(active)
    assert exc.active == active


def test_no_active_shift_carries_user_keycloak_id_attribute() -> None:
    exc = NoActiveShift(_USER_A)
    assert exc.user_keycloak_id == _USER_A


def test_shift_session_not_found_carries_session_id_attribute() -> None:
    sid = uuid4()
    exc = ShiftSessionNotFound(sid)
    assert exc.session_id == sid


# ---------- end_by_id (Sprint 7 Task 1) --------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        ShiftEndReason.MANUAL,
        ShiftEndReason.AUTO_TIMEOUT,
        ShiftEndReason.FORCED,
    ],
)
async def test_end_by_id_succeeds_with_each_reason(
    reason: ShiftEndReason, frozen_now: datetime
) -> None:
    existing = _active_for(_USER_A)
    service, session, repo = _build_service()
    repo.by_id[existing.id] = existing

    ended = await service.end_by_id(session_id=existing.id, reason=reason)

    assert ended.id == existing.id
    assert ended.shift_end_at == frozen_now
    assert ended.end_reason is reason
    assert ended.is_active is False
    assert repo.updates == [ended]
    assert session.commits == 1


async def test_end_by_id_with_unknown_id_raises_shift_session_not_found() -> None:
    service, session, _repo = _build_service()
    unknown = uuid4()

    with pytest.raises(ShiftSessionNotFound) as exc:
        await service.end_by_id(session_id=unknown, reason=ShiftEndReason.AUTO_TIMEOUT)

    assert exc.value.session_id == unknown
    assert session.rollbacks == 1
    assert session.commits == 0


async def test_end_by_id_with_already_ended_row_raises_illegal_shift_transition() -> None:
    """The auto-end job + admin force-close both rely on this to detect a
    concurrent end racing with their own — the caller swallows the exception
    and treats it as an idempotent no-op."""
    ended_already = ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=_USER_A,
        shift_start_at=datetime(2026, 5, 29, 8, 0, 0, tzinfo=UTC),
        shift_end_at=datetime(2026, 5, 29, 16, 0, 0, tzinfo=UTC),
        tablet_id="tablet-01",
        end_reason=ShiftEndReason.MANUAL,
    )
    service, session, repo = _build_service()
    repo.by_id[ended_already.id] = ended_already

    with pytest.raises(IllegalShiftTransition):
        await service.end_by_id(session_id=ended_already.id, reason=ShiftEndReason.AUTO_TIMEOUT)

    assert session.rollbacks == 1
    assert session.commits == 0


async def test_end_by_id_inside_active_transaction_raises_runtime_error() -> None:
    service, _session, _repo = _build_service(session=_FakeSession(in_transaction=True))

    with pytest.raises(RuntimeError, match="active transaction"):
        await service.end_by_id(session_id=uuid4(), reason=ShiftEndReason.AUTO_TIMEOUT)
