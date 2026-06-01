"""Unit tests for GET /api/v1/admin/audit (Sprint 7 Task 2).

Two styles per the project convention:
- Direct-await handler tests for filter coercion, audit-of-audits row shape,
  pagination defaults, and the failure-path audit row (coverage traces these
  reliably; AsyncClient does not for `await`'d returns inside ASGI).
- AsyncClient tests for role gating + the active-shift gate + query-param
  validation (page_size cap, ISO-8601 parsing of from/to).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.audit import (
    AuditLogQueryResponse,
    _filters_as_dict,
    query_audit_log,
)
from app.auth.dependencies import AuthUser
from app.db.repositories.audit_log import AuditLogQueryFilters, AuditLogRepository
from app.db.session import get_sessionmaker
from app.domain.audit import AuditLogEntry, AuditResult
from tests.unit.api.v1.conftest import make_user

pytestmark = pytest.mark.integration

_REQUEST_ID = "33333333-3333-3333-3333-333333333333"
_SHIFT_SESSION_ID = UUID("33333333-3333-3333-3333-333333333333")


def _admin_user(*, shift_session_id: UUID | None = _SHIFT_SESSION_ID) -> AuthUser:
    """An admin AuthUser with shift_session_id populated (as the dep layer would)."""
    return dataclasses.replace(make_user("dcinv-admin"), shift_session_id=shift_session_id)


def _bind_request_id() -> None:
    """Mimic the request_id middleware so current_request_id() returns ours."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=_REQUEST_ID)


def _seed_entry(
    *,
    timestamp: datetime,
    user_keycloak_id: UUID | None = None,
    entity_type: str = "qr",
    entity_id: str = "DCQR-0001",
    operation: str = "qr.bind",
    result: AuditResult = AuditResult.SUCCESS,
) -> AuditLogEntry:
    return AuditLogEntry(
        request_id=uuid4(),
        timestamp=timestamp,
        user_email="alice@example.com",
        user_keycloak_id=user_keycloak_id or UUID("11111111-1111-1111-1111-111111111111"),
        session_id=None,
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json={},
        after_json={},
        result=result,
    )


# ---------- _filters_as_dict --------------------------------------------------


def test_filters_as_dict_drops_none_values_and_renames_from_alias() -> None:
    filters = AuditLogQueryFilters(
        entity_type="qr",
        from_=datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC),
    )
    out = _filters_as_dict(filters, page=2, page_size=50)
    assert "entity_type" in out and out["entity_type"] == "qr"
    assert "from" in out  # alias, not "from_"
    assert "from_" not in out
    assert "to" not in out  # None — dropped
    assert "user_keycloak_id" not in out
    assert out["page"] == 2
    assert out["page_size"] == 50


def test_filters_as_dict_serializes_all_filter_values() -> None:
    filters = AuditLogQueryFilters(
        user_keycloak_id=UUID("11111111-1111-1111-1111-111111111111"),
        from_=datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC),
        to=datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC),
        entity_type="qr",
        entity_id="DCQR-0001",
        operation="qr.bind",
        session_id=UUID("22222222-2222-2222-2222-222222222222"),
        result=AuditResult.FAILURE,
    )
    out = _filters_as_dict(filters, page=1, page_size=20)
    assert out["user_keycloak_id"] == "11111111-1111-1111-1111-111111111111"
    assert out["from"] == "2026-06-01T00:00:00+00:00"
    assert out["to"] == "2026-06-30T23:59:59+00:00"
    assert out["entity_type"] == "qr"
    assert out["entity_id"] == "DCQR-0001"
    assert out["operation"] == "qr.bind"
    assert out["session_id"] == "22222222-2222-2222-2222-222222222222"
    assert out["result"] == "failure"  # enum -> string value


# ---------- query_audit_log handler (direct await) ----------------------------


async def test_query_audit_log_returns_envelope_with_results_and_pagination(
    session: AsyncSession,
) -> None:
    _bind_request_id()
    # Seed two rows so the result set is non-empty.
    async with get_sessionmaker()() as db:
        repo = AuditLogRepository(db)
        await repo.insert(_seed_entry(timestamp=datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)))
        await repo.insert(_seed_entry(timestamp=datetime(2026, 6, 1, 11, 0, 0, tzinfo=UTC)))
        await db.commit()

    result = await query_audit_log(
        user_keycloak_id=None,
        from_=None,
        to=None,
        entity_type=None,
        entity_id=None,
        operation=None,
        session_id=None,
        result=None,
        page=1,
        page_size=20,
        user=_admin_user(),
        session=session,
    )

    assert isinstance(result, AuditLogQueryResponse)
    assert result.page == 1
    assert result.page_size == 20
    assert result.has_more is False
    # The query reads BEFORE inserting its own audit-of-audits row, so the
    # caller sees only the two seeded rows — not three.
    assert len(result.results) == 2
    # And the audit-of-audits row IS persisted by the same transaction.
    async with get_sessionmaker()() as db:
        audit_count = await db.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE operation = 'audit.query'")
        )
        assert audit_count.scalar_one() == 1


async def test_query_audit_log_writes_audit_of_audits_row_with_results_count(
    session: AsyncSession,
) -> None:
    _bind_request_id()
    async with get_sessionmaker()() as db:
        await AuditLogRepository(db).insert(
            _seed_entry(
                timestamp=datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
                entity_type="qr",
                entity_id="DCQR-1",
            )
        )
        await db.commit()

    await query_audit_log(
        user_keycloak_id=None,
        from_=None,
        to=None,
        entity_type="qr",
        entity_id=None,
        operation=None,
        session_id=None,
        result=None,
        page=1,
        page_size=20,
        user=_admin_user(),
        session=session,
    )

    async with get_sessionmaker()() as db:
        rows = await db.execute(
            text(
                "SELECT operation, entity_type, entity_id, after_json, session_id"
                " FROM audit_log WHERE operation = 'audit.query'"
            )
        )
        audit_rows = rows.fetchall()
    assert len(audit_rows) == 1
    op, etype, eid, after, sess = audit_rows[0]
    assert op == "audit.query"
    assert etype == "audit"
    assert eid == "search"
    # results_count must reflect rows returned to the caller (not LIMIT+1 slop).
    assert after["results_count"] == 1
    # Filters as-passed (entity_type was the only filter).
    assert after["filters"]["entity_type"] == "qr"
    assert after["filters"]["page"] == 1
    assert sess == _SHIFT_SESSION_ID


async def test_query_audit_log_records_zero_results_count_on_empty_result(
    session: AsyncSession,
) -> None:
    _bind_request_id()

    result = await query_audit_log(
        user_keycloak_id=None,
        from_=None,
        to=None,
        entity_type="never-matches",
        entity_id=None,
        operation=None,
        session_id=None,
        result=None,
        page=1,
        page_size=20,
        user=_admin_user(),
        session=session,
    )

    assert result.results == []
    assert result.has_more is False
    async with get_sessionmaker()() as db:
        rows = await db.execute(
            text("SELECT after_json FROM audit_log WHERE operation = 'audit.query'")
        )
        row = rows.fetchone()
    assert row is not None
    assert row[0]["results_count"] == 0


async def test_query_audit_log_failure_path_writes_failure_audit_row(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the query raises, the audit-of-audits row must still be written with
    result=FAILURE, and the original exception must propagate."""
    _bind_request_id()
    from app.api.v1.admin import audit as audit_mod

    boom = RuntimeError("simulated query failure")

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

    with pytest.raises(RuntimeError, match="simulated query failure"):
        await query_audit_log(
            user_keycloak_id=None,
            from_=None,
            to=None,
            entity_type=None,
            entity_id=None,
            operation=None,
            session_id=None,
            result=None,
            page=1,
            page_size=20,
            user=_admin_user(),
            session=session,
        )

    async with get_sessionmaker()() as db:
        rows = await db.execute(
            text(
                "SELECT result, operation, after_json"
                " FROM audit_log WHERE operation = 'audit.query'"
            )
        )
        records = rows.fetchall()
    assert len(records) == 1
    res, op, after = records[0]
    assert res == "failure"
    assert op == "audit.query"
    # results_count is absent on the failure path since the count is undefined.
    assert "results_count" not in after


async def test_query_audit_log_paginates_with_has_more_true(
    session: AsyncSession,
) -> None:
    _bind_request_id()
    # Seed enough rows that page_size=2 will trip has_more.
    async with get_sessionmaker()() as db:
        repo = AuditLogRepository(db)
        for i in range(5):
            await repo.insert(_seed_entry(timestamp=datetime(2026, 6, 1, 10, i, 0, tzinfo=UTC)))
        await db.commit()

    result = await query_audit_log(
        user_keycloak_id=None,
        from_=None,
        to=None,
        entity_type="qr",  # narrow so the audit-of-audits row doesn't crowd the page
        entity_id=None,
        operation=None,
        session_id=None,
        result=None,
        page=1,
        page_size=2,
        user=_admin_user(),
        session=session,
    )

    assert len(result.results) == 2
    assert result.has_more is True


# ---------- routing + role + active-shift gating (AsyncClient) ----------------


async def test_get_audit_returns_403_when_caller_lacks_admin_role(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-mobile-user")  # not admin
    resp = await client.get("/api/v1/admin/audit")
    assert resp.status_code == 403


async def test_get_audit_returns_409_no_active_shift_when_admin_has_no_shift(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    """Decision I: admin endpoint is gated by require_role_with_active_shift."""
    # Wipe the shift the conftest just seeded.
    async with get_sessionmaker()() as db:
        await db.execute(text("TRUNCATE shift_sessions CASCADE"))
        await db.commit()
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/audit")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NO_ACTIVE_SHIFT"


async def test_get_audit_rejects_page_size_above_cap_with_422(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/audit?page_size=101")
    assert resp.status_code == 422


async def test_get_audit_rejects_page_below_one_with_422(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/audit?page=0")
    assert resp.status_code == 422


async def test_get_audit_accepts_iso8601_from_to(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.get(
        "/api/v1/admin/audit?from=2026-06-01T00:00:00%2B00:00&to=2026-06-30T23:59:59%2B00:00"
    )
    assert resp.status_code == 200, resp.text


async def test_get_audit_includes_session_id_semantic_note_in_openapi(
    client: httpx.AsyncClient,
) -> None:
    """Decision J: the endpoint description must call out the pre-Sprint-6
    JWT-sid semantic vs the post-Sprint-6 shift_sessions.id semantic so
    web admins reading the OpenAPI docs understand."""
    from app.main import app

    schema = app.openapi()
    paths = schema.get("paths", {})
    path = paths.get("/api/v1/admin/audit", {}) or paths.get("/api/v1/admin/audit/", {})
    description = (path.get("get") or {}).get("description") or ""
    assert "session_id" in description.lower()
    assert "2026-05-30" in description
    assert "user_keycloak_id" in description


async def test_get_audit_returns_200_with_envelope(
    client: httpx.AsyncClient, as_user: Callable[..., AuthUser]
) -> None:
    as_user("dcinv-admin")
    resp = await client.get("/api/v1/admin/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert {"results", "page", "page_size", "has_more"} <= set(body.keys())
    assert body["page"] == 1
    assert body["page_size"] == 20


# Suppress unused-import warning for cast — kept for future use in stubs.
_ = cast
