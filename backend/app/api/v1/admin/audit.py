"""Admin audit-log query endpoint. ToR §8.3, Sprint 7 Task 2.

``GET /api/v1/admin/audit`` — paginated read of the ``audit_log`` table with
eight filters per decision C of docs/sprint-7.md. Role
``dcinv-admin`` + active shift (decision I). Produces its own audit row per
ToR §5.4.6.

Pagination: 1-indexed ``page`` + ``page_size`` (default 20, max 100). The
repository computes ``has_more`` via ``LIMIT page_size + 1`` so there is no
``COUNT(*)`` round-trip — at 2-year retention x ~50 ops/day, that matters.

Audit-of-audits row per decision I:
- ``operation="audit.query"``, ``entity_type="audit"``, ``entity_id="search"``
  (the last hard-coded so this endpoint's audit rows are themselves queryable
  via ``?entity_type=audit&entity_id=search``)
- ``before_json={}``, ``after_json={"filters": ..., "results_count": N}``
- ``result=SUCCESS`` if the query returns; ``FAILURE`` if it raises
- Same transaction as the user-facing query — if the audit insert fails, the
  endpoint returns 500. "Read-without-audit" is forbidden.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser, require_role_with_active_shift
from app.db.repositories.audit_log import AuditLogQueryFilters, AuditLogRepository
from app.db.session import get_session
from app.domain.audit import AuditLogEntry, AuditResult
from app.observability.request_id import current_request_id

router = APIRouter()


class AuditLogEntryResponse(BaseModel):
    """Wire shape of a single audit_log row."""

    id: int
    request_id: UUID
    timestamp: datetime
    user_email: str
    user_keycloak_id: UUID
    session_id: UUID | None = None
    operation: str
    entity_type: str
    entity_id: str
    before_json: dict[str, Any]
    after_json: dict[str, Any]
    result: AuditResult


class AuditLogQueryResponse(BaseModel):
    """Envelope returned by ``GET /api/v1/admin/audit``."""

    results: list[AuditLogEntryResponse]
    page: int
    page_size: int
    has_more: bool


def _to_response(entry: AuditLogEntry) -> AuditLogEntryResponse:
    # ``entry.id`` is guaranteed non-None on the read path — the query method
    # populates it from the persisted BIGSERIAL. Defensive cast for mypy.
    assert entry.id is not None, "audit_log row read without persisted id"
    return AuditLogEntryResponse(
        id=entry.id,
        request_id=entry.request_id,
        timestamp=entry.timestamp,
        user_email=entry.user_email,
        user_keycloak_id=entry.user_keycloak_id,
        session_id=entry.session_id,
        operation=entry.operation,
        entity_type=entry.entity_type,
        entity_id=entry.entity_id,
        before_json=entry.before_json,
        after_json=entry.after_json,
        result=entry.result,
    )


def _filters_as_dict(filters: AuditLogQueryFilters, *, page: int, page_size: int) -> dict[str, Any]:
    """Serialize filters + pagination params for the audit-of-audits ``after_json``.

    None-valued filters are dropped so the audit row only records what was
    actually constrained. ``from_`` is renamed to the wire spelling ``from``
    so the recorded filter mirrors what the admin sent.
    """
    out: dict[str, Any] = {}
    if filters.user_keycloak_id is not None:
        out["user_keycloak_id"] = str(filters.user_keycloak_id)
    if filters.from_ is not None:
        out["from"] = filters.from_.isoformat()
    if filters.to is not None:
        out["to"] = filters.to.isoformat()
    if filters.entity_type is not None:
        out["entity_type"] = filters.entity_type
    if filters.entity_id is not None:
        out["entity_id"] = filters.entity_id
    if filters.operation is not None:
        out["operation"] = filters.operation
    if filters.session_id is not None:
        out["session_id"] = str(filters.session_id)
    if filters.result is not None:
        out["result"] = filters.result.value
    out["page"] = page
    out["page_size"] = page_size
    return out


@router.get(
    "",
    response_model=AuditLogQueryResponse,
    description=(
        "Paginated query of the audit_log table. Filters narrow the result"
        " set; pagination uses 1-indexed `page` + `page_size` (max 100) with"
        " `has_more` for next-page detection."
        "\n\n**`session_id` filter — semantic note (decision J):** audit rows"
        " before 2026-05-30 contain JWT session IDs (ephemeral; rotate within"
        " a shift). Rows from 2026-05-30 onward contain `shift_sessions.id`"
        " (one per engineer-shift). For historical data, query by"
        " `user_keycloak_id` + date range instead."
    ),
)
async def query_audit_log(
    user_keycloak_id: UUID | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    operation: str | None = Query(default=None),
    session_id: UUID | None = Query(default=None),
    result: AuditResult | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-admin")),
    session: AsyncSession = Depends(get_session),
) -> AuditLogQueryResponse:
    """Query the audit log and produce an audit-of-audits row in one transaction."""
    repo = AuditLogRepository(session)
    filters = AuditLogQueryFilters(
        user_keycloak_id=user_keycloak_id,
        from_=from_,
        to=to,
        entity_type=entity_type,
        entity_id=entity_id,
        operation=operation,
        session_id=session_id,
        result=result,
    )
    request_uuid = UUID(current_request_id())

    try:
        rows, has_more = await repo.query(filters=filters, page=page, page_size=page_size)
        query_result = AuditResult.SUCCESS
    except Exception:
        # Record the failed-query audit row even if the query itself raised,
        # then re-raise so the request returns 500. The audit insert runs in
        # the same tx — if it also fails, both roll back and the admin sees 500.
        await repo.insert(
            AuditLogEntry(
                request_id=request_uuid,
                timestamp=datetime.now(UTC),
                user_email=user.email or "",
                user_keycloak_id=UUID(user.sub),
                session_id=user.shift_session_id,
                operation="audit.query",
                entity_type="audit",
                entity_id="search",
                before_json={},
                after_json={"filters": _filters_as_dict(filters, page=page, page_size=page_size)},
                result=AuditResult.FAILURE,
            )
        )
        await session.commit()
        raise

    await repo.insert(
        AuditLogEntry(
            request_id=request_uuid,
            timestamp=datetime.now(UTC),
            user_email=user.email or "",
            user_keycloak_id=UUID(user.sub),
            session_id=user.shift_session_id,
            operation="audit.query",
            entity_type="audit",
            entity_id="search",
            before_json={},
            after_json={
                "filters": _filters_as_dict(filters, page=page, page_size=page_size),
                "results_count": len(rows),
            },
            result=query_result,
        )
    )
    await session.commit()

    return AuditLogQueryResponse(
        results=[_to_response(r) for r in rows],
        page=page,
        page_size=page_size,
        has_more=has_more,
    )
