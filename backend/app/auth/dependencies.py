"""FastAPI auth dependencies: extract Bearer token, validate against Keycloak JWKS,
expose `AuthUser`. `require_role(role)` is a factory for role-gated endpoints.

Sprint 6 Task 4 (decision F.a + G): `require_role_with_active_shift(role)` is the
composite dep for write endpoints — it role-gates, looks up the user's active
``shift_sessions`` row, and rejects with 409 ``NO_ACTIVE_SHIFT`` if none. The
caller's ``AuthUser.shift_session_id`` is the shift's UUID; services use it as
the audit row's ``session_id`` instead of the JWT ``sid`` claim.

Audience is *not* verified — Keycloak access tokens carry `aud=account` by default,
which provides no security. Issuer + signature + exp are what actually protect us.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwks import JWKSCache, get_jwks_cache
from app.config import get_settings
from app.db.repositories.shift_session import ShiftSessionRepository
from app.db.session import get_session

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class AuthUser:
    """Identity extracted from a verified Keycloak access token.

    ``shift_session_id`` is populated by ``require_role_with_active_shift`` and
    is the source of ``audit_log.session_id`` for new audit rows (Sprint 6
    decision D). Stays ``None`` on read endpoints that don't gate on a shift.
    """

    sub: str
    email: str | None
    roles: tuple[str, ...]
    session_id: str | None
    shift_session_id: UUID | None = None


class NoActiveShiftError(Exception):
    """Raised by ``require_role_with_active_shift`` when the user has no active shift.

    Translated to a 409 ``{"error": {"code": "NO_ACTIVE_SHIFT", ...}}`` body
    by the exception handler registered in ``app.main``.
    """


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _resolve_jwk(token: str, cache: JWKSCache) -> dict[str, str]:
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as e:
        raise _unauthorized("malformed token") from e
    kid = header.get("kid")
    if not kid:
        raise _unauthorized("token missing kid header")
    jwk = await cache.get_key(kid)
    if jwk is None:
        raise _unauthorized("unknown signing key")
    return jwk


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _build_auth_user(claims: dict[str, object]) -> AuthUser:
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise _unauthorized("token missing sub claim")
    realm_access = claims.get("realm_access")
    roles_raw = realm_access.get("roles") if isinstance(realm_access, dict) else None
    roles = tuple(r for r in roles_raw if isinstance(r, str)) if isinstance(roles_raw, list) else ()
    return AuthUser(
        sub=sub,
        email=_optional_str(claims.get("email")),
        roles=roles,
        session_id=_optional_str(claims.get("sid")),
    )


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AuthUser:
    """Extract Bearer token, verify against Keycloak JWKS, return AuthUser.

    Cache + settings are pulled from their lru_cache singletons rather than via
    Depends() so the function is callable directly in unit tests. Same pattern as
    `app.db.session.get_session`. Tests override by clearing those caches and
    re-binding env vars (see `clean_env`) or by monkeypatching `get_jwks_cache`.
    """
    if creds is None:
        raise _unauthorized("missing bearer token")
    token = creds.credentials
    cache = get_jwks_cache()
    jwk = await _resolve_jwk(token, cache)
    try:
        claims = jwt.decode(
            token,
            jwk,
            algorithms=["RS256"],
            issuer=get_settings().keycloak_issuer,
            # aud=account is Keycloak default; not a meaningful audience for our service.
            options={"verify_aud": False},
        )
    except JWTError as e:
        raise _unauthorized("invalid token") from e
    return _build_auth_user(claims)


def require_role(role: str) -> Callable[[AuthUser], Awaitable[AuthUser]]:
    """Dependency factory: returns a dep that 403s unless `role` is in the user's roles."""

    async def _check(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if role not in user.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing required role: {role}",
            )
        return user

    return _check


def require_role_with_active_shift(
    role: str,
) -> Callable[..., Awaitable[AuthUser]]:
    """Dependency factory for write endpoints — composes role gate + active shift lookup.

    Sprint 6 decisions F.a + G: role-gate first (so unauthorised callers don't
    pay the DB lookup), then look up the active shift for the JWT-identified
    user. Returns an ``AuthUser`` with ``shift_session_id`` populated; services
    use that as the audit row's ``session_id``. Raises
    ``NoActiveShiftError`` (→ 409 ``NO_ACTIVE_SHIFT``) if the user has no
    active shift.
    """

    async def _check(
        user: AuthUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> AuthUser:
        if role not in user.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing required role: {role}",
            )
        # Wrap the read in an explicit ``session.begin()`` so SQLAlchemy 2.0's
        # autobegun tx is committed/closed here — services downstream
        # (QRLifecycleService.bind, NetBoxWriteService.patch_with_attribution)
        # open their own ``session.begin()`` and will RuntimeError if a tx is
        # still pending. Same pattern as QRLifecycleService.bind's pre-check.
        async with session.begin():
            active = await ShiftSessionRepository(session).get_active_for_user(UUID(user.sub))
        if active is None:
            raise NoActiveShiftError()
        return dataclasses.replace(user, shift_session_id=active.id)

    return _check
