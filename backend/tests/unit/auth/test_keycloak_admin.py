"""Unit tests for app.auth.keycloak_admin — token cache, list/get, error mapping."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from app.auth.keycloak_admin import (
    KeycloakAdminClient,
    KeycloakAdminError,
    KeycloakAdminNotConfigured,
    reset_keycloak_admin_client,
)

_REALM_BASE = "https://sso.example.com/admin/realms/prod-v1"
_TOKEN_URL = "https://sso.example.com/realms/prod-v1/protocol/openid-connect/token"


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_SERVICE_TOKEN", "x")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "https://sso.example.com")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://dcinv_test:dcinv_test@localhost:5433/dcinv_test"
    )
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_ID", "dcinv-web")
    monkeypatch.setenv("KEYCLOAK_WEB_CLIENT_SECRET", "test-web-client-secret")
    monkeypatch.setenv("SESSION_COOKIE_KEY", "VAMsIWGaHXesGIhCmHI6GQsRNdLwMuZA3Aw95EO1JBo=")
    from app.config import get_settings

    get_settings.cache_clear()
    reset_keycloak_admin_client()


def _enable_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "dcinv-admin-cli")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "test-admin-secret")
    from app.config import get_settings

    get_settings.cache_clear()


async def test_get_token_raises_not_configured_when_secret_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``KEYCLOAK_ADMIN_CLIENT_SECRET`` the client refuses to talk
    to Keycloak — the web handler renders a friendly notice instead."""
    _set_env(monkeypatch)
    client = KeycloakAdminClient()
    with pytest.raises(KeycloakAdminNotConfigured):
        await client._get_token()


async def test_list_users_returns_paginated_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``list_users`` requests ``page_size + 1`` and trims, returning
    ``has_more=True`` when the server actually had more."""
    _set_env(monkeypatch)
    _enable_admin(monkeypatch)
    client = KeycloakAdminClient()
    with respx.mock(assert_all_called=True) as router:
        router.post(_TOKEN_URL).mock(
            return_value=Response(200, json={"access_token": "T", "expires_in": 60})
        )
        # 3 users returned for page_size=2 → has_more should be True
        router.get(f"{_REALM_BASE}/users").mock(
            return_value=Response(
                200,
                json=[
                    {"id": "1", "username": "alice", "email": "alice@example.com", "enabled": True},
                    {"id": "2", "username": "bob", "email": None, "enabled": False},
                    {"id": "3", "username": "carol", "email": "carol@example.com", "enabled": True},
                ],
            )
        )
        users, has_more = await client.list_users(page=1, page_size=2)

    assert has_more is True
    assert [u.username for u in users] == ["alice", "bob"]
    assert users[0].email == "alice@example.com"
    assert users[1].email is None
    assert users[0].enabled is True
    assert users[1].enabled is False


async def test_get_user_returns_none_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown user id → ``None`` so the web layer renders the custom
    404 page instead of an error flash."""
    _set_env(monkeypatch)
    _enable_admin(monkeypatch)
    client = KeycloakAdminClient()
    with respx.mock(assert_all_called=True) as router:
        router.post(_TOKEN_URL).mock(
            return_value=Response(200, json={"access_token": "T", "expires_in": 60})
        )
        router.get(f"{_REALM_BASE}/users/ghost").mock(return_value=Response(404))
        result = await client.get_user("ghost")
    assert result is None


async def test_get_user_assembles_user_with_realm_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The detail path makes two calls — user + role-mappings — and
    folds them into a single :class:`KeycloakUser`."""
    _set_env(monkeypatch)
    _enable_admin(monkeypatch)
    client = KeycloakAdminClient()
    with respx.mock(assert_all_called=True) as router:
        router.post(_TOKEN_URL).mock(
            return_value=Response(200, json={"access_token": "T", "expires_in": 60})
        )
        router.get(f"{_REALM_BASE}/users/u-1").mock(
            return_value=Response(
                200,
                json={
                    "id": "u-1",
                    "username": "alice",
                    "email": "alice@example.com",
                    "firstName": "Alice",
                    "lastName": "Wonder",
                    "enabled": True,
                    "createdTimestamp": 1717545600000,
                },
            )
        )
        router.get(f"{_REALM_BASE}/users/u-1/role-mappings/realm").mock(
            return_value=Response(
                200,
                json=[
                    {"name": "dcinv-admin"},
                    {"name": "default-roles-prod-v1"},
                    {"name": "dcinv-mobile-user"},
                ],
            )
        )
        target = await client.get_user("u-1")

    assert target is not None
    assert target.id == "u-1"
    assert target.username == "alice"
    assert target.first_name == "Alice"
    assert target.last_name == "Wonder"
    assert target.enabled is True
    assert target.created_at is not None
    # Roles arrive sorted so the detail template renders deterministically
    assert target.roles == (
        "dcinv-admin",
        "dcinv-mobile-user",
        "default-roles-prod-v1",
    )


async def test_token_is_cached_between_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two list_users calls within the token TTL should hit the token
    endpoint exactly once."""
    _set_env(monkeypatch)
    _enable_admin(monkeypatch)
    client = KeycloakAdminClient()
    with respx.mock(assert_all_called=True) as router:
        token_route = router.post(_TOKEN_URL).mock(
            return_value=Response(200, json={"access_token": "T", "expires_in": 60})
        )
        users_route = router.get(f"{_REALM_BASE}/users").mock(
            return_value=Response(200, json=[])
        )
        await client.list_users(page=1, page_size=10)
        await client.list_users(page=2, page_size=10)

    assert token_route.call_count == 1
    assert users_route.call_count == 2


async def test_non_404_4xx_from_admin_api_raises_keycloak_admin_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 (e.g. missing view-users role) is a real error — the handler
    surfaces it via an error flash rather than swallowing it as a 404."""
    _set_env(monkeypatch)
    _enable_admin(monkeypatch)
    client = KeycloakAdminClient()
    with respx.mock(assert_all_called=True) as router:
        router.post(_TOKEN_URL).mock(
            return_value=Response(200, json={"access_token": "T", "expires_in": 60})
        )
        router.get(f"{_REALM_BASE}/users").mock(return_value=Response(403))
        with pytest.raises(KeycloakAdminError) as exc:
            await client.list_users()
        assert exc.value.status_code == 403
