"""``GET /api/v1/me`` — identity bootstrap for the browser SPA.

Returns who the caller is (sub/email/roles), their active shift (if any), and —
for cookie-authenticated callers — the CSRF token to echo on writes as the
``X-CSRF-Token`` header. The native app (bearer auth) gets ``csrf_token: null``;
it doesn't need CSRF protection.

No audit row — operational read, same as ``GET /sessions/active``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.sessions import SessionInfo, _to_session_info
from app.auth.dependencies import AuthUser, get_current_user
from app.db.repositories.shift_session import ShiftSessionRepository
from app.db.session import get_session

router = APIRouter()


class MeResponse(BaseModel):
    """Identity + session bootstrap for the SPA."""

    sub: str
    email: str | None
    roles: list[str]
    csrf_token: str | None
    active_shift: SessionInfo | None


def _csrf_from_cookie(request: Request) -> str | None:
    """Pull the CSRF token out of the (httpOnly) session cookie so the SPA — which
    can't read the cookie from JS — learns the token to echo on writes."""
    from app.web.auth import SESSION_COOKIE_NAME, decode_session_cookie

    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if raw is None:
        return None
    web_user = decode_session_cookie(raw)
    return web_user.csrf_token if web_user is not None else None


@router.get("", response_model=MeResponse)
async def get_me(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MeResponse:
    """Return the caller's identity, active shift, and CSRF token (cookie auth)."""
    async with session.begin():
        active = await ShiftSessionRepository(session).get_active_for_user(UUID(user.sub))
    return MeResponse(
        sub=user.sub,
        email=user.email,
        roles=list(user.roles),
        csrf_token=_csrf_from_cookie(request),
        active_shift=_to_session_info(active) if active is not None else None,
    )
