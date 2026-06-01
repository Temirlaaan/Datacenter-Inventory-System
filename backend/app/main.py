"""FastAPI app entrypoint: lifespan-scoped logging setup, request_id middleware."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint

from app.api.v1.admin.audit import router as admin_audit_router
from app.api.v1.admin.batches import router as admin_batches_router
from app.api.v1.admin.sessions import router as admin_sessions_router
from app.api.v1.devices import router as devices_router
from app.api.v1.health import router as health_router
from app.api.v1.meta import router as meta_router
from app.api.v1.qr import router as qr_router
from app.api.v1.sessions import router as sessions_router
from app.auth.dependencies import NoActiveShiftError
from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.netbox.client import get_netbox_client
from app.netbox.errors import NetBoxClientError, NetBoxNotFound
from app.observability.logging import configure_logging
from app.services.auto_end_job import AutoEndJobStatus, auto_end_loop

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
    # IMPORTANT: this is a single-replica pattern. Running N replicas wastes
    # Nx the DB scans even though the partial unique index on shift_sessions
    # + idempotent end_reason='auto_timeout' prevent double-firing harm.
    # Sprint 8a will introduce a Postgres advisory lock or k8s CronJob for
    # multi-replica safety.
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
app.include_router(admin_sessions_router, prefix="/api/v1/admin/sessions", tags=["admin-sessions"])
app.include_router(qr_router, prefix="/api/v1/qr", tags=["qr"])
app.include_router(meta_router, prefix="/api/v1/meta", tags=["meta"])
app.include_router(devices_router, prefix="/api/v1/devices", tags=["devices"])
app.include_router(sessions_router, prefix="/api/v1/sessions", tags=["sessions"])


@app.exception_handler(NetBoxNotFound)
async def handle_netbox_not_found(_request: Request, exc: NetBoxNotFound) -> JSONResponse:
    """A NetBox 404 (e.g. an unknown device id) is a client error, not a 500."""
    logger.info("netbox_not_found", error=str(exc))
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND, content={"detail": "not found in NetBox"}
    )


@app.exception_handler(NetBoxClientError)
async def handle_netbox_error(_request: Request, exc: NetBoxClientError) -> JSONResponse:
    """A NetBox 5xx / timeout is an upstream failure — surface it as 502, not 500."""
    logger.warning("netbox_upstream_error", error=repr(exc))
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": "NetBox upstream error"}
    )


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
