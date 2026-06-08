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
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.netbox.client import get_netbox_circuit_state

_BACKUP_MARKER_DEFAULT = "/var/lib/dcinv-backups/last-success-marker"

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


def _backups_sub_object() -> dict[str, object]:
    """Build the ``backups`` /health sub-object (Sprint 9 Task 3 decision J).

    Reads the mtime of a marker file that ``scripts/backup.sh`` touches on
    successful pg_dump + S3 upload. Returns:

    - ``configured: True`` + ``last_completed_at`` + ``age_seconds`` when
      the marker exists, or
    - ``configured: True`` + ``last_completed_at: null`` when the marker
      path is set but the file isn't present yet (cron hasn't run, or the
      last attempt failed), or
    - ``configured: False`` when no marker path is set in the env (this
      deployment hasn't set up the backup cron).

    INFORMATIONAL ONLY. Stale or missing backups do NOT flip the overall
    ``/health`` to ``degraded`` — the application can't know whether
    "stale" is acceptable for this deployment (test, staging, prod-with-
    different-RPO). External monitors / Grafana alert on the sub-fields.
    """
    marker_path = os.environ.get("DCINV_BACKUP_MARKER_PATH", _BACKUP_MARKER_DEFAULT)
    if not marker_path:
        return {"configured": False}
    try:
        mtime = os.path.getmtime(marker_path)
    except OSError:
        # File missing or unreadable. Distinguishes from "no cron set up"
        # via the configured=True flag — operator sees both: "yes, you
        # configured it; no, it hasn't run successfully".
        return {"configured": True, "last_completed_at": None, "age_seconds": None}
    last_at = datetime.fromtimestamp(mtime, tz=UTC)
    age = (datetime.now(UTC) - last_at).total_seconds()
    return {
        "configured": True,
        "last_completed_at": last_at.isoformat(),
        "age_seconds": int(age),
    }


def _auto_end_job_sub_object(request: Request) -> dict[str, object]:
    """Build the ``auto_end_job`` /health sub-object (Sprint 7 Task 1 decision A).

    Reads :class:`~app.services.auto_end_job.AutoEndJobStatus` from
    ``app.state``. The status object is created unconditionally by the
    lifespan so the response shape is consistent regardless of
    ``SHIFT_AUTO_END_ENABLED`` — operators always see ``enabled``,
    ``last_iteration_at``, and ``status`` fields.

    Sprint 7 Task 1 decision 1: this sub-object is INFORMATIONAL ONLY. A
    ``"stale"`` status does NOT flip the overall ``/health`` result to
    ``degraded`` — the existing 503 trigger stays narrow ("external
    dependency unreachable"). Operators alert on the sub-field directly.
    """
    job_status = request.app.state.auto_end_job_status
    settings = get_settings()
    last_at = job_status.last_iteration_at
    return {
        "enabled": job_status.enabled,
        "last_iteration_at": last_at.isoformat() if last_at is not None else None,
        "status": job_status.health_status(
            now=datetime.now(UTC),
            interval_seconds=settings.shift_auto_end_interval_seconds,
        ),
    }


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, object]:
    """Aggregate downstream checks. 200 if all ok; 503 otherwise.

    Sprint 8a Task 2: ``netbox_circuit`` sub-object reports the circuit
    breaker state for operators. Informational only — does NOT flip the
    overall ``status`` to ``degraded``. The existing ``netbox`` per-
    downstream check (which uses a fresh ``httpx.AsyncClient``, bypassing
    the circuit) remains the 503 trigger.
    """
    db, netbox, keycloak = await asyncio.gather(
        _run_with_timeout(_check_db),
        _run_with_timeout(_check_netbox),
        _run_with_timeout(_check_keycloak),
    )
    checks = {"db": db, "netbox": netbox, "keycloak": keycloak}
    all_ok = all(c["status"] == "ok" for c in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if all_ok else "degraded",
        "checks": checks,
        "auto_end_job": _auto_end_job_sub_object(request),
        "netbox_circuit": get_netbox_circuit_state(),
        "backups": _backups_sub_object(),
    }
