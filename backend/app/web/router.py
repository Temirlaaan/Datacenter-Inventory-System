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

import secrets
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.sessions import ForceCloseRequest, force_close_session
from app.auth.dependencies import AuthUser
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
from app.domain.audit import AuditResult
from app.domain.shift_session import ShiftEndReason
from app.services.shift_session import SessionAlreadyActive, ShiftSessionService
from app.web.auth import (
    SESSION_COOKIE_MAX_AGE_SECONDS,
    SESSION_COOKIE_NAME,
    WebAdminUser,
    build_session_cookie_payload,
    decode_session_cookie,
    encode_session_cookie,
    require_web_admin,
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
        logger.warning(
            "web_oidc_callback_state_mismatch",
            has_code=bool(code),
            has_state=bool(state),
            state_match=state == expected_state if expected_state else False,
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
        {"user_email": user.email},
        status_code=status.HTTP_403_FORBIDDEN,
    )


# ---------- /web/ dashboard --------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the admin dashboard with the six counters from
    :class:`DashboardRepository`. Same data source as
    ``GET /api/v1/admin/dashboard`` — the page consumes the repo directly
    via dep injection rather than self-HTTP-calling the JSON endpoint
    (decision I)."""
    snap = await DashboardRepository(session).snapshot(now=datetime.now(UTC))
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user_email": user.email,
            "snapshot": snap,
        },
    )


# ---------- /web/batches/ list + detail (Sprint 8b Task 2) --------------------


_WEB_BATCHES_PAGE_SIZE = 20


@router.get("/batches/", response_class=HTMLResponse)
async def batches_list(
    request: Request,
    page: int = Query(default=1, ge=1),
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the paginated batch list. Calls ``QRBatchRepository.query``
    directly (decision I — no HTTP self-call). Newest-first."""
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
        },
    )


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
async def batches_detail(
    request: Request,
    batch_id: UUID,
    user: WebAdminUser = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render one batch's detail page: metadata + status counts + QR table +
    Download Labels link. Unknown ``batch_id`` → 404 + custom HTML page
    (decision 9 — web flows render HTML, not JSON)."""
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
    codes = await code_repo.find_by_batch_id(batch_id)
    status_counts = await code_repo.count_by_status_for_batch(batch_id)
    return templates.TemplateResponse(
        request,
        "batches/detail.html",
        {
            "user_email": user.email,
            "batch": batch,
            "codes": codes,
            "status_counts": status_counts,
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


@router.post("/admin/shift/start")
async def web_admin_shift_start(
    request: Request,
    workstation_id: str = Form(min_length=1, max_length=255),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Open an admin shift from the ``_admin_shift_needed.html`` form.

    The intermediate page renders when an admin has a valid cookie but no
    active shift. This handler accepts the form post (urlencoded — browsers
    ignore ``enctype="application/json"``, which was the original bug) and
    delegates to ``ShiftSessionService.start`` directly so the shift-open
    apparatus stays in one place — same decision-I pattern as
    ``web_force_close_session`` above.

    Idempotent: if ``SessionAlreadyActive`` fires (concurrent shift opened
    in another tab), the user is already in the state the page wanted, so
    303 to ``/web/`` anyway rather than surface an error.
    """
    user = _resolve_web_admin_cookie(request)
    if user is None:
        return RedirectResponse(url="/web/login", status_code=status.HTTP_303_SEE_OTHER)
    service = ShiftSessionService(session=session, repo=ShiftSessionRepository(session))
    try:
        await service.start(
            user_email=user.email,
            user_keycloak_id=user.sub,
            tablet_id=workstation_id,
        )
    except SessionAlreadyActive:
        pass
    return RedirectResponse(url="/web/", status_code=status.HTTP_303_SEE_OTHER)
