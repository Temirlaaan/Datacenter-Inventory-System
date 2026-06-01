"""Shift-session endpoints. ToR §4.1.3 + UC-5.

- ``POST /api/v1/sessions/start`` — open the engineer's shift. Body
  ``{tablet_id}``. 409 ``SESSION_ALREADY_ACTIVE`` (carries the existing shift
  per decision B) if one is already open for the JWT-identified user.
- ``POST /api/v1/sessions/end`` — end the user's active shift. Body
  ``{end_reason}`` restricted to ``manual`` / ``auto_timeout`` per
  decision E (``forced`` is admin-only — written by the Sprint 7 admin
  force-close endpoint). 409 ``NO_ACTIVE_SHIFT`` if there isn't one.
- ``GET /api/v1/sessions/active`` — return the caller's active shift or
  ``{"session": null}`` if none.

All three routes require role ``dcinv-mobile-user`` (decision I). The
backend does NOT call Keycloak's revoke endpoint on ``/end`` — mobile owns
that per decision J.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthUser, require_role
from app.db.repositories.shift_session import ShiftSessionRepository
from app.db.session import get_session
from app.domain.shift_session import ShiftEndReason, ShiftSession
from app.services.shift_session import (
    NoActiveShift,
    SessionAlreadyActive,
    ShiftSessionService,
)

router = APIRouter()


class SessionInfo(BaseModel):
    """Wire shape of a single shift session.

    ``shift_end_at`` and ``end_reason`` are present only on closed shifts;
    ``response_model_exclude_none=True`` on the start/end/get-active routes
    drops them from the active-shift wire format.
    """

    id: UUID
    user_email: str
    user_keycloak_id: UUID
    shift_start_at: datetime
    shift_end_at: datetime | None = None
    tablet_id: str
    end_reason: ShiftEndReason | None = None


class SessionResponse(BaseModel):
    """Outer envelope for all three session endpoints.

    ``session`` is ``None`` only on ``GET /active`` when no shift exists —
    that route returns a raw JSONResponse to preserve the explicit ``null``
    (decision: 200 + ``{"session": null}`` so mobile can use a single
    null-check rather than catching a 404).
    """

    session: SessionInfo | None = None


class SessionStartRequest(BaseModel):
    """``POST /api/v1/sessions/start`` payload."""

    model_config = ConfigDict(extra="forbid")

    tablet_id: str = Field(min_length=1)


class SessionEndRequest(BaseModel):
    """``POST /api/v1/sessions/end`` payload.

    Decision E: ``end_reason`` is restricted to ``manual`` and
    ``auto_timeout`` at the wire layer. ``forced`` is admin-only (written by
    the Sprint 7 admin force-close endpoint) and is rejected with 422 here.
    """

    model_config = ConfigDict(extra="forbid")

    end_reason: Literal["manual", "auto_timeout"]


def _to_session_info(session: ShiftSession) -> SessionInfo:
    return SessionInfo(
        id=session.id,
        user_email=session.user_email,
        user_keycloak_id=session.user_keycloak_id,
        shift_start_at=session.shift_start_at,
        shift_end_at=session.shift_end_at,
        tablet_id=session.tablet_id,
        end_reason=session.end_reason,
    )


def get_shift_session_service(
    session: AsyncSession = Depends(get_session),
) -> ShiftSessionService:
    """Build a per-request ``ShiftSessionService``."""
    return ShiftSessionService(session=session, repo=ShiftSessionRepository(session))


@router.post(
    "/start",
    response_model=SessionResponse,
    response_model_exclude_none=True,
)
async def start_session(
    request: SessionStartRequest,
    user: AuthUser = Depends(require_role("dcinv-mobile-user")),
    service: ShiftSessionService = Depends(get_shift_session_service),
) -> SessionResponse | JSONResponse:
    """Open a new active shift for the JWT-identified user."""
    try:
        started = await service.start(
            user_email=user.email or "",
            user_keycloak_id=UUID(user.sub),
            tablet_id=request.tablet_id,
        )
    except SessionAlreadyActive as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "SESSION_ALREADY_ACTIVE",
                    "message": "A shift is already active for this user.",
                    "active": _to_session_info(exc.active).model_dump(mode="json"),
                }
            },
        )
    return SessionResponse(session=_to_session_info(started))


@router.post(
    "/end",
    response_model=SessionResponse,
    response_model_exclude_none=True,
)
async def end_session(
    request: SessionEndRequest,
    user: AuthUser = Depends(require_role("dcinv-mobile-user")),
    service: ShiftSessionService = Depends(get_shift_session_service),
) -> SessionResponse | JSONResponse:
    """End the JWT-identified user's active shift."""
    try:
        ended = await service.end(
            user_keycloak_id=UUID(user.sub),
            reason=ShiftEndReason(request.end_reason),
        )
    except NoActiveShift:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "NO_ACTIVE_SHIFT",
                    "message": "No active shift to end for this user.",
                }
            },
        )
    return SessionResponse(session=_to_session_info(ended))


@router.get(
    "/active",
    response_model=SessionResponse,
    response_model_exclude_none=True,
)
async def get_active_session(
    user: AuthUser = Depends(require_role("dcinv-mobile-user")),
    service: ShiftSessionService = Depends(get_shift_session_service),
) -> SessionResponse | JSONResponse:
    """Return the caller's active shift, or ``{"session": null}`` if none."""
    active = await service.get_active(UUID(user.sub))
    if active is None:
        # Bypass response_model_exclude_none so the wire shape stays
        # ``{"session": null}`` instead of ``{}``.
        return JSONResponse(status_code=status.HTTP_200_OK, content={"session": None})
    return SessionResponse(session=_to_session_info(active))
