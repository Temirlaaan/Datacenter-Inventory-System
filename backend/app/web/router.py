"""Web admin router (Sprint 8b Task 0).

Three OIDC flow endpoints + the dashboard placeholder.

- ``GET /web/login`` — 302 to Keycloak's authorization endpoint with ``state``
  + ``nonce`` cookies set for CSRF / replay protection.
- ``GET /web/oidc/callback`` — verifies ``state``, exchanges the auth code
  for tokens via the Keycloak token endpoint (httpx POST with the
  confidential ``client_secret``), verifies the id_token's ``nonce``, sets
  the encrypted ``dcinv_admin_session`` cookie, 302s to ``next`` or ``/web/``.
- ``GET /web/logout`` — clears the cookie + 302s to Keycloak's end-session
  endpoint so the browser also drops its Keycloak SSO cookie.
- ``GET /web/`` — placeholder dashboard. Task 1 replaces with real content.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.admin.audit import query_audit_log_csv
from app.api.v1.admin.sessions import ForceCloseRequest, force_close_session
from app.api.v1.devices import AddCommentRequest, add_comment, get_comment_service
from app.auth.dependencies import AuthUser
from app.auth.keycloak_admin import (
    KeycloakAdminError,
    KeycloakAdminNotConfigured,
    KeycloakUser,
    get_keycloak_admin_client,
)
from app.config import get_settings
from app.db.repositories.audit_log import AuditLogQueryFilters, AuditLogRepository
from app.db.repositories.dashboard import DashboardRepository
from app.db.repositories.qr_batch import QRBatchRepository
from app.db.repositories.qr_code import QRCodeRepository
from app.db.repositories.shift_session import (
    ShiftSessionQueryFilters,
    ShiftSessionRepository,
)
from app.db.session import get_session, get_sessionmaker
from app.domain.audit import AuditLogEntry, AuditResult
from app.domain.qr import QR, QRStatus
from app.domain.shift_session import ShiftEndReason
from app.netbox.client import get_netbox_client
from app.netbox.errors import NetBoxNotFound
from app.services.device import DeviceService
from app.services.device_decommission import (
    DeviceDecommissionInconsistencyError,
    DeviceDecommissionRolledBackError,
    DeviceDecommissionService,
)
from app.services.netbox_write import NetBoxWriteService, WriteConflictError
from app.services.qr.generation import GenerateBatchRequest, QRGenerationService
from app.services.qr.lifecycle import (
    DeviceAlreadyBoundError,
    MissingVersionError,
    QRBindInconsistencyError,
    QRBindRolledBackError,
    QRLifecycleService,
    QRNotFoundError,
    QRRebindInconsistencyError,
    QRRebindRolledBackError,
    QRRetireInconsistencyError,
    QRRetireRolledBackError,
    QRStateConflictError,
    QRUnbindInconsistencyError,
    QRUnbindRolledBackError,
    SameDeviceError,
)
from app.services.shift_session import SessionAlreadyActive, ShiftSessionService
from app.web.auth import (
    SESSION_COOKIE_MAX_AGE_SECONDS,
    SESSION_COOKIE_NAME,
    WebAdminUser,
    build_session_cookie_payload,
    decode_session_cookie,
    encode_session_cookie,
    require_web_admin,
    verify_csrf_token,
)

logger = structlog.get_logger()

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Short-lived cookies set on /web/login and consumed by /web/oidc/callback for
# OIDC state + nonce verification. Max-Age = 5 minutes (the user shouldn't take
# longer than that to authenticate at Keycloak).
_OIDC_STATE_COOKIE = "__dcinv_oidc_state"
_OIDC_NONCE_COOKIE = "__dcinv_oidc_nonce"
_OIDC_NEXT_COOKIE = "__dcinv_oidc_next"
_OIDC_FLOW_COOKIE_MAX_AGE_SECONDS = 5 * 60


def _keycloak_redirect_uri(request: Request) -> str:
    """The redirect URI configured for the confidential client in Keycloak.

    Built from the inbound request's scheme + netloc so it works in both
    dev (http://localhost:8000) and prod (https://...) without a separate
    Settings field.
    """
    return f"{request.url.scheme}://{request.url.netloc}/web/oidc/callback"


@router.get("/login")
async def login(request: Request, next: str = "/web/") -> RedirectResponse:
    """Initiate the OIDC authorization-code flow.

    Generates random ``state`` + ``nonce`` tokens, stores them as short-lived
    cookies that the callback handler verifies, and 302s to Keycloak's auth
    endpoint with ``response_type=code``.
    """
    settings = get_settings()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    params = {
        "client_id": settings.keycloak_web_client_id,
        "response_type": "code",
        "scope": "openid profile email",
        "redirect_uri": _keycloak_redirect_uri(request),
        "state": state,
        "nonce": nonce,
    }
    keycloak_auth_url = (
        f"{settings.keycloak_issuer}/protocol/openid-connect/auth?{urlencode(params)}"
    )
    response = RedirectResponse(url=keycloak_auth_url, status_code=status.HTTP_302_FOUND)
    # ``secure=settings.cookie_secure`` is True in production (set
    # COOKIE_SECURE=true behind TLS); False in dev so localhost http://
    # actually receives the cookie. The browser drops Secure-flagged cookies
    # over plain HTTP, so flipping the flag wrong in dev would silently
    # break the OIDC flow.
    for cookie_name, cookie_value in (
        (_OIDC_STATE_COOKIE, state),
        (_OIDC_NONCE_COOKIE, nonce),
        (_OIDC_NEXT_COOKIE, next),
    ):
        response.set_cookie(
            cookie_name,
            cookie_value,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=_OIDC_FLOW_COOKIE_MAX_AGE_SECONDS,
        )
    return response


@router.get("/oidc/callback", response_model=None)
async def oidc_callback(
    request: Request, code: str = "", state: str = ""
) -> RedirectResponse | HTMLResponse:
    """Receive the OIDC authorization code, exchange for tokens, set the
    encrypted session cookie, redirect to the next page.

    Failure modes (all → 400 HTML page so the operator sees the problem):
    - missing ``code`` / ``state`` query params
    - ``state`` cookie missing or mismatched (CSRF guard)
    - Keycloak token endpoint returns non-200
    - id_token decode fails or ``nonce`` claim doesn't match the cookie
    """
    expected_state = request.cookies.get(_OIDC_STATE_COOKIE)
    expected_nonce = request.cookies.get(_OIDC_NONCE_COOKIE)
    next_path = request.cookies.get(_OIDC_NEXT_COOKIE, "/web/")
    if not code or not state or expected_state is None or state != expected_state:
        # Diagnostic fields split out so production logs distinguish:
        #  - has_state_query=False → Keycloak didn't echo state back (rare)
        #  - has_state_cookie=False → /web/login Set-Cookie never reached the
        #    browser OR browser dropped it OR it wasn't sent on the callback
        #    (Secure flag w/ http upstream, Path mismatch, expired flow, etc.)
        #  - state_match=False with both present → cross-tab race or stale
        #    callback URL.
        # ``is_https`` confirms uvicorn's --proxy-headers picked up
        # X-Forwarded-Proto from the reverse proxy (drives both _keycloak_redirect_uri
        # AND the Secure-cookie flow). ``cookie_names_present`` shows whether ANY
        # cookies survived the round-trip — empty list = full cookie drop.
        logger.warning(
            "web_oidc_callback_state_mismatch",
            has_code=bool(code),
            has_state_query=bool(state),
            has_state_cookie=expected_state is not None,
            state_match=(expected_state is not None and state == expected_state),
            is_https=request.url.scheme == "https",
            cookie_names_present=sorted(request.cookies.keys()),
        )
        return HTMLResponse(
            "OIDC callback rejected: state mismatch (likely CSRF or expired login).",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    settings = get_settings()
    token_url = f"{settings.keycloak_issuer}/protocol/openid-connect/token"
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _keycloak_redirect_uri(request),
        "client_id": settings.keycloak_web_client_id,
        "client_secret": settings.keycloak_web_client_secret.get_secret_value(),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(token_url, data=payload)
    except httpx.HTTPError as exc:
        logger.error("web_oidc_token_exchange_failed", error=repr(exc))
        return HTMLResponse(
            "OIDC callback rejected: token exchange failed.",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    if resp.status_code != 200:
        logger.warning(
            "web_oidc_token_exchange_non_200",
            status=resp.status_code,
            body=resp.text[:200],
        )
        return HTMLResponse(
            "OIDC callback rejected: Keycloak rejected the code.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    tokens = resp.json()
    id_token = tokens.get("id_token")
    if not id_token:
        return HTMLResponse(
            "OIDC callback rejected: no id_token in Keycloak response.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    # Skip signature verification here — Keycloak just signed it 100ms ago
    # and the upstream TLS transport already authenticated the token endpoint.
    # The JWT bearer path (app/auth/) does full JWKS verification for inbound
    # API calls; this callback path trusts its own freshly-completed handshake.
    try:
        claims = jwt.get_unverified_claims(id_token)
    except Exception:
        return HTMLResponse(
            "OIDC callback rejected: id_token parse failed.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if claims.get("nonce") != expected_nonce:
        logger.warning("web_oidc_callback_nonce_mismatch")
        return HTMLResponse(
            "OIDC callback rejected: nonce mismatch (replay guard).",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    sub_raw = claims.get("sub")
    email = claims.get("email", "")
    realm_access = claims.get("realm_access") or {}
    roles = tuple(realm_access.get("roles", []))
    from uuid import UUID

    user = build_session_cookie_payload(sub=UUID(sub_raw), email=email, roles=roles)
    cookie_value = encode_session_cookie(user)

    response = RedirectResponse(url=next_path, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        cookie_value,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
    )
    # Clean up the short-lived OIDC-flow cookies; the browser doesn't need
    # them past this exchange.
    for cookie in (_OIDC_STATE_COOKIE, _OIDC_NONCE_COOKIE, _OIDC_NEXT_COOKIE):
        response.delete_cookie(cookie)
    return response


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Clear the session cookie + 302 to Keycloak's end-session so the
    browser also drops its upstream SSO cookie."""
    settings = get_settings()
    end_session = f"{settings.keycloak_issuer}/protocol/openid-connect/logout"
    # post_logout_redirect_uri lands the user back on /web/login afterwards.
    post_logout = f"{request.url.scheme}://{request.url.netloc}/web/login"
    end_session_url = f"{end_session}?{urlencode({'post_logout_redirect_uri': post_logout, 'client_id': settings.keycloak_web_client_id})}"
    response = RedirectResponse(url=end_session_url, status_code=status.HTTP_302_FOUND)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ---------- Auth-failure → redirect/intermediate-page handlers ----------------


def _redirect_to_login(request: Request) -> RedirectResponse:
    """Build the 302 to /web/login carrying the originally-requested path."""
    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    return RedirectResponse(
        url=f"/web/login?{urlencode({'next': next_path})}",
        status_code=status.HTTP_302_FOUND,
    )


def _render_admin_shift_needed(request: Request, user: WebAdminUser) -> HTMLResponse:
    """Tiny intermediate page when admin is authenticated but has no active
    shift. Hands them a "Start admin shift" form posting to the existing
    Sprint 8a Task 0 endpoint."""
    return templates.TemplateResponse(
        request,
        "_admin_shift_needed.html",
        {"user_email": user.email, "csrf_token": user.csrf_token},
        status_code=status.HTTP_403_FORBIDDEN,
    )


# ---------- /web/ dashboard --------------------------------------------------


_DASHBOARD_ACTIVITY_FEED_SIZE = 20


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the admin dashboard with the six counters from
    :class:`DashboardRepository` plus the recent-activity audit feed
    (Sprint 10 Task 1).

    Two SQL round-trips: counter snapshot + the last 20 audit_log rows
    (newest first). Both go through repos directly via dep injection
    (decision I — no HTTP self-call). The activity feed is NOT audited
    (read; mirrors ``/web/audit/`` GET — Sprint 7 decision 8).
    """
    snap = await DashboardRepository(session).snapshot(now=datetime.now(UTC))
    activity_rows, _ = await AuditLogRepository(session).query(
        filters=AuditLogQueryFilters(),
        page=1,
        page_size=_DASHBOARD_ACTIVITY_FEED_SIZE,
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user_email": user.email,
            "snapshot": snap,
            "activity_rows": activity_rows,
            # Highest id rendered server-side — the SSE stream uses this
            # as the starting watermark so the first delivered event is
            # something the client doesn't already have in the feed.
            "highest_audit_id": (
                max((r.id for r in activity_rows if r.id is not None), default=0)
            ),
        },
    )


# ---------- GET /web/dashboard/stream — SSE activity feed (2026-06-10) ------
#
# Push new audit_log rows to the dashboard so the operator doesn't have to F5.
# Mechanism: poll-and-push at 5s cadence; each tick queries the last 20 rows,
# emits those with id > last_sent_id. PG LISTEN/NOTIFY would be tighter but
# needs a DB trigger + long-lived asyncpg connection per client; polling is
# multi-replica safe with zero schema change. Swap is internal-only if/when
# we want true realtime.
#
# The endpoint is cookie-auth (require_web_admin) and /web/* is already in
# the rate-limit UNLIMITED prefix list, so long-lived connections don't
# burn a per-minute budget.

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
    headers = {
        # Disable buffering everywhere — nginx default buffers SSE into 4k
        # chunks which destroys the realtime UX.
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Content-Type": "text/event-stream",
    }
    return StreamingResponse(
        _dashboard_stream_generator(
            sessionmaker=get_sessionmaker(),
            last_seen_id=last_event_id,
        ),
        media_type="text/event-stream",
        headers=headers,
    )


# ---------- /web/batches/ list + detail (Sprint 8b Task 2) --------------------


_WEB_BATCHES_PAGE_SIZE = 20


@router.get("/batches/", response_class=HTMLResponse)
async def batches_list(
    request: Request,
    page: int = Query(default=1, ge=1),
    flash: str | None = Query(default=None),
    flash_kind: str | None = Query(default=None),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the paginated batch list. Calls ``QRBatchRepository.query``
    directly (decision I — no HTTP self-call). Newest-first.

    ``flash`` / ``flash_kind`` query params are surfaced to the template so
    the post-retire-QR 303 redirect from ``web_qr_retire`` can show a
    confirmation/error banner (same pattern as ``/web/sessions/``).
    """
    rows, has_more = await QRBatchRepository(session).query(
        page=page, page_size=_WEB_BATCHES_PAGE_SIZE
    )
    return templates.TemplateResponse(
        request,
        "batches/list.html",
        {
            "user_email": user.email,
            "batches": rows,
            "page": page,
            "has_more": has_more,
            "has_prev": page > 1,
            "flash": flash,
            "flash_kind": flash_kind,
        },
    )


# ---------- /web/batches/new + POST /web/batches/ — create batch form -------
#
# IMPORTANT: declared BEFORE ``/batches/{batch_id}`` so FastAPI's order-based
# dispatch matches /web/batches/new to this handler and not to the
# parameterised detail route. The detail route's path converter (UUID) would
# 422 on "new" otherwise.


@router.get("/batches/new", response_class=HTMLResponse)
async def batches_new_form(
    request: Request,
    user: WebAdminUser = Depends(require_web_admin),
) -> HTMLResponse:
    """Render the "create new batch" form. POST target is ``/web/batches/``."""
    return templates.TemplateResponse(
        request,
        "batches/new.html",
        {"user_email": user.email, "csrf_token": user.csrf_token},
    )


def _parse_optional_form_int(raw: str, *, field: str) -> int | None:
    """Browsers submit empty optional fields as ``""`` instead of omitting
    them — Pydantic's ``int | None`` coerces "" to a 422. We accept the
    field as a string, strip, and treat empty as ``None``; non-empty must
    parse as a positive int or we 422 explicitly via ``HTTPException``."""
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        value = int(stripped)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field} must be a positive integer or blank",
        ) from exc
    if value < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field} must be ≥ 1 (or blank)",
        )
    return value


@router.post("/batches/")
async def web_batches_create(
    count: int = Form(ge=1, le=500),
    csrf: str = Form(alias="_csrf"),
    comment: str = Form(default="", max_length=200),
    intended_site_id: str = Form(default=""),
    intended_location_id: str = Form(default=""),
    intended_rack_id: str = Form(default=""),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Create a QR batch from the web form. Delegates to ``QRGenerationService``
    directly so the three-record-write apparatus (batch row + N FREE codes +
    one ``qr.generate_batch`` audit row) stays in one place.

    No idempotency key — the form is one-shot from a browser; double-submit
    of the same form would create two batches, which is acceptable for an
    interactive flow (the admin sees the redirect and won't double-submit).
    303 to the new batch's detail page with a flash banner.

    The ``intended_*_id`` form fields arrive as strings (HTML forms can't
    omit optional fields, only send them empty) — we parse them with
    ``_parse_optional_form_int`` rather than declaring ``int | None`` here,
    which would 422 on the empty-string browsers submit.
    """
    verify_csrf_token(csrf, user.csrf_token)
    site_id = _parse_optional_form_int(intended_site_id, field="intended_site_id")
    location_id = _parse_optional_form_int(
        intended_location_id, field="intended_location_id"
    )
    rack_id = _parse_optional_form_int(intended_rack_id, field="intended_rack_id")
    auth_user = await _build_auth_user_for_admin_action(user)
    service = QRGenerationService(
        session,
        QRBatchRepository(session),
        QRCodeRepository(session),
        AuditLogRepository(session),
    )
    payload = GenerateBatchRequest(
        count=count,
        intended_site_id=site_id,
        intended_location_id=location_id,
        intended_rack_id=rack_id,
        comment=comment.strip() or None,
    )
    batch = await service.generate_batch(payload, auth_user)
    await session.commit()
    flash = f"Batch created with {count} codes"
    target = f"/web/batches/{batch.id}?{urlencode({'flash': flash, 'flash_kind': 'info'})}"
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
async def batches_detail(
    request: Request,
    batch_id: UUID,
    show_retired: bool = Query(default=False),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render one batch's detail page: metadata + status counts + QR table +
    Download Labels link.

    ``?show_retired=1`` (default off, 2026-06-10): toggle visibility of
    RETIRED rows in the codes table. Retired stickers stay in the DB
    forever for audit purposes (Architecture §4 — state-machine final
    state, audit_log references depend on the row), but day-to-day the
    admin doesn't need to scroll past them. The status_counts chips
    always include all states regardless of the toggle.

    Unknown ``batch_id`` → 404 + custom HTML page (decision 9 — web
    flows render HTML, not JSON).
    """
    from app.domain.qr import QRStatus

    batch_repo = QRBatchRepository(session)
    code_repo = QRCodeRepository(session)
    batch = await batch_repo.get_by_id(batch_id)
    if batch is None:
        return templates.TemplateResponse(
            request,
            "_not_found.html",
            {"user_email": user.email, "resource": f"batch {batch_id}"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    all_codes = await code_repo.find_by_batch_id(batch_id)
    status_counts = await code_repo.count_by_status_for_batch(batch_id)
    codes = (
        all_codes
        if show_retired
        else [c for c in all_codes if c.status is not QRStatus.RETIRED]
    )
    retired_count = sum(1 for c in all_codes if c.status is QRStatus.RETIRED)
    # Resolve bound device ids → human-readable names for the table.
    # One NetBox round-trip per page render covers all ids; admin sees
    # "sw-01" instead of "5". Devices deleted in NetBox after binding
    # fall back to the raw id (rare).
    bound_ids = {c.bound_to_device_id for c in codes if c.bound_to_device_id is not None}
    device_names: dict[int, str] = {}
    if bound_ids:
        try:
            device_names = await DeviceService(get_netbox_client()).get_device_names_by_ids(
                bound_ids
            )
        except Exception as exc:
            # NetBox blip shouldn't break the batch detail page — log + fall back
            # to raw ids. Admin still sees everything, just not pretty names.
            logger.warning("batch_detail_device_name_lookup_failed", error=repr(exc))
    return templates.TemplateResponse(
        request,
        "batches/detail.html",
        {
            "user_email": user.email,
            "batch": batch,
            "codes": codes,
            "status_counts": status_counts,
            "csrf_token": user.csrf_token,
            "show_retired": show_retired,
            "retired_hidden_count": 0 if show_retired else retired_count,
            "device_names": device_names,
        },
    )


@router.get("/batches/{batch_id}/labels.pdf")
async def web_batches_labels_pdf(
    batch_id: UUID,
    include: str = Query(default="free", pattern="^(free|all)$"),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Cookie-authed PDF download for the batch detail page.

    ``?include=free`` (default, 2026-06-10): render only QR codes still
    in FREE state. Avoids reprinting labels for stickers already used
    (BOUND) or discarded (RETIRED). ``?include=all`` returns every code
    regardless of state.
    """
    _ = user  # role-gating side-effect only
    from app.domain.qr import QRStatus
    from app.services.pdf_labels import render_batch_labels_pdf

    batch = await QRBatchRepository(session).get_by_id(batch_id)
    if batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="batch not found"
        )
    codes = await QRCodeRepository(session).find_by_batch_id(batch_id)
    if include == "free":
        codes = [c for c in codes if c.status is QRStatus.FREE]
    pdf_bytes = await asyncio.to_thread(
        render_batch_labels_pdf, batch=batch, codes=codes
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="batch-{batch_id}.pdf"'
        },
    )


# ---------- /web/audit/ list + detail (Sprint 8b Task 3) ---------------------


_WEB_AUDIT_PAGE_SIZE = 20


def _audit_filter_query_string(
    *,
    user_keycloak_id: str | None,
    from_: str | None,
    to: str | None,
    entity_type: str | None,
    entity_id: str | None,
    operation: str | None,
    session_id: str | None,
    result: str | None,
) -> str:
    """Re-encode the eight audit filters as a URL-encoded query string.

    Used by the list template's pagination + "Download CSV" links so the
    operator's filter context survives page navigation. Empty / None
    values are dropped so the URL stays clean.
    """
    params = {
        "user_keycloak_id": user_keycloak_id,
        "from": from_,
        "to": to,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "operation": operation,
        "session_id": session_id,
        "result": result,
    }
    return urlencode({k: v for k, v in params.items() if v})


@router.get("/audit/", response_class=HTMLResponse)
async def audit_list(
    request: Request,
    page: int = Query(default=1, ge=1),
    user_keycloak_id: UUID | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    operation: str | None = Query(default=None),
    session_id: UUID | None = Query(default=None),
    result: AuditResult | None = Query(default=None),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Filtered, paginated audit log page.

    Reuses ``AuditLogRepository.query`` directly (decision I — no HTTP
    self-call). The page itself is NOT audited; only the JSON endpoint and
    the CSV export write audit-of-audits rows (Sprint 7 Task 2 + Sprint 8b
    Task 3 decision 6). The web page consuming the same data is just a
    re-render of the same query result, not a separate read.
    """
    filters = AuditLogQueryFilters(
        user_keycloak_id=user_keycloak_id,
        from_=from_,
        to=to,
        entity_type=entity_type,
        entity_id=entity_id,
        operation=operation,
        session_id=session_id,
        result=result,
    )
    rows, has_more = await AuditLogRepository(session).query(
        filters=filters, page=page, page_size=_WEB_AUDIT_PAGE_SIZE
    )
    # Re-encode the user-submitted filters for pagination + CSV-download links.
    filter_qs = _audit_filter_query_string(
        user_keycloak_id=str(user_keycloak_id) if user_keycloak_id else None,
        from_=from_.isoformat() if from_ else None,
        to=to.isoformat() if to else None,
        entity_type=entity_type,
        entity_id=entity_id,
        operation=operation,
        session_id=str(session_id) if session_id else None,
        result=result.value if result else None,
    )
    return templates.TemplateResponse(
        request,
        "audit/list.html",
        {
            "user_email": user.email,
            "rows": rows,
            "page": page,
            "has_more": has_more,
            "has_prev": page > 1,
            "filter_qs": filter_qs,
            "filters": {
                "user_keycloak_id": str(user_keycloak_id) if user_keycloak_id else "",
                "from": from_.isoformat() if from_ else "",
                "to": to.isoformat() if to else "",
                "entity_type": entity_type or "",
                "entity_id": entity_id or "",
                "operation": operation or "",
                "session_id": str(session_id) if session_id else "",
                "result": result.value if result else "",
            },
            "result_choices": [r.value for r in AuditResult],
        },
    )


@router.get("/audit/csv")
async def web_audit_csv(
    request: Request,
    user_keycloak_id: UUID | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    operation: str | None = Query(default=None),
    session_id: UUID | None = Query(default=None),
    result: AuditResult | None = Query(default=None),
    page_size: int = Query(default=1000, ge=1, le=10000),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Cookie-authed CSV export — delegates to the bearer-only JSON handler
    so the ``audit.export_csv`` audit-of-audits row (ToR §5.4.6) still
    writes. Same class of fix as ``web_batches_labels_pdf``: the API
    endpoint requires a JWT bearer, browsers only carry the Fernet cookie.

    IMPORTANT: declared BEFORE ``/audit/{audit_id}`` so FastAPI's
    order-based dispatch matches /web/audit/csv to this handler. The
    detail route's ``audit_id: int`` would otherwise 422 on "csv".
    """
    _ = request
    auth_user = await _build_auth_user_for_admin_action(user)
    return await query_audit_log_csv(
        user_keycloak_id=user_keycloak_id,
        from_=from_,
        to=to,
        entity_type=entity_type,
        entity_id=entity_id,
        operation=operation,
        session_id=session_id,
        result=result,
        page_size=page_size,
        user=auth_user,
        session=session,
    )


@router.get("/audit/{audit_id}", response_class=HTMLResponse)
async def audit_detail(
    request: Request,
    audit_id: int,
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Single audit_log row: all 12 columns + pretty-printed JSON blobs.

    Unknown id → custom HTML 404 page via ``_not_found.html`` (decision 9,
    reused from Task 2).
    """
    entry = await AuditLogRepository(session).get_by_id(audit_id)
    if entry is None:
        return templates.TemplateResponse(
            request,
            "_not_found.html",
            {"user_email": user.email, "resource": f"audit row {audit_id}"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return templates.TemplateResponse(
        request,
        "audit/detail.html",
        {"user_email": user.email, "entry": entry},
    )


# ---------- /web/sessions/ list + inline force-close (Sprint 8b Task 4) -----


_WEB_SESSIONS_PAGE_SIZE = 20


def _sessions_filter_query_string(
    *,
    user_keycloak_id: str | None,
    from_: str | None,
    to: str | None,
    active_only: bool,
) -> str:
    """Re-encode the four shift filters as a URL-encoded query string.

    Pagination links + the post-force-close redirect carry the operator's
    filter context so the page they land on isn't a silent widening of
    scope. ``active_only`` is only included when True so the URL stays
    clean for the default case.
    """
    params: dict[str, str] = {}
    if user_keycloak_id:
        params["user_keycloak_id"] = user_keycloak_id
    if from_:
        params["from"] = from_
    if to:
        params["to"] = to
    if active_only:
        params["active_only"] = "true"
    return urlencode(params)


@router.get("/sessions/", response_class=HTMLResponse)
async def sessions_list(
    request: Request,
    page: int = Query(default=1, ge=1),
    user_keycloak_id: UUID | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    active_only: bool = Query(default=False),
    flash: str | None = Query(default=None),
    flash_kind: str | None = Query(default=None),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the shift-sessions list with per-row force-close form.

    Reuses ``ShiftSessionRepository.query`` directly (decision I — no HTTP
    self-call). Decision 9: no audit row (operational read, mirrors
    Sprint 7 decision 8 for ``GET /admin/sessions``). ``flash`` /
    ``flash_kind`` query params are surfaced to the template so the
    post-force-close redirect can show a confirmation banner.
    """
    filters = ShiftSessionQueryFilters(
        user_keycloak_id=user_keycloak_id,
        from_=from_,
        to=to,
        active_only=active_only,
    )
    rows, has_more = await ShiftSessionRepository(session).query(
        filters=filters, page=page, page_size=_WEB_SESSIONS_PAGE_SIZE
    )
    filter_qs = _sessions_filter_query_string(
        user_keycloak_id=str(user_keycloak_id) if user_keycloak_id else None,
        from_=from_.isoformat() if from_ else None,
        to=to.isoformat() if to else None,
        active_only=active_only,
    )
    return templates.TemplateResponse(
        request,
        "sessions/list.html",
        {
            "user_email": user.email,
            "rows": rows,
            "page": page,
            "has_more": has_more,
            "has_prev": page > 1,
            "filter_qs": filter_qs,
            "filters": {
                "user_keycloak_id": str(user_keycloak_id) if user_keycloak_id else "",
                "from": from_.isoformat() if from_ else "",
                "to": to.isoformat() if to else "",
                "active_only": active_only,
            },
            "end_reason_values": [r.value for r in ShiftEndReason],
            "flash": flash,
            "flash_kind": flash_kind,
            "csrf_token": user.csrf_token,
        },
    )


def _sessions_flash_redirect(*, flash: str, flash_kind: str, filter_qs: str) -> RedirectResponse:
    """302 back to ``/web/sessions/`` carrying a flash message + the filter
    QS so the operator stays in their filtered view after a force-close."""
    params = {"flash": flash, "flash_kind": flash_kind}
    qs = urlencode(params)
    target = f"/web/sessions/?{qs}"
    if filter_qs:
        target = f"{target}&{filter_qs}"
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/sessions/{session_id}/force-close")
async def web_force_close_session(
    request: Request,
    session_id: UUID,
    reason: str = Form(min_length=1, max_length=500),
    csrf: str = Form(alias="_csrf"),
    user_keycloak_id: str | None = Form(default=None),
    from_: str | None = Form(default=None, alias="from"),
    to: str | None = Form(default=None),
    active_only_value: str | None = Form(default=None, alias="active_only"),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """End the target shift via the existing JSON handler, redirect to the
    list with a flash banner.

    Delegates to ``app.api.v1.admin.sessions.force_close_session`` so the
    three-record-write apparatus stays in one place (decision 1). On
    ``HTTPException(404)`` the user sees an error flash; idempotent
    already-ended targets surface as the success flash (the JSON handler
    returns 200 with a CONFLICT-result audit row for them).

    Filter form fields are echoed back via hidden inputs so the redirect
    can preserve the operator's filter context (decision 3).
    """
    # The underlying JSON handler audits with the caller's shift_session_id —
    # look up the web admin's own active shift so the audit row attributes
    # correctly. ``require_web_admin`` already guarantees an active shift
    # exists (else AdminShiftNeeded was raised); this is just a second
    # round-trip to grab its id (revised D2 — not stored on WebAdminUser).
    #
    # The lookup runs in a FRESH session so the FastAPI-injected ``session``
    # stays in its no-transaction-active state — ``force_close_session``
    # opens its own ``async with session.begin()`` and would error if we
    # already auto-started a transaction by using ``session`` for the
    # lookup ("A transaction is already begun on this Session").
    _ = request  # FastAPI binds; unused inside this handler
    verify_csrf_token(csrf, user.csrf_token)
    async with get_sessionmaker()() as lookup_session:
        active = await ShiftSessionRepository(lookup_session).get_active_for_user(user.sub)
    # Class invariant from require_web_admin: active is not None here.
    assert active is not None, "require_web_admin must have raised AdminShiftNeeded"
    auth_user = AuthUser(
        sub=str(user.sub),
        email=user.email,
        roles=tuple(user.roles),
        session_id=None,
        shift_session_id=active.id,
    )
    filter_qs = _sessions_filter_query_string(
        user_keycloak_id=user_keycloak_id or None,
        from_=from_ or None,
        to=to or None,
        active_only=bool(active_only_value),
    )
    try:
        await force_close_session(
            session_id=session_id,
            body=ForceCloseRequest(reason=reason),
            user=auth_user,
            session=session,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return _sessions_flash_redirect(
                flash="Shift not found", flash_kind="error", filter_qs=filter_qs
            )
        raise
    return _sessions_flash_redirect(
        flash="Shift force-closed", flash_kind="info", filter_qs=filter_qs
    )


# ---------- /web/admin/shift/start --- "Open admin shift" form target -------


def _resolve_web_admin_cookie(request: Request) -> WebAdminUser | None:
    """Cookie + admin-role check without the active-shift lookup.

    ``require_web_admin`` raises ``AdminShiftNeeded`` when the user has no
    active shift — but the whole point of this handler IS to open one. So
    we re-do the lighter half of that dep inline. Returns ``None`` on any
    auth failure; caller redirects to /web/login (same information-leak
    rule as require_web_admin).
    """
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if raw is None:
        return None
    user = decode_session_cookie(raw)
    if user is None or "dcinv-admin" not in user.roles:
        return None
    return user


_WEB_ADMIN_TABLET_ID = "web-admin"
"""Hardcoded ``shift_sessions.tablet_id`` value for web-driven admin shifts
(2026-06-10 simplification). The column is shared with mobile (which uses
the physical tablet's id); admins all log in from browsers where typing a
"workstation id" added no audit value beyond "web". Mobile flow unchanged.
"""


@router.post("/admin/shift/start")
async def web_admin_shift_start(
    request: Request,
    csrf: str = Form(alias="_csrf"),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Open an admin shift from the ``_admin_shift_needed.html`` form.

    The intermediate page renders when an admin has a valid cookie but no
    active shift. The form has no fields anymore (2026-06-10) — just the
    CSRF token and a Start button; the underlying ``tablet_id`` column is
    populated with the hardcoded ``"web-admin"`` sentinel.

    Idempotent: if ``SessionAlreadyActive`` fires (concurrent shift opened
    in another tab), the user is already in the state the page wanted, so
    303 to ``/web/`` anyway rather than surface an error.
    """
    user = _resolve_web_admin_cookie(request)
    if user is None:
        return RedirectResponse(url="/web/login", status_code=status.HTTP_303_SEE_OTHER)
    verify_csrf_token(csrf, user.csrf_token)
    service = ShiftSessionService(session=session, repo=ShiftSessionRepository(session))
    try:
        await service.start(
            user_email=user.email,
            user_keycloak_id=user.sub,
            tablet_id=_WEB_ADMIN_TABLET_ID,
        )
    except SessionAlreadyActive:
        pass
    return RedirectResponse(url="/web/", status_code=status.HTTP_303_SEE_OTHER)


# ---------- Auth shim: web cookie → AuthUser for JSON-layer services --------


async def _build_auth_user_for_admin_action(user: WebAdminUser) -> AuthUser:
    """Look up the admin's active shift_session_id + build an ``AuthUser``.

    Same pattern as ``web_force_close_session``'s shim block. Required so the
    JSON-layer services (QRGenerationService, QRLifecycleService,
    DeviceDecommissionService) can write audit rows attributed to the admin's
    current shift. ``require_web_admin`` already guarantees an active shift
    exists, so the lookup either returns it or trips the asserted invariant.

    Uses a FRESH session so the FastAPI-injected per-request session stays in
    its no-transaction-active state — the services open their own
    ``async with session.begin()`` blocks.
    """
    async with get_sessionmaker()() as lookup_session:
        active = await ShiftSessionRepository(lookup_session).get_active_for_user(user.sub)
    assert active is not None, "require_web_admin must have raised AdminShiftNeeded"
    return AuthUser(
        sub=str(user.sub),
        email=user.email,
        roles=tuple(user.roles),
        session_id=None,
        shift_session_id=active.id,
    )


# ---------- POST /web/batches/{batch_id}/bulk-retire (Sprint 10 Task 3) -----


@router.post("/batches/{batch_id}/bulk-retire")
async def web_batches_bulk_retire(
    batch_id: UUID,
    csrf: str = Form(alias="_csrf"),
    qr_ids: list[str] = Form(default=[]),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Multi-select retire of FREE codes from the batch detail page.

    Submits a checkbox list (`qr_ids`) to retire in one operator action.
    Iterates calling ``QRLifecycleService.retire`` per QR — each retire
    keeps its three-record-write atomic semantics (decision G). NOT a
    bulk SQL UPDATE.

    Aggregated flash: ``Retired N of M — K failed (see audit log)``.
    Already-RETIRED codes (race with another tab / mobile retire) count
    as success in the aggregate; the admin gets a consistent "done" view.
    Empty selection → flash banner "No QR codes selected", no work.
    """
    verify_csrf_token(csrf, user.csrf_token)

    def _redirect(flash: str, flash_kind: str) -> RedirectResponse:
        qs = urlencode({"flash": flash, "flash_kind": flash_kind})
        return RedirectResponse(
            url=f"/web/batches/{batch_id}?{qs}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if not qr_ids:
        return _redirect("No QR codes selected", "error")

    auth_user = await _build_auth_user_for_admin_action(user)
    lifecycle = QRLifecycleService(
        netbox_client=get_netbox_client(),
        session=session,
        qr_code_repo=QRCodeRepository(session),
        audit_log_repo=AuditLogRepository(session),
        write_service=NetBoxWriteService(
            get_netbox_client(), session, AuditLogRepository(session)
        ),
    )

    succeeded = 0
    failed: list[str] = []
    for qr_id in qr_ids:
        try:
            await lifecycle.retire(qr_id=qr_id, expected_version=None, user=auth_user)
            succeeded += 1
        except QRStateConflictError as exc:
            # already-RETIRED → idempotent no-op; counts as success.
            # bound-now / mid-state → real failure.
            if exc.current_status.value == "retired":
                succeeded += 1
            else:
                failed.append(qr_id)
        except (
            QRNotFoundError,
            MissingVersionError,
            QRRetireRolledBackError,
            QRRetireInconsistencyError,
        ):
            failed.append(qr_id)

    total = len(qr_ids)
    if failed:
        return _redirect(
            f"Retired {succeeded} of {total} — {len(failed)} failed (see audit log)",
            "error",
        )
    return _redirect(f"Retired {succeeded} QR codes", "info")


# ---------- POST /web/batches/{batch_id}/delete — force-delete a batch ------


@router.post("/batches/{batch_id}/delete")
async def web_batches_delete(
    batch_id: UUID,
    csrf: str = Form(alias="_csrf"),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Force-delete an entire batch: unbind any BOUND labels, then hard-delete
    every QR row + the batch row, writing a ``batch.delete`` audit row.

    Destructive admin escape hatch (web-only — mobile never deletes batches).
    Ordering and safety:

    1. Unbind each BOUND label via ``QRLifecycleService.unbind`` (its own
       three-record write, freeing the NetBox device's ``qr_id``). If ANY
       bound label can't be cleanly unbound, the whole delete aborts — we
       never hard-delete a row whose NetBox device still points at it. The
       already-unbound labels stay FREE (retry-safe); the batch survives.
    2. With every label FREE/RETIRED, delete ``qr_codes`` then ``qr_batches``
       (FK order) + the audit row in one transaction.

    The per-label ``qr.unbind`` audit rows and the summary ``batch.delete``
    row reference QRs/batch by string id (no FK), so they outlive the delete.
    """
    from uuid import uuid4

    verify_csrf_token(csrf, user.csrf_token)

    def _to_list(flash: str, flash_kind: str) -> RedirectResponse:
        qs = urlencode({"flash": flash, "flash_kind": flash_kind})
        return RedirectResponse(
            url=f"/web/batches/?{qs}", status_code=status.HTTP_303_SEE_OTHER
        )

    def _to_detail(flash: str, flash_kind: str) -> RedirectResponse:
        qs = urlencode({"flash": flash, "flash_kind": flash_kind})
        return RedirectResponse(
            url=f"/web/batches/{batch_id}?{qs}", status_code=status.HTTP_303_SEE_OTHER
        )

    auth_user = await _build_auth_user_for_admin_action(user)
    netbox = get_netbox_client()
    device_service = DeviceService(netbox)

    # Read batch + codes in their own tx so the autobegun transaction closes
    # before the lifecycle service opens its own.
    async with session.begin():
        batch = await QRBatchRepository(session).get_by_id(batch_id)
        codes = await QRCodeRepository(session).find_by_batch_id(batch_id)
    if batch is None:
        return _to_list(f"Batch {batch_id} not found", "error")

    bound = [c for c in codes if c.status is QRStatus.BOUND]
    lifecycle = QRLifecycleService(
        netbox_client=netbox,
        session=session,
        qr_code_repo=QRCodeRepository(session),
        audit_log_repo=AuditLogRepository(session),
        write_service=NetBoxWriteService(netbox, session, AuditLogRepository(session)),
    )

    unbound = 0
    unbind_failed: list[str] = []
    for qr in bound:
        device_id = qr.bound_to_device_id
        assert device_id is not None  # BOUND invariant
        try:
            device = await device_service.get_device(device_id)
            await lifecycle.unbind(
                qr_id=qr.id,
                expected_version=device.version,
                reason=f"batch {batch_id} force-deleted",
                user=auth_user,
            )
            unbound += 1
        except (
            NetBoxNotFound,
            WriteConflictError,
            QRStateConflictError,
            QRNotFoundError,
            QRUnbindRolledBackError,
            QRUnbindInconsistencyError,
        ):
            unbind_failed.append(qr.id)

    if unbind_failed:
        # Abort before any hard-delete — leave the batch intact for a retry.
        return _to_detail(
            f"Unbound {unbound} of {len(bound)} bound labels — "
            f"{len(unbind_failed)} could not be cleared, batch not deleted "
            "(see audit log)",
            "error",
        )

    # All labels are now FREE/RETIRED — hard-delete codes, batch, + audit row.
    async with session.begin():
        await QRCodeRepository(session).delete_by_batch_id(batch_id)
        await QRBatchRepository(session).delete(batch_id)
        await AuditLogRepository(session).insert(
            AuditLogEntry(
                request_id=uuid4(),
                timestamp=datetime.now(UTC),
                user_email=user.email or "",
                user_keycloak_id=user.sub,
                session_id=auth_user.shift_session_id,
                operation="batch.delete",
                entity_type="batch",
                entity_id=str(batch_id),
                before_json={"count": batch.count, "comment": batch.comment},
                after_json={"deleted_codes": len(codes), "unbound": unbound},
                result=AuditResult.SUCCESS,
            )
        )

    detail = f"{len(codes)} codes" + (f", {unbound} unbound" if unbound else "")
    return _to_list(f"Batch deleted ({detail})", "info")


# ---------- POST /web/qr/{qr_id}/retire — inline retire-QR form -------------


@router.post("/qr/{qr_id}/retire")
async def web_qr_retire(
    qr_id: str,
    csrf: str = Form(alias="_csrf"),
    batch_id: UUID | None = Form(default=None),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Retire a QR from the inline form on the batch detail page.

    Scope-limited to FREE→RETIRED: the batch detail template only renders
    the retire button on FREE rows (BOUND retires need a device version
    that's awkward to surface in HTML and is better routed through the
    mobile flow or the decommission-device page). If a BOUND QR arrives
    here anyway (race), the service raises ``QRStateConflictError`` and
    we flash an error instead of silently corrupting state.

    Idempotent: ``QRStateConflictError`` on an already-RETIRED QR maps to
    an info flash ("QR already retired") rather than an error.

    ``batch_id`` is the originating batch (hidden form input from the
    detail template) so the post-action 303 lands the admin back on the
    same detail page rather than the batch list — preserves their
    inspection context when retiring multiple FREE codes in a row.
    Falls back to the list when absent (curl / hand-rolled POST).
    """
    verify_csrf_token(csrf, user.csrf_token)
    auth_user = await _build_auth_user_for_admin_action(user)
    lifecycle = QRLifecycleService(
        netbox_client=get_netbox_client(),
        session=session,
        qr_code_repo=QRCodeRepository(session),
        audit_log_repo=AuditLogRepository(session),
        write_service=NetBoxWriteService(
            get_netbox_client(), session, AuditLogRepository(session)
        ),
    )
    target = f"/web/batches/{batch_id}" if batch_id is not None else "/web/batches/"
    try:
        await lifecycle.retire(qr_id=qr_id, expected_version=None, user=auth_user)
    except QRNotFoundError:
        flash, kind = f"QR {qr_id} not registered", "error"
    except QRStateConflictError as exc:
        # FREE→RETIRED happy path won't trip this; only an already-RETIRED
        # row or a concurrent bind would. Treat already-RETIRED as a no-op,
        # bound-now as an error.
        if exc.current_status.value == "retired":
            flash, kind = f"QR {qr_id} already retired", "info"
        else:
            flash, kind = (
                f"QR {qr_id} is {exc.current_status.value} — use device decommission to retire it",
                "error",
            )
    except MissingVersionError:
        flash, kind = (
            f"QR {qr_id} is bound — retire it via device decommission instead",
            "error",
        )
    except (QRRetireRolledBackError, QRRetireInconsistencyError):
        flash, kind = f"QR {qr_id} retire rolled back — see audit log", "error"
    else:
        flash, kind = f"QR {qr_id} retired", "info"
    return RedirectResponse(
        url=f"{target}?{urlencode({'flash': flash, 'flash_kind': kind})}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------- POST /web/qr/{qr_id}/unbind — free a BOUND label ----------------


@router.post("/qr/{qr_id}/unbind")
async def web_qr_unbind(
    qr_id: str,
    csrf: str = Form(alias="_csrf"),
    batch_id: UUID | None = Form(default=None),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Unbind a BOUND label (BOUND→FREE) from the batch detail page —
    "just detach the device" without retiring the sticker.

    The admin never types a device version: the handler reads the QR to
    find its bound device, fetches that device's ``last_updated`` for OCC,
    then delegates to ``QRLifecycleService.unbind`` (same three-record
    write as the mobile ``POST /api/v1/qr/{id}/unbind``).

    ``batch_id`` (hidden form input) routes the 303 back to the originating
    batch page; absent → the batch list.
    """
    verify_csrf_token(csrf, user.csrf_token)
    auth_user = await _build_auth_user_for_admin_action(user)
    netbox = get_netbox_client()
    target = f"/web/batches/{batch_id}" if batch_id is not None else "/web/batches/"

    def _redirect(flash: str, flash_kind: str) -> RedirectResponse:
        return RedirectResponse(
            url=f"{target}?{urlencode({'flash': flash, 'flash_kind': flash_kind})}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Read the QR (in its own tx so the autobegun one closes before the
    # service opens its own) to find the bound device for the OCC version.
    async with session.begin():
        qr = await QRCodeRepository(session).get_by_id(qr_id)
    if qr is None:
        return _redirect(f"QR {qr_id} not registered", "error")
    if qr.status is not QRStatus.BOUND or qr.bound_to_device_id is None:
        return _redirect(f"QR {qr_id} is {qr.status.value} — nothing to unbind", "error")

    try:
        device = await DeviceService(netbox).get_device(qr.bound_to_device_id)
    except NetBoxNotFound:
        return _redirect(
            f"Device {qr.bound_to_device_id} not found in NetBox", "error"
        )

    lifecycle = _device_lifecycle(session)
    try:
        await lifecycle.unbind(
            qr_id=qr_id,
            expected_version=device.version,
            reason="unbound by admin from batch view",
            user=auth_user,
        )
    except QRNotFoundError:
        return _redirect(f"QR {qr_id} not registered", "error")
    except QRStateConflictError as exc:
        return _redirect(
            f"QR {qr_id} is {exc.current_status.value} — nothing to unbind", "error"
        )
    except WriteConflictError:
        return _redirect(
            "Device was modified concurrently — reload and try again", "error"
        )
    except NetBoxNotFound:
        return _redirect(
            f"Device {qr.bound_to_device_id} not found in NetBox", "error"
        )
    except (QRUnbindRolledBackError, QRUnbindInconsistencyError):
        return _redirect(f"Unbind of QR {qr_id} rolled back — see audit log", "error")
    return _redirect(f"QR {qr_id} unbound", "info")


# ---------- POST /web/qr/{qr_id}/restore — undo an accidental retire --------


@router.post("/qr/{qr_id}/restore")
async def web_qr_restore(
    qr_id: str,
    csrf: str = Form(alias="_csrf"),
    batch_id: UUID | None = Form(default=None),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Restore a RETIRED QR back to FREE.

    Added 2026-06-11 after admins reported retiring working stickers by
    mistake. RETIRED → FREE has no NetBox side-effect; the historical
    ``bound_*`` fields on the QR are NOT auto-restored (see
    ``QR.restore`` docstring for the rationale), so a restored QR can
    be re-bound to any device via the normal mobile flow.

    Web-only operation — the mobile app never undoes a retire (any
    field-side mistakes are mediated by an admin reviewing the audit
    log). The batch detail template renders this button on RETIRED
    rows only when ``?show_retired=1`` is on, so the action is at most
    one extra click for a deliberate operator.

    Idempotent flash: already-FREE/BOUND comes back as a 409-shaped
    info banner ("QR ... is FREE, nothing to restore") so re-submission
    doesn't surface as a scary error.
    """
    verify_csrf_token(csrf, user.csrf_token)
    auth_user = await _build_auth_user_for_admin_action(user)
    lifecycle = QRLifecycleService(
        netbox_client=get_netbox_client(),
        session=session,
        qr_code_repo=QRCodeRepository(session),
        audit_log_repo=AuditLogRepository(session),
        write_service=NetBoxWriteService(
            get_netbox_client(), session, AuditLogRepository(session)
        ),
    )
    # Always preserve ?show_retired=1 on the return URL — operator is
    # likely batch-restoring several rows from the same view.
    target_path = (
        f"/web/batches/{batch_id}?show_retired=1"
        if batch_id is not None
        else "/web/batches/"
    )
    try:
        await lifecycle.restore(qr_id=qr_id, user=auth_user)
    except QRNotFoundError:
        flash, kind = f"QR {qr_id} not registered", "error"
    except QRStateConflictError as exc:
        flash, kind = (
            f"QR {qr_id} is {exc.current_status.value} — nothing to restore",
            "info",
        )
    else:
        flash, kind = f"QR {qr_id} restored to FREE", "info"
    sep = "&" if "?" in target_path else "?"
    return RedirectResponse(
        url=f"{target_path}{sep}{urlencode({'flash': flash, 'flash_kind': kind})}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------- /web/qr/{qr_id}/rebind — reassign a BOUND label to another device


_WEB_REBIND_SEARCH_PAGE_SIZE = 20


def _rebind_redirect(
    *, qr_id: str, flash: str, flash_kind: str, device_id: int | None = None
) -> RedirectResponse:
    """303 back to the rebind wizard (optionally keeping the picked device)."""
    params: dict[str, str] = {"flash": flash, "flash_kind": flash_kind}
    if device_id is not None:
        params["device_id"] = str(device_id)
    return RedirectResponse(
        url=f"/web/qr/{qr_id}/rebind?{urlencode(params)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/qr/{qr_id}/rebind", response_class=HTMLResponse)
async def web_qr_rebind_form(
    request: Request,
    qr_id: str,
    q: str | None = Query(default=None, max_length=255),
    device_id: int | None = Query(default=None, ge=1),
    flash: str | None = Query(default=None),
    flash_kind: str | None = Query(default=None),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Rebind wizard: move a BOUND QR to another device (2026-06-16).

    Web counterpart of the mobile ``POST /api/v1/qr/{id}/rebind`` — same
    ``QRLifecycleService.rebind`` underneath (decision I: direct service call).

    Single page, server-rendered, three states driven by query params:
    - base: the QR + its current device (by name) + a device-search box;
    - ``?q=`` : candidate devices matching the name (reuses DeviceService.search);
    - ``?device_id=`` : the chosen target + a reason field + confirm button
      (POSTs to the same path).

    Only BOUND QRs are rebindable; FREE/RETIRED render an explanatory note
    instead of the wizard.
    """
    qr = await QRCodeRepository(session).get_by_id(qr_id)
    if qr is None:
        return templates.TemplateResponse(
            request,
            "_not_found.html",
            {"user_email": user.email, "resource": f"QR {qr_id}"},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    device_service = DeviceService(get_netbox_client())

    # Current device name (best-effort — a NetBox blip shows the raw id).
    current_device_name: str | None = None
    if qr.bound_to_device_id is not None:
        try:
            names = await device_service.get_device_names_by_ids({qr.bound_to_device_id})
            current_device_name = names.get(qr.bound_to_device_id)
        except Exception as exc:
            logger.warning("rebind_current_device_name_failed", error=repr(exc))

    candidates: list[Any] = []
    candidates_error: str | None = None
    target: Any = None
    target_error: str | None = None
    is_bound = qr.status is QRStatus.BOUND

    if is_bound and q and q.strip():
        try:
            envelope = await device_service.search(
                name=q.strip(), page=1, page_size=_WEB_REBIND_SEARCH_PAGE_SIZE
            )
            # Drop the QR's current device from the candidate list — rebinding
            # to it is a no-op (the service would 409 SAME_DEVICE anyway).
            candidates = [
                d for d in envelope.results if d.data.id != qr.bound_to_device_id
            ]
        except Exception as exc:
            candidates_error = f"Could not search devices: {type(exc).__name__}"

    if is_bound and device_id is not None:
        try:
            target = await device_service.get_device(device_id)
        except NetBoxNotFound:
            target_error = f"Device {device_id} not found in NetBox"
        except Exception as exc:
            target_error = f"Could not load device {device_id}: {type(exc).__name__}"

    return templates.TemplateResponse(
        request,
        "qr/rebind.html",
        {
            "user_email": user.email,
            "qr": qr,
            "is_bound": is_bound,
            "current_device_name": current_device_name,
            "query": q or "",
            "candidates": candidates,
            "candidates_error": candidates_error,
            "target": target,
            "target_error": target_error,
            "flash": flash,
            "flash_kind": flash_kind,
            "csrf_token": user.csrf_token,
        },
    )


@router.post("/qr/{qr_id}/rebind")
async def web_qr_rebind(
    qr_id: str,
    csrf: str = Form(alias="_csrf"),
    device_id: int = Form(ge=1),
    reason: str = Form(min_length=1, max_length=2000),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Execute the rebind. Fetches the target device's ``last_updated`` itself
    (the admin never types a version — same pattern as the decommission form),
    then delegates to ``QRLifecycleService.rebind``. Errors → flash back to the
    wizard; success → flash on the QR search page.
    """
    verify_csrf_token(csrf, user.csrf_token)
    auth_user = await _build_auth_user_for_admin_action(user)
    netbox = get_netbox_client()
    device_service = DeviceService(netbox)

    # OCC version for the target device — fetched here, passed to rebind.
    try:
        target = await device_service.get_device(device_id)
    except NetBoxNotFound:
        return _rebind_redirect(
            qr_id=qr_id, flash=f"Device {device_id} not found in NetBox", flash_kind="error"
        )

    audit_repo = AuditLogRepository(session)
    lifecycle = QRLifecycleService(
        netbox_client=netbox,
        session=session,
        qr_code_repo=QRCodeRepository(session),
        audit_log_repo=audit_repo,
        write_service=NetBoxWriteService(netbox, session, audit_repo),
    )
    target_label = target.data.name or f"device {device_id}"
    try:
        await lifecycle.rebind(
            qr_id=qr_id,
            new_device_id=device_id,
            expected_version=target.version,
            reason=reason,
            user=auth_user,
        )
    except QRNotFoundError:
        return _rebind_redirect(
            qr_id=qr_id, flash=f"QR {qr_id} not registered", flash_kind="error"
        )
    except QRStateConflictError as exc:
        return _rebind_redirect(
            qr_id=qr_id,
            flash=f"QR is {exc.current_status.value} — only a BOUND label can be rebound",
            flash_kind="error",
        )
    except SameDeviceError:
        return _rebind_redirect(
            qr_id=qr_id,
            flash=f"QR is already bound to {target_label}",
            flash_kind="info",
            device_id=device_id,
        )
    except DeviceAlreadyBoundError as exc:
        return _rebind_redirect(
            qr_id=qr_id,
            flash=(
                f"{target_label} already has QR {exc.existing_qr_id} — "
                "unbind it first"
            ),
            flash_kind="error",
            device_id=device_id,
        )
    except NetBoxNotFound:
        return _rebind_redirect(
            qr_id=qr_id, flash=f"Device {device_id} not found in NetBox", flash_kind="error"
        )
    except WriteConflictError:
        return _rebind_redirect(
            qr_id=qr_id,
            flash=f"{target_label} was modified concurrently — reload and try again",
            flash_kind="error",
            device_id=device_id,
        )
    except (QRRebindRolledBackError, QRRebindInconsistencyError):
        return _rebind_redirect(
            qr_id=qr_id,
            flash=f"Rebind of QR {qr_id} rolled back — see audit log",
            flash_kind="error",
        )
    # Success → back to the QR search page with the new state.
    qs = urlencode(
        {"qr_id": qr_id, "flash": f"QR {qr_id} rebound to {target_label}", "flash_kind": "info"}
    )
    return RedirectResponse(
        url=f"/web/qr/search?{qs}", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------- /web/devices/decommission GET + POST — decommission form --------


@router.get("/devices/decommission", response_class=HTMLResponse)
async def devices_decommission_form(
    request: Request,
    user: WebAdminUser = Depends(require_web_admin),
    flash: str | None = Query(default=None),
    flash_kind: str | None = Query(default=None),
) -> HTMLResponse:
    """Render the decommission-device form. POST target is the same path."""
    return templates.TemplateResponse(
        request,
        "devices/decommission.html",
        {
            "user_email": user.email,
            "flash": flash,
            "flash_kind": flash_kind,
            "csrf_token": user.csrf_token,
        },
    )


def _decommission_redirect(*, flash: str, flash_kind: str) -> RedirectResponse:
    """303 back to the decommission form with a flash banner."""
    qs = urlencode({"flash": flash, "flash_kind": flash_kind})
    return RedirectResponse(
        url=f"/web/devices/decommission?{qs}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/devices/decommission")
async def web_devices_decommission(
    device_id: int = Form(ge=1),
    reason: str = Form(min_length=1, max_length=2000),
    csrf: str = Form(alias="_csrf"),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Decommission ``device_id`` with ``reason``. Form takes only id + reason;
    the handler fetches the device's current ``last_updated`` itself and
    passes it as the OCC version to ``DeviceDecommissionService.decommission``.

    Error surface (all → 303 back to the form with a flash banner):
    - 404 (unknown device) → error flash
    - 409 (device modified between our read and the decommission, or bound QR
      not in BOUND state) → error flash
    - 5xx (rollback / inconsistency) → error flash pointing at audit log
    """
    verify_csrf_token(csrf, user.csrf_token)
    auth_user = await _build_auth_user_for_admin_action(user)
    device_service = DeviceService(get_netbox_client())
    try:
        current = await device_service.get_device(device_id)
    except NetBoxNotFound:
        return _decommission_redirect(
            flash=f"Device {device_id} not found in NetBox", flash_kind="error"
        )
    netbox = get_netbox_client()
    audit_repo = AuditLogRepository(session)
    qr_code_repo = QRCodeRepository(session)
    write_service = NetBoxWriteService(netbox, session, audit_repo)
    lifecycle_service = QRLifecycleService(
        netbox_client=netbox,
        session=session,
        qr_code_repo=qr_code_repo,
        audit_log_repo=audit_repo,
        write_service=write_service,
    )
    decom_service = DeviceDecommissionService(
        netbox_client=netbox,
        session=session,
        qr_code_repo=qr_code_repo,
        write_service=write_service,
        lifecycle_service=lifecycle_service,
    )
    try:
        await decom_service.decommission(
            device_id=device_id,
            expected_version=current.version,
            reason=reason,
            user=auth_user,
        )
    except NetBoxNotFound:
        return _decommission_redirect(
            flash=f"Device {device_id} not found in NetBox", flash_kind="error"
        )
    except WriteConflictError:
        return _decommission_redirect(
            flash=(
                f"Device {device_id} was modified concurrently — "
                "reload and try again"
            ),
            flash_kind="error",
        )
    except QRStateConflictError as exc:
        return _decommission_redirect(
            flash=(
                f"Bound QR is {exc.current_status.value} — cannot decommission cleanly"
            ),
            flash_kind="error",
        )
    except (
        QRRetireRolledBackError,
        DeviceDecommissionRolledBackError,
        DeviceDecommissionInconsistencyError,
    ):
        return _decommission_redirect(
            flash=(
                f"Decommission of device {device_id} rolled back — see audit log"
            ),
            flash_kind="error",
        )
    return _decommission_redirect(
        flash=f"Device {device_id} decommissioned", flash_kind="info"
    )


# ---------- POST /web/devices/bulk-decommission (2026-06-10) -----------------
#
# Mirror of /web/batches/{id}/bulk-retire. Multi-select on the device search
# results page lets admin decommission a rack of servers in one click. Each
# device is decommissioned via a separate ``DeviceDecommissionService.decommission``
# call — three-record-write atomic semantics preserved per device. NOT a
# bulk PATCH (would lose journal entries + audit rows per device + QR-retire
# compensation).


@router.post("/devices/bulk-decommission")
async def web_devices_bulk_decommission(
    csrf: str = Form(alias="_csrf"),
    device_ids: list[int] = Form(default=[]),
    reason: str = Form(min_length=1, max_length=2000),
    return_to: str = Form(default="/web/devices/search"),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Decommission a list of devices with one shared ``reason``.

    Iterates per-id: fetch device → call ``DeviceDecommissionService.decommission``
    with the device's own ``last_updated`` as the OCC version. Each call is
    independent — a 409 on device #3 doesn't roll back the successful first
    two. Aggregated flash: ``Decommissioned N of M — K failed (see audit log)``.

    Already-decommissioned devices: NetBox will refuse the status PATCH (the
    decommissioned status is idempotent on the data side, but the service
    raises if the QR-bound state doesn't match). We count those as failed
    rather than silently-success — the admin should know which ids were
    no-ops, since the audit log won't have a row for them.

    Asymmetry vs bulk retire (which counts already-RETIRED as success): an
    already-RETIRED QR is a "clean" idempotent no-op (the QR's terminal
    state matches the requested action). An already-decommissioned device
    raises ``QRStateConflictError`` because its bound QR is by then in
    some inconsistent intermediate state — that's a real signal worth
    surfacing, not a no-op.

    ``return_to`` lets the search page round-trip its filter querystring so
    the admin lands back on the same result set. Defaults to the bare search
    URL when the form omits it (curl / hand-rolled POST).
    """
    verify_csrf_token(csrf, user.csrf_token)

    # Defense-in-depth: validate ``return_to`` is an internal search URL.
    # CSRF + uvicorn header sanitisation already guard against the obvious
    # open-redirect / CRLF-injection attacks, but the one-line allow-list
    # closes the chain in case any future CSRF bypass surfaces.
    safe_return_to = (
        return_to
        if return_to.startswith("/web/devices/")
        else "/web/devices/search"
    )

    def _redirect(flash: str, flash_kind: str) -> RedirectResponse:
        # Append flash to safe_return_to. The URL may already have a `?` from
        # the round-tripped filter qs; in that case use `&`.
        sep = "&" if "?" in safe_return_to else "?"
        qs = urlencode({"flash": flash, "flash_kind": flash_kind})
        return RedirectResponse(
            url=f"{safe_return_to}{sep}{qs}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if not device_ids:
        return _redirect("No devices selected", "error")

    auth_user = await _build_auth_user_for_admin_action(user)
    netbox = get_netbox_client()
    device_service = DeviceService(netbox)
    succeeded = 0
    failed: list[int] = []

    for device_id in device_ids:
        try:
            current = await device_service.get_device(device_id)
        except NetBoxNotFound:
            failed.append(device_id)
            continue

        # New repos / write_service per iteration: each decommission opens
        # its own session.begin() and we don't want shared state across
        # rows. Cheap — these are thin objects, no I/O at construction.
        audit_repo = AuditLogRepository(session)
        qr_code_repo = QRCodeRepository(session)
        write_service = NetBoxWriteService(netbox, session, audit_repo)
        lifecycle_service = QRLifecycleService(
            netbox_client=netbox,
            session=session,
            qr_code_repo=qr_code_repo,
            audit_log_repo=audit_repo,
            write_service=write_service,
        )
        decom_service = DeviceDecommissionService(
            netbox_client=netbox,
            session=session,
            qr_code_repo=qr_code_repo,
            write_service=write_service,
            lifecycle_service=lifecycle_service,
        )
        try:
            await decom_service.decommission(
                device_id=device_id,
                expected_version=current.version,
                reason=reason,
                user=auth_user,
            )
            succeeded += 1
        except (
            NetBoxNotFound,
            WriteConflictError,
            QRStateConflictError,
            QRRetireRolledBackError,
            DeviceDecommissionRolledBackError,
            DeviceDecommissionInconsistencyError,
        ):
            failed.append(device_id)

    total = len(device_ids)
    if failed:
        return _redirect(
            f"Decommissioned {succeeded} of {total} — {len(failed)} failed "
            "(see audit log)",
            "error",
        )
    return _redirect(f"Decommissioned {succeeded} devices", "info")


# ---------- /web/devices/search + /web/devices/{id} (Sprint 9 Task 2) -------
#
# IMPORTANT: declared BEFORE /devices/{device_id} so FastAPI's order-based
# dispatch matches /web/devices/search to this handler (not to the int-typed
# detail route). Same pattern as /web/batches/new before /web/batches/{id}.

_WEB_DEVICE_AUDIT_PAGE_SIZE = 20


@router.get("/devices/search", response_class=HTMLResponse)
async def web_devices_search(
    request: Request,
    name: str | None = Query(default=None, max_length=255),
    asset_tag: str | None = Query(default=None, max_length=255),
    serial: str | None = Query(default=None, max_length=255),
    site: str | None = Query(default=None, max_length=16),
    rack: str | None = Query(default=None, max_length=16),
    page: int = Query(default=1, ge=1),
    flash: str | None = Query(default=None),
    flash_kind: str | None = Query(default=None),
    user: WebAdminUser = Depends(require_web_admin),
) -> HTMLResponse:
    """Search NetBox devices via the same API the mobile app uses (Sprint
    9 Task 1). Filter form at the top; results table below when any
    filter is set. Read-only; no audit row.

    ``site`` / ``rack`` are accepted as strings (not ``int`` query params)
    so the GET form can submit empty values without tripping FastAPI's
    int coercion → 422 JSON (the form always posts every field, blank or
    not). Non-numeric / out-of-range values are ignored as "no filter".

    ``flash`` / ``flash_kind`` surface the bulk-decommission redirect
    banner so the admin sees aggregated success/failure right where they
    selected the rows."""

    def _opt_int(raw: str | None) -> int | None:
        raw = (raw or "").strip()
        return int(raw) if raw.isdigit() and int(raw) >= 1 else None

    site_id = _opt_int(site)
    rack_id = _opt_int(rack)
    service = DeviceService(get_netbox_client())
    submitted = any(
        v is not None and v != ""
        for v in (name, asset_tag, serial, site_id, rack_id)
    )
    results: list[Any] = []
    has_more = False
    error: str | None = None
    if submitted:
        try:
            envelope = await service.search(
                name=name,
                asset_tag=asset_tag,
                serial=serial,
                site_id=site_id,
                rack_id=rack_id,
                page=page,
                page_size=20,
            )
            results = envelope.results
            has_more = envelope.has_more
        except Exception as exc:  # NetBox transport / circuit / etc.
            error = f"Could not search devices: {type(exc).__name__}"
    return templates.TemplateResponse(
        request,
        "devices/search.html",
        {
            "user_email": user.email,
            "submitted": submitted,
            "filters": {
                "name": name or "",
                "asset_tag": asset_tag or "",
                "serial": serial or "",
                "site_id": site_id if site_id is not None else "",
                "rack_id": rack_id if rack_id is not None else "",
            },
            "results": results,
            "has_more": has_more,
            "has_prev": page > 1,
            "page": page,
            "error": error,
            "flash": flash,
            "flash_kind": flash_kind,
            "csrf_token": user.csrf_token,
        },
    )


@router.get("/devices/{device_id}", response_class=HTMLResponse)
async def web_devices_detail(
    request: Request,
    device_id: int,
    flash: str | None = Query(default=None),
    flash_kind: str | None = Query(default=None),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Read-only device detail page. Sprint 9 Task 2.

    Shows device fields + a comments-add form (CSRF-protected) + the 20
    most recent audit rows for ``entity_type=device, entity_id={id}``.
    Does NOT expose edit / decommission controls — those are mobile-only
    or have their own dedicated forms.
    """
    service = DeviceService(get_netbox_client())
    try:
        device = await service.get_device(device_id)
    except NetBoxNotFound:
        return templates.TemplateResponse(
            request,
            "_not_found.html",
            {"user_email": user.email, "resource": f"device {device_id}"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    # The QR currently bound to this device (if any) drives the "QR label"
    # card — reassign / replace / bind controls. A read on the request
    # session autobegins a tx, but this handler does no further session
    # writes, so that's fine here.
    bound_qr = await QRCodeRepository(session).find_by_bound_device_id(device_id)
    audit_rows, audit_has_more = await AuditLogRepository(session).query(
        filters=AuditLogQueryFilters(
            entity_type="device", entity_id=str(device_id)
        ),
        page=1,
        page_size=_WEB_DEVICE_AUDIT_PAGE_SIZE,
    )
    return templates.TemplateResponse(
        request,
        "devices/detail.html",
        {
            "user_email": user.email,
            "device": device,
            "bound_qr": bound_qr,
            "audit_rows": audit_rows,
            "audit_has_more": audit_has_more,
            "csrf_token": user.csrf_token,
            "flash": flash,
            "flash_kind": flash_kind,
        },
    )


@router.post("/devices/{device_id}/comments")
async def web_devices_add_comment(
    request: Request,
    device_id: int,
    comment: str = Form(min_length=1, max_length=2000),
    csrf: str = Form(alias="_csrf"),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> RedirectResponse:
    """CSRF-protected comments form post. Delegates to ``add_comment`` JSON
    handler via direct Python call so the three-record-write apparatus
    (NetBox journal + audit row) stays in one place — same decision-I
    pattern as ``web_force_close_session``.

    Web admin's ``dcinv-admin`` role bypasses the JSON handler's
    ``dcinv-mobile-user`` dep gate because direct delegation skips dep
    resolution; the admin is authorised via their own auth path
    (``require_web_admin``).
    """
    _ = request  # FastAPI binds; unused
    verify_csrf_token(csrf, user.csrf_token)
    auth_user = await _build_auth_user_for_admin_action(user)
    flash_target = (
        f"/web/devices/{device_id}?{urlencode({'flash': 'Comment added', 'flash_kind': 'info'})}"
    )
    try:
        await add_comment(
            device_id=device_id,
            request=AddCommentRequest(comment=comment),
            user=auth_user,
            comment_service=get_comment_service(
                write_service=NetBoxWriteService(
                    get_netbox_client(), session, AuditLogRepository(session)
                )
            ),
            sessionmaker=sessionmaker,
            idempotency_key=None,
        )
    except Exception as exc:  # NetBoxNotFound, NetBoxValidationError, transport
        flash_target = (
            f"/web/devices/{device_id}?"
            + urlencode(
                {
                    "flash": f"Could not add comment: {type(exc).__name__}",
                    "flash_kind": "error",
                }
            )
        )
    return RedirectResponse(url=flash_target, status_code=status.HTTP_303_SEE_OTHER)


def _device_redirect(*, device_id: int, flash: str, flash_kind: str) -> RedirectResponse:
    """303 back to the device detail page with a flash banner."""
    qs = urlencode({"flash": flash, "flash_kind": flash_kind})
    return RedirectResponse(
        url=f"/web/devices/{device_id}?{qs}", status_code=status.HTTP_303_SEE_OTHER
    )


def _device_lifecycle(session: AsyncSession) -> QRLifecycleService:
    """Build a QRLifecycleService bound to the request session (decision I)."""
    netbox = get_netbox_client()
    audit_repo = AuditLogRepository(session)
    return QRLifecycleService(
        netbox_client=netbox,
        session=session,
        qr_code_repo=QRCodeRepository(session),
        audit_log_repo=audit_repo,
        write_service=NetBoxWriteService(netbox, session, audit_repo),
    )


@router.post("/devices/{device_id}/replace-qr")
async def web_devices_replace_qr(
    device_id: int,
    new_qr_id: str = Form(min_length=1, max_length=255),
    csrf: str = Form(alias="_csrf"),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Set the QR bound to ``device_id`` to ``new_qr_id`` (2026-06-19).

    Two cases, one form:
    - device has no QR → ``bind`` the new FREE label;
    - device already has QR X → ``unbind`` X (X→FREE) then ``bind`` the new
      label (Y→BOUND). The DB's ``qr_one_per_device`` partial unique index
      forbids two BOUND labels on one device, so the unbind must land first.

    Each step is its own three-record write; there is a brief window after a
    successful unbind where the device has no QR. If the follow-up bind fails
    the handler says so explicitly (old label freed, new not bound) rather
    than pretending the device is unchanged.

    OCC versions are fetched here (the admin never types one); the device's
    ``last_updated`` changes after the unbind PATCH, so it is re-read before
    the bind.
    """
    verify_csrf_token(csrf, user.csrf_token)
    new_qr_id = new_qr_id.strip()
    auth_user = await _build_auth_user_for_admin_action(user)
    device_service = DeviceService(get_netbox_client())

    # Current binding — read inside an explicit tx so the autobegun
    # transaction is closed before the lifecycle service opens its own.
    async with session.begin():
        current_qr = await QRCodeRepository(session).find_by_bound_device_id(device_id)

    if current_qr is not None and current_qr.id == new_qr_id:
        return _device_redirect(
            device_id=device_id,
            flash=f"QR {new_qr_id} is already bound to this device",
            flash_kind="info",
        )

    try:
        device = await device_service.get_device(device_id)
    except NetBoxNotFound:
        return _device_redirect(
            device_id=device_id,
            flash=f"Device {device_id} not found in NetBox",
            flash_kind="error",
        )

    lifecycle = _device_lifecycle(session)

    # Step 1 — unbind the existing label, if any.
    if current_qr is not None:
        try:
            await lifecycle.unbind(
                qr_id=current_qr.id,
                expected_version=device.version,
                reason=f"replaced by {new_qr_id}",
                user=auth_user,
            )
        except QRStateConflictError:
            return _device_redirect(
                device_id=device_id,
                flash=(
                    f"QR {current_qr.id} is no longer bound — reload and try again"
                ),
                flash_kind="error",
            )
        except WriteConflictError:
            return _device_redirect(
                device_id=device_id,
                flash="Device was modified concurrently — reload and try again",
                flash_kind="error",
            )
        except (QRUnbindRolledBackError, QRUnbindInconsistencyError):
            return _device_redirect(
                device_id=device_id,
                flash=f"Could not unbind QR {current_qr.id} — see audit log",
                flash_kind="error",
            )
        # The unbind PATCH changed the device's last_updated — re-read it.
        try:
            device = await device_service.get_device(device_id)
        except NetBoxNotFound:
            return _device_redirect(
                device_id=device_id,
                flash=(
                    f"QR {current_qr.id} unbound, but device {device_id} then "
                    "vanished from NetBox — no new QR bound"
                ),
                flash_kind="error",
            )

    # Step 2 — bind the new label.
    freed = f"QR {current_qr.id} freed; " if current_qr is not None else ""
    try:
        await lifecycle.bind(
            qr_id=new_qr_id,
            device_id=device_id,
            expected_version=device.version,
            user=auth_user,
        )
    except QRNotFoundError:
        return _device_redirect(
            device_id=device_id,
            flash=f"{freed}QR {new_qr_id} is not registered — nothing bound",
            flash_kind="error",
        )
    except QRStateConflictError as exc:
        return _device_redirect(
            device_id=device_id,
            flash=(
                f"{freed}QR {new_qr_id} is {exc.current_status.value} — "
                "only a FREE label can be bound"
            ),
            flash_kind="error",
        )
    except DeviceAlreadyBoundError as exc:
        return _device_redirect(
            device_id=device_id,
            flash=f"{freed}Device already has QR {exc.existing_qr_id}",
            flash_kind="error",
        )
    except WriteConflictError:
        return _device_redirect(
            device_id=device_id,
            flash=f"{freed}Device was modified concurrently — reload and try again",
            flash_kind="error",
        )
    except (QRBindRolledBackError, QRBindInconsistencyError):
        return _device_redirect(
            device_id=device_id,
            flash=f"{freed}Could not bind QR {new_qr_id} — see audit log",
            flash_kind="error",
        )

    verb = "replaced with" if current_qr is not None else "bound"
    return _device_redirect(
        device_id=device_id,
        flash=f"Device QR {verb} {new_qr_id}",
        flash_kind="info",
    )


# ---------- /web/qr/search — QR lookup by id --------------------------------


_WEB_QR_SEARCH_AUDIT_PAGE_SIZE = 20
# Substring search cap. 50 keeps the page responsive on very loose
# fragments like "DCQR" without dumping the whole table; the template
# surfaces "more matches truncated" when this is hit.
_WEB_QR_SEARCH_MATCH_LIMIT = 50


@router.get("/qr/search", response_class=HTMLResponse)
async def web_qr_search(
    request: Request,
    qr_id: str | None = Query(default=None, max_length=255),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """One-page QR lookup: form + (optional) result block.

    ``GET /web/qr/search`` with no ``qr_id`` renders just the search form.
    With ``?qr_id=...`` renders the form pre-filled + a result block
    showing the QR row, the bound NetBox device (if any), and the 20
    most recent audit rows for this QR.

    Read-only — no writes, no audit row (operational read, mirrors
    ``/admin/audit`` per Sprint 7 decision 8). NetBox is consulted only
    when the QR is BOUND; FREE and RETIRED skip the network round-trip.
    """
    if qr_id is None or not qr_id.strip():
        return templates.TemplateResponse(
            request,
            "qr/search.html",
            {
                "user_email": user.email,
                "submitted_qr_id": "",
                "qr": None,
                "device": None,
                "device_error": None,
                "audit_rows": None,
                "audit_has_more": False,
                "matches": None,
                "matches_truncated": False,
                "lookup_attempted": False,
            },
        )
    qr_id = qr_id.strip()
    code_repo = QRCodeRepository(session)
    qr = await code_repo.get_by_id(qr_id)
    matches: list[QR] | None = None
    matches_truncated = False
    if qr is None and len(qr_id) >= 3:
        # Substring fallback so an admin who types "7F3A" finds
        # "DCQR-7F3A2B" without remembering the full slug. Capped at the
        # repository limit + 1 so we can flag truncation in the template.
        matches = await code_repo.search_by_id_substring(
            fragment=qr_id, limit=_WEB_QR_SEARCH_MATCH_LIMIT + 1
        )
        if len(matches) > _WEB_QR_SEARCH_MATCH_LIMIT:
            matches_truncated = True
            matches = matches[:_WEB_QR_SEARCH_MATCH_LIMIT]
        # If substring returned exactly one match, treat it as if the admin
        # had typed the full id — render the detail block directly.
        if len(matches) == 1:
            qr = matches[0]
            matches = None
    device = None
    device_error: str | None = None
    audit_rows: list[AuditLogEntry] = []
    audit_has_more = False
    if qr is not None:
        if qr.bound_to_device_id is not None:
            try:
                device = await DeviceService(get_netbox_client()).get_device(
                    qr.bound_to_device_id
                )
            except NetBoxNotFound:
                device_error = (
                    f"Bound device {qr.bound_to_device_id} not found in NetBox "
                    "(stale binding?)."
                )
            except Exception as exc:  # NetBoxClientError / circuit open / etc.
                device_error = f"Could not fetch bound device: {type(exc).__name__}"
        audit_rows, audit_has_more = await AuditLogRepository(session).query(
            filters=AuditLogQueryFilters(entity_type="qr", entity_id=qr.id),
            page=1,
            page_size=_WEB_QR_SEARCH_AUDIT_PAGE_SIZE,
        )
    return templates.TemplateResponse(
        request,
        "qr/search.html",
        {
            "user_email": user.email,
            "submitted_qr_id": qr_id,
            "qr": qr,
            "device": device,
            "device_error": device_error,
            "audit_rows": audit_rows,
            "audit_has_more": audit_has_more,
            "matches": matches,
            "matches_truncated": matches_truncated,
            "lookup_attempted": True,
        },
    )


# ---------- /web/users/ — list + detail (read-only over Keycloak admin) ------


_WEB_USERS_PAGE_SIZE = 20


@router.get("/users/", response_class=HTMLResponse)
async def web_users_list(
    request: Request,
    page: int = Query(default=1, ge=1),
    search: str | None = Query(default=None, max_length=255),
    user: WebAdminUser = Depends(require_web_admin),
) -> HTMLResponse:
    """Paginated user list via Keycloak admin REST API. Renders a
    "not configured" notice when ``KEYCLOAK_ADMIN_CLIENT_SECRET`` is
    unset, so the page degrades gracefully on hosts that haven't yet
    set up the admin client.
    """
    client = get_keycloak_admin_client()
    users: list[KeycloakUser] = []
    has_more = False
    error: str | None = None
    not_configured = False
    try:
        users, has_more = await client.list_users(
            page=page, page_size=_WEB_USERS_PAGE_SIZE, search=search or None
        )
    except KeycloakAdminNotConfigured:
        not_configured = True
    except KeycloakAdminError as exc:
        error = f"Keycloak admin API error: {exc}"
    return templates.TemplateResponse(
        request,
        "users/list.html",
        {
            "user_email": user.email,
            "users": users,
            "page": page,
            "has_more": has_more,
            "has_prev": page > 1,
            "search": search or "",
            "not_configured": not_configured,
            "error": error,
        },
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def web_users_detail(
    request: Request,
    user_id: str,
    user: WebAdminUser = Depends(require_web_admin),
) -> HTMLResponse:
    """Single-user detail page: identity, enable state, realm roles,
    created-at timestamp. Read-only; write operations are out of scope
    for this slice (would need their own audit-row + CSRF flow)."""
    client = get_keycloak_admin_client()
    try:
        target = await client.get_user(user_id)
    except KeycloakAdminNotConfigured:
        return templates.TemplateResponse(
            request,
            "users/list.html",
            {
                "user_email": user.email,
                "users": [],
                "page": 1,
                "has_more": False,
                "has_prev": False,
                "search": "",
                "not_configured": True,
                "error": None,
            },
        )
    except KeycloakAdminError as exc:
        return templates.TemplateResponse(
            request,
            "_not_found.html",
            {"user_email": user.email, "resource": f"user {user_id} ({exc})"},
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    if target is None:
        return templates.TemplateResponse(
            request,
            "_not_found.html",
            {"user_email": user.email, "resource": f"user {user_id}"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return templates.TemplateResponse(
        request,
        "users/detail.html",
        {"user_email": user.email, "target": target},
    )
