"""Unit tests for app.services.idempotency — hash determinism, exception shape,
IdempotencyResult.record() guard rails, and the race-loss branch under mocking.

The race-loss branch needs deterministic coverage because asyncio scheduling on
the integration race tests tends to let the loser see the winner's committed
row via the SELECT-first path, never exercising the IntegrityError fallback.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.idempotency import (
    IdempotencyKeyConflict,
    IdempotencyResult,
    _canonical_hash,
    with_idempotency,
    with_optional_idempotency_outer,
)

_USER = UUID("11111111-1111-1111-1111-111111111111")
_KEY = "some-key"


def test_canonical_hash_is_deterministic_for_same_payload() -> None:
    payload = {"count": 10, "comment": "rack 14"}
    assert _canonical_hash(payload) == _canonical_hash(payload)


def test_canonical_hash_is_order_independent_within_a_dict() -> None:
    assert _canonical_hash({"a": 1, "b": 2}) == _canonical_hash({"b": 2, "a": 1})


def test_canonical_hash_distinguishes_different_values() -> None:
    assert _canonical_hash({"x": 1}) != _canonical_hash({"x": 2})


def test_canonical_hash_distinguishes_different_keys() -> None:
    assert _canonical_hash({"a": 1}) != _canonical_hash({"b": 1})


def test_canonical_hash_distinguishes_integer_and_float() -> None:
    # Common idempotency footgun — JSON serializes 1 and 1.0 differently.
    assert _canonical_hash({"x": 1}) != _canonical_hash({"x": 1.0})


def test_idempotency_key_conflict_is_a_plain_exception_subclass() -> None:
    exc = IdempotencyKeyConflict()
    assert isinstance(exc, Exception)


async def test_idempotency_result_record_raises_when_called_on_replay() -> None:
    result = IdempotencyResult(
        is_replay=True,
        cached_status=201,
        cached_response={"ok": True},
        session=None,
        user_keycloak_id=_USER,
        key=_KEY,
    )
    with pytest.raises(RuntimeError, match="replay"):
        await result.record(response_status=201, response_body={})


async def test_idempotency_result_record_raises_when_called_twice() -> None:
    session = AsyncMock()
    result = IdempotencyResult(
        is_replay=False,
        cached_status=None,
        cached_response=None,
        session=session,
        user_keycloak_id=_USER,
        key=_KEY,
    )
    await result.record(response_status=201, response_body={"ok": True})
    with pytest.raises(RuntimeError, match="twice"):
        await result.record(response_status=201, response_body={"ok": True})


# Race-loss branch -- deterministic coverage via mocked session ----------------


def _fake_winner_row(request_hash: str) -> SimpleNamespace:
    """A stand-in for an IdempotencyKeyModel row.

    The service reads ``request_hash``, ``response_status``, ``response_body``;
    everything else is irrelevant for the branch under test.
    """
    return SimpleNamespace(
        request_hash=request_hash,
        response_status=201,
        response_body={"ok": True},
    )


def _mock_session_for_race_loss(
    *,
    select_returns: list[object | None],
    insert_raises: BaseException | None,
) -> AsyncMock:
    """Build an AsyncSession mock for the race-loss code path.

    ``scalar`` pops from ``select_returns`` in order (Step 1 SELECT, then the
    post-rollback SELECT). ``execute`` raises ``insert_raises`` on call.
    """
    session = AsyncMock()
    session.scalar.side_effect = list(select_returns)
    if insert_raises is not None:
        session.execute.side_effect = insert_raises
    return session


async def test_with_idempotency_race_loss_with_matching_hash_yields_replay() -> None:
    payload = {"x": 1}
    request_hash = _canonical_hash(payload)
    winner_row = _fake_winner_row(request_hash)
    session = _mock_session_for_race_loss(
        select_returns=[None, winner_row],
        insert_raises=IntegrityError("INSERT", {}, Exception("dup")),
    )

    async with with_idempotency(session, _USER, _KEY, payload) as result:
        assert result.is_replay is True
        assert result.cached_status == 201
        assert result.cached_response == {"ok": True}

    session.rollback.assert_awaited_once()
    assert session.scalar.await_count == 2


async def test_with_idempotency_race_loss_with_different_hash_raises_conflict() -> None:
    payload = {"x": 1}
    winner_row = _fake_winner_row(request_hash="different-hash-from-elsewhere")
    session = _mock_session_for_race_loss(
        select_returns=[None, winner_row],
        insert_raises=IntegrityError("INSERT", {}, Exception("dup")),
    )

    with pytest.raises(IdempotencyKeyConflict):
        async with with_idempotency(session, _USER, _KEY, payload):
            pass  # pragma: no cover - must not enter the body


# Sprint 9 Task 0 — separate-session wrapper for non-batch endpoints ----------


def _fake_replay_row(*, request_hash: str, status: int, body: dict | None) -> SimpleNamespace:
    return SimpleNamespace(
        request_hash=request_hash, response_status=status, response_body=body
    )


def _async_session_cm_factory(session_mock: AsyncMock) -> object:
    """Wrap an AsyncMock session in an async context manager that mimics
    ``async_sessionmaker.__call__()`` — yields the session on ``__aenter__``."""

    class _CM:
        async def __aenter__(self) -> AsyncMock:
            return session_mock

        async def __aexit__(self, *_a: object) -> bool:
            return False

    def _factory() -> _CM:
        return _CM()

    return _factory


async def test_with_optional_idempotency_outer_passes_through_when_no_key() -> None:
    """No header → wrapper calls ``do_work`` once and surfaces its tuple
    without touching the sessionmaker."""
    sessionmaker_called = False

    def _sm() -> object:
        nonlocal sessionmaker_called
        sessionmaker_called = True
        raise AssertionError("sessionmaker must not be called when key is None")

    async def _do_work() -> tuple[int, dict]:
        return 201, {"id": "abc"}

    status_code, body = await with_optional_idempotency_outer(
        sessionmaker=cast(async_sessionmaker[AsyncSession], _sm),
        user_keycloak_id=_USER,
        idempotency_key=None,
        request_payload={"x": 1},
        do_work=_do_work,
    )
    assert status_code == 201
    assert body == {"id": "abc"}
    assert not sessionmaker_called


async def test_with_optional_idempotency_outer_returns_cached_on_replay() -> None:
    """Existing row with matching hash → wrapper returns its cached
    (status, body) and skips ``do_work``."""
    payload = {"tablet_id": "T1"}
    hash_ = _canonical_hash(payload)
    cached = _fake_replay_row(
        request_hash=hash_, status=200, body={"session": {"id": "ABC"}}
    )

    select_session = AsyncMock()
    select_session.scalar = AsyncMock(return_value=cached)
    sm = _async_session_cm_factory(select_session)

    do_work_called = False

    async def _do_work() -> tuple[int, dict]:
        nonlocal do_work_called
        do_work_called = True
        return 999, {}

    status_code, body = await with_optional_idempotency_outer(
        sessionmaker=cast(async_sessionmaker[AsyncSession], sm),
        user_keycloak_id=_USER,
        idempotency_key=_KEY,
        request_payload=payload,
        do_work=_do_work,
    )
    assert status_code == 200
    assert body == {"session": {"id": "ABC"}}
    assert not do_work_called


async def test_with_optional_idempotency_outer_raises_422_on_payload_mismatch() -> None:
    """Existing row with DIFFERENT hash → raises ``HTTPException(422)``
    surfaced as ``Idempotency-Key reused …``."""
    from fastapi import HTTPException

    existing = _fake_replay_row(
        request_hash="different-hash", status=200, body={"ok": True}
    )
    select_session = AsyncMock()
    select_session.scalar = AsyncMock(return_value=existing)
    sm = _async_session_cm_factory(select_session)

    async def _do_work() -> tuple[int, dict]:  # pragma: no cover - never runs
        return 200, {}

    with pytest.raises(HTTPException) as exc:
        await with_optional_idempotency_outer(
            sessionmaker=cast(async_sessionmaker[AsyncSession], sm),
            user_keycloak_id=_USER,
            idempotency_key=_KEY,
            request_payload={"tablet_id": "T1"},
            do_work=_do_work,
        )
    assert exc.value.status_code == 422
    assert "Idempotency-Key reused" in exc.value.detail


async def test_with_optional_idempotency_outer_runs_work_and_stores_on_first_call() -> None:
    """No existing row → runs ``do_work`` and writes the result via a
    second sessionmaker call."""
    payload = {"end_reason": "manual"}

    sessions: list[AsyncMock] = []

    def _factory() -> object:
        sess = AsyncMock()
        sess.scalar = AsyncMock(return_value=None)  # check: no row
        sess.execute = AsyncMock()  # record: succeeds
        sess.commit = AsyncMock()
        sess.rollback = AsyncMock()
        sessions.append(sess)

        class _CM:
            async def __aenter__(self) -> AsyncMock:
                return sess

            async def __aexit__(self, *_a: object) -> bool:
                return False

        return _CM()

    async def _do_work() -> tuple[int, dict]:
        return 200, {"session": {"id": "X"}}

    status_code, body = await with_optional_idempotency_outer(
        sessionmaker=cast(async_sessionmaker[AsyncSession], _factory),
        user_keycloak_id=_USER,
        idempotency_key=_KEY,
        request_payload=payload,
        do_work=_do_work,
    )
    assert status_code == 200
    assert body == {"session": {"id": "X"}}
    # Two sessionmaker() opens: one for check (no row), one for record.
    assert len(sessions) == 2
    sessions[1].execute.assert_awaited_once()
    sessions[1].commit.assert_awaited_once()


async def test_with_optional_idempotency_outer_returns_winners_response_on_race_loss() -> None:
    """First call lost the INSERT race; winner already recorded its
    response → wrapper returns the winner's cached response."""
    payload = {"x": 1}
    hash_ = _canonical_hash(payload)
    winner_row = _fake_replay_row(
        request_hash=hash_, status=201, body={"id": "WIN"}
    )

    check_session = AsyncMock()
    check_session.scalar = AsyncMock(return_value=None)  # first check: no row

    record_session = AsyncMock()
    record_session.execute = AsyncMock(
        side_effect=IntegrityError("INSERT", {}, Exception("dup"))
    )
    record_session.rollback = AsyncMock()
    record_session.scalar = AsyncMock(return_value=winner_row)  # post-race re-read

    sessions = [check_session, record_session]
    cursor = {"i": 0}

    def _factory() -> object:
        sess = sessions[cursor["i"]]
        cursor["i"] += 1

        class _CM:
            async def __aenter__(self) -> AsyncMock:
                return sess

            async def __aexit__(self, *_a: object) -> bool:
                return False

        return _CM()

    async def _do_work() -> tuple[int, dict]:
        return 201, {"id": "LOSE"}

    status_code, body = await with_optional_idempotency_outer(
        sessionmaker=cast(async_sessionmaker[AsyncSession], _factory),
        user_keycloak_id=_USER,
        idempotency_key=_KEY,
        request_payload=payload,
        do_work=_do_work,
    )
    assert status_code == 201
    assert body == {"id": "WIN"}  # winner's response, not ours
    record_session.rollback.assert_awaited_once()


