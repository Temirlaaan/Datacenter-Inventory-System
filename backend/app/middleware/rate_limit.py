"""Per-user rate limiting (Sprint 8a Task 3, ToR §5.4.7).

FastAPI middleware that enforces per-user, per-endpoint-class fixed-window
budgets. Three classes:

- ``READ`` (GET / HEAD / OPTIONS on non-``/admin/`` paths) — default 60/min
- ``WRITE`` (POST / PATCH / PUT / DELETE on non-``/admin/`` paths) — default 20/min
- ``ADMIN`` (any method under ``/api/v1/admin/``) — default 30/min
- ``UNLIMITED`` (``/health``, ``/docs``, ``/openapi.json``, ``/redoc``) — bypass

On exhaustion: ``429`` + ``Retry-After: <seconds>`` header + structured body
``{"error": {"code": "RATE_LIMIT_EXCEEDED", "retry_after_seconds": N}}``.

**Per-replica state, NOT cluster-wide (Sprint 8a plan decision F).** This
in-process implementation enforces a budget per replica; total cluster-wide
rate is N x per-replica budget. Cluster-wide state needs Redis or Postgres-
backed counters; both are larger decisions deferred to the first multi-
replica deployment.

**Sub identity from unverified JWT claims (decision 4).** The rate-limit key
just needs the ``sub`` claim; full signature verification happens later in
``require_role``. If the JWT is missing or malformed, the request bypasses
rate limiting and proceeds to the auth dep, which will 401. Unauthenticated
requests are not rate-limit-protected — acceptable on a VPN-only deployment.

**Module-level bucket dict** keyed by ``(sub, class, window_index)``.
Unbounded growth is acceptable for Sprint 8a's scale (~50 users x 1440
minutes/day = ~72k entries/day max for a process that gets restarted);
cluster-wide storage solves it permanently in Sprint 9+.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from jose import jwt
from starlette import status

from app.config import get_settings

logger = structlog.get_logger()

_WINDOW_SECONDS = 60
_UNLIMITED_PATHS: frozenset[str] = frozenset(
    {"/", "/health", "/docs", "/openapi.json", "/redoc"}
)
_UNLIMITED_PREFIXES: tuple[str, ...] = ("/web/", "/static/")
"""Sprint 8b Task 0 decision I: ``/web/*`` pages go through ``require_web_admin``
which calls admin JSON endpoints internally via FastAPI dep injection (not
HTTP), so admin-bucket rate limits don't double-fire. ``/static/*`` is browser-
cached CSS — bypassing the rate limit means a hard reload doesn't consume
budget."""


class RateLimitClass(StrEnum):
    """Endpoint classification for rate limiting (Sprint 8a Task 3 decision 2)."""

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"
    UNLIMITED = "unlimited"


_buckets: dict[tuple[str, RateLimitClass, int], int] = {}


def reset_rate_limit_buckets() -> None:
    """Test helper: clear all bucket state so each test starts at zero."""
    _buckets.clear()


def _classify_request(method: str, path: str) -> RateLimitClass:
    """Map (method, path) to a rate-limit class (Sprint 8a decision 2; Sprint
    8b Task 0 decision I added the /web/ + /static/ prefix bypass)."""
    if path in _UNLIMITED_PATHS:
        return RateLimitClass.UNLIMITED
    for prefix in _UNLIMITED_PREFIXES:
        if path.startswith(prefix):
            return RateLimitClass.UNLIMITED
    if path.startswith("/api/v1/admin/"):
        return RateLimitClass.ADMIN
    if method in {"GET", "HEAD", "OPTIONS"}:
        return RateLimitClass.READ
    # POST / PATCH / PUT / DELETE (and any other write-shaped method).
    return RateLimitClass.WRITE


def _extract_user_sub(authorization_header: str | None) -> str | None:
    """Pull the ``sub`` claim from a Bearer JWT without verifying signature.

    Decision 4: rate-limit keying does NOT need full verification (that
    happens in ``require_role``). A forged ``sub`` lets an attacker mess
    with their own bucket; real auth still rejects them downstream. Saves a
    JWKS lookup on every request.

    Returns ``None`` for missing / malformed headers or JWT parse failures —
    the middleware bypasses rate limiting in that case and lets the auth
    dep return 401.
    """
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1]
    try:
        claims = jwt.get_unverified_claims(token)
    except Exception:
        return None
    sub = claims.get("sub")
    if not isinstance(sub, str):
        return None
    return sub


def _current_window_index(now: datetime) -> int:
    """Floor(now-epoch / WINDOW_SECONDS) — fixed-window bucket index."""
    return int(now.timestamp() // _WINDOW_SECONDS)


def _seconds_until_next_window(now: datetime) -> int:
    """Seconds remaining in the current window — surfaced as ``Retry-After``."""
    seconds_into_window = int(now.timestamp()) % _WINDOW_SECONDS
    return _WINDOW_SECONDS - seconds_into_window


def _consume(
    *,
    sub: str,
    cls: RateLimitClass,
    limit: int,
    now: datetime,
) -> tuple[bool, int]:
    """Try to consume one request from the (sub, cls, current_window) bucket.

    Returns ``(allowed, retry_after_seconds)``. When allowed, the count is
    incremented; when rejected, the count is NOT incremented (so a client
    that respects ``Retry-After`` and waits doesn't get a bigger backlog).
    """
    window = _current_window_index(now)
    key = (sub, cls, window)
    current = _buckets.get(key, 0)
    if current >= limit:
        return False, _seconds_until_next_window(now)
    _buckets[key] = current + 1
    return True, 0


def _limit_for_class(cls: RateLimitClass) -> int:
    """Read the per-minute limit for ``cls`` from settings."""
    settings = get_settings()
    if cls is RateLimitClass.READ:
        return settings.rate_limit_read_per_minute
    if cls is RateLimitClass.WRITE:
        return settings.rate_limit_write_per_minute
    if cls is RateLimitClass.ADMIN:
        return settings.rate_limit_admin_per_minute
    # UNLIMITED — caller shouldn't reach this branch (we early-return), but
    # a sentinel max keeps the function total over the enum.
    return 1 << 31


async def rate_limit_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Per-user fixed-window rate limit (ToR §5.4.7, Sprint 8a Task 3).

    Order: registered AFTER ``request_id_middleware`` so ``request_id`` is
    bound for the structured 429 log. When disabled via
    ``RATE_LIMIT_ENABLED=false``, the middleware short-circuits to
    ``call_next`` without inspecting the request.
    """
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return await call_next(request)

    cls = _classify_request(request.method, request.url.path)
    if cls is RateLimitClass.UNLIMITED:
        return await call_next(request)

    sub = _extract_user_sub(request.headers.get("Authorization"))
    if sub is None:
        # No identifiable user — let the auth dep handle the 401. Not our job
        # to enforce a budget against requests that will be rejected anyway.
        return await call_next(request)

    limit = _limit_for_class(cls)
    allowed, retry_after = _consume(sub=sub, cls=cls, limit=limit, now=datetime.now(UTC))
    if allowed:
        return await call_next(request)

    logger.warning(
        "rate_limit_exceeded",
        sub=sub,
        rate_limit_class=cls.value,
        limit_per_minute=limit,
        retry_after_seconds=retry_after,
        path=request.url.path,
        method=request.method,
    )
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        headers={"Retry-After": str(retry_after)},
        content={
            "error": {
                "code": "RATE_LIMIT_EXCEEDED",
                "message": f"Too many requests; try again in {retry_after} seconds.",
                "retry_after_seconds": retry_after,
            }
        },
    )
