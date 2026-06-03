"""Web admin OIDC + encrypted session cookie auth (Sprint 8b Task 0).

Separate from the JWT bearer flow in ``app/auth/`` — that path is for the
mobile API (`/api/v1/*`). The web path uses a Keycloak authorization-code
OIDC flow with a confidential client (``client_secret`` held server-side, no
PKCE), and stores the post-login identity in a Fernet-encrypted cookie.

Cookie payload is a JSON blob of ``{"sub", "email", "roles", "exp"}`` —
**identity only**, not the raw JWT. We don't need to call upstream APIs as
the user from the web path; we just need to render pages.

``require_web_admin`` is the FastAPI dep every ``/web/*`` page uses:
- decode cookie → :class:`WebAdminUser`
- check ``dcinv-admin`` role
- look up an active shift in the DB (Sprint 7 decision I — admin actions
  must have shift attribution)
- on failure, raise :class:`WebAdminAuthRequired` (route handler converts
  to 302 to ``/web/login``) or :class:`AdminShiftNeeded` (route handler
  shows the "open admin shift" intermediate page).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request

from app.config import get_settings
from app.db.repositories.shift_session import ShiftSessionRepository
from app.db.session import get_sessionmaker

logger = structlog.get_logger()

SESSION_COOKIE_NAME = "dcinv_admin_session"
"""Browser cookie name carrying the encrypted session blob."""

SESSION_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 8
"""8 hours — matches a working shift; mirrors the cookie's payload ``exp``."""

_ADMIN_ROLE = "dcinv-admin"


@dataclass(frozen=True, slots=True)
class WebAdminUser:
    """The decoded session cookie. Different from ``AuthUser`` (JWT bearer)
    — web cookies carry identity only, not full JWT context."""

    sub: UUID
    email: str
    roles: tuple[str, ...]
    exp: datetime
    """Cookie payload expiry. The browser ``Max-Age`` enforces the same
    boundary client-side; ``exp`` is the server-side authority."""


class WebAdminAuthRequired(Exception):
    """No (valid) session cookie. Route handler → 302 to ``/web/login``."""


class AdminShiftNeeded(Exception):
    """Cookie is valid + admin role present, but no active shift. Route
    handler → render the "open admin shift" intermediate page.

    Carries the user so the intermediate page can render
    ``Hello {email}, open a shift to continue``.
    """

    def __init__(self, user: WebAdminUser) -> None:
        super().__init__("admin shift required")
        self.user = user


# ---------- Fernet (lazy singleton) -------------------------------------------


_fernet_instance: Fernet | None = None


def _fernet() -> Fernet:
    """Lazy-singleton Fernet built from ``SESSION_COOKIE_KEY``.

    Settings are wiped + re-loaded between tests via ``clean_env``; the
    cached ``Fernet`` would otherwise hold the old key. The
    :func:`reset_web_auth_cache` helper clears this for the same reason
    Sprint 8a Tasks 2 + 3 reset their module-level singletons.
    """
    global _fernet_instance
    if _fernet_instance is None:
        settings = get_settings()
        _fernet_instance = Fernet(settings.session_cookie_key.get_secret_value().encode("utf-8"))
    return _fernet_instance


def reset_web_auth_cache() -> None:
    """Clear the cached Fernet so the next call re-reads settings.

    Used by the test ``clean_env`` fixture (mirrors Sprint 8a's
    ``reset_netbox_circuit()`` + ``reset_rate_limit_buckets()`` pattern).
    """
    global _fernet_instance
    _fernet_instance = None


# ---------- cookie encode / decode --------------------------------------------


def encode_session_cookie(user: WebAdminUser) -> str:
    """Serialize + Fernet-encrypt the user identity for the cookie value."""
    payload = {
        "sub": str(user.sub),
        "email": user.email,
        "roles": list(user.roles),
        "exp": int(user.exp.timestamp()),
    }
    return _fernet().encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")


def decode_session_cookie(raw: str) -> WebAdminUser | None:
    """Decrypt + parse + exp-check the cookie.

    Returns ``None`` on any failure: tampered cookie, wrong Fernet key,
    expired payload, malformed JSON, missing fields. The route handler
    treats ``None`` as "no valid auth" → redirect to login.
    """
    try:
        plaintext = _fernet().decrypt(raw.encode("ascii"))
    except InvalidToken:
        return None
    try:
        payload = json.loads(plaintext)
        sub = UUID(payload["sub"])
        email = str(payload["email"])
        roles = tuple(str(r) for r in payload["roles"])
        exp = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if exp <= datetime.now(UTC):
        return None
    return WebAdminUser(sub=sub, email=email, roles=roles, exp=exp)


def build_session_cookie_payload(*, sub: UUID, email: str, roles: tuple[str, ...]) -> WebAdminUser:
    """Construct a fresh ``WebAdminUser`` with ``exp`` set ``SESSION_COOKIE_MAX_AGE_SECONDS``
    in the future. Used by the OIDC callback handler after a successful token
    exchange."""
    exp = datetime.now(UTC) + timedelta(seconds=SESSION_COOKIE_MAX_AGE_SECONDS)
    return WebAdminUser(sub=sub, email=email, roles=roles, exp=exp)


# ---------- FastAPI dep -------------------------------------------------------


async def require_web_admin(request: Request) -> WebAdminUser:
    """Dep for every ``/web/*`` page that needs an authenticated admin.

    On any auth failure, raises :class:`WebAdminAuthRequired`. On a valid
    cookie + admin role but no active shift, raises :class:`AdminShiftNeeded`
    carrying the user (so the intermediate page can greet them).
    """
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if raw is None:
        raise WebAdminAuthRequired()
    user = decode_session_cookie(raw)
    if user is None:
        raise WebAdminAuthRequired()
    if _ADMIN_ROLE not in user.roles:
        # Authenticated but not authorised — same outcome (back to login)
        # so a non-admin can't differentiate "wrong cookie" from "wrong role"
        # via the response. They simply can't reach /web/*.
        logger.info("web_admin_auth_missing_role", sub=str(user.sub), roles=user.roles)
        raise WebAdminAuthRequired()
    async with get_sessionmaker()() as session:
        active = await ShiftSessionRepository(session).get_active_for_user(user.sub)
    if active is None:
        raise AdminShiftNeeded(user)
    return user
