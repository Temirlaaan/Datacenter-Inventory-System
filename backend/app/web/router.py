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
    MissingVersionError,
    QRLifecycleService,
    QRNotFoundError,
    QRRetireInconsistencyError,
    QRRetireRolledBackError,
    QRStateConflictError,
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
        },
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
    site_id: int | None = Query(default=None, ge=1, alias="site"),
    rack_id: int | None = Query(default=None, ge=1, alias="rack"),
    page: int = Query(default=1, ge=1),
    user: WebAdminUser = Depends(require_web_admin),
) -> HTMLResponse:
    """Search NetBox devices via the same API the mobile app uses (Sprint
    9 Task 1). Filter form at the top; results table below when any
    filter is set. Read-only; no audit row."""
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


# ---------- /web/qr/search — QR lookup by id --------------------------------


_WEB_QR_SEARCH_AUDIT_PAGE_SIZE = 20


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
                "lookup_attempted": False,
            },
        )
    qr_id = qr_id.strip()
    qr = await QRCodeRepository(session).get_by_id(qr_id)
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
            filters=AuditLogQueryFilters(entity_type="qr", entity_id=qr_id),
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
