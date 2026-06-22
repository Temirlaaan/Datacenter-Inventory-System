"""``/web/users/`` — read-only user list + detail over the Keycloak admin API.

Extracted from ``app.web.router``. Read-only: write operations (enable/disable,
role changes) would need their own audit-row + CSRF flow and are out of scope.
Degrades gracefully to a "not configured" notice when the Keycloak admin client
secret is unset.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import HTMLResponse

from app.auth.keycloak_admin import (
    KeycloakAdminError,
    KeycloakAdminNotConfigured,
    KeycloakUser,
    get_keycloak_admin_client,
)
from app.web.auth import WebAdminUser, require_web_admin
from app.web.templating import templates

router = APIRouter()

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
