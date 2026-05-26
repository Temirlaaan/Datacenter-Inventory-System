"""Unit tests for app.services.idempotency — hash determinism, exception shape,
IdempotencyResult.record() guard rails, and the race-loss branch under mocking.

The race-loss branch needs deterministic coverage because asyncio scheduling on
the integration race tests tends to let the loser see the winner's committed
row via the SELECT-first path, never exercising the IntegrityError fallback.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from app.services.idempotency import (
    IdempotencyKeyConflict,
    IdempotencyResult,
    _canonical_hash,
    with_idempotency,
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
