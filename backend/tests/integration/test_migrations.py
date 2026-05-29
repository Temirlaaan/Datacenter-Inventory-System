"""Integration tests for the Sprint 2 migration ``a1b2c3d4e5f6``.

Verifies the migration round-trips, the database-level invariants from
Architecture §4 actually reject illegal rows, and downgrade leaves no orphan
tables, indexes, or enum types behind.

Each test runs against the live test DB (docker-compose.test.yml on port 5433)
gated by tests/integration/conftest.py.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_engine, get_sessionmaker

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _alembic(*args: str) -> None:
    """Run ``alembic <args>`` as a subprocess, asserting it exits 0."""
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
    """Force a clean upgrade-head before tests and a clean downgrade-base after.

    Module-scoped so the seven assertions share a single migration cycle.
    """
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    _alembic("downgrade", "base")


@pytest.fixture(autouse=True)
async def _truncate_qr_tables() -> AsyncGenerator[None, None]:
    """Wipe QR rows between tests so each one starts from an empty registry.

    Cleared lazily: cache_clear before yield rebuilds the engine inside whichever
    event loop the test runs in, so asyncpg connections are owned by that loop.
    """
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text("TRUNCATE qr_codes, qr_batches CASCADE"))
        await session.commit()
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


async def _insert_batch(session: AsyncSession, batch_id: str) -> None:
    await session.execute(
        text(
            "INSERT INTO qr_batches"
            " (id, created_by_email, created_by_keycloak_id, count)"
            " VALUES (:id, 'alice@example.com',"
            " '11111111-1111-1111-1111-111111111111', 10)"
        ),
        {"id": batch_id},
    )


# ----- schema-presence checks ---------------------------------------------------


async def test_migration_creates_all_expected_tables() -> None:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'public'"
                "   AND table_name IN ('qr_batches', 'qr_codes', 'audit_log')"
                " ORDER BY table_name"
            )
        )
        assert [row[0] for row in result] == ["audit_log", "qr_batches", "qr_codes"]


async def test_migration_creates_qr_state_consistency_check_constraint() -> None:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text(
                "SELECT constraint_name FROM information_schema.check_constraints"
                " WHERE constraint_name = 'qr_state_consistency'"
            )
        )
        assert result.scalar_one() == "qr_state_consistency"


async def test_migration_creates_partial_unique_index_with_bound_predicate() -> None:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text("SELECT indexdef FROM pg_indexes" " WHERE indexname = 'qr_one_per_device'")
        )
        indexdef = result.scalar_one()
        assert "UNIQUE" in indexdef
        assert "bound_to_device_id" in indexdef
        # Predicate ensures the index only applies to bound rows. Postgres expands
        # the literal to ``'bound'::qr_status`` once it resolves the enum type.
        assert "status = 'bound'::qr_status" in indexdef


# ----- behavioural checks (the invariants actually reject illegal rows) ---------


async def test_check_constraint_rejects_bound_qr_with_null_device_id() -> None:
    async with get_sessionmaker()() as session:
        await _insert_batch(session, "00000000-0000-0000-0000-000000000010")
        with pytest.raises(IntegrityError, match="qr_state_consistency"):
            await session.execute(
                text(
                    "INSERT INTO qr_codes (id, batch_id, status,"
                    " bound_to_device_id, bound_at, bound_by_email)"
                    " VALUES ('DCQR-AAAAAAAA',"
                    " '00000000-0000-0000-0000-000000000010', 'bound',"
                    " NULL, NULL, NULL)"
                )
            )


async def test_partial_unique_index_rejects_two_bound_qrs_for_same_device() -> None:
    async with get_sessionmaker()() as session:
        await _insert_batch(session, "00000000-0000-0000-0000-000000000020")
        await session.execute(
            text(
                "INSERT INTO qr_codes (id, batch_id, status,"
                " bound_to_device_id, bound_at, bound_by_email)"
                " VALUES ('DCQR-BBBBBBBB',"
                " '00000000-0000-0000-0000-000000000020', 'bound', 42,"
                " NOW(), 'alice@example.com')"
            )
        )
        with pytest.raises(IntegrityError, match="qr_one_per_device"):
            await session.execute(
                text(
                    "INSERT INTO qr_codes (id, batch_id, status,"
                    " bound_to_device_id, bound_at, bound_by_email)"
                    " VALUES ('DCQR-CCCCCCCC',"
                    " '00000000-0000-0000-0000-000000000020', 'bound', 42,"
                    " NOW(), 'alice@example.com')"
                )
            )


async def test_partial_unique_index_allows_retired_and_bound_for_same_device() -> None:
    # A retired QR for device 42 must NOT block a new bound QR for device 42 —
    # otherwise a damaged-label workflow would brick the device's slot.
    async with get_sessionmaker()() as session:
        await _insert_batch(session, "00000000-0000-0000-0000-000000000030")
        await session.execute(
            text(
                "INSERT INTO qr_codes (id, batch_id, status, retired_at,"
                " retired_reason, bound_to_device_id)"
                " VALUES ('DCQR-DDDDDDDD',"
                " '00000000-0000-0000-0000-000000000030', 'retired', NOW(),"
                " 'damaged', 42)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO qr_codes (id, batch_id, status,"
                " bound_to_device_id, bound_at, bound_by_email)"
                " VALUES ('DCQR-EEEEEEEE',"
                " '00000000-0000-0000-0000-000000000030', 'bound', 42,"
                " NOW(), 'alice@example.com')"
            )
        )
        await session.commit()  # both inserts succeeded


async def test_audit_log_session_id_is_nullable() -> None:
    # shift_sessions doesn't exist yet; Sprint 2 inserts audit rows without it.
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns"
                " WHERE table_name = 'audit_log' AND column_name = 'session_id'"
            )
        )
        assert result.scalar_one() == "YES"


# ----- downgrade leaves nothing behind ------------------------------------------


async def test_downgrade_drops_tables_and_enum_types() -> None:
    """Run a full downgrade-base cycle and verify nothing from the migration remains.

    Restores the schema via ``upgrade head`` after the assertions so subsequent
    test runs (and the module-scoped teardown) find a clean state.
    """
    _alembic("downgrade", "base")
    try:
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
        async with get_sessionmaker()() as session:
            tables = await session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public'"
                    "   AND table_name IN ('qr_batches','qr_codes','audit_log')"
                )
            )
            assert tables.fetchall() == []
            enums = await session.execute(
                text("SELECT typname FROM pg_type" " WHERE typname IN ('qr_status','audit_result')")
            )
            assert enums.fetchall() == []
        await get_engine().dispose()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
    finally:
        # Restore the schema so the module teardown fixture has something to drop.
        _alembic("upgrade", "head")
