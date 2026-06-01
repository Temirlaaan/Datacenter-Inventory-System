"""Integration tests for the Sprint 6 ``shift_sessions`` migration.

Verifies the migration round-trips, the database-level invariants from
decision H (CHECK pairing shift_end_at with end_reason, partial unique index
for one-active-session-per-user) actually reject illegal rows, and downgrade
leaves no orphan table or enum behind.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.session import get_engine, get_sessionmaker

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_USER_A = "11111111-1111-1111-1111-111111111111"
_USER_B = "22222222-2222-2222-2222-222222222222"


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


# ----- schema-presence checks -------------------------------------------------


async def test_migration_creates_shift_sessions_table() -> None:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'public' AND table_name = 'shift_sessions'"
            )
        )
        assert result.scalar_one() == "shift_sessions"


async def test_migration_creates_shift_end_reason_enum_type() -> None:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text(
                "SELECT e.enumlabel FROM pg_type t"
                " JOIN pg_enum e ON e.enumtypid = t.oid"
                " WHERE t.typname = 'shift_end_reason'"
                " ORDER BY e.enumsortorder"
            )
        )
        labels = [row[0] for row in result]
    assert labels == ["manual", "inactivity_timeout", "admin_force_close"]


async def test_migration_creates_shift_end_consistency_check_constraint() -> None:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text(
                "SELECT constraint_name FROM information_schema.check_constraints"
                " WHERE constraint_name = 'shift_end_consistency'"
            )
        )
        assert result.scalar_one() == "shift_end_consistency"


async def test_migration_creates_partial_unique_index_on_active_sessions() -> None:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text(
                "SELECT indexdef FROM pg_indexes"
                " WHERE indexname = 'shift_sessions_one_active_per_user'"
            )
        )
        indexdef = result.scalar_one()
        assert "UNIQUE" in indexdef
        assert "user_keycloak_id" in indexdef
        assert "shift_end_at IS NULL" in indexdef


# ----- behavioural checks: CHECK constraint -----------------------------------


async def test_check_constraint_rejects_active_session_with_end_reason() -> None:
    async with get_sessionmaker()() as session:
        with pytest.raises(IntegrityError, match="shift_end_consistency"):
            await session.execute(
                text(
                    "INSERT INTO shift_sessions"
                    " (id, user_email, user_keycloak_id, shift_start_at,"
                    "  shift_end_at, tablet_id, end_reason)"
                    " VALUES ('aaaaaaaa-0000-0000-0000-000000000001',"
                    " 'alice@example.com', :user_id, NOW(),"
                    " NULL, 'tablet-01', 'manual')"
                ),
                {"user_id": _USER_A},
            )


async def test_check_constraint_rejects_ended_session_without_end_reason() -> None:
    async with get_sessionmaker()() as session:
        with pytest.raises(IntegrityError, match="shift_end_consistency"):
            await session.execute(
                text(
                    "INSERT INTO shift_sessions"
                    " (id, user_email, user_keycloak_id, shift_start_at,"
                    "  shift_end_at, tablet_id, end_reason)"
                    " VALUES ('aaaaaaaa-0000-0000-0000-000000000002',"
                    " 'alice@example.com', :user_id, NOW(),"
                    " NOW(), 'tablet-01', NULL)"
                ),
                {"user_id": _USER_A},
            )


async def test_check_constraint_accepts_active_session_with_both_end_fields_null() -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "INSERT INTO shift_sessions"
                " (id, user_email, user_keycloak_id, shift_start_at,"
                "  shift_end_at, tablet_id, end_reason)"
                " VALUES ('aaaaaaaa-0000-0000-0000-000000000003',"
                " 'alice@example.com', :user_id, NOW(),"
                " NULL, 'tablet-01', NULL)"
            ),
            {"user_id": _USER_A},
        )
        await session.commit()


async def test_check_constraint_accepts_ended_session_with_both_end_fields_set() -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "INSERT INTO shift_sessions"
                " (id, user_email, user_keycloak_id, shift_start_at,"
                "  shift_end_at, tablet_id, end_reason)"
                " VALUES ('aaaaaaaa-0000-0000-0000-000000000004',"
                " 'alice@example.com', :user_id, NOW(),"
                " NOW(), 'tablet-01', 'inactivity_timeout')"
            ),
            {"user_id": _USER_A},
        )
        await session.commit()


# ----- behavioural checks: partial unique index -------------------------------


async def test_partial_unique_index_rejects_two_active_sessions_for_same_user() -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "INSERT INTO shift_sessions"
                " (id, user_email, user_keycloak_id, shift_start_at, tablet_id)"
                " VALUES ('aaaaaaaa-0000-0000-0000-000000000005',"
                " 'alice@example.com', :user_id, NOW(), 'tablet-01')"
            ),
            {"user_id": _USER_A},
        )
        with pytest.raises(IntegrityError, match="shift_sessions_one_active_per_user"):
            await session.execute(
                text(
                    "INSERT INTO shift_sessions"
                    " (id, user_email, user_keycloak_id, shift_start_at, tablet_id)"
                    " VALUES ('aaaaaaaa-0000-0000-0000-000000000006',"
                    " 'alice@example.com', :user_id, NOW(), 'tablet-02')"
                ),
                {"user_id": _USER_A},
            )


async def test_partial_unique_index_allows_ended_and_active_sessions_for_same_user() -> None:
    # An ended session must NOT block opening a new one — otherwise a user
    # could never start a second shift.
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "INSERT INTO shift_sessions"
                " (id, user_email, user_keycloak_id, shift_start_at,"
                "  shift_end_at, tablet_id, end_reason)"
                " VALUES ('aaaaaaaa-0000-0000-0000-000000000007',"
                " 'alice@example.com', :user_id, NOW(),"
                " NOW(), 'tablet-01', 'manual')"
            ),
            {"user_id": _USER_A},
        )
        await session.execute(
            text(
                "INSERT INTO shift_sessions"
                " (id, user_email, user_keycloak_id, shift_start_at, tablet_id)"
                " VALUES ('aaaaaaaa-0000-0000-0000-000000000008',"
                " 'alice@example.com', :user_id, NOW(), 'tablet-02')"
            ),
            {"user_id": _USER_A},
        )
        await session.commit()


async def test_partial_unique_index_allows_active_sessions_for_different_users() -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "INSERT INTO shift_sessions"
                " (id, user_email, user_keycloak_id, shift_start_at, tablet_id)"
                " VALUES ('aaaaaaaa-0000-0000-0000-000000000009',"
                " 'alice@example.com', :user_id, NOW(), 'tablet-01')"
            ),
            {"user_id": _USER_A},
        )
        await session.execute(
            text(
                "INSERT INTO shift_sessions"
                " (id, user_email, user_keycloak_id, shift_start_at, tablet_id)"
                " VALUES ('aaaaaaaa-0000-0000-0000-00000000000a',"
                " 'bob@example.com', :user_id, NOW(), 'tablet-02')"
            ),
            {"user_id": _USER_B},
        )
        await session.commit()


# ----- downgrade leaves nothing behind ----------------------------------------


async def test_downgrade_drops_shift_sessions_table_and_enum_type() -> None:
    _alembic("downgrade", "-1")
    try:
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
        async with get_sessionmaker()() as session:
            tables = await session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public' AND table_name = 'shift_sessions'"
                )
            )
            assert tables.fetchall() == []
            enums = await session.execute(
                text("SELECT typname FROM pg_type WHERE typname = 'shift_end_reason'")
            )
            assert enums.fetchall() == []
        await get_engine().dispose()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
    finally:
        _alembic("upgrade", "head")
