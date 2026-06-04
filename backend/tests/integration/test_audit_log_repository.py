"""Integration tests for ``AuditLogRepository.query`` (Sprint 7 Task 2).

The insert path has been integration-tested indirectly via Sprint 2's
``QRGenerationService`` tests since Sprint 2. This file focuses on the new
read path: filters, pagination, has_more, and the ordering contract.
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

from app.db.repositories.audit_log import AuditLogQueryFilters, AuditLogRepository
from app.db.session import get_engine, get_sessionmaker
from app.domain.audit import AuditLogEntry, AuditResult

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_USER_A = UUID("11111111-1111-1111-1111-111111111111")
_USER_B = UUID("22222222-2222-2222-2222-222222222222")
_SESSION_X = UUID("aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa")
_SESSION_Y = UUID("bbbbbbbb-0000-0000-0000-bbbbbbbbbbbb")


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
        await session.execute(text("TRUNCATE audit_log CASCADE"))
        await session.commit()
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


def _entry(
    *,
    timestamp: datetime,
    user_keycloak_id: UUID = _USER_A,
    user_email: str = "alice@example.com",
    session_id: UUID | None = _SESSION_X,
    operation: str = "qr.bind",
    entity_type: str = "qr",
    entity_id: str = "DCQR-0001",
    result: AuditResult = AuditResult.SUCCESS,
) -> AuditLogEntry:
    return AuditLogEntry(
        request_id=uuid4(),
        timestamp=timestamp,
        user_email=user_email,
        user_keycloak_id=user_keycloak_id,
        session_id=session_id,
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json={},
        after_json={},
        result=result,
    )


async def _insert_all(entries: list[AuditLogEntry]) -> None:
    async with get_sessionmaker()() as db:
        repo = AuditLogRepository(db)
        for e in entries:
            await repo.insert(e)
        await db.commit()


# --- filters ----------------------------------------------------------------


async def test_query_with_no_filters_returns_all_rows() -> None:
    await _insert_all(
        [
            _entry(timestamp=_NOW - timedelta(minutes=2)),
            _entry(timestamp=_NOW - timedelta(minutes=1)),
        ]
    )
    async with get_sessionmaker()() as db:
        rows, has_more = await AuditLogRepository(db).query(
            filters=AuditLogQueryFilters(), page=1, page_size=20
        )
    assert len(rows) == 2
    assert has_more is False


async def test_query_filters_by_user_keycloak_id() -> None:
    await _insert_all(
        [
            _entry(timestamp=_NOW - timedelta(minutes=1), user_keycloak_id=_USER_A),
            _entry(timestamp=_NOW - timedelta(minutes=2), user_keycloak_id=_USER_B),
        ]
    )
    async with get_sessionmaker()() as db:
        rows, _ = await AuditLogRepository(db).query(
            filters=AuditLogQueryFilters(user_keycloak_id=_USER_A), page=1, page_size=20
        )
    assert [r.user_keycloak_id for r in rows] == [_USER_A]


async def test_query_filters_by_from_to_timestamp_inclusive() -> None:
    t0 = _NOW - timedelta(minutes=10)
    t1 = _NOW - timedelta(minutes=5)
    t2 = _NOW - timedelta(minutes=2)
    await _insert_all([_entry(timestamp=t0), _entry(timestamp=t1), _entry(timestamp=t2)])
    async with get_sessionmaker()() as db:
        rows, _ = await AuditLogRepository(db).query(
            filters=AuditLogQueryFilters(from_=t1, to=t2), page=1, page_size=20
        )
    assert sorted(r.timestamp for r in rows) == sorted([t1, t2])


async def test_query_filters_by_entity_type_and_entity_id_together() -> None:
    await _insert_all(
        [
            _entry(timestamp=_NOW - timedelta(minutes=1), entity_type="qr", entity_id="DCQR-0001"),
            _entry(timestamp=_NOW - timedelta(minutes=2), entity_type="qr", entity_id="DCQR-0002"),
            _entry(
                timestamp=_NOW - timedelta(minutes=3),
                entity_type="device",
                entity_id="DCQR-0001",
            ),
        ]
    )
    async with get_sessionmaker()() as db:
        rows, _ = await AuditLogRepository(db).query(
            filters=AuditLogQueryFilters(entity_type="qr", entity_id="DCQR-0001"),
            page=1,
            page_size=20,
        )
    assert len(rows) == 1
    assert rows[0].entity_type == "qr"
    assert rows[0].entity_id == "DCQR-0001"


async def test_query_filters_by_operation() -> None:
    await _insert_all(
        [
            _entry(timestamp=_NOW - timedelta(minutes=1), operation="qr.bind"),
            _entry(timestamp=_NOW - timedelta(minutes=2), operation="device.update"),
        ]
    )
    async with get_sessionmaker()() as db:
        rows, _ = await AuditLogRepository(db).query(
            filters=AuditLogQueryFilters(operation="qr.bind"), page=1, page_size=20
        )
    assert [r.operation for r in rows] == ["qr.bind"]


async def test_query_filters_by_session_id() -> None:
    await _insert_all(
        [
            _entry(timestamp=_NOW - timedelta(minutes=1), session_id=_SESSION_X),
            _entry(timestamp=_NOW - timedelta(minutes=2), session_id=_SESSION_Y),
            _entry(timestamp=_NOW - timedelta(minutes=3), session_id=None),
        ]
    )
    async with get_sessionmaker()() as db:
        rows, _ = await AuditLogRepository(db).query(
            filters=AuditLogQueryFilters(session_id=_SESSION_X), page=1, page_size=20
        )
    assert [r.session_id for r in rows] == [_SESSION_X]


async def test_query_filters_by_result_enum() -> None:
    await _insert_all(
        [
            _entry(timestamp=_NOW - timedelta(minutes=1), result=AuditResult.SUCCESS),
            _entry(timestamp=_NOW - timedelta(minutes=2), result=AuditResult.FAILURE),
            _entry(timestamp=_NOW - timedelta(minutes=3), result=AuditResult.CONFLICT),
        ]
    )
    async with get_sessionmaker()() as db:
        rows, _ = await AuditLogRepository(db).query(
            filters=AuditLogQueryFilters(result=AuditResult.FAILURE), page=1, page_size=20
        )
    assert [r.result for r in rows] == [AuditResult.FAILURE]


# --- pagination + has_more --------------------------------------------------


async def test_query_pagination_walks_pages_with_has_more() -> None:
    await _insert_all([_entry(timestamp=_NOW - timedelta(minutes=i + 1)) for i in range(5)])
    async with get_sessionmaker()() as db:
        repo = AuditLogRepository(db)
        page1, has_more1 = await repo.query(filters=AuditLogQueryFilters(), page=1, page_size=2)
        page2, has_more2 = await repo.query(filters=AuditLogQueryFilters(), page=2, page_size=2)
        page3, has_more3 = await repo.query(filters=AuditLogQueryFilters(), page=3, page_size=2)
    assert len(page1) == 2 and has_more1 is True
    assert len(page2) == 2 and has_more2 is True
    assert len(page3) == 1 and has_more3 is False
    # All five distinct rows.
    assert len({r.id for r in page1 + page2 + page3}) == 5


async def test_query_pagination_returns_empty_for_out_of_range_page() -> None:
    await _insert_all([_entry(timestamp=_NOW - timedelta(minutes=1))])
    async with get_sessionmaker()() as db:
        rows, has_more = await AuditLogRepository(db).query(
            filters=AuditLogQueryFilters(), page=10, page_size=20
        )
    assert rows == []
    assert has_more is False


async def test_query_orders_by_timestamp_desc_then_id_desc() -> None:
    """Identical timestamps must have a stable tiebreaker (id DESC) so
    pagination across the timestamp index doesn't return duplicates or skip
    rows between consecutive pages."""
    same_ts = _NOW - timedelta(minutes=5)
    await _insert_all(
        [
            _entry(timestamp=same_ts, entity_id="DCQR-a"),
            _entry(timestamp=same_ts, entity_id="DCQR-b"),
            _entry(timestamp=_NOW - timedelta(minutes=1), entity_id="DCQR-newest"),
        ]
    )
    async with get_sessionmaker()() as db:
        rows, _ = await AuditLogRepository(db).query(
            filters=AuditLogQueryFilters(), page=1, page_size=20
        )
    # Newest first; within identical-timestamp rows, id DESC means insertion-
    # order-LIFO (later-inserted higher id wins).
    assert [r.entity_id for r in rows] == ["DCQR-newest", "DCQR-b", "DCQR-a"]


async def test_query_returns_empty_when_table_empty() -> None:
    async with get_sessionmaker()() as db:
        rows, has_more = await AuditLogRepository(db).query(
            filters=AuditLogQueryFilters(), page=1, page_size=20
        )
    assert rows == []
    assert has_more is False


async def test_query_populates_id_field_on_read() -> None:
    """Sprint 7 Task 2: AuditLogEntry.id (added this sprint) is populated
    when reading rows back through query()."""
    await _insert_all([_entry(timestamp=_NOW - timedelta(minutes=1))])
    async with get_sessionmaker()() as db:
        rows, _ = await AuditLogRepository(db).query(
            filters=AuditLogQueryFilters(), page=1, page_size=20
        )
    assert len(rows) == 1
    assert rows[0].id is not None and rows[0].id > 0


# === Sprint 8b Task 3: get_by_id ============================================


async def test_get_by_id_returns_row_when_present() -> None:
    await _insert_all([_entry(timestamp=_NOW - timedelta(minutes=1))])
    async with get_sessionmaker()() as db:
        rows, _ = await AuditLogRepository(db).query(
            filters=AuditLogQueryFilters(), page=1, page_size=20
        )
        seeded_id = rows[0].id
        assert seeded_id is not None
        fetched = await AuditLogRepository(db).get_by_id(seeded_id)
    assert fetched is not None
    assert fetched.id == seeded_id


async def test_get_by_id_returns_none_for_unknown_id() -> None:
    async with get_sessionmaker()() as db:
        assert await AuditLogRepository(db).get_by_id(999_999_999) is None
