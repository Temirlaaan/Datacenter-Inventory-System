"""Integration tests for app.services.idempotency.

Covers the happy path (insert placeholder, record response, replay returns
cached), conflict (different payload), cross-user isolation, the headline
race-condition test (concurrent same-key requests via asyncio.gather), the
TTL-cleanup query, and rollback-erases-placeholder.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.session import get_engine, get_sessionmaker
from app.services.idempotency import (
    IdempotencyKeyConflict,
    with_idempotency,
)

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


@pytest.fixture(autouse=True)
async def _truncate() -> AsyncGenerator[None, None]:
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE idempotency_keys"))
        await session.commit()
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


def _user() -> UUID:
    return UUID("11111111-1111-1111-1111-111111111111")


async def _count_rows() -> int:
    async with get_sessionmaker()() as session:
        result = await session.execute(text("SELECT COUNT(*) FROM idempotency_keys"))
        count: int = result.scalar_one()
        return count


# ---------------------------------------------------------------------------


async def test_first_call_returns_non_replay_and_inserts_placeholder() -> None:
    payload = {"count": 10}
    async with get_sessionmaker()() as session:
        async with with_idempotency(session, _user(), "k1", payload) as result:
            assert result.is_replay is False
            assert result.cached_status is None
            assert result.cached_response is None
        await session.commit()

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT response_status, response_body, request_hash" " FROM idempotency_keys")
            )
        ).one()
    assert row.response_status is None
    assert row.response_body is None
    assert len(row.request_hash) == 64  # SHA-256 hex


async def test_record_updates_response_status_and_body() -> None:
    async with get_sessionmaker()() as session:
        async with with_idempotency(session, _user(), "k1", {"x": 1}) as result:
            await result.record(response_status=201, response_body={"batch_id": "abc"})
        await session.commit()

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT response_status, response_body FROM idempotency_keys")
            )
        ).one()
    assert row.response_status == 201
    assert row.response_body == {"batch_id": "abc"}


async def test_second_call_with_same_payload_returns_replay_with_cached_response() -> None:
    payload = {"x": 1}
    async with get_sessionmaker()() as session:
        async with with_idempotency(session, _user(), "k1", payload) as result:
            await result.record(response_status=201, response_body={"ok": True})
        await session.commit()

    async with get_sessionmaker()() as session:
        async with with_idempotency(session, _user(), "k1", payload) as result:
            assert result.is_replay is True
            assert result.cached_status == 201
            assert result.cached_response == {"ok": True}

    # No new placeholder was inserted.
    assert await _count_rows() == 1


async def test_second_call_with_different_payload_raises_conflict() -> None:
    async with get_sessionmaker()() as session:
        async with with_idempotency(session, _user(), "k1", {"x": 1}) as result:
            await result.record(response_status=201, response_body={"ok": True})
        await session.commit()

    async with get_sessionmaker()() as session:
        with pytest.raises(IdempotencyKeyConflict):
            async with with_idempotency(session, _user(), "k1", {"x": 2}):
                pass  # pragma: no cover — must not enter the body


async def test_different_users_with_same_key_are_isolated() -> None:
    user_a = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    user_b = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    async with get_sessionmaker()() as session:
        async with with_idempotency(session, user_a, "k1", {"x": 1}) as result:
            await result.record(response_status=201, response_body={"who": "a"})
        await session.commit()

    async with get_sessionmaker()() as session:
        async with with_idempotency(session, user_b, "k1", {"x": 1}) as result:
            assert result.is_replay is False  # different user → independent slot
            await result.record(response_status=201, response_body={"who": "b"})
        await session.commit()

    assert await _count_rows() == 2


async def test_unique_constraint_blocks_concurrent_inserts_for_same_user_key() -> None:
    # Verifies the DB-level guard underpinning the service's race-safety.
    async with get_sessionmaker()() as session_a:
        async with get_sessionmaker()() as session_b:
            await session_a.execute(
                text(
                    "INSERT INTO idempotency_keys"
                    " (user_keycloak_id, key, request_hash)"
                    " VALUES (:u, 'k', 'h')"
                ),
                {"u": _user()},
            )
            await session_a.commit()

            with pytest.raises(IntegrityError):
                await session_b.execute(
                    text(
                        "INSERT INTO idempotency_keys"
                        " (user_keycloak_id, key, request_hash)"
                        " VALUES (:u, 'k', 'h')"
                    ),
                    {"u": _user()},
                )


async def test_concurrent_requests_with_same_key_run_work_exactly_once() -> None:
    """Two concurrent calls with the same key: exactly one does the work.

    The losing coroutine blocks on the UNIQUE constraint, observes the winner's
    commit, and returns the cached response. We assert via a shared counter
    mutated only inside the non-replay branch.
    """
    payload = {"x": 1}
    worker_invocations = 0
    replay_count = 0
    nonreplay_count = 0

    async def one_request() -> dict[str, int | bool | None]:
        nonlocal worker_invocations, replay_count, nonreplay_count
        async with get_sessionmaker()() as session:
            async with with_idempotency(session, _user(), "k1", payload) as result:
                if result.is_replay:
                    replay_count += 1
                    return {
                        "is_replay": True,
                        "cached_status": result.cached_status,
                    }
                nonreplay_count += 1
                worker_invocations += 1
                await result.record(response_status=201, response_body={"ok": True})
            await session.commit()
            return {"is_replay": False, "cached_status": 201}

    a, b = await asyncio.gather(one_request(), one_request())

    assert worker_invocations == 1
    assert nonreplay_count == 1
    assert replay_count == 1
    # Both callers know the response status (winner from work, loser from cache).
    assert {a["is_replay"], b["is_replay"]} == {True, False}
    assert await _count_rows() == 1


async def test_ttl_cleanup_query_deletes_rows_older_than_24_hours() -> None:
    """Verify the index supports the intended cleanup DELETE.

    We don't ship a cleanup job in Sprint 2 (per anti-criteria), but we prove
    that an operator can run the canonical query and it does the right thing.
    """
    async with get_sessionmaker()() as session:
        # Stale: created 25 hours ago.
        await session.execute(
            text(
                "INSERT INTO idempotency_keys"
                " (user_keycloak_id, key, request_hash, created_at)"
                " VALUES (:u, 'stale', 'h1', NOW() - INTERVAL '25 hours')"
            ),
            {"u": _user()},
        )
        # Fresh: created now.
        await session.execute(
            text(
                "INSERT INTO idempotency_keys"
                " (user_keycloak_id, key, request_hash)"
                " VALUES (:u, 'fresh', 'h2')"
            ),
            {"u": _user()},
        )
        await session.commit()

        deleted = await session.execute(
            text(
                "DELETE FROM idempotency_keys"
                " WHERE created_at < NOW() - INTERVAL '24 hours'"
                " RETURNING key"
            )
        )
        keys = sorted(row[0] for row in deleted)
        await session.commit()

    assert keys == ["stale"]
    assert await _count_rows() == 1


async def test_concurrent_same_key_different_payloads_raises_conflict_on_loser() -> None:
    """Race two requests with the same key but different payloads.

    Both SELECT see nothing, both try INSERT. One commits; the other's INSERT
    raises IntegrityError, then SELECT sees the winner's hash mismatches →
    ``IdempotencyKeyConflict``. Outcome is deterministic regardless of who
    wins: exactly one "success" and one "conflict".
    """

    async def attempt(payload: dict[str, int]) -> str:
        try:
            async with get_sessionmaker()() as session:
                async with with_idempotency(session, _user(), "k1", payload) as result:
                    if not result.is_replay:
                        await result.record(response_status=201, response_body={"p": payload})
                await session.commit()
            return "success"
        except IdempotencyKeyConflict:
            return "conflict"

    results = await asyncio.gather(attempt({"x": 1}), attempt({"x": 2}))
    assert sorted(results) == ["conflict", "success"]


async def test_rollback_after_placeholder_insert_leaves_table_empty() -> None:
    """If the caller's work raises, the placeholder vanishes with the rollback."""

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        async with get_sessionmaker()() as session:
            try:
                async with with_idempotency(session, _user(), "k1", {"x": 1}):
                    raise _Boom()
            except _Boom:
                await session.rollback()
                raise

    assert await _count_rows() == 0
