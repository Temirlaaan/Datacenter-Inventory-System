"""Integration tests for ``ShiftSessionRepository`` against the live test DB."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.repositories.shift_session import ShiftSessionQueryFilters, ShiftSessionRepository
from app.db.session import get_engine, get_sessionmaker
from app.domain.shift_session import ShiftEndReason, ShiftSession

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 5, 29, 9, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 5, 29, 17, 0, 0, tzinfo=UTC)
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
async def _truncate_tables() -> AsyncGenerator[None, None]:
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE shift_sessions CASCADE"))
        await session.commit()
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


def _active(
    *,
    session_id: UUID | None = None,
    user_keycloak_id: UUID = _USER_A,
    user_email: str = "alice@example.com",
    tablet_id: str = "tablet-01",
) -> ShiftSession:
    return ShiftSession(
        id=session_id or uuid4(),
        user_email=user_email,
        user_keycloak_id=user_keycloak_id,
        shift_start_at=_NOW,
        shift_end_at=None,
        tablet_id=tablet_id,
        end_reason=None,
    )


# --- insert + get_by_id ------------------------------------------------------


async def test_shift_session_repository_insert_then_get_by_id_round_trips() -> None:
    session = _active()
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        await repo.insert(session)
        await db.commit()

        fetched = await repo.get_by_id(session.id)
    assert fetched == session


async def test_shift_session_repository_get_by_id_returns_none_for_unknown_uuid() -> None:
    async with get_sessionmaker()() as db:
        assert await ShiftSessionRepository(db).get_by_id(uuid4()) is None


async def test_shift_session_repository_round_trips_ended_session() -> None:
    ended = ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=_USER_A,
        shift_start_at=_NOW,
        shift_end_at=_LATER,
        tablet_id="tablet-01",
        end_reason=ShiftEndReason.AUTO_TIMEOUT,
    )
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(ended)
        await db.commit()

        fetched = await ShiftSessionRepository(db).get_by_id(ended.id)
    assert fetched == ended
    assert fetched is not None and fetched.end_reason is ShiftEndReason.AUTO_TIMEOUT


# --- get_active_for_user -----------------------------------------------------


async def test_get_active_for_user_returns_active_session_when_present() -> None:
    session = _active()
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(session)
        await db.commit()

        fetched = await ShiftSessionRepository(db).get_active_for_user(_USER_A)
    assert fetched == session


async def test_get_active_for_user_returns_none_when_no_session_exists() -> None:
    async with get_sessionmaker()() as db:
        assert await ShiftSessionRepository(db).get_active_for_user(_USER_A) is None


async def test_get_active_for_user_ignores_ended_sessions() -> None:
    # An ended-only history must look the same as "no session" to start().
    ended = ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=_USER_A,
        shift_start_at=_NOW,
        shift_end_at=_LATER,
        tablet_id="tablet-01",
        end_reason=ShiftEndReason.MANUAL,
    )
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(ended)
        await db.commit()

        assert await ShiftSessionRepository(db).get_active_for_user(_USER_A) is None


async def test_get_active_for_user_does_not_return_other_users_active_session() -> None:
    other = _active(user_keycloak_id=_USER_B, user_email="bob@example.com")
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(other)
        await db.commit()

        assert await ShiftSessionRepository(db).get_active_for_user(_USER_A) is None


# --- insert race: partial unique index ---------------------------------------


async def test_insert_second_active_session_for_same_user_raises_integrity_error() -> None:
    # The Task 2 service layer catches this IntegrityError to raise
    # SessionAlreadyActive -> 409, so the repo must NOT wrap it in
    # RepositoryError. Mirrors QRCodeRepository.update()'s contract.
    first = _active()
    second = _active(tablet_id="tablet-02")
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(first)
        await db.commit()

    async with get_sessionmaker()() as db:
        with pytest.raises(IntegrityError):
            await ShiftSessionRepository(db).insert(second)
            await db.commit()


async def test_insert_active_session_after_ended_for_same_user_succeeds() -> None:
    # If a user's previous shift is ended, a new active shift must be allowed.
    ended = ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=_USER_A,
        shift_start_at=_NOW,
        shift_end_at=_LATER,
        tablet_id="tablet-01",
        end_reason=ShiftEndReason.MANUAL,
    )
    new_active = _active(tablet_id="tablet-02")
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(ended)
        await ShiftSessionRepository(db).insert(new_active)
        await db.commit()

        fetched = await ShiftSessionRepository(db).get_active_for_user(_USER_A)
    assert fetched == new_active


# --- update (end transition) -------------------------------------------------


async def test_update_persists_end_transition() -> None:
    session = _active()
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(session)
        await db.commit()

    ended = session.end(reason=ShiftEndReason.MANUAL, at=_LATER)
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).update(ended)
        await db.commit()

    async with get_sessionmaker()() as db:
        fetched = await ShiftSessionRepository(db).get_by_id(session.id)
    assert fetched == ended
    assert fetched is not None and fetched.end_reason is ShiftEndReason.MANUAL


async def test_update_does_not_commit_implicitly() -> None:
    session = _active()
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(session)
        await db.commit()

    ended = session.end(reason=ShiftEndReason.MANUAL, at=_LATER)
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).update(ended)
        # Deliberately no commit.

    # The transition must not be visible in a fresh session.
    async with get_sessionmaker()() as db:
        fetched = await ShiftSessionRepository(db).get_by_id(session.id)
    assert fetched is not None and fetched.is_active


async def test_insert_does_not_commit_implicitly() -> None:
    session = _active()
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(session)
        # Deliberately no commit.

    async with get_sessionmaker()() as db:
        result = await db.execute(text("SELECT COUNT(*) FROM shift_sessions"))
        assert result.scalar_one() == 0


# --- find_stale_active (Sprint 7 Task 1) -------------------------------------


def _active_at(*, start_at: datetime, user_keycloak_id: UUID, tablet_id: str) -> ShiftSession:
    return ShiftSession(
        id=uuid4(),
        user_email="alice@example.com",
        user_keycloak_id=user_keycloak_id,
        shift_start_at=start_at,
        shift_end_at=None,
        tablet_id=tablet_id,
        end_reason=None,
    )


async def test_find_stale_active_returns_shifts_older_than_threshold() -> None:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    stale = _active_at(
        start_at=base - timedelta(hours=13), user_keycloak_id=_USER_A, tablet_id="t1"
    )
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(stale)
        await db.commit()

    async with get_sessionmaker()() as db:
        rows = await ShiftSessionRepository(db).find_stale_active(
            older_than=base - timedelta(hours=12)
        )
    assert [r.id for r in rows] == [stale.id]


async def test_find_stale_active_excludes_fresh_active_shift() -> None:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    fresh = _active_at(start_at=base - timedelta(hours=1), user_keycloak_id=_USER_A, tablet_id="t1")
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(fresh)
        await db.commit()

    async with get_sessionmaker()() as db:
        rows = await ShiftSessionRepository(db).find_stale_active(
            older_than=base - timedelta(hours=12)
        )
    assert rows == []


async def test_find_stale_active_excludes_already_ended_shift_even_if_old() -> None:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    old_ended = _active_at(
        start_at=base - timedelta(hours=20), user_keycloak_id=_USER_A, tablet_id="t1"
    ).end(reason=ShiftEndReason.MANUAL, at=base - timedelta(hours=18))
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(old_ended)
        await db.commit()

    async with get_sessionmaker()() as db:
        rows = await ShiftSessionRepository(db).find_stale_active(
            older_than=base - timedelta(hours=12)
        )
    assert rows == []


async def test_find_stale_active_orders_by_shift_start_at_ascending() -> None:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    older = _active_at(
        start_at=base - timedelta(hours=24), user_keycloak_id=_USER_A, tablet_id="t1"
    )
    newer_but_still_stale = _active_at(
        start_at=base - timedelta(hours=13), user_keycloak_id=_USER_B, tablet_id="t2"
    )
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        await repo.insert(newer_but_still_stale)
        await repo.insert(older)
        await db.commit()

    async with get_sessionmaker()() as db:
        rows = await ShiftSessionRepository(db).find_stale_active(
            older_than=base - timedelta(hours=12)
        )
    assert [r.id for r in rows] == [older.id, newer_but_still_stale.id]


# --- query (Sprint 7 Task 3) -------------------------------------------------


async def test_query_with_no_filters_returns_all_rows_in_desc_order() -> None:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    older = _active_at(start_at=base - timedelta(hours=2), user_keycloak_id=_USER_A, tablet_id="t1")
    newer = _active_at(start_at=base - timedelta(hours=1), user_keycloak_id=_USER_B, tablet_id="t2")
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        await repo.insert(older)
        await repo.insert(newer)
        await db.commit()

    async with get_sessionmaker()() as db:
        rows, has_more = await ShiftSessionRepository(db).query(
            filters=ShiftSessionQueryFilters(), page=1, page_size=20
        )
    assert [r.id for r in rows] == [newer.id, older.id]
    assert has_more is False


async def test_query_filters_by_user_keycloak_id() -> None:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    alice = _active_at(start_at=base - timedelta(hours=1), user_keycloak_id=_USER_A, tablet_id="t1")
    bob = _active_at(start_at=base - timedelta(hours=2), user_keycloak_id=_USER_B, tablet_id="t2")
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        await repo.insert(alice)
        await repo.insert(bob)
        await db.commit()

    async with get_sessionmaker()() as db:
        rows, _ = await ShiftSessionRepository(db).query(
            filters=ShiftSessionQueryFilters(user_keycloak_id=_USER_A),
            page=1,
            page_size=20,
        )
    assert [r.id for r in rows] == [alice.id]


async def test_query_filters_by_from_to_on_shift_start_at_inclusive() -> None:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    s1 = _active_at(start_at=base - timedelta(hours=10), user_keycloak_id=_USER_A, tablet_id="t1")
    s2 = _active_at(start_at=base - timedelta(hours=5), user_keycloak_id=_USER_B, tablet_id="t2")
    s3 = _active_at(
        start_at=base - timedelta(hours=2),
        user_keycloak_id=UUID("33333333-3333-3333-3333-333333333333"),
        tablet_id="t3",
    )
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        for s in (s1, s2, s3):
            await repo.insert(s)
        await db.commit()

    async with get_sessionmaker()() as db:
        rows, _ = await ShiftSessionRepository(db).query(
            filters=ShiftSessionQueryFilters(
                from_=base - timedelta(hours=5), to=base - timedelta(hours=2)
            ),
            page=1,
            page_size=20,
        )
    assert sorted(r.id for r in rows) == sorted([s2.id, s3.id])


async def test_query_active_only_true_excludes_ended_shifts() -> None:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    active = _active_at(
        start_at=base - timedelta(hours=1), user_keycloak_id=_USER_A, tablet_id="t1"
    )
    ended = _active_at(
        start_at=base - timedelta(hours=2), user_keycloak_id=_USER_B, tablet_id="t2"
    ).end(reason=ShiftEndReason.MANUAL, at=base - timedelta(minutes=30))
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        await repo.insert(active)
        await repo.insert(ended)
        await db.commit()

    async with get_sessionmaker()() as db:
        rows, _ = await ShiftSessionRepository(db).query(
            filters=ShiftSessionQueryFilters(active_only=True), page=1, page_size=20
        )
    assert [r.id for r in rows] == [active.id]


async def test_query_active_only_false_includes_ended_shifts() -> None:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    active = _active_at(
        start_at=base - timedelta(hours=1), user_keycloak_id=_USER_A, tablet_id="t1"
    )
    ended = _active_at(
        start_at=base - timedelta(hours=2), user_keycloak_id=_USER_B, tablet_id="t2"
    ).end(reason=ShiftEndReason.MANUAL, at=base - timedelta(minutes=30))
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        await repo.insert(active)
        await repo.insert(ended)
        await db.commit()

    async with get_sessionmaker()() as db:
        rows, _ = await ShiftSessionRepository(db).query(
            filters=ShiftSessionQueryFilters(), page=1, page_size=20
        )
    assert sorted(r.id for r in rows) == sorted([active.id, ended.id])


async def test_query_pagination_walks_pages_with_has_more() -> None:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    users = [UUID(f"{i:08x}-0000-0000-0000-000000000000") for i in range(1, 6)]
    sessions = [
        _active_at(start_at=base - timedelta(hours=i), user_keycloak_id=users[i], tablet_id=f"t{i}")
        for i in range(5)
    ]
    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        for s in sessions:
            await repo.insert(s)
        await db.commit()

    async with get_sessionmaker()() as db:
        repo = ShiftSessionRepository(db)
        page1, has_more1 = await repo.query(filters=ShiftSessionQueryFilters(), page=1, page_size=2)
        page2, has_more2 = await repo.query(filters=ShiftSessionQueryFilters(), page=2, page_size=2)
        page3, has_more3 = await repo.query(filters=ShiftSessionQueryFilters(), page=3, page_size=2)
    assert len(page1) == 2 and has_more1 is True
    assert len(page2) == 2 and has_more2 is True
    assert len(page3) == 1 and has_more3 is False
    assert len({r.id for r in page1 + page2 + page3}) == 5


async def test_query_returns_empty_for_out_of_range_page() -> None:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    async with get_sessionmaker()() as db:
        await ShiftSessionRepository(db).insert(
            _active_at(start_at=base, user_keycloak_id=_USER_A, tablet_id="t1")
        )
        await db.commit()

    async with get_sessionmaker()() as db:
        rows, has_more = await ShiftSessionRepository(db).query(
            filters=ShiftSessionQueryFilters(), page=10, page_size=20
        )
    assert rows == []
    assert has_more is False


async def test_query_returns_empty_when_table_empty() -> None:
    async with get_sessionmaker()() as db:
        rows, has_more = await ShiftSessionRepository(db).query(
            filters=ShiftSessionQueryFilters(), page=1, page_size=20
        )
    assert rows == []
    assert has_more is False
