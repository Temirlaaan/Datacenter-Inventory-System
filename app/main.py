"""FastAPI app entrypoint: lifespan-scoped logging setup, request_id middleware."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from app.config import get_settings
from app.db.session import get_engine
from app.observability.logging import configure_logging

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Validate settings (fail-fast), configure logging, dispose engine on shutdown."""
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("app_starting", log_level=settings.log_level)
    yield
    logger.info("app_stopping")
    # Close the connection pool. Cheap if the engine was never actually used.
    await get_engine().dispose()


app = FastAPI(title="DC Inventory Backend", version="0.1.0", lifespan=lifespan)


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


# TODO(2026-05-13, claude): remove _test route when /health lands in Sprint 1 Task 6.
@app.get("/_test")
async def _test_route() -> dict[str, bool]:
    """Temporary verification endpoint for Task 2 manual logging check."""
    logger.info("test_route_hit")
    return {"ok": True}
