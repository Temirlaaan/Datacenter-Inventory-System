"""GET /health — liveness + readiness probe with per-downstream checks.

Returns 200 + `{"status": "ok", ...}` if every downstream is reachable; 503 +
`{"status": "degraded", ...}` if any is not. Each check is bounded by a 2s
timeout and they run concurrently, so total endpoint latency ≤ ~2s even when
one downstream stalls.

The endpoint is intentionally unauthenticated — orchestrators (Docker compose,
k8s) probe it without credentials.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_sessionmaker

_PER_CHECK_TIMEOUT_SECONDS = 2.0

router = APIRouter()


@asynccontextmanager
async def _open_session() -> AsyncIterator[AsyncSession]:
    """Wrapper around the sessionmaker — exposed as a module attribute so tests can stub it
    without monkeypatching the SQLAlchemy machinery underneath."""
    async with get_sessionmaker()() as session:
        yield session


def _categorize_http_error(exc: httpx.HTTPError) -> str:
    """Map httpx errors to short, generic categories. Raw exception messages can leak
    DNS topology / internal IPs through this unauthenticated endpoint."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection_error"
    if isinstance(exc, httpx.NetworkError):
        return "network_error"
    return "transport_error"


async def _check_db() -> dict[str, str]:
    try:
        async with _open_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        # /health is unauthenticated — don't echo the connection string or asyncpg
        # error text back to anyone who can reach the port.
        return {"status": "unreachable", "detail": "connection_error"}
    return {"status": "ok"}


async def _check_netbox() -> dict[str, str]:
    """One-shot probe — bypass the NetBox client's retry loop, which would burn the budget."""
    settings = get_settings()
    url = f"{str(settings.netbox_url).rstrip('/')}/api/status/"
    try:
        async with httpx.AsyncClient(timeout=_PER_CHECK_TIMEOUT_SECONDS) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        return {"status": "unreachable", "detail": _categorize_http_error(e)}
    if resp.status_code >= 500:
        return {"status": "unhealthy", "detail": f"http_{resp.status_code}"}
    return {"status": "ok"}


async def _check_keycloak() -> dict[str, str]:
    """Probe the JWKS endpoint directly — cache hits would mask a current outage."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=_PER_CHECK_TIMEOUT_SECONDS) as client:
            resp = await client.get(settings.jwks_url)
    except httpx.HTTPError as e:
        return {"status": "unreachable", "detail": _categorize_http_error(e)}
    if resp.status_code >= 500:
        return {"status": "unhealthy", "detail": f"http_{resp.status_code}"}
    return {"status": "ok"}


async def _run_with_timeout(check: Callable[[], Awaitable[dict[str, str]]]) -> dict[str, str]:
    try:
        return await asyncio.wait_for(check(), timeout=_PER_CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        return {"status": "timeout", "detail": "budget_exceeded"}


@router.get("/health")
async def health(response: Response) -> dict[str, object]:
    """Aggregate downstream checks. 200 if all ok; 503 otherwise."""
    db, netbox, keycloak = await asyncio.gather(
        _run_with_timeout(_check_db),
        _run_with_timeout(_check_netbox),
        _run_with_timeout(_check_keycloak),
    )
    checks = {"db": db, "netbox": netbox, "keycloak": keycloak}
    all_ok = all(c["status"] == "ok" for c in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if all_ok else "degraded", "checks": checks}
