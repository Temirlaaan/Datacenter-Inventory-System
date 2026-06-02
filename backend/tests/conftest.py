"""Shared test fixtures."""

import asyncio
import contextlib

import pytest

_APP_ENV_KEYS = (
    "NETBOX_URL",
    "NETBOX_SERVICE_TOKEN",
    "KEYCLOAK_BASE_URL",
    "KEYCLOAK_REALM",
    "DATABASE_URL",
    "LOG_LEVEL",
    "JWKS_CACHE_TTL_SECONDS",
    "DCINV_SECRETS_DIR",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Function-scoped: clear app env vars and the cached factories before each test."""
    for key in _APP_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    from app.auth.jwks import get_jwks_cache
    from app.config import get_settings
    from app.db.session import get_engine, get_sessionmaker
    from app.netbox.client import get_netbox_client, reset_netbox_circuit
    from app.services.meta import get_meta_cache

    # If a previous test populated the NetBox client singleton, close its httpx pool
    # before discarding the cache. Skipping this would leak the connection pool until
    # process exit AND keep the client bound to a now-defunct event loop.
    if get_netbox_client.cache_info().currsize > 0:
        cached_client = get_netbox_client()
        with contextlib.suppress(Exception):
            asyncio.run(cached_client.aclose())

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    get_jwks_cache.cache_clear()
    get_netbox_client.cache_clear()
    get_meta_cache.cache_clear()
    # Sprint 8a Task 2: clear the cached NetBox circuit so the next test
    # starts with a fresh CLOSED circuit + freshly-read settings (no
    # failure-count leakage across tests).
    reset_netbox_circuit()
