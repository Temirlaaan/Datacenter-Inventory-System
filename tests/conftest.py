"""Shared test fixtures."""

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
    """Function-scoped: clear app env vars and the get_settings() cache before each test."""
    for key in _APP_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    from app.config import get_settings

    get_settings.cache_clear()
