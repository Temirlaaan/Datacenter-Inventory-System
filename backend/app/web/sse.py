"""Dashboard activity-feed SSE stream (extracted from ``app.web.router``).

``GET /web/dashboard/stream`` pushes new ``audit_log`` rows to the dashboard so
the operator doesn't have to F5. Mechanism: poll-and-push at a 5s cadence; each
tick queries the last rows and emits those with ``id > last_sent_id``. PG
LISTEN/NOTIFY would be tighter but needs a DB trigger + long-lived asyncpg
connection per client; polling is multi-replica safe with zero schema change.
Swap is internal-only if/when we want true realtime.

The endpoint is cookie-auth (``require_web_admin``) and ``/web/*`` is already in
the rate-limit UNLIMITED prefix list, so long-lived connections don't burn a
per-minute budget. A concurrent-connection cap bounds total DB-poll load.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.audit_log import AuditLogQueryFilters, AuditLogRepository
from app.db.session import get_sessionmaker
from app.domain.audit import AuditLogEntry
from app.web.auth import WebAdminUser, require_web_admin

logger = structlog.get_logger()

router = APIRouter()

_SSE_TICK_INTERVAL_SECONDS = 5.0
_SSE_HEARTBEAT_EVERY_TICKS = 3  # 15s — beats nginx's default 60s idle timeout
_SSE_PAGE_SIZE = 20
# Cap pages walked per tick so a runaway burst (or an attacker mass-
# triggering audit rows) can't make one tick allocate unbounded memory.
# 10 pages * 20 = 200 rows per tick is way above any realistic admin-action burst.
_SSE_MAX_PAGES_PER_TICK = 10
# On a tick that raised, back off proportionally so a sustained DB outage
# doesn't spam logs at 5s cadence.
_SSE_ERROR_BACKOFF_SECONDS = 30.0
# Each open stream is a long-lived coroutine polling the DB every tick, so the
# total DB-poll load is O(open connections). Cap concurrent streams so a pile
# of forgotten browser tabs (or a misbehaving client reconnecting in a loop)
# can't grow that load without bound — over the cap, the endpoint returns 503
# and the dashboard silently falls back to its page-refresh model.
_SSE_MAX_CONCURRENT_CONNECTIONS = 50
_sse_active_connections = 0


async def _counted_stream(inner: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Wrap the stream generator to decrement the active-connection counter
    when the connection ends (client disconnect, error, or shutdown).

    The matching increment happens in the endpoint *before* the response is
    returned (so the cap check is race-free); Starlette always drives this
    body iterator to completion or ``aclose()``, so the ``finally`` runs and
    the slot is released exactly once."""
    global _sse_active_connections
    try:
        async for chunk in inner:
            yield chunk
    finally:
        _sse_active_connections -= 1


def reset_sse_connection_count() -> None:
    """Test helper: reset the active-connection counter to zero."""
    global _sse_active_connections
    _sse_active_connections = 0


def _format_sse_event(*, event: str, data: str) -> bytes:
    """Serialise one SSE message.

    SSE spec: each newline in the payload must start a fresh ``data:``
    line, and a single blank line terminates the event. Single-line JSON
    means we don't normally hit this, but defensive split-and-rejoin so a
    future field with an embedded ``\\n`` doesn't corrupt the frame.
    """
    data_lines = "\n".join(f"data: {part}" for part in data.split("\n"))
    return f"event: {event}\n{data_lines}\n\n".encode()


def _activity_row_to_json(row: AuditLogEntry) -> dict[str, object]:
    """Serialise one audit row for the SSE payload — same shape the dashboard
    template renders, minus the JSONB diffs (kept compact for the stream)."""
    return {
        "id": row.id,
        "timestamp": row.timestamp.isoformat(),
        "user_email": row.user_email,
        "operation": row.operation,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "result": row.result.value,
    }


async def _dashboard_stream_generator(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    last_seen_id: int,
) -> AsyncIterator[bytes]:
    """Yield SSE bytes: ``audit`` events for new rows, ``ping`` heartbeats
    every ``_SSE_HEARTBEAT_EVERY_TICKS`` ticks.

    Each tick paginates from page 1 until a page contains a row at-or-below
    ``watermark`` or the page cap is hit (``_SSE_MAX_PAGES_PER_TICK``).
    This catches bursts larger than a single ``_SSE_PAGE_SIZE`` page (e.g.
    bulk-decommissioning 50 devices generates ~50 audit rows inside one
    5s tick window) without unbounded memory.

    DB / network errors are caught per-tick: the generator logs and backs
    off ``_SSE_ERROR_BACKOFF_SECONDS`` rather than unwinding and forcing
    EventSource to reconnect (which produces 5xx noise on sustained
    outages).
    """
    import json

    tick = 0
    watermark = last_seen_id
    while True:
        try:
            new_rows = await _collect_new_audit_rows(sessionmaker, watermark)
        except Exception as exc:
            logger.warning(
                "dashboard_stream_tick_failed",
                error=type(exc).__name__,
                watermark=watermark,
            )
            await asyncio.sleep(_SSE_ERROR_BACKOFF_SECONDS)
            continue

        # Emit oldest-first so the client can prepend in order — pages
        # returned newest-first, so reverse the accumulated list.
        for row in reversed(new_rows):
            yield _format_sse_event(
                event="audit", data=json.dumps(_activity_row_to_json(row))
            )
            assert row.id is not None  # narrowed by the collector; quiets mypy
            watermark = row.id

        tick += 1
        if tick % _SSE_HEARTBEAT_EVERY_TICKS == 0:
            yield _format_sse_event(event="ping", data="")

        await asyncio.sleep(_SSE_TICK_INTERVAL_SECONDS)


async def _collect_new_audit_rows(
    sessionmaker: async_sessionmaker[AsyncSession], watermark: int
) -> list[AuditLogEntry]:
    """Page through ``audit_log`` newest-first, collecting rows with
    ``id > watermark``. Stops at the first page containing a row at-or-below
    the watermark (we've covered every new row) or at the page cap."""
    async with sessionmaker() as db:
        repo = AuditLogRepository(db)
        accumulated: list[AuditLogEntry] = []
        for page in range(1, _SSE_MAX_PAGES_PER_TICK + 1):
            rows, has_more = await repo.query(
                filters=AuditLogQueryFilters(),
                page=page,
                page_size=_SSE_PAGE_SIZE,
            )
            if not rows:
                break
            new_on_page = [
                r for r in rows if r.id is not None and r.id > watermark
            ]
            accumulated.extend(new_on_page)
            # Page held some row at-or-below watermark → we've seen everything new.
            if len(new_on_page) < len(rows):
                break
            if not has_more:
                break
        return accumulated


@router.get("/dashboard/stream")
async def dashboard_stream(
    last_event_id: int = Query(default=0, ge=0, alias="last_id"),
    user: WebAdminUser = Depends(require_web_admin),
) -> StreamingResponse:
    """``text/event-stream`` of new audit_log rows for the dashboard feed.

    ``last_id`` query param (set by the template from the highest id of the
    server-rendered feed) prevents re-delivering rows the user already sees.
    The browser's native ``Last-Event-ID`` reconnect header is not honoured
    here — too easy to confuse with the dashboard's server-side watermark;
    use the explicit query param.
    """
    _ = user  # role-gating side-effect only
    global _sse_active_connections
    if _sse_active_connections >= _SSE_MAX_CONCURRENT_CONNECTIONS:
        logger.warning(
            "dashboard_stream_rejected_at_capacity",
            active=_sse_active_connections,
            cap=_SSE_MAX_CONCURRENT_CONNECTIONS,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Activity stream at capacity; the dashboard will refresh on reload.",
            headers={"Retry-After": str(int(_SSE_TICK_INTERVAL_SECONDS))},
        )
    # Reserve the slot before returning so the cap check is race-free; the
    # matching release is in _counted_stream's finally.
    _sse_active_connections += 1
    headers = {
        # Disable buffering everywhere — nginx default buffers SSE into 4k
        # chunks which destroys the realtime UX.
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Content-Type": "text/event-stream",
    }
    return StreamingResponse(
        _counted_stream(
            _dashboard_stream_generator(
                sessionmaker=get_sessionmaker(),
                last_seen_id=last_event_id,
            )
        ),
        media_type="text/event-stream",
        headers=headers,
    )
