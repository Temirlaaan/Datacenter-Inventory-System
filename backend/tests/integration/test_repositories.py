"""Integration tests for app.db.repositories.

Covers all three repositories against the live test DB. Includes a
statement-count assertion for ``QRCodeRepository.bulk_insert`` (must issue one
multi-row INSERT, not 50 individual statements) and a no-implicit-commit
assertion for ``AuditLogRepository.insert`` (transaction ownership belongs to
the calling service, not the repo).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.repositories import (
    AuditLogRepository,
    QRBatchRepository,
    QRCodeRepository,
    RepositoryError,
)
from app.db.session import get_engine, get_sessionmaker
from app.domain.audit import AuditLogEntry, AuditResult
from app.domain.qr import QR, QRBatch, QRStatus

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


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
        await session.execute(text("TRUNCATE qr_codes, qr_batches, audit_log CASCADE"))
        await session.commit()
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


# --- helpers ------------------------------------------------------------------


def _batch(batch_id: UUID | None = None, *, count: int = 10) -> QRBatch:
    return QRBatch(
        id=batch_id or uuid4(),
        created_at=_NOW,
        created_by_email="alice@example.com",
        created_by_keycloak_id=UUID("11111111-1111-1111-1111-111111111111"),
        count=count,
        intended_site_id=1,
        intended_location_id=2,
        intended_rack_id=3,
        comment="test batch",
    )


def _free_qr(qr_id: str, batch_id: UUID) -> QR:
    return QR(
        id=qr_id,
        batch_id=batch_id,
        status=QRStatus.FREE,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=None,
        retired_reason=None,
    )


def _audit_entry(**overrides: object) -> AuditLogEntry:
    base = dict(
        request_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        timestamp=_NOW,
        user_email="alice@example.com",
        user_keycloak_id=UUID("11111111-1111-1111-1111-111111111111"),
        session_id=None,
        operation="qr.generate_batch",
        entity_type="batch",
        entity_id="some-batch-id",
        before_json={},
        after_json={"count": 10},
        result=AuditResult.SUCCESS,
    )
    base.update(overrides)
    return AuditLogEntry(**base)  # type: ignore[arg-type]


# === QRBatchRepository ========================================================


async def test_qr_batch_repository_insert_then_get_by_id_round_trips() -> None:
    batch = _batch()
    async with get_sessionmaker()() as session:
        repo = QRBatchRepository(session)
        await repo.insert(batch)
        await session.commit()

        fetched = await repo.get_by_id(batch.id)
    assert fetched == batch


async def test_qr_batch_repository_get_by_id_returns_none_for_unknown_uuid() -> None:
    async with get_sessionmaker()() as session:
        repo = QRBatchRepository(session)
        assert await repo.get_by_id(uuid4()) is None


async def test_qr_batch_repository_insert_with_duplicate_id_raises_repository_error() -> None:
    batch = _batch()
    async with get_sessionmaker()() as session:
        repo = QRBatchRepository(session)
        await repo.insert(batch)
        await session.commit()

    async with get_sessionmaker()() as session:
        repo = QRBatchRepository(session)
        with pytest.raises(RepositoryError):
            await repo.insert(batch)


async def test_qr_batch_repository_query_returns_empty_on_empty_table() -> None:
    async with get_sessionmaker()() as session:
        rows, has_more = await QRBatchRepository(session).query(page=1, page_size=20)
    assert rows == []
    assert has_more is False


async def test_qr_batch_repository_query_orders_newest_first() -> None:
    older = _batch()
    newer = _batch()
    older_ts = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
    newer_ts = datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)
    async with get_sessionmaker()() as session:
        repo = QRBatchRepository(session)
        # Insert in reverse-chronological insertion order so the test catches
        # any implicit "ORDER BY inserted-position" assumption.
        await repo.insert(_batch(batch_id=newer.id))
        await repo.insert(_batch(batch_id=older.id))
        await session.execute(
            text("UPDATE qr_batches SET created_at = :ts WHERE id = :id"),
            [
                {"ts": newer_ts, "id": newer.id},
                {"ts": older_ts, "id": older.id},
            ],
        )
        await session.commit()

        rows, _has_more = await repo.query(page=1, page_size=20)
    assert [r.id for r in rows] == [newer.id, older.id]


async def test_qr_batch_repository_query_paginates_with_has_more() -> None:
    """5 rows, page_size=2 → page 1 (2, has_more=True), page 2 (2, has_more=True),
    page 3 (1, has_more=False). Same ``LIMIT page_size + 1`` shape as the audit
    repo's query method."""
    batches = [_batch() for _ in range(5)]
    base_ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    async with get_sessionmaker()() as session:
        repo = QRBatchRepository(session)
        for b in batches:
            await repo.insert(b)
        # Distinct timestamps so the newest-first ordering is deterministic.
        for i, b in enumerate(batches):
            await session.execute(
                text("UPDATE qr_batches SET created_at = :ts WHERE id = :id"),
                {"ts": base_ts + timedelta(minutes=i), "id": b.id},
            )
        await session.commit()

        page1_rows, page1_more = await repo.query(page=1, page_size=2)
        page2_rows, page2_more = await repo.query(page=2, page_size=2)
        page3_rows, page3_more = await repo.query(page=3, page_size=2)

    assert len(page1_rows) == 2 and page1_more is True
    assert len(page2_rows) == 2 and page2_more is True
    assert len(page3_rows) == 1 and page3_more is False
    # Across the three pages, every row appears exactly once.
    seen = {r.id for r in page1_rows + page2_rows + page3_rows}
    assert seen == {b.id for b in batches}


# === QRCodeRepository =========================================================


async def test_qr_code_repository_bulk_insert_then_find_by_batch_id_round_trips() -> None:
    batch = _batch()
    codes = [_free_qr(f"DCQR-AAAA000{i}", batch.id) for i in range(5)]
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert(codes)
        await session.commit()

        fetched = await QRCodeRepository(session).find_by_batch_id(batch.id)
    assert fetched == sorted(codes, key=lambda c: c.id)


async def test_qr_code_repository_find_by_batch_id_returns_codes_sorted_by_id() -> None:
    batch = _batch()
    # Insert deliberately out of alphabetical order.
    codes = [
        _free_qr("DCQR-CCCCCCCC", batch.id),
        _free_qr("DCQR-AAAAAAAA", batch.id),
        _free_qr("DCQR-BBBBBBBB", batch.id),
    ]
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert(codes)
        await session.commit()

        fetched = await QRCodeRepository(session).find_by_batch_id(batch.id)
    assert [c.id for c in fetched] == ["DCQR-AAAAAAAA", "DCQR-BBBBBBBB", "DCQR-CCCCCCCC"]


async def test_qr_code_repository_delete_by_batch_id_removes_all_rows() -> None:
    batch = _batch()
    codes = [_free_qr(f"DCQR-DEL0000{i}", batch.id) for i in range(4)]
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert(codes)
        await session.commit()

        await QRCodeRepository(session).delete_by_batch_id(batch.id)
        await session.commit()

        remaining = await QRCodeRepository(session).find_by_batch_id(batch.id)
    assert remaining == []


async def test_qr_code_repository_delete_by_batch_id_leaves_other_batches() -> None:
    keep = _batch()
    drop = _batch()
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(keep)
        await QRBatchRepository(session).insert(drop)
        await QRCodeRepository(session).bulk_insert([_free_qr("DCQR-KEEP0001", keep.id)])
        await QRCodeRepository(session).bulk_insert([_free_qr("DCQR-DROP0001", drop.id)])
        await session.commit()

        await QRCodeRepository(session).delete_by_batch_id(drop.id)
        await session.commit()

        kept = await QRCodeRepository(session).find_by_batch_id(keep.id)
        dropped = await QRCodeRepository(session).find_by_batch_id(drop.id)
    assert [c.id for c in kept] == ["DCQR-KEEP0001"]
    assert dropped == []


async def test_qr_batch_repository_delete_removes_the_batch_row() -> None:
    batch = _batch()
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await session.commit()

        await QRBatchRepository(session).delete(batch.id)
        await session.commit()

        fetched = await QRBatchRepository(session).get_by_id(batch.id)
    assert fetched is None


async def test_qr_code_repository_get_by_id_returns_qr_when_present() -> None:
    batch = _batch()
    qr = _free_qr("DCQR-AAAAAAAA", batch.id)
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()

        fetched = await QRCodeRepository(session).get_by_id("DCQR-AAAAAAAA")
    assert fetched == qr


async def test_qr_code_repository_get_by_id_returns_none_for_unknown_id() -> None:
    async with get_sessionmaker()() as session:
        assert await QRCodeRepository(session).get_by_id("DCQR-ZZZZZZZZ") is None


async def test_qr_code_repository_search_by_id_substring_finds_partial_match() -> None:
    """``search_by_id_substring`` returns rows whose id contains the fragment,
    case-insensitively. Powers the /web/qr/search "type-7F3A-find-the-7F3A2B"
    UX so admins don't need to remember the full slug."""
    batch = _batch()
    codes = [
        _free_qr("DCQR-7F3A2B01", batch.id),
        _free_qr("DCQR-7F3AC123", batch.id),
        _free_qr("DCQR-ABCD0000", batch.id),  # control — should NOT match "7F3A"
    ]
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert(codes)
        await session.commit()

        # Upper-case fragment matches lower-case stored ids via ILIKE.
        fetched = await QRCodeRepository(session).search_by_id_substring(fragment="7f3a")
    assert {c.id for c in fetched} == {"DCQR-7F3A2B01", "DCQR-7F3AC123"}


async def test_qr_code_repository_search_by_id_substring_empty_fragment_returns_empty_list() -> None:
    """Defensive: empty fragment short-circuits — never accidentally dump the
    whole ``qr_codes`` table."""
    batch = _batch()
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([_free_qr("DCQR-AAAAAAAA", batch.id)])
        await session.commit()

        assert await QRCodeRepository(session).search_by_id_substring(fragment="") == []


async def test_qr_code_repository_search_by_id_substring_respects_limit() -> None:
    """Cap matches at ``limit`` to keep the page render bounded on very loose
    fragments. Ordering by id makes the truncated set deterministic."""
    batch = _batch()
    codes = [_free_qr(f"DCQR-AAAA00{i:02d}", batch.id) for i in range(10)]
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert(codes)
        await session.commit()

        fetched = await QRCodeRepository(session).search_by_id_substring(
            fragment="AAAA", limit=3
        )
    assert len(fetched) == 3
    # Sorted-by-id means the lowest three are returned.
    assert [c.id for c in fetched] == ["DCQR-AAAA0000", "DCQR-AAAA0001", "DCQR-AAAA0002"]


async def test_qr_code_repository_exists_returns_true_for_known_id() -> None:
    batch = _batch()
    qr = _free_qr("DCQR-AAAAAAAA", batch.id)
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()

        assert await QRCodeRepository(session).exists("DCQR-AAAAAAAA") is True


async def test_qr_code_repository_exists_returns_false_for_unknown_id() -> None:
    async with get_sessionmaker()() as session:
        assert await QRCodeRepository(session).exists("DCQR-ZZZZZZZZ") is False


async def test_qr_code_repository_count_by_status_for_batch_returns_per_status_counts() -> None:
    """Mixed batch: 3 free + 2 bound + 1 retired → counts split correctly.
    Bound codes need distinct ``bound_to_device_id`` values because of the
    ``qr_one_per_device`` partial unique index."""
    batch = _batch()
    codes: list[QR] = [_free_qr(f"DCQR-F{i:07d}", batch.id) for i in range(3)]
    for i in range(2):
        codes.append(
            QR(
                id=f"DCQR-B{i:07d}",
                batch_id=batch.id,
                status=QRStatus.BOUND,
                bound_to_device_id=2000 + i,
                bound_at=_NOW,
                bound_by_email="alice@example.com",
                retired_at=None,
                retired_reason=None,
            )
        )
    codes.append(
        QR(
            id="DCQR-R0000000",
            batch_id=batch.id,
            status=QRStatus.RETIRED,
            bound_to_device_id=None,
            bound_at=None,
            bound_by_email=None,
            retired_at=_NOW,
            retired_reason="lost",
        )
    )
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert(codes)
        await session.commit()

        counts = await QRCodeRepository(session).count_by_status_for_batch(batch.id)
    assert counts == {QRStatus.FREE: 3, QRStatus.BOUND: 2, QRStatus.RETIRED: 1}


async def test_qr_code_repository_count_by_status_for_batch_returns_zeros_for_unknown_batch() -> (
    None
):
    """Unknown batch id → all three statuses present with zero counts (no
    exception, no 404). Caller decides whether the batch exists."""
    async with get_sessionmaker()() as session:
        counts = await QRCodeRepository(session).count_by_status_for_batch(uuid4())
    assert counts == {QRStatus.FREE: 0, QRStatus.BOUND: 0, QRStatus.RETIRED: 0}


async def test_qr_code_repository_count_by_status_for_batch_ignores_other_batches() -> None:
    """Codes in another batch must NOT leak into this batch's counts."""
    batch_a = _batch()
    batch_b = _batch()
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch_a)
        await QRBatchRepository(session).insert(batch_b)
        await QRCodeRepository(session).bulk_insert(
            [_free_qr(f"DCQR-A{i:07d}", batch_a.id) for i in range(3)]
            + [_free_qr(f"DCQR-B{i:07d}", batch_b.id) for i in range(7)]
        )
        await session.commit()

        counts_a = await QRCodeRepository(session).count_by_status_for_batch(batch_a.id)
    assert counts_a[QRStatus.FREE] == 3


async def test_qr_code_repository_bulk_insert_empty_list_is_noop() -> None:
    async with get_sessionmaker()() as session:
        await QRCodeRepository(session).bulk_insert([])
        # No exception; nothing inserted.
        result = await session.execute(text("SELECT COUNT(*) FROM qr_codes"))
        assert result.scalar_one() == 0


async def test_qr_code_repository_bulk_insert_50_codes_issues_one_insert_statement() -> None:
    batch = _batch(count=50)
    codes = [_free_qr(f"DCQR-AB0000{i:02d}", batch.id) for i in range(50)]

    statements: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        if "INSERT INTO qr_codes" in statement:
            statements.append(statement)

    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await session.commit()

    engine = get_engine()
    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        async with get_sessionmaker()() as session:
            await QRCodeRepository(session).bulk_insert(codes)
            await session.commit()
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert len(statements) == 1, f"expected 1 INSERT statement, got {len(statements)}"


async def test_qr_code_repository_bulk_insert_duplicate_id_raises_repository_error() -> None:
    batch = _batch()
    qr = _free_qr("DCQR-AAAAAAAA", batch.id)
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()

    async with get_sessionmaker()() as session:
        with pytest.raises(RepositoryError):
            await QRCodeRepository(session).bulk_insert([qr])


async def test_qr_code_repository_round_trips_bound_state() -> None:
    batch = _batch()
    bound = QR(
        id="DCQR-BBBBBBBB",
        batch_id=batch.id,
        status=QRStatus.BOUND,
        bound_to_device_id=42,
        bound_at=_NOW,
        bound_by_email="alice@example.com",
        retired_at=None,
        retired_reason=None,
    )
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([bound])
        await session.commit()

        fetched = await QRCodeRepository(session).get_by_id("DCQR-BBBBBBBB")
    assert fetched == bound
    assert fetched is not None and fetched.status is QRStatus.BOUND


async def test_qr_code_repository_round_trips_retired_state() -> None:
    batch = _batch()
    retired = QR(
        id="DCQR-RRRRRRRR",
        batch_id=batch.id,
        status=QRStatus.RETIRED,
        bound_to_device_id=None,
        bound_at=None,
        bound_by_email=None,
        retired_at=_NOW,
        retired_reason="damaged",
    )
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([retired])
        await session.commit()

        fetched = await QRCodeRepository(session).get_by_id("DCQR-RRRRRRRR")
    assert fetched == retired
    assert fetched is not None and fetched.status is QRStatus.RETIRED


# --- find_by_bound_device_id (Sprint 5 Task 4) ---


async def test_find_by_bound_device_id_returns_qr_when_present() -> None:
    batch = _batch()
    bound = QR(
        id="DCQR-BOUNDED1",
        batch_id=batch.id,
        status=QRStatus.BOUND,
        bound_to_device_id=42,
        bound_at=_NOW,
        bound_by_email="alice@example.com",
        retired_at=None,
        retired_reason=None,
    )
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([bound])
        await session.commit()

        fetched = await QRCodeRepository(session).find_by_bound_device_id(42)
    assert fetched == bound


async def test_find_by_bound_device_id_returns_none_when_no_bound_qr() -> None:
    async with get_sessionmaker()() as session:
        assert await QRCodeRepository(session).find_by_bound_device_id(42) is None


async def test_find_by_bound_device_id_returns_none_when_only_retired_qrs_for_device() -> None:
    """Historical bound_to_device_id is preserved on RETIRED rows (Sprint 2),
    but find_by_bound_device_id filters to status='bound' so they don't match.
    """
    batch = _batch()
    retired_with_history = QR(
        id="DCQR-USEDONCE",
        batch_id=batch.id,
        status=QRStatus.RETIRED,
        bound_to_device_id=42,  # historical
        bound_at=_NOW,
        bound_by_email="alice@example.com",
        retired_at=_NOW,
        retired_reason=None,
    )
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([retired_with_history])
        await session.commit()

        assert await QRCodeRepository(session).find_by_bound_device_id(42) is None


# === AuditLogRepository =======================================================


async def test_audit_log_repository_insert_persists_all_columns() -> None:
    entry = _audit_entry(
        session_id=UUID("22222222-2222-2222-2222-222222222222"),
        before_json={"prev": "x"},
        after_json={"new": "y", "count": 5},
        result=AuditResult.SUCCESS,
    )
    async with get_sessionmaker()() as session:
        await AuditLogRepository(session).insert(entry)
        await session.commit()

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text(
                    "SELECT request_id, user_email, session_id, operation,"
                    " entity_type, entity_id, before_json, after_json, result::text"
                    " FROM audit_log"
                )
            )
        ).one()
    assert row.request_id == entry.request_id
    assert row.user_email == entry.user_email
    assert row.session_id == entry.session_id
    assert row.operation == entry.operation
    assert row.entity_type == entry.entity_type
    assert row.entity_id == entry.entity_id
    assert row.before_json == {"prev": "x"}
    assert row.after_json == {"new": "y", "count": 5}
    assert row.result == "success"


async def test_audit_log_repository_insert_allows_null_session_id() -> None:
    entry = _audit_entry(session_id=None)
    async with get_sessionmaker()() as session:
        await AuditLogRepository(session).insert(entry)
        await session.commit()

    async with get_sessionmaker()() as session:
        result = await session.execute(text("SELECT session_id FROM audit_log"))
        assert result.scalar_one() is None


async def test_audit_log_repository_insert_with_null_user_email_raises_repository_error() -> None:
    # audit_log has no unique/FK constraints; a NOT NULL violation is the
    # cleanest way to confirm the IntegrityError -> RepositoryError wrap.
    entry = _audit_entry(user_email=None)
    async with get_sessionmaker()() as session:
        with pytest.raises(RepositoryError):
            await AuditLogRepository(session).insert(entry)


async def test_audit_log_repository_does_not_commit_implicitly() -> None:
    # Critical for the Task 6 atomicity guarantee: the repo must leave commit
    # ownership to the caller so a single transaction can wrap batch + codes +
    # audit-log writes.
    entry = _audit_entry()
    async with get_sessionmaker()() as session:
        await AuditLogRepository(session).insert(entry)
        # Deliberately no commit.

    # Open a fresh session — the uncommitted row must not be visible.
    async with get_sessionmaker()() as session:
        result = await session.execute(text("SELECT COUNT(*) FROM audit_log"))
        assert result.scalar_one() == 0


# === QRCodeRepository state-transition methods (Sprint 4) ====================


async def test_qr_code_repository_update_persists_status_transition() -> None:
    batch = _batch()
    qr = _free_qr("DCQR-UPDATE01", batch.id)
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()

    bound = qr.bind(device_id=99, by_email="alice@example.com", at=_NOW)
    async with get_sessionmaker()() as session:
        await QRCodeRepository(session).update(bound)
        await session.commit()

    async with get_sessionmaker()() as session:
        fetched = await QRCodeRepository(session).get_by_id("DCQR-UPDATE01")
    assert fetched == bound
    assert fetched is not None and fetched.status is QRStatus.BOUND
    assert fetched.bound_to_device_id == 99


async def test_qr_code_repository_update_does_not_commit_implicitly() -> None:
    batch = _batch()
    qr = _free_qr("DCQR-UPDATE02", batch.id)
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()

    bound = qr.bind(device_id=100, by_email="alice@example.com", at=_NOW)
    async with get_sessionmaker()() as session:
        await QRCodeRepository(session).update(bound)
        # Deliberately no commit.

    # The transition must not be visible in a fresh session.
    async with get_sessionmaker()() as session:
        fetched = await QRCodeRepository(session).get_by_id("DCQR-UPDATE02")
    assert fetched is not None and fetched.status is QRStatus.FREE


async def test_qr_code_repository_update_qr_one_per_device_race_raises_integrity_error() -> None:
    # The Sprint 4 bind orchestration catches IntegrityError on the partial
    # unique index and converts it to QRAlreadyBoundError. So update() must
    # NOT wrap it in RepositoryError (unlike bulk_insert) — the caller needs
    # the specific exception type.
    batch = _batch()
    qr_a = _free_qr("DCQR-UPDATE03", batch.id)
    qr_b = _free_qr("DCQR-UPDATE04", batch.id)
    bound_a = qr_a.bind(device_id=77, by_email="alice@example.com", at=_NOW)
    bound_b = qr_b.bind(device_id=77, by_email="alice@example.com", at=_NOW)

    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([qr_a, qr_b])
        await session.commit()
        await QRCodeRepository(session).update(bound_a)
        await session.commit()

    async with get_sessionmaker()() as session:
        with pytest.raises(IntegrityError):
            await QRCodeRepository(session).update(bound_b)
            await session.commit()


async def test_qr_code_repository_get_by_id_for_update_returns_qr_when_present() -> None:
    batch = _batch()
    qr = _free_qr("DCQR-FORUPD01", batch.id)
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()

    async with get_sessionmaker()() as session:
        async with session.begin():
            fetched = await QRCodeRepository(session).get_by_id_for_update("DCQR-FORUPD01")
    assert fetched == qr


async def test_qr_code_repository_get_by_id_for_update_returns_none_for_unknown_id() -> None:
    async with get_sessionmaker()() as session:
        async with session.begin():
            assert await QRCodeRepository(session).get_by_id_for_update("DCQR-UNKNOWN0") is None


async def test_qr_code_repository_get_by_id_for_update_issues_for_update_clause() -> None:
    batch = _batch()
    qr = _free_qr("DCQR-FORUPD02", batch.id)
    async with get_sessionmaker()() as session:
        await QRBatchRepository(session).insert(batch)
        await QRCodeRepository(session).bulk_insert([qr])
        await session.commit()

    statements: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        if "qr_codes" in statement and "SELECT" in statement.upper():
            statements.append(statement)

    engine = get_engine()
    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        async with get_sessionmaker()() as session:
            async with session.begin():
                await QRCodeRepository(session).get_by_id_for_update("DCQR-FORUPD02")
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert any(
        "FOR UPDATE" in s.upper() for s in statements
    ), f"expected FOR UPDATE in issued SELECT statements, got: {statements}"
