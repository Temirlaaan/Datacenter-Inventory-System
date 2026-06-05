"""FastAPI app entrypoint: lifespan-scoped logging setup, request_id middleware."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint

from app.api.v1.admin.audit import router as admin_audit_router
from app.api.v1.admin.batches import router as admin_batches_router
from app.api.v1.admin.dashboard import router as admin_dashboard_router
from app.api.v1.admin.sessions import router as admin_sessions_router
from app.api.v1.devices import router as devices_router
from app.api.v1.health import router as health_router
from app.api.v1.meta import router as meta_router
from app.api.v1.qr import router as qr_router
from app.api.v1.sessions import router as sessions_router
from app.auth.dependencies import NoActiveShiftError
from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.middleware.rate_limit import rate_limit_middleware
from app.netbox.client import get_netbox_client
from app.netbox.errors import NetBoxCircuitOpenError, NetBoxClientError, NetBoxNotFound
from app.observability.logging import configure_logging
from app.services.auto_end_job import AutoEndJobStatus, auto_end_loop
from app.web.auth import AdminShiftNeeded, WebAdminAuthRequired
from app.web.router import _redirect_to_login, _render_admin_shift_needed
from app.web.router import router as web_router

logger = structlog.get_logger()

# Bounded shutdown wait for the auto-end loop. The loop wakes immediately on
# its cancel_event, so 5s is generous; 1s would be too tight if the GIL is
# loaded at shutdown. Sprint 7 Task 1 decision A.
_AUTO_END_SHUTDOWN_TIMEOUT_SECONDS = 5.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Validate settings (fail-fast), configure logging, dispose engine on shutdown."""
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("app_starting", log_level=settings.log_level)

    # Auto-end stale shift sessions (Sprint 7 Task 1). The status object is
    # ALWAYS attached to app.state so /health has a consistent shape; the
    # task is only scheduled when SHIFT_AUTO_END_ENABLED is true.
    #
    # Multi-replica safe (Sprint 8a Task 1): the loop's per-iteration body
    # is wrapped in a Postgres advisory lock; only one replica acquires the
    # lock per interval and runs the work. Lock-loser replicas tick cleanly
    # without going stale on /health. See
    # app/services/auto_end_job.py:_AUTO_END_JOB_ADVISORY_LOCK_ID.
    app.state.auto_end_job_status = AutoEndJobStatus(enabled=settings.shift_auto_end_enabled)
    app.state.auto_end_job_cancel = asyncio.Event()
    app.state.auto_end_job_task = None
    if settings.shift_auto_end_enabled:
        app.state.auto_end_job_task = asyncio.create_task(
            auto_end_loop(
                sessionmaker=get_sessionmaker(),
                status=app.state.auto_end_job_status,
                cancel_event=app.state.auto_end_job_cancel,
                interval_seconds=float(settings.shift_auto_end_interval_seconds),
                threshold_hours=settings.shift_auto_end_threshold_hours,
            )
        )

    yield

    logger.info("app_stopping")
    # Drain the auto-end loop before disposing the engine — the loop holds
    # sessions from the same engine.
    if app.state.auto_end_job_task is not None:
        app.state.auto_end_job_cancel.set()
        try:
            await asyncio.wait_for(
                app.state.auto_end_job_task,
                timeout=_AUTO_END_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("auto_end_job_shutdown_timeout")
            app.state.auto_end_job_task.cancel()

    # Close pooled connections — cheap if the engine/client were never actually used.
    await get_engine().dispose()
    await get_netbox_client().aclose()


app = FastAPI(title="DC Inventory Backend", version="0.1.0", lifespan=lifespan)
# /health mounted at the root (not /api/v1/) — orchestrators expect unversioned probes.
app.include_router(health_router)
app.include_router(admin_batches_router, prefix="/api/v1/admin/batches", tags=["batches"])
app.include_router(admin_audit_router, prefix="/api/v1/admin/audit", tags=["audit"])
app.include_router(
    admin_dashboard_router, prefix="/api/v1/admin/dashboard", tags=["admin-dashboard"]
)
app.include_router(admin_sessions_router, prefix="/api/v1/admin/sessions", tags=["admin-sessions"])
app.include_router(qr_router, prefix="/api/v1/qr", tags=["qr"])
app.include_router(meta_router, prefix="/api/v1/meta", tags=["meta"])
app.include_router(devices_router, prefix="/api/v1/devices", tags=["devices"])
app.include_router(sessions_router, prefix="/api/v1/sessions", tags=["sessions"])
# Sprint 8b Task 0: web admin surface (HTML, cookie auth, OIDC redirect flow).
app.include_router(web_router, prefix="/web", tags=["web"])
# Static assets mounted at top level (not under /web/) so the browser caches
# CSS without sending the session cookie on every request, and the rate-limit
# middleware's UNLIMITED bypass for /static/ has a stable prefix.
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "web" / "static")),
    name="static",
)


@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    """Bare-hostname convenience: ``GET /`` → ``/web/``.

    Without this, a user typing the hostname (or an LB liveness probe
    pointed at ``/``) gets FastAPI's default JSON 404. The admin surface
    lives under ``/web/`` (the OIDC redirect flow takes over from there).
    307 keeps the method semantically correct and isn't aggressively
    cached, leaving room to serve real content at ``/`` later if needed.
    """
    return RedirectResponse(url="/web/", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@app.exception_handler(NetBoxNotFound)
async def handle_netbox_not_found(_request: Request, exc: NetBoxNotFound) -> JSONResponse:
    """A NetBox 404 (e.g. an unknown device id) is a client error, not a 500."""
    logger.info("netbox_not_found", error=str(exc))
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND, content={"detail": "not found in NetBox"}
    )


@app.exception_handler(NetBoxCircuitOpenError)
async def handle_netbox_circuit_open(
    _request: Request, exc: NetBoxCircuitOpenError
) -> JSONResponse:
    """Sprint 8a Task 2: NetBox circuit is OPEN — fast-fail with 503 + Retry-After.

    Distinguished from the 502 ``NetBoxClientError`` handler below: 502 means
    "I asked NetBox and got a bad response," 503 means "I'm currently
    refusing to call NetBox because it's been failing." The handler is
    registered BEFORE the broader ``NetBoxClientError`` handler so FastAPI's
    most-specific-handler-wins dispatch routes correctly.
    """
    logger.warning(
        "netbox_circuit_open_short_circuit",
        recovery_timeout_seconds=exc.recovery_timeout_seconds,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        headers={"Retry-After": str(exc.recovery_timeout_seconds)},
        content={
            "error": {
                "code": "NETBOX_CIRCUIT_OPEN",
                "message": "NetBox is currently unavailable; try again later",
                "retry_after_seconds": exc.recovery_timeout_seconds,
            }
        },
    )


@app.exception_handler(NetBoxClientError)
async def handle_netbox_error(_request: Request, exc: NetBoxClientError) -> JSONResponse:
    """A NetBox 5xx / timeout is an upstream failure — surface it as 502, not 500."""
    logger.warning("netbox_upstream_error", error=repr(exc))
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": "NetBox upstream error"}
    )


@app.exception_handler(WebAdminAuthRequired)
async def handle_web_admin_auth_required(request: Request, _exc: WebAdminAuthRequired) -> Response:
    """Sprint 8b Task 0: missing/invalid session cookie → 302 to /web/login.
    Web flows use redirects, not status codes — the browser follows."""
    return _redirect_to_login(request)


@app.exception_handler(AdminShiftNeeded)
async def handle_admin_shift_needed(request: Request, exc: AdminShiftNeeded) -> Response:
    """Sprint 8b Task 0: authenticated admin without an active shift → render
    the intermediate "open shift" page (decision C). Carries the user so the
    page can greet them by email."""
    return _render_admin_shift_needed(request, exc.user)


@app.exception_handler(NoActiveShiftError)
async def handle_no_active_shift(_request: Request, _exc: NoActiveShiftError) -> JSONResponse:
    """Sprint 6 decision G: write endpoints require an active shift; the
    dep-layer ``require_role_with_active_shift`` raises this when the user
    has none, translated here to the structured 409 mobile clients can show.
    """
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": {
                "code": "NO_ACTIVE_SHIFT",
                "message": "No active shift — start a shift before performing this action.",
            }
        },
    )


# Sprint 8a Task 3: rate limit middleware registered BEFORE request_id
# middleware in source order so request_id ends up OUTER (Starlette applies
# user_middleware in reverse-registration order). That way structlog
# contextvars are already bound when the rate-limit middleware logs a 429.
app.middleware("http")(rate_limit_middleware)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Generate or propagate X-Request-ID, bind to structlog contextvars, log completion."""
    req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=req_id,
        method=request.method,
        path=request.url.path,
    )
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        # Log latency + exc_info for failures, then re-raise so FastAPI's exception
        # handler returns the 500. Without this the request_completed line is missing
        # for any failed request, breaking observability of error paths.
        logger.exception(
            "request_failed",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        raise
    response.headers["X-Request-ID"] = req_id
    logger.info(
        "request_completed",
        status=response.status_code,
        latency_ms=int((time.monotonic() - start) * 1000),
    )
    return response
