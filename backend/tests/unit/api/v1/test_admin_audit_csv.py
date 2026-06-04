"""Unit + integration tests for GET /api/v1/admin/audit/csv (Sprint 8b Task 3).

Same direct-await + AsyncClient split as ``test_admin_audit.py``. The CSV
endpoint shares filter handling with the JSON endpoint; tests here focus on
the CSV-specific behaviour: header row, row encoding, audit-of-audits row
shape, failure path, page_size cap.
"""

from __future__ import annotations

import csv
import dataclasses
import io
from collections.abc import AsyncIterable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.audit import (
    _CSV_COLUMNS,
    _CSV_PAGE_SIZE_MAX,
    query_audit_log_csv,
)
from app.auth.dependencies import AuthUser
from app.db.repositories.audit_log import AuditLogQueryFilters, AuditLogRepository
from app.db.session import get_sessionmaker
from app.domain.audit import AuditLogEntry, AuditResult
from tests.unit.api.v1.conftest import make_user

pytestmark = pytest.mark.integration

_REQUEST_ID = "33333333-3333-3333-3333-333333333333"
_SHIFT_SESSION_ID = UUID("33333333-3333-3333-3333-333333333333")
_USER_KEYCLOAK_ID = UUID("11111111-1111-1111-1111-111111111111")


def _admin_user(*, shift_session_id: UUID | None = _SHIFT_SESSION_ID) -> AuthUser:
    return dataclasses.replace(make_user("dcinv-admin"), shift_session_id=shift_session_id)


def _bind_request_id() -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=_REQUEST_ID)


def _seed_entry(
    *,
    timestamp: datetime,
    entity_type: str = "qr",
    entity_id: str = "DCQR-0001",
    operation: str = "qr.bind",
    result: AuditResult = AuditResult.SUCCESS,
) -> AuditLogEntry:
    return AuditLogEntry(
        request_id=uuid4(),
        timestamp=timestamp,
        user_email="alice@example.com",
        user_keycloak_id=_USER_KEYCLOAK_ID,
        session_id=None,
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json={},
        after_json={"k": "v"},
        result=result,
    )


async def _collect_body(stream_iter: AsyncIterable[bytes | str]) -> bytes:
    """Drain a StreamingResponse's body_iterator into bytes."""
    chunks: list[bytes] = []
    async for chunk in stream_iter:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    return b"".join(chunks)


# ---------- header + content shape ------------------------------------------


async def test_csv_handler_returns_streaming_response_with_text_csv_media_type(
    session: AsyncSession,
) -> None:
    _bind_request_id()
    resp = await query_audit_log_csv(
        user_keycloak_id=None,
        from_=None,
        to=None,
        entity_type=None,
        entity_id=None,
        operation=None,
        session_id=None,
        result=None,
        page_size=1000,
        user=_admin_user(),
        session=session,
    )
    assert resp.media_type == "text/csv"
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert ".csv" in disposition


async def test_csv_body_starts_with_header_row(
    session: AsyncSession,
) -> None:
    _bind_request_id()
    resp = await query_audit_log_csv(
        user_keycloak_id=None,
        from_=None,
        to=None,
        entity_type=None,
        entity_id=None,
        operation=None,
        session_id=None,
        result=None,
        page_size=1000,
        user=_admin_user(),
        session=session,
    )
    body = await _collect_body(resp.body_iterator)
    first_line = body.decode("utf-8").splitlines()[0]
    assert first_line == ",".join(_CSV_COLUMNS)


async def test_csv_encodes_seeded_rows_with_full_column_set(
    session: AsyncSession,
) -> None:
    _bind_request_id()
    async with get_sessionmaker()() as db:
        await AuditLogRepository(db).insert(
            _seed_entry(
                timestamp=datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
                entity_id="DCQR-CSV001",
            )
        )
        await db.commit()

    resp = await query_audit_log_csv(
        user_keycloak_id=None,
        from_=None,
        to=None,
        entity_type="qr",
        entity_id="DCQR-CSV001",
        operation=None,
        session_id=None,
        result=None,
        page_size=1000,
        user=_admin_user(),
        session=session,
    )
    body = await _collect_body(resp.body_iterator)
    reader = csv.DictReader(io.StringIO(body.decode("utf-8")))
    rows = list(reader)
    assert len(rows) == 1
    row = rows[0]
    assert row["entity_id"] == "DCQR-CSV001"
    assert row["operation"] == "qr.bind"
    assert row["result"] == "success"
    # JSONB columns round-trip as compact JSON.
    assert row["after_json"] == '{"k":"v"}'
    # Timestamp uses ISO-8601 with UTC offset.
    assert row["timestamp"].startswith("2026-06-01T10:00:00")


# ---------- audit-of-audits row ---------------------------------------------


async def test_csv_writes_audit_of_audits_row_with_rows_exported_count(
    session: AsyncSession,
) -> None:
    _bind_request_id()
    async with get_sessionmaker()() as db:
        await AuditLogRepository(db).insert(
            _seed_entry(timestamp=datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC))
        )
        await db.commit()

    resp = await query_audit_log_csv(
        user_keycloak_id=None,
        from_=None,
        to=None,
        entity_type="qr",
        entity_id=None,
        operation=None,
        session_id=None,
        result=None,
        page_size=1000,
        user=_admin_user(),
        session=session,
    )
    # Drain the body so the audit row's "rows_exported" can be reconciled
    # against what the client actually got.
    body = await _collect_body(resp.body_iterator)
    data_lines = [line for line in body.decode("utf-8").splitlines() if line][1:]
    assert len(data_lines) == 1

    async with get_sessionmaker()() as db:
        rows = await db.execute(
            text(
                "SELECT operation, entity_type, entity_id, after_json, session_id, result"
                " FROM audit_log WHERE operation = 'audit.export_csv'"
            )
        )
        records = rows.fetchall()
    assert len(records) == 1
    op, etype, eid, after, sess, res = records[0]
    assert op == "audit.export_csv"
    assert etype == "audit"
    assert eid == "export"
    assert after["rows_exported"] == 1
    assert after["filters"]["entity_type"] == "qr"
    assert sess == _SHIFT_SESSION_ID
    assert res == "success"


async def test_csv_failure_path_writes_failure_audit_row_and_reraises(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The query raises (e.g., DB error). The audit-of-audits row must still
    be written with ``result=FAILURE`` and the exception must propagate."""
    _bind_request_id()
    from app.api.v1.admin import audit as audit_mod

    boom = RuntimeError("simulated csv-export failure")

    class _BoomRepo:
        def __init__(self, _session: AsyncSession) -> None:
            self._real = AuditLogRepository(_session)

        async def query(
            self, *, filters: AuditLogQueryFilters, page: int, page_size: int
        ) -> tuple[list[AuditLogEntry], bool]:
            raise boom

        async def insert(self, entry: AuditLogEntry) -> None:
            await self._real.insert(entry)

    monkeypatch.setattr(audit_mod, "AuditLogRepository", _BoomRepo)

    with pytest.raises(RuntimeError, match="simulated csv-export failure"):
        await query_audit_log_csv(
            user_keycloak_id=None,
            from_=None,
            to=None,
            entity_type=None,
            entity_id=None,
            operation=None,
            session_id=None,
            result=None,
            page_size=1000,
            user=_admin_user(),
            session=session,
        )

    async with get_sessionmaker()() as db:
        rows = await db.execute(
            text("SELECT result, after_json FROM audit_log" " WHERE operation = 'audit.export_csv'")
        )
        records = rows.fetchall()
    assert len(records) == 1
    res, after = records[0]
    assert res == "failure"
    # rows_exported is absent on the failure path (count is undefined).
    assert "rows_exported" not in after


# ---------- routing + role + active-shift + page_size cap -------------------


async def test_csv_returns_403_when_caller_lacks_admin_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")
    resp = await client.get("/api/v1/admin/audit/csv")
    assert resp.status_code == 403


async def test_csv_returns_409_no_active_shift_when_admin_has_no_shift(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    async with get_sessionmaker()() as db:
        await db.execute(text("TRUNCATE shift_sessions CASCADE"))
        await db.commit()
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/audit/csv")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NO_ACTIVE_SHIFT"


async def test_csv_rejects_page_size_above_cap_with_422(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.get(f"/api/v1/admin/audit/csv?page_size={_CSV_PAGE_SIZE_MAX + 1}")
    assert resp.status_code == 422


async def test_csv_e2e_returns_text_csv_with_header(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/audit/csv?page_size=10")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.text
    assert body.splitlines()[0] == ",".join(_CSV_COLUMNS)


# Suppress unused warning for timedelta — kept for future test extensions.
_ = timedelta
