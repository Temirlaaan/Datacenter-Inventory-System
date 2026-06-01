"""Admin shift-session endpoints. ToR §8.3, Sprint 7 Task 3.

- ``GET /api/v1/admin/sessions`` — list shifts with filters + pagination.
  No audit row (decision 8: shift listings are operational, not §5.4.6
  sensitive reads).
- ``POST /api/v1/admin/sessions/{session_id}/force-close`` — end someone
  else's shift with ``end_reason='forced'`` and a mandatory ``reason``
  string. Produces an audit row (real or no-op). Idempotent on
  already-ended targets.

Both gates: ``dcinv-admin`` role + active shift (decision I, consistent
with Sprint 7 Task 2 ``/admin/audit``).

The endpoint orchestrates the force-close multi-record write directly
(repo + audit) rather than going through ``ShiftSessionService.end_by_id``,
because ``ShiftSessionService`` is deliberately NOT part of the
Architecture §3.1 three-record-write apparatus. Task 1's ``end_by_id`` is
for stand-alone callers like the auto-end job, which produces no audit
row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser, require_role_with_active_shift
from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.shift_session import (
    ShiftSessionQueryFilters,
    ShiftSessionRepository,
)
from app.db.session import get_session
from app.domain.audit import AuditLogEntry, AuditResult
from app.domain.shift_session import ShiftEndReason, ShiftSession
from app.observability.request_id import current_request_id

router = APIRouter()


class ShiftSessionResponse(BaseModel):
    """Wire shape of a single shift_sessions row."""

    id: UUID
    user_email: str
    user_keycloak_id: UUID
    shift_start_at: datetime
    shift_end_at: datetime | None = None
    tablet_id: str
    end_reason: ShiftEndReason | None = None


class ShiftSessionListResponse(BaseModel):
    """Envelope returned by ``GET /api/v1/admin/sessions``."""

    results: list[ShiftSessionResponse]
    page: int
    page_size: int
    has_more: bool


class ForceCloseRequest(BaseModel):
    """``POST /api/v1/admin/sessions/{id}/force-close`` payload.

    Decision D: ``reason`` is required (admin must justify) and capped at
    500 chars to bound the audit row's ``after_json`` size.
    """

    reason: str = Field(min_length=1, max_length=500)


def _to_response(shift: ShiftSession) -> ShiftSessionResponse:
    return ShiftSessionResponse(
        id=shift.id,
        user_email=shift.user_email,
        user_keycloak_id=shift.user_keycloak_id,
        shift_start_at=shift.shift_start_at,
        shift_end_at=shift.shift_end_at,
        tablet_id=shift.tablet_id,
        end_reason=shift.end_reason,
    )


def _shift_before_snapshot(shift: ShiftSession) -> dict[str, Any]:
    """Pre-end snapshot for the audit row's ``before_json``."""
    return {
        "shift_start_at": shift.shift_start_at.isoformat(),
        "user_keycloak_id": str(shift.user_keycloak_id),
        "user_email": shift.user_email,
        "tablet_id": shift.tablet_id,
        "active": shift.is_active,
    }


@router.get(
    "",
    response_model=ShiftSessionListResponse,
    response_model_exclude_none=True,
)
async def list_sessions(
    user_keycloak_id: UUID | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    active_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-admin")),
    session: AsyncSession = Depends(get_session),
) -> ShiftSessionListResponse:
    """List shifts with offset pagination. No audit row (decision 8)."""
    _ = user  # role-gating side effect only; reads aren't audited here
    repo = ShiftSessionRepository(session)
    filters = ShiftSessionQueryFilters(
        user_keycloak_id=user_keycloak_id, from_=from_, to=to, active_only=active_only
    )
    rows, has_more = await repo.query(filters=filters, page=page, page_size=page_size)
    return ShiftSessionListResponse(
        results=[_to_response(r) for r in rows],
        page=page,
        page_size=page_size,
        has_more=has_more,
    )


@router.post(
    "/{session_id}/force-close",
    response_model=ShiftSessionResponse,
    response_model_exclude_none=True,
)
async def force_close_session(
    session_id: UUID,
    body: ForceCloseRequest,
    user: AuthUser = Depends(require_role_with_active_shift("dcinv-admin")),
    session: AsyncSession = Depends(get_session),
) -> ShiftSessionResponse:
    """End ``session_id`` with ``end_reason='forced'``.

    Idempotent: a target that's already ended returns 200 with its current
    state plus an audit row carrying ``result=CONFLICT`` and
    ``after_json.no_op=True``. Unknown ``session_id`` returns 404 (no audit
    row — admin typed the wrong id, not a state-change conflict).
    """
    shift_repo = ShiftSessionRepository(session)
    audit_repo = AuditLogRepository(session)
    request_uuid = UUID(current_request_id())
    now = datetime.now(UTC)

    async with session.begin():
        existing = await shift_repo.get_by_id(session_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="shift session not found"
            )

        if not existing.is_active:
            # Idempotent no-op: audit it (result=CONFLICT) and return current state.
            await audit_repo.insert(
                AuditLogEntry(
                    request_id=request_uuid,
                    timestamp=now,
                    user_email=user.email or "",
                    user_keycloak_id=UUID(user.sub),
                    session_id=user.shift_session_id,
                    operation="shift_session.force_close",
                    entity_type="shift_session",
                    entity_id=str(session_id),
                    before_json=_shift_before_snapshot(existing),
                    after_json={
                        "reason": body.reason,
                        "end_reason": (existing.end_reason.value if existing.end_reason else None),
                        "shift_end_at": (
                            existing.shift_end_at.isoformat() if existing.shift_end_at else None
                        ),
                        "no_op": True,
                    },
                    result=AuditResult.CONFLICT,
                )
            )
            return _to_response(existing)

        # Real force-close: end the shift, then audit it.
        ended = existing.end(reason=ShiftEndReason.FORCED, at=now)
        await shift_repo.update(ended)
        await audit_repo.insert(
            AuditLogEntry(
                request_id=request_uuid,
                timestamp=now,
                user_email=user.email or "",
                user_keycloak_id=UUID(user.sub),
                session_id=user.shift_session_id,
                operation="shift_session.force_close",
                entity_type="shift_session",
                entity_id=str(session_id),
                before_json=_shift_before_snapshot(existing),
                after_json={
                    "reason": body.reason,
                    "end_reason": ShiftEndReason.FORCED.value,
                    "shift_end_at": ended.shift_end_at.isoformat() if ended.shift_end_at else None,
                },
                result=AuditResult.SUCCESS,
            )
        )

    return _to_response(ended)
