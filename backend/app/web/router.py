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
from pathlib import Path
from urllib.parse import urlencode

import httpx
import structlog
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import jwt

from app.config import get_settings
from app.web.auth import (
    SESSION_COOKIE_MAX_AGE_SECONDS,
    SESSION_COOKIE_NAME,
    WebAdminUser,
    build_session_cookie_payload,
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
    # Note: ``secure=True`` belongs in production behind TLS. For dev/test
    # against http://localhost, leave it off so the cookie actually sets.
    # Deployment runs behind TLS (CLAUDE.md "VPN-only" doesn't imply
    # plaintext); ops can flip via a Settings knob in Sprint 9+ if needed.
    for cookie_name, cookie_value in (
        (_OIDC_STATE_COOKIE, state),
        (_OIDC_NONCE_COOKIE, nonce),
        (_OIDC_NEXT_COOKIE, next),
    ):
        response.set_cookie(
            cookie_name,
            cookie_value,
            httponly=True,
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


# ---------- /web/ placeholder dashboard --------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard_placeholder(
    request: Request, user: WebAdminUser = Depends(require_web_admin)
) -> HTMLResponse:
    """Sprint 8b Task 0 placeholder. Task 1 replaces with the real
    counters dashboard. Proves the auth flow round-trips end-to-end before
    any page-specific code lands."""
    return templates.TemplateResponse(
        request,
        "_dashboard_placeholder.html",
        {"user_email": user.email},
    )
