"""Keycloak Admin REST API client (2026-06-07).

Powers ``/web/users/`` — list + inspect Keycloak users from the admin
surface. Read-only at this stage; write operations (disable user, role
changes) deliberately deferred — they need their own audit-row + CSRF
flow plus a clear decision on which writes the admin tool should expose.

Auth: client_credentials grant against a confidential admin client
(``KEYCLOAK_ADMIN_CLIENT_ID`` / ``KEYCLOAK_ADMIN_CLIENT_SECRET``). The
client's service account needs the ``realm-management.view-users`` role.
Token cached in-process with an expiry-based refresh; the cache is
per-process (each replica re-fetches its own token, which is fine —
tokens are cheap to issue).

Errors:
- :class:`KeycloakAdminNotConfigured` when the secret env var is unset
  (web handler renders a friendly "configure KEYCLOAK_ADMIN_CLIENT_*"
  notice instead of crashing).
- :class:`KeycloakAdminError` for any HTTP / transport failure when
  talking to Keycloak — the web handler maps these to an error flash.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from app.config import get_settings

logger = structlog.get_logger()

_TOKEN_REFRESH_LEEWAY_SECONDS = 10
"""Re-fetch the token this many seconds before its actual expiry, so a
request that races the expiry boundary doesn't 401 mid-flight."""

_HTTP_TIMEOUT_SECONDS = 10.0


class KeycloakAdminNotConfigured(Exception):
    """``KEYCLOAK_ADMIN_CLIENT_SECRET`` is unset; the admin endpoints
    can't be reached. /web/users/ surfaces a friendly notice."""


class KeycloakAdminError(Exception):
    """Any transport / non-2xx response from Keycloak. Carries the
    HTTP status (or ``None`` for transport failures) so callers can
    distinguish 4xx (likely misconfiguration) from 5xx (transient)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class KeycloakUser:
    """Subset of the Keycloak user representation we surface to admins.

    The wire shape is large; we project to the fields the inventory
    admin actually needs: identity, contact, enable state, timestamps,
    and (when fetched) realm roles.
    """

    id: str
    username: str
    email: str | None
    first_name: str | None
    last_name: str | None
    enabled: bool
    created_at: datetime | None
    roles: tuple[str, ...] = ()


def _project_user(payload: dict[str, Any]) -> KeycloakUser:
    """Translate a Keycloak REST user representation to :class:`KeycloakUser`.

    Defensive about missing fields — Keycloak omits null-valued keys
    rather than emitting them as ``null``, and admin payloads vary by
    realm config (e.g. ``createdTimestamp`` may be off for some realms).
    """
    created_raw = payload.get("createdTimestamp")
    created_at: datetime | None = None
    if isinstance(created_raw, int):
        # Keycloak ships createdTimestamp as ms-since-epoch.
        created_at = datetime.fromtimestamp(created_raw / 1000, tz=UTC)
    return KeycloakUser(
        id=str(payload["id"]),
        username=str(payload.get("username", "")),
        email=str(payload["email"]) if payload.get("email") else None,
        first_name=str(payload["firstName"]) if payload.get("firstName") else None,
        last_name=str(payload["lastName"]) if payload.get("lastName") else None,
        enabled=bool(payload.get("enabled", False)),
        created_at=created_at,
    )


@dataclass(slots=True)
class _CachedToken:
    """In-memory access token + its absolute expiry. Singleton on the
    client — one refresh ever 60s under normal load."""

    access_token: str
    expires_at: datetime


class KeycloakAdminClient:
    """Async wrapper over the Keycloak Admin REST API.

    Constructed once at module level (see :func:`get_keycloak_admin_client`).
    Holds a cached service-account token; refreshes on demand.

    Methods cover the read-only feature set we surface in /web/users/:
    list, get, and per-user realm roles. Write operations stay out of
    scope until a separate spec lands.
    """

    def __init__(self) -> None:
        self._token: _CachedToken | None = None
        self._token_lock = asyncio.Lock()

    # ---------- token management ------------------------------------------

    async def _get_token(self) -> str:
        """Return a valid access token, refreshing if expired or near
        expiry. Serialised via an asyncio lock so concurrent requests
        don't all hit Keycloak's token endpoint at the same time."""
        settings = get_settings()
        if settings.keycloak_admin_client_secret is None:
            raise KeycloakAdminNotConfigured(
                "KEYCLOAK_ADMIN_CLIENT_SECRET is not set"
            )
        now = datetime.now(UTC)
        leeway = timedelta(seconds=_TOKEN_REFRESH_LEEWAY_SECONDS)
        if self._token is not None and self._token.expires_at - leeway > now:
            return self._token.access_token
        async with self._token_lock:
            # Second-check under the lock — a concurrent caller may have
            # refreshed while we waited.
            if self._token is not None and self._token.expires_at - leeway > now:
                return self._token.access_token
            await self._refresh_token()
            assert self._token is not None
            return self._token.access_token

    async def _refresh_token(self) -> None:
        settings = get_settings()
        secret = settings.keycloak_admin_client_secret
        assert secret is not None  # checked by caller
        payload = {
            "grant_type": "client_credentials",
            "client_id": settings.keycloak_admin_client_id,
            "client_secret": secret.get_secret_value(),
        }
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.post(settings.keycloak_token_url, data=payload)
        except httpx.HTTPError as exc:
            logger.error("keycloak_admin_token_transport_error", error=repr(exc))
            raise KeycloakAdminError(
                f"transport error fetching admin token: {type(exc).__name__}"
            ) from exc
        if resp.status_code != 200:
            logger.warning(
                "keycloak_admin_token_non_200",
                status=resp.status_code,
                body=resp.text[:200],
            )
            raise KeycloakAdminError(
                f"Keycloak token endpoint returned {resp.status_code}",
                status_code=resp.status_code,
            )
        body = resp.json()
        access_token = body.get("access_token")
        expires_in_seconds = body.get("expires_in")
        if not isinstance(access_token, str) or not isinstance(expires_in_seconds, int):
            raise KeycloakAdminError(
                "Keycloak token response missing access_token / expires_in"
            )
        self._token = _CachedToken(
            access_token=access_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )

    # ---------- public API ------------------------------------------------

    async def list_users(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
    ) -> tuple[list[KeycloakUser], bool]:
        """List users, newest first. Returns ``(users, has_more)`` — we
        request ``page_size + 1`` and trim, mirroring the audit-log
        pagination pattern (Sprint 7 Task 2)."""
        first = (page - 1) * page_size
        max_results = page_size + 1
        params: dict[str, str | int] = {"first": first, "max": max_results}
        if search:
            params["search"] = search
        payload = await self._get("/users", params=params)
        if not isinstance(payload, list):
            raise KeycloakAdminError("Expected a list from /users")
        users = [_project_user(u) for u in payload[:page_size]]
        has_more = len(payload) > page_size
        return users, has_more

    async def get_user(self, user_id: str) -> KeycloakUser | None:
        """Single-user fetch. Returns ``None`` for 404 so the web layer
        renders the custom 404 page instead of a flash banner."""
        try:
            payload = await self._get(f"/users/{user_id}")
        except KeycloakAdminError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not isinstance(payload, dict):
            raise KeycloakAdminError("Expected a dict from /users/{id}")
        roles = await self.get_user_realm_roles(user_id)
        user = _project_user(payload)
        return KeycloakUser(
            id=user.id,
            username=user.username,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            enabled=user.enabled,
            created_at=user.created_at,
            roles=roles,
        )

    async def get_user_realm_roles(self, user_id: str) -> tuple[str, ...]:
        """Realm roles assigned to ``user_id``. Excludes client roles —
        the inventory tool only cares about ``dcinv-*`` realm roles
        and the realm-default roles."""
        payload = await self._get(f"/users/{user_id}/role-mappings/realm")
        if not isinstance(payload, list):
            raise KeycloakAdminError("Expected a list from /role-mappings/realm")
        return tuple(sorted(str(r["name"]) for r in payload if "name" in r))

    # ---------- transport helpers ----------------------------------------

    async def _get(
        self, path: str, *, params: dict[str, str | int] | None = None
    ) -> Any:
        token = await self._get_token()
        settings = get_settings()
        url = f"{settings.keycloak_admin_realm_base}{path}"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            logger.error(
                "keycloak_admin_get_transport_error", path=path, error=repr(exc)
            )
            raise KeycloakAdminError(
                f"transport error: {type(exc).__name__}"
            ) from exc
        if resp.status_code == 404:
            raise KeycloakAdminError(
                "Keycloak returned 404", status_code=404
            )
        if resp.status_code >= 400:
            logger.warning(
                "keycloak_admin_get_non_2xx",
                path=path,
                status=resp.status_code,
                body=resp.text[:200],
            )
            raise KeycloakAdminError(
                f"Keycloak returned {resp.status_code}",
                status_code=resp.status_code,
            )
        return resp.json()


# ---------- process-wide singleton --------------------------------------------

_singleton: KeycloakAdminClient | None = None


def get_keycloak_admin_client() -> KeycloakAdminClient:
    """Lazy singleton. One token cache + one asyncio.Lock per process."""
    global _singleton
    if _singleton is None:
        _singleton = KeycloakAdminClient()
    return _singleton


def reset_keycloak_admin_client() -> None:
    """Clear the cached client + its token. Used by tests + when the
    admin client secret is rotated at runtime (no live config-rotation
    flow exists yet, but the helper is ready)."""
    global _singleton
    _singleton = None
